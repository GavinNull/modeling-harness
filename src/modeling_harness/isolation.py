"""Sandbox planning and production isolation gates.

This module deliberately plans container invocations without starting them.  A
caller can therefore inspect and approve the complete isolation contract before
handing the command to a privileged runtime.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
from threading import Lock
from types import MappingProxyType
from typing import ClassVar, Literal, Mapping
from uuid import uuid4


REVIEWER_ROLES = frozenset(
    {
        "mathematical_reviewer",
        "reproducibility_reviewer",
        "evidence_communication_reviewer",
    }
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_IMAGE_DIGEST = re.compile(r"^.+@sha256:[a-f0-9]{64}$")
_SAFE_CONTAINER_NAME = re.compile(r"[^a-z0-9_.-]+")
_DISALLOWED_HOST_PATH_PARTS = frozenset(
    {"docker.sock", "containerd.sock", "podman.sock"}
)
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_DOMAIN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_WINDOWS_REPARSE_POINT = 0x0400
_MAX_CPUS = 1024.0
_MAX_MEMORY_BYTES = 1 << 50
_MAX_DISK_BYTES = 1 << 54
_MAX_WALL_TIME_SECONDS = 31 * 24 * 60 * 60
_MAX_PIDS = 1_048_576


class IsolationError(ValueError):
    """Raised when a sandbox request violates an isolation invariant."""


class ProductionIsolationError(IsolationError):
    """Raised when a non-production backend is used for a release decision."""


def _require_bounded_positive_integer(
    name: str,
    value: object,
    maximum: int,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise IsolationError(f"{name} must be an integer")
    if value <= 0:
        raise IsolationError(f"{name} must be positive")
    if value > maximum:
        raise IsolationError(f"{name} exceeds the safety maximum {maximum}")


def _require_bounded_positive_real(
    name: str,
    value: object,
    maximum: float,
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IsolationError(f"{name} must be a finite real number")
    if isinstance(value, float) and not math.isfinite(value):
        raise IsolationError(f"{name} must be finite")
    if value <= 0:
        raise IsolationError(f"{name} must be positive")
    if value > maximum:
        raise IsolationError(f"{name} exceeds the safety maximum {maximum:g}")


@dataclass(frozen=True)
class ResourceLimits:
    """Finite resources attached to one isolated attempt."""

    cpus: float
    memory_bytes: int
    disk_bytes: int
    wall_time_seconds: int
    pids: int = 256

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Fail closed before resource values can reach a runtime command."""

        _require_bounded_positive_real("cpus", self.cpus, _MAX_CPUS)
        _require_bounded_positive_integer(
            "memory_bytes", self.memory_bytes, _MAX_MEMORY_BYTES
        )
        _require_bounded_positive_integer(
            "disk_bytes", self.disk_bytes, _MAX_DISK_BYTES
        )
        _require_bounded_positive_integer(
            "wall_time_seconds",
            self.wall_time_seconds,
            _MAX_WALL_TIME_SECONDS,
        )
        _require_bounded_positive_integer("pids", self.pids, _MAX_PIDS)


InputKind = Literal["raw-input", "promoted-artifact", "approved-toolchain"]


def _absolute_without_link_resolution(path: Path) -> Path:
    """Return a normalized absolute path while preserving symlink components."""

    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
    )


