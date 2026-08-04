from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path, PurePosixPath
import subprocess
from threading import Event
from typing import Any

import pytest

from modeling_harness.isolation import LocalSandboxBackend, SandboxPlan
from modeling_harness.packets import PacketValidator, sha256_json
from modeling_harness.runtime import (
    ContainerExitError,
    DockerPreflightReport,
    DockerTaskExecutor,
    ExecutionAttestationLedger,
    ExecutionCancelled,
    ExecutionTimedOut,
    PlanValidationError,
    ProductionUnavailable,
    ResultPacketError,
    docker_preflight,
)


ROOT = Path(__file__).parents[1]
H_A = "a" * 64
H_B = "b" * 64
IMAGE = "example.invalid/agent@sha256:" + "c" * 64
NOW = "2026-01-02T03:04:05Z"


def task_packet() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "packet_id": "packet-task-001",
        "task_id": "task-001",
        "run_id": "run-001",
        "attempt_id": "attempt-001",
        "parent_packet_id": None,
        "role_id": "problem_definition_router",
        "task_purpose": "task-answer",
        "objective": "Produce one generic role-scoped result.",
        "scope": {"in": ["generic capability"], "out": ["peer workspaces"]},
        "input_artifacts": [],
        "allowed_tools": [],
        "required_deliverables": [
            {
                "deliverable_id": "deliverable-001",
                "description": "Structured result",
                "media_type": "application/json",
            }
        ],
        "acceptance_criteria": [
            {
                "criterion_id": "criterion-001",
                "description": "The result validates.",
                "evidence_type": "test",
                "hard_gate": True,
            }
        ],
        "workspace": {
            "workspace_id": "workspace-001",
            "container_id": "container-001",
            "write_root": (
                "/runs/project-001/task-001/problem_definition_router/attempt-001/"
            ),
            "read_only_mounts": [],
            "fresh_environment_required": True,
        },
        "prompt": {
            "prompt_id": "prompt-001",
            "version": "1.0.0",
            "sha256": H_A,
        },
        "benchmark_access_level": "none",
        "execution_limits": {
            "wall_time_seconds": 30,
            "cpu_cores": 1,
            "memory_mb": 256,
            "disk_mb": 256,
            "network_policy": "deny",
        },
        "created_at": NOW,
    }


def result_packet(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "packet_id": "packet-result-001",
        "task_id": task["task_id"],
        "run_id": task["run_id"],
        "attempt_id": task["attempt_id"],
        "role_id": task["role_id"],
        "task_packet_sha256": sha256_json(task),
        "status": "pass",
        "summary": "Generic result completed.",
        "artifact_manifest": {
            "artifact_id": "artifact-manifest-001",
            "manifest_sha256": H_B,
            "location": "/runs/project-001/result-manifest.json",
        },
        "claims_and_sources": [],
        "assumptions": [],
        "validation_performed": [],
        "metrics": {},
        "known_failures": [],
        "uncertainties": [],
        "provenance": {
            "prompt_sha256": task["prompt"]["sha256"],
            "container_image_digest": "sha256:" + "c" * 64,
            "input_manifest_sha256s": [],
            "random_seeds": [7],
        },
        "completed_at": NOW,
    }


def strict_plan(tmp_path: Path) -> SandboxPlan:
    workspace = (tmp_path / "run-workspace").resolve()
    container = "mh-container-001"
    command = (
        "docker",
        "run",
        "--rm",
        "--name",
        container,
        "--read-only",
        "--network",
        "none",
        "--mount",
        f"type=bind,src={workspace},dst=/workspace,rw",
        "--workdir",
        "/workspace",
        IMAGE,
        "agent-entrypoint",
    )
    return SandboxPlan(
        backend_name="docker-strict",
        production_eligible=True,
        test_only=False,
        container_name=container,
        command=command,
        host_write_root=workspace,
        container_write_root=PurePosixPath("/workspace"),
        readonly_inputs=(),
        wall_time_seconds=30,
        isolation_attestation={
            "new_container": True,
            "unique_write_root": True,
            "raw_inputs_read_only": True,
            "other_workspaces_unmounted": True,
            "root_filesystem_read_only": True,
            "network_default_deny": True,
            "network_egress_enforced": True,
            "input_hashes_verified": True,
            "reviewer_fresh_environment": True,
        },
        input_verifications=(),
    )


