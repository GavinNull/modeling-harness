"""Production Docker execution for provider-neutral agent images.

The isolation module produces an inspectable :class:`SandboxPlan`; this module
is the deliberately small privileged boundary that executes such a plan.  It
never accepts a shell command string, never enables ``shell=True``, and rejects
plans which do not carry the strict Docker backend's isolation and mount
attestations.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
from threading import Event, Lock
import time
from typing import Any
from uuid import uuid4

from modeling_harness.isolation import (
    ProductionIsolationError,
    SandboxPlan,
    require_production_backend,
    sha256_path,
)
from modeling_harness.packets import (
    PacketSchemaError,
    PacketValidator,
    canonical_json_bytes,
    sha256_json,
)


_IMAGE_DIGEST = re.compile(r"^.+@sha256:[a-f0-9]{64}$")
_SHELL_EXECUTABLES = frozenset(
    {
        "sh",
        "bash",
        "dash",
        "zsh",
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
    }
)
_REQUIRED_ATTESTATIONS = frozenset(
    {
        "new_container",
        "unique_write_root",
        "raw_inputs_read_only",
        "other_workspaces_unmounted",
        "root_filesystem_read_only",
        "network_egress_enforced",
        "input_hashes_verified",
        "reviewer_fresh_environment",
    }
)


class RuntimeExecutionError(RuntimeError):
    """Base class for preflight and execution failures."""


class ProductionUnavailable(RuntimeExecutionError):
    """Raised when the Docker production prerequisite is unavailable."""


class PlanValidationError(RuntimeExecutionError):
    """Raised before execution when a plan is incomplete or has been modified."""


class ExecutionFailed(RuntimeExecutionError):
    """Base class for failures that have an auditable execution record."""

    def __init__(self, message: str, record: "ExecutionRecord") -> None:
        super().__init__(message)
        self.record = record


class ExecutionTimedOut(ExecutionFailed):
    """The Docker run exceeded the sandbox wall-time budget."""


class ExecutionCancelled(ExecutionFailed):
    """The control plane cancelled the Docker run."""


class ContainerExitError(ExecutionFailed):
    """The agent container exited non-zero."""


class ResultPacketError(ExecutionFailed):
    """The required result packet was missing, invalid, or not task-bound."""


class AttestationLedgerError(RuntimeExecutionError):
    """Persistent execution attestation could not be written or verified."""


@dataclass(frozen=True)
class DockerPreflightReport:
    docker_executable: str
    binary_found: bool
    client_version: str | None
    daemon_ready: bool
    server_version: str | None
    errors: tuple[str, ...]

    @property
    def production_available(self) -> bool:
        return (
            self.binary_found
            and bool(self.client_version)
            and self.daemon_ready
            and bool(self.server_version)
            and not self.errors
        )

    def as_json(self) -> dict[str, Any]:
        value = asdict(self)
        value["production_available"] = self.production_available
        value["errors"] = list(self.errors)
        return value


@dataclass(frozen=True)
class ExecutionRecord:
    execution_id: str
    status: str
    run_id: str
    task_id: str
    attempt_id: str
    session_id: str
    backend_name: str
    container_name: str
    workspace: str
    started_at: str
    completed_at: str
    duration_seconds: float
    return_code: int | None
    timed_out: bool
    cancelled: bool
    command_sha256: str
    plan_sha256: str
    task_packet_sha256: str
    result_packet_sha256: str | None
    stdout_sha256: str
    stderr_sha256: str
    stdout_path: str
    stderr_path: str
    result_packet_path: str
    cleanup_actions: tuple[str, ...]
    workspace_preserved: bool
    production_attested: bool = False
    attestation_sequence: int | None = None
    attestation_hash: str | None = None

    def as_json(self) -> dict[str, Any]:
        value = asdict(self)
        value["cleanup_actions"] = list(self.cleanup_actions)
        return value

    def attestation_payload(self) -> dict[str, Any]:
        value = self.as_json()
        value.pop("production_attested")
        value.pop("attestation_sequence")
        value.pop("attestation_hash")
        return value


@dataclass(frozen=True)
class ExecutionAttestationReceipt:
    sequence: int
    previous_hash: str
    attestation_hash: str


class ExecutionAttestationLedger:
    """Durable append-only SHA-256 chain for execution evidence."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.durable = self.path != ":memory:"
        if self.durable:
            target = Path(self.path).expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            self.path = str(target)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.execute("PRAGMA synchronous=FULL")
        if self.durable:
            self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_attestation (
                sequence INTEGER PRIMARY KEY,
                execution_id TEXT NOT NULL UNIQUE,
                previous_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                attestation_hash TEXT NOT NULL UNIQUE
            )
            """
        )
        self._connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS execution_attestation_no_update
            BEFORE UPDATE ON execution_attestation
            BEGIN
                SELECT RAISE(ABORT, 'execution attestation is append-only');
            END
            """
        )
        self._connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS execution_attestation_no_delete
            BEFORE DELETE ON execution_attestation
            BEGIN
                SELECT RAISE(ABORT, 'execution attestation is append-only');
            END
            """
        )
        self._connection.commit()
        self._lock = Lock()

    def append(self, record: ExecutionRecord) -> ExecutionAttestationReceipt:
        payload = record.attestation_payload()
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._lock:
            row = self._connection.execute(
                """
                SELECT sequence, attestation_hash
                FROM execution_attestation
                ORDER BY sequence DESC
                LIMIT 1
                """
            ).fetchone()
            sequence = 1 if row is None else int(row[0]) + 1
            previous_hash = "0" * 64 if row is None else str(row[1])
            attestation_hash = _sha256_bytes(
                json.dumps(
                    {
                        "sequence": sequence,
                        "previous_hash": previous_hash,
                        "payload": payload,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
            try:
                self._connection.execute(
                    """
                    INSERT INTO execution_attestation(
                        sequence, execution_id, previous_hash,
                        payload_json, attestation_hash
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        sequence,
                        record.execution_id,
                        previous_hash,
                        payload_json,
                        attestation_hash,
                    ),
                )
                self._connection.commit()
            except sqlite3.Error as exc:
                self._connection.rollback()
                raise AttestationLedgerError(
                    f"execution attestation append failed: {exc}"
                ) from exc
        return ExecutionAttestationReceipt(
            sequence=sequence,
            previous_hash=previous_hash,
            attestation_hash=attestation_hash,
        )

    def verify(self) -> int:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT sequence, execution_id, previous_hash,
                       payload_json, attestation_hash
                FROM execution_attestation
                ORDER BY sequence
                """
            ).fetchall()
        previous = "0" * 64
        for expected_sequence, row in enumerate(rows, start=1):
            sequence, execution_id, previous_hash, payload_json, digest = row
            if sequence != expected_sequence or previous_hash != previous:
                raise AttestationLedgerError("execution attestation chain is broken")
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError as exc:
                raise AttestationLedgerError(
                    "execution attestation payload is invalid JSON"
                ) from exc
            if payload.get("execution_id") != execution_id:
                raise AttestationLedgerError(
                    "execution attestation ID binding is invalid"
                )
            actual = _sha256_bytes(
                json.dumps(
                    {
                        "sequence": sequence,
                        "previous_hash": previous_hash,
                        "payload": payload,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
            if actual != digest:
                raise AttestationLedgerError(
                    "execution attestation hash verification failed"
                )
            previous = digest
        return len(rows)

    def payload(self, execution_id: str) -> Mapping[str, Any]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT payload_json
                FROM execution_attestation
                WHERE execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
        if row is None:
            raise KeyError(execution_id)
        value = json.loads(row[0])
        if not isinstance(value, dict):
            raise AttestationLedgerError("execution attestation payload is malformed")
        return value

    def close(self) -> None:
        self._connection.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _command_hash(command: Sequence[str]) -> str:
    return _sha256_bytes(
        json.dumps(
            list(command),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _plan_hash(plan: SandboxPlan) -> str:
    value = {
        "backend_name": plan.backend_name,
        "production_eligible": plan.production_eligible,
        "test_only": plan.test_only,
        "container_name": plan.container_name,
        "command": list(plan.command),
        "host_write_root": str(plan.host_write_root),
        "container_write_root": str(plan.container_write_root),
        "readonly_inputs": [
            {
                "source": str(mount.source),
                "target": str(mount.target),
                "source_kind": mount.source_kind,
                "content_sha256": mount.content_sha256,
            }
            for mount in plan.readonly_inputs
        ],
        "wall_time_seconds": plan.wall_time_seconds,
        "isolation_attestation": dict(plan.isolation_attestation),
        "input_verifications": [
            {
                "source": str(proof.source),
                "source_kind": proof.source_kind,
                "content_sha256": proof.content_sha256,
                "approved_root": str(proof.approved_root),
                "approved_manifest_sha256": proof.approved_manifest_sha256,
            }
            for proof in plan.input_verifications
        ],
    }
    return _sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _run_probe(
    runner: Callable[..., Any],
    command: list[str],
    *,
    timeout_seconds: float,
) -> tuple[bool, str, str]:
    try:
        completed = runner(
            command,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "", str(exc)
    stdout = str(getattr(completed, "stdout", "") or "").strip()
    stderr = str(getattr(completed, "stderr", "") or "").strip()
    return getattr(completed, "returncode", 1) == 0, stdout, stderr


def docker_preflight(
    docker_executable: str = "docker",
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., Any] = subprocess.run,
    timeout_seconds: float = 10.0,
) -> DockerPreflightReport:
    """Check Docker binary, client version, and daemon availability."""

    if not docker_executable or "\x00" in docker_executable:
        return DockerPreflightReport(
            docker_executable=docker_executable,
            binary_found=False,
            client_version=None,
            daemon_ready=False,
            server_version=None,
            errors=("Docker executable is empty or invalid.",),
        )
    resolved = which(docker_executable)
    if resolved is None:
        return DockerPreflightReport(
            docker_executable=docker_executable,
            binary_found=False,
            client_version=None,
            daemon_ready=False,
            server_version=None,
            errors=(
                "Docker binary was not found; production execution is unavailable.",
            ),
        )

    errors: list[str] = []
    client_ok, client_version, client_error = _run_probe(
        runner,
        [resolved, "version", "--format", "{{.Client.Version}}"],
        timeout_seconds=timeout_seconds,
    )
    if not client_ok or not client_version:
        errors.append(
            "Docker client version probe failed"
            + (f": {client_error}" if client_error else ".")
        )
        client_version = None

    daemon_ok, server_version, daemon_error = _run_probe(
        runner,
        [resolved, "info", "--format", "{{.ServerVersion}}"],
        timeout_seconds=timeout_seconds,
    )
    if not daemon_ok or not server_version:
        errors.append(
            "Docker daemon is unavailable"
            + (f": {daemon_error}" if daemon_error else ".")
        )
        server_version = None

    return DockerPreflightReport(
        docker_executable=resolved,
        binary_found=True,
        client_version=client_version,
        daemon_ready=daemon_ok and server_version is not None,
        server_version=server_version,
        errors=tuple(errors),
    )


class DockerTaskExecutor:
    """Execute one strict Docker sandbox plan and return immutable audit data.

    Agent image contract:

    - input: ``/workspace/task_packet.json``;
    - output: ``/workspace/result_packet.json``;
    - promoted inputs: read-only below ``/inputs``;
    - the image entrypoint reads/writes those paths without caller-supplied
      shell fragments.
    """

    def __init__(
        self,
        packet_validator: PacketValidator,
        *,
        docker_executable: str = "docker",
        preflight_checker: Callable[[str], DockerPreflightReport] | None = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        cleanup_runner: Callable[..., Any] = subprocess.run,
        monotonic: Callable[[], float] = time.monotonic,
        poll_seconds: float = 0.1,
        attestation_ledger: ExecutionAttestationLedger | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.packet_validator = packet_validator
        self.docker_executable = docker_executable
        self.preflight_checker = preflight_checker or (
            lambda executable: docker_preflight(executable)
        )
        self.popen_factory = popen_factory
        self.cleanup_runner = cleanup_runner
        self.monotonic = monotonic
        self.poll_seconds = poll_seconds
        self.attestation_ledger = attestation_ledger
        self._container_names: set[str] = set()
        self._lock = Lock()

    def execute(
        self,
        plan: SandboxPlan,
        task_packet: Mapping[str, Any],
        *,
        cancel_event: Event | None = None,
    ) -> ExecutionRecord:
        """Run one container.  Every subprocess call uses an argument array."""

        try:
            require_production_backend(plan)
        except ProductionIsolationError as exc:
            raise PlanValidationError(str(exc)) from exc
        self._validate_plan(plan)
        preflight = self.preflight_checker(self.docker_executable)
        if not preflight.production_available:
            detail = "; ".join(preflight.errors) or "Docker preflight failed."
            raise ProductionUnavailable(detail)

        with self._lock:
            if plan.container_name in self._container_names:
                raise PlanValidationError("container name was already executed")
            self._container_names.add(plan.container_name)

        workspace = plan.host_write_root.resolve()
        if workspace.exists():
            raise PlanValidationError(
                "execution workspace already exists; use a fresh attempt/workspace"
            )
        workspace.mkdir(parents=True, exist_ok=False)
        task_path = workspace / "task_packet.json"
        result_path = workspace / "result_packet.json"
        stdout_path = workspace / "stdout.log"
        stderr_path = workspace / "stderr.log"
        audit_path = workspace / "execution-audit.json"
        task_bytes = canonical_json_bytes(task_packet)
        with task_path.open("xb") as handle:
            handle.write(task_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        task_path.chmod(0o444)

        started_at = _utc_now()
        started_clock = self.monotonic()
        stdout = b""
        stderr = b""
        return_code: int | None = None
        timed_out = False
        cancelled = False
        cleanup_actions: list[str] = ["workspace-preserved-for-audit"]
        process: Any | None = None

        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
        else:
            process = self.popen_factory(
                list(plan.command),
                shell=False,
                cwd=workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = started_clock + plan.wall_time_seconds
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    stdout, stderr = process.communicate(
                        timeout=min(self.poll_seconds, remaining)
                    )
                    return_code = int(process.returncode)
                    break
                except subprocess.TimeoutExpired:
                    continue

        if process is not None and (timed_out or cancelled):
            try:
                process.terminate()
                cleanup_actions.append("docker-client-terminated")
                stdout, stderr = process.communicate(timeout=self.poll_seconds)
            except (OSError, subprocess.SubprocessError):
                process.kill()
                cleanup_actions.append("docker-client-killed")
                stdout, stderr = process.communicate()
            return_code = getattr(process, "returncode", None)
            self._force_remove_container(plan.container_name)
            cleanup_actions.append("docker-rm-force-requested")
        elif return_code not in (None, 0):
            self._force_remove_container(plan.container_name)
            cleanup_actions.append("docker-rm-force-requested")
        else:
            cleanup_actions.append("docker-rm-via-run---rm")

        stdout_bytes = self._as_bytes(stdout)
        stderr_bytes = self._as_bytes(stderr)
        self._write_log(stdout_path, stdout_bytes)
        self._write_log(stderr_path, stderr_bytes)

        result_hash: str | None = None
        result_problem: str | None = None
        if not timed_out and not cancelled and return_code == 0:
            try:
                result = self._load_result_packet(result_path)
                self.packet_validator.validate_schema("ResultPacket", result)
                self._validate_result_binding(result, task_packet)
                result_hash = sha256_json(result)
            except (OSError, UnicodeError, json.JSONDecodeError, PacketSchemaError,
                    PlanValidationError) as exc:
                result_problem = str(exc)

        status = (
            "cancelled"
            if cancelled
            else "timed-out"
            if timed_out
            else "container-failed"
            if return_code not in (0,)
            else "invalid-result"
            if result_problem is not None
            else "succeeded"
        )
        record = ExecutionRecord(
            execution_id=f"execution-{uuid4().hex}",
            status=status,
            run_id=str(task_packet["run_id"]),
            task_id=str(task_packet["task_id"]),
            attempt_id=str(task_packet["attempt_id"]),
            session_id=str(
                task_packet.get("metadata", {}).get(
                    "session_id",
                    task_packet["workspace"]["workspace_id"],
                )
            ),
            backend_name=plan.backend_name,
            container_name=plan.container_name,
            workspace=str(workspace),
            started_at=started_at,
            completed_at=_utc_now(),
            duration_seconds=max(0.0, self.monotonic() - started_clock),
            return_code=return_code,
            timed_out=timed_out,
            cancelled=cancelled,
            command_sha256=_command_hash(plan.command),
            plan_sha256=_plan_hash(plan),
            task_packet_sha256=_sha256_bytes(task_bytes),
            result_packet_sha256=result_hash,
            stdout_sha256=_sha256_bytes(stdout_bytes),
            stderr_sha256=_sha256_bytes(stderr_bytes),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            result_packet_path=str(result_path),
            cleanup_actions=tuple(cleanup_actions),
            workspace_preserved=True,
        )
        record = self._attest(record)
        self._write_audit(audit_path, record)

        if cancelled:
            raise ExecutionCancelled("container execution was cancelled", record)
        if timed_out:
            raise ExecutionTimedOut("container execution exceeded wall time", record)
        if return_code != 0:
            raise ContainerExitError(
                f"container exited with status {return_code}", record
            )
        if result_problem is not None:
            raise ResultPacketError(result_problem, record)
        return record

    def _attest(self, record: ExecutionRecord) -> ExecutionRecord:
        ledger = self.attestation_ledger
        if ledger is None:
            return record
        receipt = ledger.append(record)
        if ledger.verify() < receipt.sequence:
            raise AttestationLedgerError(
                "execution attestation was not durably verifiable"
            )
        if not ledger.durable:
            return record
        return replace(
            record,
            production_attested=True,
            attestation_sequence=receipt.sequence,
            attestation_hash=receipt.attestation_hash,
        )

    def _validate_plan(self, plan: SandboxPlan) -> None:
        if not isinstance(plan.command, tuple) or not plan.command:
            raise PlanValidationError("Docker plan command must be a non-empty tuple")
        if any(
            not isinstance(argument, str)
            or not argument
            or "\x00" in argument
            or "\n" in argument
            or "\r" in argument
            for argument in plan.command
        ):
            raise PlanValidationError("Docker plan contains an invalid argument")
        if len(plan.command) < 3 or plan.command[1] != "run":
            raise PlanValidationError("Docker plan must invoke docker run")
        if Path(plan.command[0]).name.lower() != Path(
            self.docker_executable
        ).name.lower():
            raise PlanValidationError("Docker plan executable does not match executor")
        for required in ("--rm", "--read-only", "--name", "--workdir"):
            if plan.command.count(required) != 1:
                raise PlanValidationError(
                    f"Docker plan requires exactly one {required}"
                )
        if self._value_after(plan.command, "--name") != plan.container_name:
            raise PlanValidationError("Docker plan container name was modified")
        if self._value_after(plan.command, "--workdir") != "/workspace":
            raise PlanValidationError("Docker workdir must be /workspace")
        workspace_mount = (
            f"type=bind,src={plan.host_write_root},dst=/workspace,rw"
        )
        mount_values = [
            plan.command[index + 1]
            for index, item in enumerate(plan.command[:-1])
            if item == "--mount"
        ]
        expected_mounts = [workspace_mount]
        expected_mounts.extend(
            f"type=bind,src={mount.source},dst={mount.target},readonly"
            for mount in plan.readonly_inputs
        )
        if sorted(mount_values) != sorted(expected_mounts):
            raise PlanValidationError("Docker mount arguments do not match the plan")
        if not all(
            plan.isolation_attestation.get(item) is True
            for item in _REQUIRED_ATTESTATIONS
        ):
            raise PlanValidationError("Docker plan lacks mandatory attestations")
        if len(plan.input_verifications) != len(plan.readonly_inputs):
            raise PlanValidationError("Docker plan mount proof count is incomplete")
        for mount, proof in zip(plan.readonly_inputs, plan.input_verifications):
            try:
                source = mount.source.resolve(strict=True)
            except OSError as exc:
                raise PlanValidationError("planned input mount is unavailable") from exc
            if proof.source != source:
                raise PlanValidationError("mount proof source does not match")
            if proof.content_sha256 != mount.content_sha256:
                raise PlanValidationError("mount proof content hash does not match")
            if sha256_path(mount.source) != proof.content_sha256:
                raise PlanValidationError("mounted input changed after planning")
            if not (
                source == proof.approved_root
                or source.is_relative_to(proof.approved_root)
            ):
                raise PlanValidationError("mount proof does not bind an approved root")

        images = [
            (index, argument)
            for index, argument in enumerate(plan.command)
            if _IMAGE_DIGEST.fullmatch(argument)
        ]
        if len(images) != 1:
            raise PlanValidationError("Docker plan requires one digest-pinned image")
        image_index = images[0][0]
        entrypoint = plan.command[image_index + 1 :]
        if entrypoint:
            executable = Path(entrypoint[0]).name.lower()
            if executable in _SHELL_EXECUTABLES:
                raise PlanValidationError("shell entrypoints are not accepted")
            if any(
                argument.lower() in {"-c", "/c", "-command"}
                for argument in entrypoint
            ):
                raise PlanValidationError("caller-supplied shell commands are forbidden")

    @staticmethod
    def _value_after(command: Sequence[str], flag: str) -> str:
        index = command.index(flag)
        if index + 1 >= len(command):
            raise PlanValidationError(f"Docker flag {flag} has no value")
        return command[index + 1]

    def _force_remove_container(self, container_name: str) -> None:
        try:
            self.cleanup_runner(
                [self.docker_executable, "rm", "-f", container_name],
                shell=False,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            # Failure is represented by the explicit cleanup action in the
            # execution audit; the workspace and logs remain for incident review.
            return

    @staticmethod
    def _as_bytes(value: Any) -> bytes:
        if value is None:
            return b""
        if isinstance(value, bytes):
            return value
        return str(value).encode("utf-8", errors="replace")

    @staticmethod
    def _write_log(path: Path, content: bytes) -> None:
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _load_result_packet(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise PlanValidationError(
                "agent image did not produce /workspace/result_packet.json"
            )
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise PlanValidationError("result_packet.json must contain a JSON object")
        return document

    @staticmethod
    def _validate_result_binding(
        result: Mapping[str, Any], task: Mapping[str, Any]
    ) -> None:
        for field in ("task_id", "run_id", "attempt_id", "role_id"):
            if result.get(field) != task.get(field):
                raise PlanValidationError(f"ResultPacket {field} is not task-bound")
        if result.get("task_packet_sha256") != sha256_json(task):
            raise PlanValidationError("ResultPacket task_packet_sha256 mismatch")
        provenance = result.get("provenance")
        if not isinstance(provenance, Mapping):
            raise PlanValidationError("ResultPacket provenance is missing")
        if provenance.get("prompt_sha256") != task["prompt"]["sha256"]:
            raise PlanValidationError("ResultPacket prompt lineage mismatch")
        expected_inputs = sorted(
            item["manifest_sha256"] for item in task["input_artifacts"]
        )
        if sorted(provenance.get("input_manifest_sha256s", [])) != expected_inputs:
            raise PlanValidationError("ResultPacket input lineage mismatch")

    @staticmethod
    def _write_audit(path: Path, record: ExecutionRecord) -> None:
        content = json.dumps(
            record.as_json(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