def _reject_link_or_reparse_components(path: Path) -> None:
    """Reject symlink/reparse traversal, including an escaping final component."""

    absolute = _absolute_without_link_resolution(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if _is_link_or_reparse(current):
            raise IsolationError(
                f"symlink or reparse-point paths are not valid sandbox inputs: {path}"
            )


def _is_filesystem_root(path: Path) -> bool:
    absolute = _absolute_without_link_resolution(path)
    return absolute == Path(absolute.anchor)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_path(path: str | Path) -> str:
    """Hash a regular file or a deterministic, link-free directory manifest."""

    declared = _absolute_without_link_resolution(Path(path))
    if _is_filesystem_root(declared):
        raise IsolationError("filesystem roots cannot be hashed as sandbox inputs")
    if not declared.exists():
        raise IsolationError(f"sandbox input does not exist: {declared}")
    _reject_link_or_reparse_components(declared)
    real = declared.resolve(strict=True)
    if real.is_file():
        if not stat.S_ISREG(real.stat().st_mode):
            raise IsolationError(f"sandbox input must be a regular file: {real}")
        return _sha256_file(real)
    if not real.is_dir():
        raise IsolationError(f"sandbox input must be a file or directory: {real}")

    entries: list[dict[str, object]] = []

    def visit(directory: Path) -> None:
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            _reject_link_or_reparse_components(child)
            relative = child.relative_to(real).as_posix()
            if child.is_dir():
                entries.append({"path": relative, "type": "directory"})
                visit(child)
            elif child.is_file() and stat.S_ISREG(child.stat().st_mode):
                entries.append(
                    {
                        "path": relative,
                        "type": "file",
                        "size": child.stat().st_size,
                        "sha256": _sha256_file(child),
                    }
                )
            else:
                raise IsolationError(
                    f"special files cannot be mounted into a sandbox: {child}"
                )

    visit(real)
    manifest = json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(manifest).hexdigest()


@dataclass(frozen=True)
class ReadOnlyMount:
    """One approved read-only input.

    ``source_kind`` is intentionally closed.  In particular there is no
    ``agent-workspace`` kind, so an unpromoted peer workspace cannot be
    represented as a valid input.
    """

    source: Path
    target: PurePosixPath
    source_kind: InputKind
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        if ".." in self.source.parts:
            raise IsolationError("read-only mount source cannot contain parent traversal")
        source = _absolute_without_link_resolution(self.source)
        target = PurePosixPath(self.target)
        if self.source_kind not in {
            "raw-input",
            "promoted-artifact",
            "approved-toolchain",
        }:
            raise IsolationError("mount source_kind is not an approved immutable input")
        if not target.is_absolute() or not target.is_relative_to(PurePosixPath("/inputs")):
            raise IsolationError("read-only mount targets must be below /inputs")
        if target == PurePosixPath("/inputs"):
            raise IsolationError("read-only mount target must name an input")
        if any(part.lower() in _DISALLOWED_HOST_PATH_PARTS for part in source.parts):
            raise IsolationError("host container sockets must never be mounted")
        if self.content_sha256 is not None and not _SHA256.fullmatch(
            self.content_sha256
        ):
            raise IsolationError("content_sha256 must be lower-case SHA-256")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "target", target)

    @property
    def real_source(self) -> Path:
        return self.source.resolve(strict=True)


@dataclass(frozen=True)
class ApprovedInputRoot:
    """Control-plane trust anchor for one immutable input namespace."""

    root: Path
    source_kind: InputKind
    manifest_sha256: str
    approved_by: str = "main_agent"
    immutable_snapshot: bool = True

    def __post_init__(self) -> None:
        if self.source_kind not in {
            "raw-input",
            "promoted-artifact",
            "approved-toolchain",
        }:
            raise IsolationError("approved root has an unknown source kind")
        if self.approved_by != "main_agent":
            raise IsolationError("input roots require main_agent approval")
        if self.immutable_snapshot is not True:
            raise IsolationError("approved input root must be an immutable snapshot")
        if not _SHA256.fullmatch(self.manifest_sha256):
            raise IsolationError("approved root manifest must be lower-case SHA-256")
        declared = _absolute_without_link_resolution(self.root)
        if _is_filesystem_root(declared):
            raise IsolationError("filesystem root cannot be an approved input root")
        if not declared.exists() or not declared.is_dir():
            raise IsolationError("approved input root must be an existing directory")
        _reject_link_or_reparse_components(declared)
        object.__setattr__(self, "root", declared.resolve(strict=True))