def ready_preflight(_: str) -> DockerPreflightReport:
    return DockerPreflightReport(
        docker_executable="docker",
        binary_found=True,
        client_version="27.0.0",
        daemon_ready=True,
        server_version="27.0.0",
        errors=(),
    )


class FakeProcess:
    def __init__(
        self,
        *,
        cwd: Path,
        returncode: int = 0,
        result: dict[str, Any] | str | None = None,
    ) -> None:
        self.cwd = Path(cwd)
        self.returncode = returncode
        self.result = result
        self.terminated = False
        self.killed = False
        self._written = False

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        if not self._written and self.result is not None:
            content = (
                self.result
                if isinstance(self.result, str)
                else json.dumps(self.result)
            )
            (self.cwd / "result_packet.json").write_text(content, encoding="utf-8")
            self._written = True
        return b"stdout", b"stderr"

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class TimeoutProcess(FakeProcess):
    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        if timeout is not None:
            raise subprocess.TimeoutExpired(["docker", "run"], timeout)
        return b"partial", b"timeout"


def popen_for(
    *,
    returncode: int = 0,
    result: dict[str, Any] | str | None = None,
    process_type: type[FakeProcess] = FakeProcess,
) -> Any:
    def factory(command: list[str], **kwargs: Any) -> FakeProcess:
        assert isinstance(command, list)
        assert kwargs["shell"] is False
        return process_type(
            cwd=kwargs["cwd"],
            returncode=returncode,
            result=result,
        )

    return factory


def cleanup_runner(command: list[str], **kwargs: Any) -> Any:
    assert isinstance(command, list)
    assert kwargs["shell"] is False
    return subprocess.CompletedProcess(command, 0, b"", b"")


def executor(tmp_path: Path, popen: Any) -> DockerTaskExecutor:
    del tmp_path
    return DockerTaskExecutor(
        PacketValidator.from_project_root(ROOT),
        preflight_checker=ready_preflight,
        popen_factory=popen,
        cleanup_runner=cleanup_runner,
        poll_seconds=0.01,
    )


def test_preflight_reports_missing_binary_and_daemon_failure() -> None:
    missing = docker_preflight(which=lambda _: None)
    assert missing.production_available is False
    assert len(missing.errors) == 1

    calls: list[list[str]] = []

    def runner(command: list[str], **_: Any) -> Any:
        calls.append(command)
        if command[1] == "version":
            return subprocess.CompletedProcess(command, 0, "27.0.0\n", "")
        return subprocess.CompletedProcess(command, 1, "", "daemon unavailable")

    failed = docker_preflight(
        which=lambda _: "docker",
        runner=runner,
    )
    assert failed.binary_found is True
    assert failed.client_version == "27.0.0"
    assert failed.daemon_ready is False
    assert len(calls) == 2


def test_execute_rejects_local_plan_and_unavailable_docker(tmp_path: Path) -> None:
    local = SandboxPlan(
        backend_name=LocalSandboxBackend.backend_name,
        production_eligible=False,
        test_only=True,
        container_name="local-container-001",
        command=("local-test-only",),
        host_write_root=tmp_path / "local",
        container_write_root=PurePosixPath("/workspace"),
        readonly_inputs=(),
        wall_time_seconds=1,
    )
    with pytest.raises(PlanValidationError):
        executor(tmp_path, popen_for()).execute(local, task_packet())

    unavailable = DockerTaskExecutor(
        PacketValidator.from_project_root(ROOT),
        preflight_checker=lambda _: DockerPreflightReport(
            "docker", False, None, False, None, ("Docker missing.",)
        ),
        popen_factory=popen_for(),
    )
    with pytest.raises(ProductionUnavailable):
        unavailable.execute(strict_plan(tmp_path), task_packet())


def test_command_tampering_is_rejected_before_workspace_creation(
    tmp_path: Path,
) -> None:
    plan = strict_plan(tmp_path)
    tampered = replace(
        plan,
        command=tuple(item for item in plan.command if item != "--read-only"),
    )
    with pytest.raises(PlanValidationError):
        executor(tmp_path, popen_for()).execute(tampered, task_packet())
    assert not plan.host_write_root.exists()

    shell_plan = replace(plan, command=(*plan.command[:-1], "sh", "-c"))
    with pytest.raises(PlanValidationError):
        executor(tmp_path, popen_for()).execute(shell_plan, task_packet())