@dataclass(frozen=True)
class EgressControllerAttestation:
    """Trusted proof that a named network enforces proxy-only read access."""

    attestation_id: str
    controller_network: str
    proxy_url: str
    allowed_domains: tuple[str, ...]
    policy_sha256: str
    attested_by: str
    firewall_enforced: bool
    direct_egress_denied: bool
    dns_via_controller: bool
    readonly_http_methods_only: bool

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.attestation_id):
            raise IsolationError("invalid egress attestation_id")
        if (
            not self.controller_network.strip()
            or self.controller_network in {"bridge", "host", "default", "none"}
        ):
            raise IsolationError("egress controller requires a dedicated network")
        if not self.proxy_url.startswith(("http://", "https://")):
            raise IsolationError("egress controller proxy URL must be explicit")
        domains = tuple(domain.strip().lower() for domain in self.allowed_domains)
        if (
            not domains
            or len(domains) != len(set(domains))
            or any("*" in domain or not _DOMAIN.fullmatch(domain) for domain in domains)
        ):
            raise IsolationError("egress controller domains must be explicit and unique")
        if not _SHA256.fullmatch(self.policy_sha256):
            raise IsolationError("egress policy must be bound to a SHA-256 digest")
        if self.attested_by not in {"main_agent", "sandbox_platform_engineer"}:
            raise IsolationError("egress controller attestor is not authorized")
        enforcement = (
            self.firewall_enforced,
            self.direct_egress_denied,
            self.dns_via_controller,
            self.readonly_http_methods_only,
        )
        if not all(value is True for value in enforcement):
            raise IsolationError("egress controller attestation must prove all controls")
        object.__setattr__(self, "allowed_domains", domains)


@dataclass(frozen=True)
class ReviewIsolationContext:
    """Author runtime state which a new reviewer environment must not inherit."""

    author_attempt_id: str
    author_session_id: str
    author_container_name: str
    author_write_root: Path

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "author_write_root", self.author_write_root.expanduser().resolve()
        )


@dataclass(frozen=True)
class SandboxRequest:
    """A complete, inspectable request for one fresh sandbox."""

    project_id: str
    task_id: str
    role_id: str
    attempt_id: str
    session_id: str
    host_write_root: Path
    image: str
    limits: ResourceLimits
    readonly_inputs: tuple[ReadOnlyMount, ...] = ()
    network_policy: Literal["deny", "allowlisted-readonly"] = "deny"
    network_proxy_url: str | None = None
    allowed_domains: tuple[str, ...] = ()
    egress_attestation_id: str | None = None
    review_context: ReviewIsolationContext | None = None
    forbidden_workspace_roots: tuple[Path, ...] = ()
    entrypoint: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.limits, ResourceLimits):
            raise IsolationError("limits must be a ResourceLimits instance")
        self.limits.validate()
        for field_name in (
            "project_id",
            "task_id",
            "role_id",
            "attempt_id",
            "session_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise IsolationError(f"invalid {field_name}: {value!r}")
        if not _IMAGE_DIGEST.fullmatch(self.image):
            raise IsolationError("container image must be pinned by sha256 digest")
        if self.network_policy not in {"deny", "allowlisted-readonly"}:
            raise IsolationError("unknown network policy")
        if self.network_policy == "deny":
            if (
                self.network_proxy_url is not None
                or self.allowed_domains
                or self.egress_attestation_id is not None
            ):
                raise IsolationError("deny network policy cannot include proxy settings")
        elif (
            not self.network_proxy_url
            or not self.allowed_domains
        ):
            raise IsolationError(
                "allowlisted-readonly networking requires proxy and domains"
            )
        domains = tuple(domain.strip().lower() for domain in self.allowed_domains)
        if domains and (
            len(domains) != len(set(domains))
            or any("*" in domain or not _DOMAIN.fullmatch(domain) for domain in domains)
        ):
            raise IsolationError("allowed domains must be explicit and unique")
        if any(not item.strip() for item in self.entrypoint):
            raise IsolationError("entrypoint arguments cannot be blank")

        write_root = self.host_write_root.expanduser().resolve()
        forbidden = tuple(path.expanduser().resolve() for path in self.forbidden_workspace_roots)
        object.__setattr__(self, "host_write_root", write_root)
        object.__setattr__(self, "forbidden_workspace_roots", forbidden)
        object.__setattr__(self, "allowed_domains", domains)


@dataclass(frozen=True)
class MountVerification:
    source: Path
    source_kind: InputKind
    content_sha256: str
    approved_root: Path
    approved_manifest_sha256: str


@dataclass(frozen=True)
class SandboxPlan:
    """Immutable runtime plan emitted by a backend."""

    backend_name: str
    production_eligible: bool
    test_only: bool
    container_name: str
    command: tuple[str, ...]
    host_write_root: Path
    container_write_root: PurePosixPath
    readonly_inputs: tuple[ReadOnlyMount, ...]
    wall_time_seconds: int
    isolation_attestation: Mapping[str, bool] = field(default_factory=dict)
    input_verifications: tuple[MountVerification, ...] = ()


class SandboxBackend(ABC):
    """Backend contract.  Production release is a separate explicit property."""

    backend_name: ClassVar[str]
    production_eligible: ClassVar[bool]
    test_only: ClassVar[bool]

    @abstractmethod
    def plan(self, request: SandboxRequest) -> SandboxPlan:
        """Validate a request and return a non-executed runtime plan."""


def _is_same_or_descendant(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _paths_overlap(left: Path, right: Path) -> bool:
    return _is_same_or_descendant(left, right) or _is_same_or_descendant(right, left)


def _validate_workspace_boundaries(request: SandboxRequest) -> None:
    write_root = request.host_write_root
    for forbidden in request.forbidden_workspace_roots:
        if _paths_overlap(write_root, forbidden):
            raise IsolationError("write root overlaps a forbidden agent workspace")

    seen_targets: set[PurePosixPath] = set()
    for mount in request.readonly_inputs:
        if mount.target in seen_targets:
            raise IsolationError(f"duplicate container mount target: {mount.target}")
        seen_targets.add(mount.target)
        if _paths_overlap(mount.source.resolve(strict=False), write_root):
            raise IsolationError("read-only input and writable workspace overlap")
        for forbidden in request.forbidden_workspace_roots:
            if _is_same_or_descendant(mount.source, forbidden):
                raise IsolationError("another agent workspace cannot be mounted")


def _validate_review_freshness(request: SandboxRequest) -> None:
    is_reviewer = request.role_id in REVIEWER_ROLES
    if is_reviewer and request.review_context is None:
        raise IsolationError("reviewer runs require explicit author isolation context")
    if not is_reviewer and request.review_context is not None:
        raise IsolationError("review isolation context is only valid for reviewer roles")
    if request.review_context is None:
        return

    context = request.review_context
    if request.attempt_id == context.author_attempt_id:
        raise IsolationError("reviewer cannot reuse the author's attempt")
    if request.session_id == context.author_session_id:
        raise IsolationError("reviewer cannot inherit the author's session")
    if _paths_overlap(request.host_write_root, context.author_write_root):
        raise IsolationError("reviewer writable directory overlaps author state")
    for mount in request.readonly_inputs:
        if _paths_overlap(
            mount.source.resolve(strict=False), context.author_write_root
        ):
            raise IsolationError("reviewer cannot mount author runtime state")


class DockerSandboxBackend(SandboxBackend):
    """Strict Docker command planner; it never starts Docker itself."""

    backend_name = "docker-strict"
    production_eligible = True
    test_only = False

    def __init__(
        self,
        docker_executable: str = "docker",
        *,
        approved_input_roots: tuple[ApprovedInputRoot, ...] = (),
        egress_attestations: tuple[EgressControllerAttestation, ...] = (),
    ) -> None:
        if not docker_executable.strip():
            raise IsolationError("docker_executable cannot be blank")
        self._docker_executable = docker_executable
        roots = tuple(approved_input_roots)
        if len({(root.root, root.source_kind) for root in roots}) != len(roots):
            raise IsolationError("approved input roots must be unique")
        self._approved_input_roots = roots
        attestations = {item.attestation_id: item for item in egress_attestations}
        if len(attestations) != len(egress_attestations):
            raise IsolationError("egress attestation IDs must be unique")
        self._egress_attestations = attestations
        self._issued_attempts: set[tuple[str, str, str, str]] = set()
        self._lock = Lock()

    def plan(self, request: SandboxRequest) -> SandboxPlan:
        request.limits.validate()
        _validate_workspace_boundaries(request)
        _validate_review_freshness(request)
        input_verifications = self._verify_mounts(request)
        egress = self._verify_egress(request)
        attempt_key = (
            request.project_id,
            request.task_id,
            request.role_id,
            request.attempt_id,
        )
        with self._lock:
            if attempt_key in self._issued_attempts:
                raise IsolationError(
                    "an attempt may be planned only once; retry with a new attempt_id"
                )
            self._issued_attempts.add(attempt_key)

        suffix = uuid4().hex[:12]
        raw_name = (
            f"mh-{request.project_id}-{request.task_id}-"
            f"{request.role_id}-{request.attempt_id}-{suffix}"
        ).lower()
        container_name = _SAFE_CONTAINER_NAME.sub("-", raw_name)[:128].strip("-.")
        command: list[str] = [
            self._docker_executable,
            "run",
            "--rm",
            "--name",
            container_name,
            "--read-only",
            "--user",
            "65532:65532",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(request.limits.pids),
            "--cpus",
            f"{request.limits.cpus:g}",
            "--memory",
            str(request.limits.memory_bytes),
            "--storage-opt",
            f"size={request.limits.disk_bytes}",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=67108864",
            "--mount",
            (
                "type=bind,"
                f"src={request.host_write_root},"
                "dst=/workspace,rw"
            ),
            "--workdir",
            "/workspace",
        ]

        if request.network_policy == "deny":
            command.extend(("--network", "none"))
        else:
            assert egress is not None
            command.extend(
                (
                    "--network",
                    egress.controller_network,
                    "--env",
                    f"HTTPS_PROXY={egress.proxy_url}",
                    "--env",
                    f"HTTP_PROXY={egress.proxy_url}",
                    "--env",
                    f"MODELING_HARNESS_ALLOWED_DOMAINS={','.join(egress.allowed_domains)}",
                    "--env",
                    f"MODELING_HARNESS_EGRESS_POLICY_SHA256={egress.policy_sha256}",
                )
            )

        for mount in request.readonly_inputs:
            command.extend(
                (
                    "--mount",
                    f"type=bind,src={mount.source},dst={mount.target},readonly",
                )
            )
        command.append(request.image)
        command.extend(request.entrypoint)

        return SandboxPlan(
            backend_name=self.backend_name,
            production_eligible=self.production_eligible,
            test_only=self.test_only,
            container_name=container_name,
            command=tuple(command),
            host_write_root=request.host_write_root,
            container_write_root=PurePosixPath("/workspace"),
            readonly_inputs=request.readonly_inputs,
            wall_time_seconds=request.limits.wall_time_seconds,
            isolation_attestation=MappingProxyType(
                {
                    "new_container": True,
                    "unique_write_root": True,
                    "raw_inputs_read_only": True,
                    "other_workspaces_unmounted": True,
                    "root_filesystem_read_only": True,
                    "network_default_deny": request.network_policy == "deny",
                    "network_egress_enforced": (
                        request.network_policy == "deny" or egress is not None
                    ),
                    "input_hashes_verified": len(input_verifications)
                    == len(request.readonly_inputs),
                    "reviewer_fresh_environment": request.review_context is not None
                    if request.role_id in REVIEWER_ROLES
                    else True,
                }
            ),
            input_verifications=input_verifications,
        )

    def _verify_mounts(
        self, request: SandboxRequest
    ) -> tuple[MountVerification, ...]:
        verifications: list[MountVerification] = []
        for mount in request.readonly_inputs:
            if mount.content_sha256 is None:
                raise IsolationError("production mounts require a content SHA-256")
            if _is_filesystem_root(mount.source):
                raise IsolationError("filesystem root cannot be mounted")
            if not mount.source.exists():
                raise IsolationError(f"input mount does not exist: {mount.source}")
            _reject_link_or_reparse_components(mount.source)
            real_source = mount.real_source
            matching_roots = [
                root
                for root in self._approved_input_roots
                if root.source_kind == mount.source_kind
                and _is_same_or_descendant(real_source, root.root)
            ]
            if len(matching_roots) != 1:
                raise IsolationError(
                    "mount source is not contained by exactly one approved root "
                    "of the declared kind"
                )
            approved = matching_roots[0]
            actual_hash = sha256_path(mount.source)
            if actual_hash != mount.content_sha256:
                raise IsolationError(
                    "input content hash mismatch against approved manifest attestation"
                )
            verifications.append(
                MountVerification(
                    source=real_source,
                    source_kind=mount.source_kind,
                    content_sha256=actual_hash,
                    approved_root=approved.root,
                    approved_manifest_sha256=approved.manifest_sha256,
                )
            )
        return tuple(verifications)

    def _verify_egress(
        self, request: SandboxRequest
    ) -> EgressControllerAttestation | None:
        if request.network_policy == "deny":
            return None
        attestation = self._egress_attestations.get(
            str(request.egress_attestation_id)
        )
        if attestation is None:
            raise IsolationError(
                "allowlisted networking has no trusted egress controller attestation"
            )
        if request.network_proxy_url != attestation.proxy_url:
            raise IsolationError("requested proxy does not match trusted egress controller")
        if request.allowed_domains != attestation.allowed_domains:
            raise IsolationError(
                "requested domains do not match trusted egress controller policy"
            )
        return attestation


class LocalSandboxBackend(SandboxBackend):
    """Test-only local planner.

    This backend provides no process, filesystem, network, or reviewer-state
    isolation.  It exists only so unit tests can exercise orchestration logic
    without Docker and is categorically ineligible for production release.
    """

    backend_name = "local-test-only"
    production_eligible = False
    test_only = True

    def plan(self, request: SandboxRequest) -> SandboxPlan:
        request.limits.validate()
        _validate_workspace_boundaries(request)
        _validate_review_freshness(request)
        return SandboxPlan(
            backend_name=self.backend_name,
            production_eligible=False,
            test_only=True,
            container_name=f"local-{uuid4().hex}",
            command=("local-test-only", *request.entrypoint),
            host_write_root=request.host_write_root,
            container_write_root=PurePosixPath("/workspace"),
            readonly_inputs=request.readonly_inputs,
            wall_time_seconds=request.limits.wall_time_seconds,
            isolation_attestation=MappingProxyType(
                {
                    "new_container": False,
                    "unique_write_root": True,
                    "raw_inputs_read_only": False,
                    "other_workspaces_unmounted": False,
                    "root_filesystem_read_only": False,
                    "network_default_deny": False,
                    "network_egress_enforced": False,
                    "input_hashes_verified": False,
                    "reviewer_fresh_environment": False,
                }
            ),
            input_verifications=(),
        )


def require_production_backend(
    backend_or_plan: SandboxBackend | SandboxPlan,
) -> None:
    """Fail closed unless a strict, non-test backend backs a release."""

    eligible = backend_or_plan.production_eligible
    test_only = backend_or_plan.test_only
    strict_type = (
        isinstance(backend_or_plan, DockerSandboxBackend)
        if isinstance(backend_or_plan, SandboxBackend)
        else backend_or_plan.backend_name == DockerSandboxBackend.backend_name
    )
    if not eligible or test_only or not strict_type:
        raise ProductionIsolationError(
            "production release requires a production-eligible isolated backend; "
            "the local backend is test-only"
        )
    if isinstance(backend_or_plan, SandboxPlan):
        required_attestations = {
            "new_container",
            "unique_write_root",
            "raw_inputs_read_only",
            "other_workspaces_unmounted",
            "root_filesystem_read_only",
            "network_egress_enforced",
            "input_hashes_verified",
            "reviewer_fresh_environment",
        }
        if not all(
            backend_or_plan.isolation_attestation.get(item) is True
            for item in required_attestations
        ):
            raise ProductionIsolationError(
                "production sandbox plan lacks mandatory isolation attestations"
            )
        if len(backend_or_plan.input_verifications) != len(
            backend_or_plan.readonly_inputs
        ):
            raise ProductionIsolationError(
                "production sandbox plan lacks input verification bindings"
            )