def test_timeout_and_cancellation_fail_closed_with_audit(tmp_path: Path) -> None:
    ticks = iter((0.0, 2.0, 2.0))
    timed_executor = DockerTaskExecutor(
        PacketValidator.from_project_root(ROOT),
        preflight_checker=ready_preflight,
        popen_factory=popen_for(process_type=TimeoutProcess),
        cleanup_runner=cleanup_runner,
        monotonic=lambda: next(ticks),
        poll_seconds=0.01,
    )
    plan = replace(strict_plan(tmp_path), wall_time_seconds=1)
    with pytest.raises(ExecutionTimedOut) as timed:
        timed_executor.execute(plan, task_packet())
    assert timed.value.record.timed_out is True
    assert Path(timed.value.record.workspace, "execution-audit.json").is_file()
    assert "docker-rm-force-requested" in timed.value.record.cleanup_actions

    cancel_plan = strict_plan(tmp_path / "cancel")
    cancel = Event()
    cancel.set()
    with pytest.raises(ExecutionCancelled) as cancelled:
        executor(tmp_path, popen_for()).execute(
            cancel_plan, task_packet(), cancel_event=cancel
        )
    assert cancelled.value.record.cancelled is True


def test_nonzero_missing_and_invalid_results_fail_closed(tmp_path: Path) -> None:
    nonzero_plan = strict_plan(tmp_path / "nonzero")
    with pytest.raises(ContainerExitError) as nonzero:
        executor(tmp_path, popen_for(returncode=9)).execute(
            nonzero_plan, task_packet()
        )
    assert nonzero.value.record.return_code == 9
    assert nonzero.value.record.workspace_preserved is True

    missing_plan = strict_plan(tmp_path / "missing")
    with pytest.raises(ResultPacketError):
        executor(tmp_path, popen_for()).execute(missing_plan, task_packet())

    invalid_plan = strict_plan(tmp_path / "invalid")
    with pytest.raises(ResultPacketError):
        executor(tmp_path, popen_for(result="{}")).execute(
            invalid_plan, task_packet()
        )


def test_success_returns_hashed_audit_record(tmp_path: Path) -> None:
    task = task_packet()
    plan = strict_plan(tmp_path)
    record = executor(
        tmp_path,
        popen_for(result=result_packet(task)),
    ).execute(plan, task)
    assert record.status == "succeeded"
    assert record.return_code == 0
    assert record.result_packet_sha256 == sha256_json(result_packet(task))
    assert len(record.stdout_sha256) == 64
    assert len(record.stderr_sha256) == 64
    assert record.production_attested is False
    assert Path(record.workspace, "task_packet.json").is_file()
    assert Path(record.workspace, "execution-audit.json").is_file()


def test_persistent_attestation_ledger_binds_execution_evidence(
    tmp_path: Path,
) -> None:
    task = task_packet()
    ledger = ExecutionAttestationLedger(tmp_path / "attestations.sqlite3")
    runtime = DockerTaskExecutor(
        PacketValidator.from_project_root(ROOT),
        preflight_checker=ready_preflight,
        popen_factory=popen_for(result=result_packet(task)),
        cleanup_runner=cleanup_runner,
        attestation_ledger=ledger,
    )
    record = runtime.execute(strict_plan(tmp_path / "attested"), task)
    assert record.production_attested is True
    assert record.attestation_sequence == 1
    assert len(record.attestation_hash or "") == 64
    assert ledger.verify() == 1
    payload = ledger.payload(record.execution_id)
    assert payload["run_id"] == task["run_id"]
    assert payload["task_id"] == task["task_id"]
    assert payload["attempt_id"] == task["attempt_id"]
    assert payload["session_id"] == task["workspace"]["workspace_id"]
    assert payload["container_name"] == record.container_name
    assert payload["plan_sha256"] == record.plan_sha256
    assert payload["result_packet_sha256"] == record.result_packet_sha256
    assert payload["stdout_sha256"] == record.stdout_sha256
    assert payload["stderr_sha256"] == record.stderr_sha256
    ledger.close()
