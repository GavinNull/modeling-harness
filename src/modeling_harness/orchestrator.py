"""Main-agent-only orchestration over packets, routing, isolation and lifecycle.

This module is intentionally a control-plane implementation.  It plans
sandboxes and hands immutable task packets to an injected executor; it never
calls an LLM, starts Docker, or mutates an agent's output itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Any, Protocol
from uuid import uuid4

from modeling_harness.config import load_yaml
from modeling_harness.agent_body import (
    AgentBodyControlAuthorizationV1,
    AgentBodyMergeV1,
    AgentBodyProvenanceV1,
    OpaqueEvaluationReceiptV1,
    AgentBodyBoundaryError,
    ConstructionLineageEntry,
    ConstructionLineageLedger,
    MAIN_AGENT_ACTIONS,
    MAIN_AGENT_OUTPUTS,
    MAIN_AGENT_WRITES,
    OpaqueEvaluationReceiptLedger,
    ReleaseGateDisposition,
    ReleaseGateLedger,
    canonical_sha256 as agent_body_sha256,
    enforce_main_agent_boundary,
)
from modeling_harness.governance import (
    CORE_BUILDER_ROLES,
    FeedbackFirewall,
    GovernanceError,
    ProvenanceError,
    ReviewEvidenceVault,
    SourceProvenanceLedger,
    bootstrap_source_provenance_control_plane,
    validate_artifact_provenance,
    validate_role_data_boundary,
)
from modeling_harness.isolation import (
    DockerSandboxBackend,
    REVIEWER_ROLES,
    ReadOnlyMount,
    ResourceLimits,
    ReviewIsolationContext,
    SandboxBackend,
    SandboxPlan,
    SandboxRequest,
    require_production_backend,
)
from modeling_harness.packets import (
    PacketContext,
    PacketError,
    PacketValidator,
    SQLitePacketIdentityLedger,
    sha256_json,
)
from modeling_harness.routing import RoutingError, StarRouter
from modeling_harness.runtime import (
    DockerTaskExecutor,
    ExecutionAttestationLedger,
    ExecutionRecord,
)
from modeling_harness.state_machine import (
    LifecycleDefinition,
    RunLedger,
    TransitionEvidence,
    TransitionRejected,
)


SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
MAIN_AGENT_ID = "main_agent"
REVIEWER_ORDER = (
    "mathematical_reviewer",
    "reproducibility_reviewer",
    "evidence_communication_reviewer",
)
EARLIEST_IMPACT_EVENT = MappingProxyType(
    {
        "definition": "revision.restart_from_definition",
        "understanding": "revision.restart_from_understanding",
        "creation": "revision.restart_from_creation",
    }
)


class OrchestrationError(ValueError):
    """Base class for orchestration policy failures."""


class DispatchRejected(OrchestrationError):
    """Raised when a role dispatch cannot satisfy the immutable contract."""


class ReviewRoundError(OrchestrationError):
    """Raised when blind independent review protocol is violated."""


class RetryBudgetExhausted(OrchestrationError):
    """Raised before an attempt would exceed the configured retry budget."""


class ReleaseRejected(OrchestrationError):
    """Raised when any production release gate fails closed."""


class ArtifactVerifier(Protocol):
    """Minimal content-store interface consumed by the control plane."""

    def verify_promoted(self, promoted_manifest_sha256: str) -> Mapping[str, Any]:
        """Verify one promoted immutable snapshot and return its full manifest."""

    def resolve_promoted_artifact(
        self,
        promoted_manifest_sha256: str,
        artifact_id: str,
    ) -> Path:
        """Return the trusted immutable path for one promoted artifact."""

    def read_promoted_artifact(
        self,
        promoted_manifest_sha256: str,
        artifact_id: str,
    ) -> bytes:
        """Read one verified promoted artifact from the trusted store."""


class TaskExecutor(Protocol):
    """Dependency-injected execution boundary."""

    def execute(self, plan: SandboxPlan, task_packet: Mapping[str, Any]) -> Any:
        """Record or execute an already validated sandbox plan and TaskPacket."""


class RecordingExecutor:
    """Test executor that records immutable snapshots and performs no work."""

    def __init__(self) -> None:
        self.calls: list[tuple[SandboxPlan, dict[str, Any]]] = []

    def execute(self, plan: SandboxPlan, task_packet: Mapping[str, Any]) -> int:
        self.calls.append((plan, deepcopy(dict(task_packet))))
        return len(self.calls)


@dataclass(frozen=True)
class PromptRef:
    prompt_id: str
    version: str
    sha256: str

    @classmethod
    def from_file(
        cls,
        *,
        prompt_id: str,
        version: str,
        path: str | Path,
    ) -> "PromptRef":
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        return cls(prompt_id=prompt_id, version=version, sha256=digest)

    def __post_init__(self) -> None:
        if not self.prompt_id.strip() or not self.version.strip():
            raise DispatchRejected("prompt_id and prompt version are required")
        if not SHA256_RE.fullmatch(self.sha256):
            raise DispatchRejected("prompt sha256 must be lowercase SHA-256")


@dataclass(frozen=True)
class PromotedInput:
    """One verified, content-addressed input mounted read-only."""

    artifact_id: str
    manifest_sha256: str
    content_sha256: str
    source_path: Path
    producer_task_id: str
    producer_run_id: str
    producer_attempt_id: str
    producer_role_id: str
    source_provenance_sha256s: tuple[str, ...]
    real_task_derived: bool
    classification: str = "internal"
    content_kind: str = "task-artifact"
    mount_name: str | None = None

    def __post_init__(self) -> None:
        if not self.artifact_id.strip():
            raise DispatchRejected("artifact_id is required")
        if not SHA256_RE.fullmatch(self.manifest_sha256):
            raise DispatchRejected("manifest_sha256 must be lowercase SHA-256")
        if not SHA256_RE.fullmatch(self.content_sha256):
            raise DispatchRejected("content_sha256 must be lowercase SHA-256")
        if (
            not isinstance(self.source_provenance_sha256s, tuple)
            or not self.source_provenance_sha256s
            or len(self.source_provenance_sha256s)
            != len(set(self.source_provenance_sha256s))
            or any(
                not SHA256_RE.fullmatch(item)
                for item in self.source_provenance_sha256s
            )
        ):
            raise DispatchRejected(
                "PromotedInput requires unique source provenance hashes"
            )
        if not isinstance(self.real_task_derived, bool):
            raise DispatchRejected("PromotedInput requires a boolean provenance taint")
        for field_name in (
            "producer_task_id",
            "producer_run_id",
            "producer_attempt_id",
            "producer_role_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise DispatchRejected(f"{field_name} is required")
        if self.classification not in {
            "public",
            "internal",
            "restricted",
            "benchmark-hidden",
        }:
            raise DispatchRejected("unknown input classification")
        from modeling_harness.governance import INPUT_CONTENT_KINDS

        if self.content_kind not in INPUT_CONTENT_KINDS:
            raise DispatchRejected("unknown input content kind")
        source = self.source_path.expanduser().resolve()
        object.__setattr__(self, "source_path", source)

    @property
    def mount_path(self) -> str:
        raw = self.mount_name or self.artifact_id
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip(".-")
        if not safe:
            raise DispatchRejected("mount name cannot be normalized safely")
        return f"/inputs/{safe}"


@dataclass(frozen=True)
class DispatchRecord:
    role_id: str
    attempt_id: str
    session_id: str
    task_packet: Mapping[str, Any]
    sandbox_request: SandboxRequest
    sandbox_plan: SandboxPlan


@dataclass(frozen=True)
class ReviewRound:
    round_id: str
    candidate_manifest_sha256: str
    candidate_manifest: Mapping[str, Any]
    reviewer_dispatches: Mapping[str, DispatchRecord]
    author_attempt_id: str


@dataclass(frozen=True)
class RevisionRestart:
    earliest_impact: str
    target_state: str
    dispatch: DispatchRecord
    prior_attempt_id: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _opaque_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _document_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class Orchestrator:
    """Recoverable main-agent control plane for all sixteen role sandboxes."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        runtime_root: str | Path,
        project_id: str,
        task_id: str,
        run_id: str,
        backend: SandboxBackend,
        artifact_verifier: ArtifactVerifier,
        executor: TaskExecutor,
        container_image: str,
        limits: ResourceLimits,
        network_proxy_url: str | None = None,
        allowed_domains: Sequence[str] = (),
        egress_attestation_id: str | None = None,
        ledger: RunLedger | None = None,
        packet_validator: PacketValidator | None = None,
        review_vault: ReviewEvidenceVault | None = None,
        provenance_ledger: SourceProvenanceLedger | None = None,
        production_mode: bool = False,
        execution_attestation_ledger: ExecutionAttestationLedger | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.runtime_root = Path(runtime_root).resolve()
        self.project_id = project_id
        self.task_id = task_id
        self.run_id = run_id
        self._backend = backend
        self.artifact_verifier = artifact_verifier
        self._executor = executor
        self._production_mode = production_mode
        self._execution_attestation_ledger = execution_attestation_ledger
        self.container_image = container_image
        self.limits = limits
        self.network_proxy_url = network_proxy_url
        self.allowed_domains = tuple(allowed_domains)
        self.egress_attestation_id = egress_attestation_id

        registry = load_yaml(
            self.project_root / "workspaces/architect/role_registry.yaml"
        )
        if not isinstance(registry, dict) or not isinstance(registry.get("roles"), list):
            raise DispatchRejected("role registry is malformed")
        role_entries = registry["roles"]
        self._roles = {
            entry["id"]: entry
            for entry in role_entries
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        }
        subagent_ids = tuple(
            role_id for role_id in self._roles if role_id != MAIN_AGENT_ID
        )
        if len(subagent_ids) != 16:
            raise DispatchRejected(
                f"orchestrator requires exactly 16 subagents, got {len(subagent_ids)}"
            )
        builder_ids = tuple(
            role_id
            for role_id in subagent_ids
            if self._roles[role_id].get("kind") in {"builder", "author"}
        )
        self.router = StarRouter(subagent_ids, builder_ids=builder_ids)
        self.packet_validator = packet_validator or PacketValidator.from_project_root(
            self.project_root
        )
        if production_mode:
            if type(backend) is not DockerSandboxBackend:
                raise DispatchRejected(
                    "production_mode requires the strict DockerSandboxBackend"
                )
            require_production_backend(backend)
            identity_ledger = self.packet_validator.identity_ledger
            if (
                not isinstance(identity_ledger, SQLitePacketIdentityLedger)
                or identity_ledger.durable is not True
            ):
                raise DispatchRejected(
                    "production_mode requires a persistent SQLitePacketIdentityLedger"
                )
            if not isinstance(executor, DockerTaskExecutor):
                raise DispatchRejected(
                    "production_mode requires DockerTaskExecutor"
                )
            if (
                execution_attestation_ledger is None
                or execution_attestation_ledger.durable is not True
                or executor.attestation_ledger is not execution_attestation_ledger
            ):
                raise DispatchRejected(
                    "production_mode requires the executor's persistent "
                    "ExecutionAttestationLedger"
                )
        if ledger is None:
            definition = LifecycleDefinition.load(
                self.project_root / "workspaces/architect/state_machine.yaml",
                self.project_root / "workspaces/architect/role_registry.yaml",
            )
            ledger = RunLedger(
                definition,
                run_id=run_id,
                initial_attempt_id="attempt-initial",
            )
        self.ledger = ledger
        if review_vault is not None and provenance_ledger is not None and (
            review_vault.provenance_ledger is not provenance_ledger
        ):
            raise DispatchRejected(
                "review and artifact provenance must share one trusted ledger"
            )
        self.provenance_ledger = (
            provenance_ledger
            or (
                review_vault.provenance_ledger
                if review_vault is not None
                else bootstrap_source_provenance_control_plane().ledger
            )
        )
        self.review_vault = review_vault or ReviewEvidenceVault(
            self.provenance_ledger
        )
        self.feedback_firewall = FeedbackFirewall(self.review_vault)
        enforce_main_agent_boundary(
            actions=MAIN_AGENT_ACTIONS,
            writes=MAIN_AGENT_WRITES,
            outputs=MAIN_AGENT_OUTPUTS,
        )
        self._agent_body_construction_ledger = ConstructionLineageLedger()
        self._opaque_evaluation_receipt_ledger = OpaqueEvaluationReceiptLedger()
        self._agent_body_release_ledger = ReleaseGateLedger()

        self._dispatches: dict[tuple[str, str], DispatchRecord] = {}
        self._execution_records: dict[tuple[str, str], ExecutionRecord] = {}
        self._active_by_role: dict[str, DispatchRecord] = {}
        self._workspace_roots: set[Path] = set()
        self._session_ids: set[str] = set()
        self._sealed_attempts: set[str] = set()
        self._failed_stage_attempts: dict[str, int] = {}
        self._review_rounds: dict[str, ReviewRound] = {}
        self._pending_reviews: dict[str, dict[str, dict[str, Any]]] = {}
        self._completed_review_packet_ids: dict[str, tuple[str, ...]] = {}
        self._cancelled = False

    @property
    def subagent_ids(self) -> frozenset[str]:
        return self.router.subagent_ids

    @property
    def backend(self) -> SandboxBackend:
        """Read-only backend selected at construction."""

        return self._backend

    @property
    def production_mode(self) -> bool:
        return self._production_mode

    def dispatch(
        self,
        *,
        actor: str,
        role_id: str,
        objective: str,
        prompt: PromptRef,
        promoted_inputs: Sequence[PromotedInput],
        attempt_id: str | None = None,
        parent_packet_id: str | None = None,
        review_context: ReviewIsolationContext | None = None,
        required_deliverables: Sequence[Mapping[str, Any]] | None = None,
        acceptance_criteria: Sequence[Mapping[str, Any]] | None = None,
        allowed_tools: Sequence[Mapping[str, Any]] = (),
        task_purpose: str | None = None,
        control_authorization: AgentBodyControlAuthorizationV1 | None = None,
    ) -> DispatchRecord:
        record = self._prepare_dispatch(
            actor=actor,
            role_id=role_id,
            objective=objective,
            prompt=prompt,
            promoted_inputs=promoted_inputs,
            attempt_id=attempt_id,
            parent_packet_id=parent_packet_id,
            review_context=review_context,
            required_deliverables=required_deliverables,
            acceptance_criteria=acceptance_criteria,
            allowed_tools=allowed_tools,
            task_purpose=task_purpose,
            control_authorization=control_authorization,
        )
        self._activate_dispatch(record)
        return record

    def _prepare_dispatch(
        self,
        *,
        actor: str,
        role_id: str,
        objective: str,
        prompt: PromptRef,
        promoted_inputs: Sequence[PromotedInput],
        attempt_id: str | None,
        parent_packet_id: str | None,
        review_context: ReviewIsolationContext | None,
        required_deliverables: Sequence[Mapping[str, Any]] | None,
        acceptance_criteria: Sequence[Mapping[str, Any]] | None,
        allowed_tools: Sequence[Mapping[str, Any]],
        task_purpose: str | None = None,
        control_authorization: AgentBodyControlAuthorizationV1 | None = None,
    ) -> DispatchRecord:
        if self._cancelled:
            raise DispatchRejected("cancelled runs cannot create new dispatches")
        self.router.authorize_dispatch(actor)
        if role_id not in self.router.subagent_ids:
            raise DispatchRejected(f"unknown subagent role {role_id!r}")
        if not isinstance(objective, str) or not objective.strip():
            raise DispatchRejected("dispatch objective is required")

        attempt = attempt_id or _opaque_id("attempt")
        session = _opaque_id("session")
        if (role_id, attempt) in self._dispatches:
            raise DispatchRejected("attempt_id reuse is forbidden")
        if attempt in self._sealed_attempts:
            raise DispatchRejected("sealed attempt_id cannot be reused")
        if session in self._session_ids:
            raise DispatchRejected("session_id reuse is forbidden")

        role = self._roles[role_id]
        purpose = task_purpose or (
            "evaluation"
            if role_id == "benchmark_curator" or role.get("kind") == "reviewer"
            else "generic-governance"
            if role_id in CORE_BUILDER_ROLES
            else "task-answer"
        )
        inputs = tuple(promoted_inputs)
        if not inputs:
            raise DispatchRejected(
                "every subagent dispatch requires at least one promoted input hash"
            )
        if len({item.manifest_sha256 for item in inputs}) != len(inputs):
            raise DispatchRejected("promoted input manifests must be unique")
        if len({item.mount_path for item in inputs}) != len(inputs):
            raise DispatchRejected("promoted input mount paths must be unique")
        require_trusted_path = (
            role_id in CORE_BUILDER_ROLES
            or purpose
            in {"generic-governance", "role-identity", "agent-core-modification"}
        )
        input_manifests = tuple(
            self._verify_promoted_input(
                item,
                attempt,
                require_trusted_path=require_trusted_path,
            )
            for item in inputs
        )

        try:
            validate_role_data_boundary(
                role_id=role_id,
                role_kind=str(role.get("kind", "")),
                task_purpose=purpose,
                content_kinds=(item.content_kind for item in inputs),
            )
            if purpose == "agent-core-modification":
                if role_id not in CORE_BUILDER_ROLES:
                    raise GovernanceError(
                        "Agent-body construction may target only a canonical core builder"
                    )
                if type(control_authorization) is not AgentBodyControlAuthorizationV1:
                    raise GovernanceError(
                        "Agent-body dispatch requires the positive closed control authorization"
                    )
                self.feedback_firewall.dispatch_control_authorization(
                    actor=actor,
                    recipient_role_id=role_id,
                    authorization=control_authorization,
                )
                expected_hash = agent_body_sha256(
                    control_authorization.as_mapping()
                )
                matches = [
                    item
                    for item in inputs
                    if item.content_kind == "agent-body-control-authorization"
                    and item.content_sha256 == expected_hash
                ]
                if len(matches) != 1:
                    raise GovernanceError(
                        "Agent-body dispatch requires exactly one hash-bound closed authorization"
                    )
            elif control_authorization is not None:
                raise GovernanceError(
                    "Agent-body control authorization requires agent-core-modification purpose"
                )
        except (GovernanceError, PacketError) as exc:
            raise DispatchRejected(str(exc)) from exc
        network_policy = role.get("network_policy")
        if network_policy not in {"deny", "allowlisted-readonly"}:
            raise DispatchRejected("role has an invalid network policy")
        if network_policy == "allowlisted-readonly" and (
            not self.network_proxy_url
            or not self.allowed_domains
            or not self.egress_attestation_id
        ):
            raise DispatchRejected(
                "allowlisted-readonly role requires proxy, domains, and an "
                "egress attestation"
            )

        host_write_root = (
            self.runtime_root
            / self.project_id
            / self.task_id
            / role_id
            / attempt
        ).resolve()
        if any(
            host_write_root == prior or host_write_root.is_relative_to(prior)
            or prior.is_relative_to(host_write_root)
            for prior in self._workspace_roots
        ):
            raise DispatchRejected("new dispatch workspace overlaps an existing workspace")

        mounts = tuple(
            ReadOnlyMount(
                source=item.source_path,
                target=PurePosixPath(item.mount_path),
                source_kind="promoted-artifact",
                content_sha256=item.content_sha256,
            )
            for item in inputs
        )
        request = SandboxRequest(
            project_id=self.project_id,
            task_id=self.task_id,
            role_id=role_id,
            attempt_id=attempt,
            session_id=session,
            host_write_root=host_write_root,
            image=self.container_image,
            limits=self.limits,
            readonly_inputs=mounts,
            network_policy=network_policy,
            network_proxy_url=(
                self.network_proxy_url
                if network_policy == "allowlisted-readonly"
                else None
            ),
            allowed_domains=(
                self.allowed_domains
                if network_policy == "allowlisted-readonly"
                else ()
            ),
            egress_attestation_id=(
                self.egress_attestation_id
                if network_policy == "allowlisted-readonly"
                else None
            ),
            review_context=review_context,
            forbidden_workspace_roots=tuple(self._workspace_roots),
            entrypoint=("modeling-harness-agent", role_id),
        )
        plan = self._backend.plan(request)
        task_packet: dict[str, Any] = {
            "schema_version": "1.0.0",
            "packet_id": _opaque_id("packet"),
            "task_id": self.task_id,
            "run_id": self.run_id,
            "attempt_id": attempt,
            "parent_packet_id": parent_packet_id,
            "role_id": role_id,
            "task_purpose": purpose,
            "objective": objective,
            "scope": {
                "in": ["Only the role charter and promoted immutable inputs."],
                "out": [
                    "Direct peer communication, unpromoted workspaces, and "
                    "unapproved specialization."
                ],
            },
            "input_artifacts": [
                {
                    "artifact_id": item.artifact_id,
                    "manifest_sha256": item.manifest_sha256,
                    "content_sha256": item.content_sha256,
                    "logical_path": next(
                        artifact["logical_path"]
                        for artifact in manifest["artifacts"]
                        if artifact["artifact_id"] == item.artifact_id
                    ),
                    "mount_path": item.mount_path,
                    "classification": item.classification,
                    "content_kind": item.content_kind,
                    "source_provenance_sha256s": list(
                        item.source_provenance_sha256s
                    ),
                    "real_task_derived": item.real_task_derived,
                }
                for item, manifest in zip(inputs, input_manifests)
            ],
            "allowed_tools": [deepcopy(dict(item)) for item in allowed_tools],
            "required_deliverables": [
                deepcopy(dict(item))
                for item in (
                    required_deliverables
                    or (
                        {
                            "deliverable_id": "deliverable-result",
                            "description": "Role-scoped ResultPacket and artifacts.",
                            "media_type": "application/json",
                        },
                    )
                )
            ],
            "acceptance_criteria": [
                deepcopy(dict(item))
                for item in (
                    acceptance_criteria
                    or (
                        {
                            "criterion_id": "criterion-contract",
                            "description": (
                                "Schema, isolation, lineage, and role-scope gates pass."
                            ),
                            "evidence_type": "test",
                            "hard_gate": True,
                        },
                    )
                )
            ],
            "workspace": {
                "workspace_id": _opaque_id("workspace"),
                "container_id": plan.container_name,
                "write_root": (
                    f"/runs/{self.project_id}/{self.task_id}/{role_id}/{attempt}/"
                ),
                "read_only_mounts": [item.mount_path for item in inputs],
                "fresh_environment_required": True,
            },
            "prompt": {
                "prompt_id": prompt.prompt_id,
                "version": prompt.version,
                "sha256": prompt.sha256,
            },
            "benchmark_access_level": (
                "hidden-benchmark-admin"
                if role_id == "benchmark_curator"
                and any(
                    item.classification == "benchmark-hidden" for item in inputs
                )
                else "none"
            ),
            "execution_limits": {
                "wall_time_seconds": self.limits.wall_time_seconds,
                "cpu_cores": self.limits.cpus,
                "memory_mb": max(128, self.limits.memory_bytes // (1024 * 1024)),
                "disk_mb": max(128, self.limits.disk_bytes // (1024 * 1024)),
                "network_policy": network_policy,
            },
            "created_at": _utc_now(),
            "metadata": {
                "session_id": session,
                **(
                    {
                        "control_authorization_sha256": (
                            control_authorization.authorization_sha256
                        )
                    }
                    if control_authorization is not None
                    else {}
                ),
            },
        }
        try:
            self.packet_validator.validate(
                "TaskPacket",
                task_packet,
                context=PacketContext(
                    input_manifests=input_manifests,
                    enforce_parent=parent_packet_id is not None,
                    expected_parent_packet_id=parent_packet_id,
                ),
            )
        except PacketError as exc:
            raise DispatchRejected(str(exc)) from exc
        self.router.authorize(MAIN_AGENT_ID, role_id, "TaskPacket")
        return DispatchRecord(
            role_id=role_id,
            attempt_id=attempt,
            session_id=session,
            task_packet=MappingProxyType(deepcopy(task_packet)),
            sandbox_request=request,
            sandbox_plan=plan,
        )

    def _activate_dispatch(self, record: DispatchRecord) -> None:
        key = (record.role_id, record.attempt_id)
        if key in self._dispatches:
            raise DispatchRejected("attempt dispatch was already activated")
        execution = self._executor.execute(record.sandbox_plan, record.task_packet)
        if self._production_mode:
            if not isinstance(execution, ExecutionRecord):
                raise DispatchRejected(
                    "production dispatch requires an ExecutionRecord"
                )
            self._validate_production_execution(record, execution)
        self._dispatches[key] = record
        if isinstance(execution, ExecutionRecord):
            self._execution_records[key] = execution
        self._active_by_role[record.role_id] = record
        self._workspace_roots.add(record.sandbox_request.host_write_root)
        self._session_ids.add(record.session_id)

    def _validate_production_execution(
        self, dispatch: DispatchRecord, execution: ExecutionRecord
    ) -> None:
        require_production_backend(dispatch.sandbox_plan)
        if (
            execution.status != "succeeded"
            or execution.production_attested is not True
            or execution.run_id != self.run_id
            or execution.task_id != self.task_id
            or execution.attempt_id != dispatch.attempt_id
            or execution.session_id != dispatch.session_id
            or execution.container_name != dispatch.sandbox_plan.container_name
        ):
            raise DispatchRejected(
                "production execution record is not a successful bound attestation"
            )
        if (
            not SHA256_RE.fullmatch(execution.attestation_hash or "")
            or execution.attestation_sequence is None
        ):
            raise DispatchRejected("production execution lacks ledger coordinates")
        ledger = self._execution_attestation_ledger
        if ledger is None or ledger.verify() < execution.attestation_sequence:
            raise DispatchRejected("execution attestation ledger does not verify")
        if dict(ledger.payload(execution.execution_id)) != execution.attestation_payload():
            raise DispatchRejected(
                "execution record does not match its persistent ledger payload"
            )

    def _load_verified_promoted_manifest(
        self, manifest_sha256: str, attempt_id: str
    ) -> Mapping[str, Any]:
        try:
            verified = self.artifact_verifier.verify_promoted(manifest_sha256)
            if not isinstance(verified, Mapping):
                raise DispatchRejected(
                    "artifact verifier must return the full ArtifactManifest"
                )
            self.packet_validator.validate_schema("ArtifactManifest", verified)
            if sha256_json(verified) != manifest_sha256:
                raise DispatchRejected("verified manifest hash does not match input")
            if verified.get("promotion", {}).get("status") != "promoted":
                raise DispatchRejected("input manifest is not promoted")
        except Exception as exc:
            incident_hash = _document_hash(
                {
                    "classification": "hash_mismatch",
                    "manifest_sha256": manifest_sha256,
                    "attempt_id": attempt_id,
                }
            )
            try:
                self.ledger.quarantine(
                    actor="orchestrator",
                    trigger="hash_mismatch",
                    attempt_id=attempt_id,
                    incident_hash=incident_hash,
                )
            except TransitionRejected:
                pass
            raise DispatchRejected(
                f"promoted input verification failed: {exc}"
            ) from exc
        return deepcopy(dict(verified))

    def _verify_promoted_input(
        self,
        item: PromotedInput,
        attempt_id: str,
        *,
        require_trusted_path: bool,
    ) -> Mapping[str, Any]:
        verified = self._verify_promoted_provenance_tree(
            item.manifest_sha256,
            attempt_id,
            require_authorized_agent_body_source=require_trusted_path,
            verified={},
        )
        expected_bindings = {
            "task_id": item.producer_task_id,
            "run_id": item.producer_run_id,
            "attempt_id": item.producer_attempt_id,
        }
        for field, expected in expected_bindings.items():
            if verified.get(field) != expected:
                raise DispatchRejected(
                    f"promoted input {field} does not match its expected producer"
                )
        producer = verified.get("producer")
        if not isinstance(producer, Mapping) or producer.get(
            "role_id"
        ) != item.producer_role_id:
            raise DispatchRejected(
                "promoted input role_id does not match its expected producer"
            )
        artifacts = verified.get("artifacts")
        if not isinstance(artifacts, list):
            raise DispatchRejected("promoted input manifest has no artifact list")
        matching = [
            artifact
            for artifact in artifacts
            if isinstance(artifact, Mapping)
            and artifact.get("artifact_id") == item.artifact_id
        ]
        if len(matching) != 1:
            raise DispatchRejected(
                "promoted input artifact_id must identify exactly one manifest artifact"
            )
        artifact = matching[0]
        if artifact.get("content_sha256") != item.content_sha256:
            raise DispatchRejected(
                "promoted input content hash does not match its manifest artifact"
            )
        if artifact.get("classification") != item.classification:
            raise DispatchRejected(
                "promoted input classification does not match its manifest artifact"
            )
        if artifact.get("content_kind") != item.content_kind:
            raise DispatchRejected(
                "promoted input content_kind does not match its manifest artifact"
            )
        if tuple(verified.get("source_provenance_sha256s", ())) != (
            item.source_provenance_sha256s
        ):
            raise DispatchRejected(
                "PromotedInput provenance hashes do not match ArtifactManifest"
            )
        if verified.get("real_task_derived") is not item.real_task_derived:
            raise DispatchRejected(
                "PromotedInput taint does not match ArtifactManifest"
            )
        if require_trusted_path:
            resolver = getattr(
                self.artifact_verifier,
                "resolve_promoted_artifact",
                None,
            )
            if not callable(resolver):
                raise DispatchRejected(
                    "Agent-body or identity input lacks a trusted artifact path registry"
                )
            try:
                trusted_path = Path(
                    resolver(item.manifest_sha256, item.artifact_id)
                ).expanduser().resolve()
            except Exception as exc:
                raise DispatchRejected(
                    f"trusted artifact path resolution failed: {exc}"
                ) from exc
            if trusted_path != item.source_path:
                raise DispatchRejected(
                    "promoted input source path does not match trusted artifact registry"
                )
        return verified

    def _verify_promoted_provenance_tree(
        self,
        manifest_sha256: str,
        attempt_id: str,
        *,
        require_authorized_agent_body_source: bool,
        verified: dict[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        existing = verified.get(manifest_sha256)
        if existing is not None:
            return existing
        manifest = self._load_verified_promoted_manifest(
            manifest_sha256, attempt_id
        )
        parents = tuple(
            self._verify_promoted_provenance_tree(
                parent_hash,
                attempt_id,
                require_authorized_agent_body_source=(
                    require_authorized_agent_body_source
                ),
                verified=verified,
            )
            for parent_hash in manifest["input_manifest_sha256s"]
        )
        try:
            validate_artifact_provenance(
                manifest,
                provenance_ledger=self.provenance_ledger,
                input_manifests=parents,
                require_authorized_agent_body_source=(
                    require_authorized_agent_body_source
                ),
            )
        except ProvenanceError as exc:
            raise DispatchRejected(str(exc)) from exc
        verified[manifest_sha256] = manifest
        return manifest

    def route_packet(
        self, *, sender: str, recipient: str, packet_type: str, attempt_id: str
    ) -> None:
        """Authorize one route, quarantining direct peer communication attempts."""

        try:
            self.router.authorize(sender, recipient, packet_type)
        except RoutingError:
            if sender in self.router.subagent_ids and recipient in self.router.subagent_ids:
                incident_hash = _document_hash(
                    {
                        "sender": sender,
                        "recipient": recipient,
                        "packet_type": packet_type,
                        "classification": "direct_peer_communication",
                    }
                )
                try:
                    self.ledger.quarantine(
                        actor="orchestrator",
                        trigger="direct_peer_communication",
                        attempt_id=attempt_id,
                        incident_hash=incident_hash,
                    )
                except TransitionRejected:
                    pass
            raise

    def advance(
        self,
        *,
        actor: str,
        event: str,
        attempt_id: str,
        evidence: TransitionEvidence,
    ) -> None:
        self.ledger.transition(
            actor=actor,
            event=event,
            attempt_id=attempt_id,
            evidence=evidence,
        )

    def dispatch_reviewers(
        self,
        *,
        actor: str,
        candidate: PromotedInput,
        author_dispatch: DispatchRecord,
        prompts: Mapping[str, PromptRef],
    ) -> ReviewRound:
        if actor != MAIN_AGENT_ID:
            raise ReviewRoundError("only main_agent may dispatch reviewers")
        if author_dispatch.role_id in REVIEWER_ROLES:
            raise ReviewRoundError("review candidate must be authored by a non-reviewer")
        if set(prompts) != set(REVIEWER_ORDER):
            raise ReviewRoundError("exactly three role-specific reviewer prompts required")
        if author_dispatch.attempt_id in self._sealed_attempts:
            raise ReviewRoundError("sealed author attempt cannot seed a review round")
        candidate_manifest = self._verify_promoted_input(
            candidate,
            author_dispatch.attempt_id,
            require_trusted_path=False,
        )

        round_id = _opaque_id("review-round")
        context = ReviewIsolationContext(
            author_attempt_id=author_dispatch.attempt_id,
            author_session_id=author_dispatch.session_id,
            author_container_name=author_dispatch.sandbox_plan.container_name,
            author_write_root=author_dispatch.sandbox_request.host_write_root,
        )
        records: dict[str, DispatchRecord] = {}
        for role_id in REVIEWER_ORDER:
            records[role_id] = self.dispatch(
                actor=actor,
                role_id=role_id,
                objective=(
                    "Independently review the immutable candidate under the "
                    "assigned profile; do not inspect peer reviews."
                ),
                prompt=prompts[role_id],
                promoted_inputs=(candidate,),
                parent_packet_id=author_dispatch.task_packet["packet_id"],
                review_context=context,
            )
        hashes = {
            tuple(
                item["manifest_sha256"]
                for item in record.task_packet["input_artifacts"]
            )
            for record in records.values()
        }
        if hashes != {(candidate.manifest_sha256,)}:
            raise ReviewRoundError("reviewers did not receive the identical snapshot")
        if len({record.attempt_id for record in records.values()}) != 3:
            raise ReviewRoundError("reviewers must use distinct attempts")
        if len({record.session_id for record in records.values()}) != 3:
            raise ReviewRoundError("reviewers must use distinct sessions")
        if len(
            {
                record.sandbox_request.host_write_root
                for record in records.values()
            }
        ) != 3:
            raise ReviewRoundError("reviewers must use distinct write roots")

        review_round = ReviewRound(
            round_id=round_id,
            candidate_manifest_sha256=candidate.manifest_sha256,
            candidate_manifest=MappingProxyType(deepcopy(dict(candidate_manifest))),
            reviewer_dispatches=MappingProxyType(records),
            author_attempt_id=author_dispatch.attempt_id,
        )
        self._review_rounds[round_id] = review_round
        self._pending_reviews[round_id] = {}
        return review_round

    def submit_review(
        self,
        *,
        round_id: str,
        actor: str,
        review_packet: Mapping[str, Any],
        peer_review_seen: bool = False,
    ) -> bool:
        try:
            review_round = self._review_rounds[round_id]
        except KeyError as exc:
            raise ReviewRoundError(f"unknown review round {round_id!r}") from exc
        if round_id in self._completed_review_packet_ids:
            raise ReviewRoundError("review round is already complete")
        if actor not in REVIEWER_ROLES or review_packet.get("reviewer_role_id") != actor:
            raise ReviewRoundError("only the named reviewer may submit its review")
        if peer_review_seen:
            incident = _document_hash(
                {
                    "round_id": round_id,
                    "reviewer": actor,
                    "classification": "direct_peer_communication",
                }
            )
            self.ledger.quarantine(
                actor="orchestrator",
                trigger="direct_peer_communication",
                attempt_id=review_round.reviewer_dispatches[actor].attempt_id,
                incident_hash=incident,
            )
            raise ReviewRoundError("first-pass reviewers must remain mutually blind")
        if (
            review_packet.get("candidate_manifest_sha256")
            != review_round.candidate_manifest_sha256
        ):
            incident = _document_hash(
                {
                    "round_id": round_id,
                    "reviewer": actor,
                    "classification": "hash_mismatch",
                }
            )
            self.ledger.quarantine(
                actor="orchestrator",
                trigger="hash_mismatch",
                attempt_id=review_round.reviewer_dispatches[actor].attempt_id,
                incident_hash=incident,
            )
            raise ReviewRoundError("review references the wrong candidate hash")

        dispatch = review_round.reviewer_dispatches[actor]
        self.packet_validator.validate(
            "ReviewPacket",
            review_packet,
            context=PacketContext(
                task_packet=dispatch.task_packet,
                candidate_manifest=review_round.candidate_manifest,
            ),
        )
        self.router.authorize(actor, MAIN_AGENT_ID, "ReviewPacket")
        pending = self._pending_reviews[round_id]
        packet_id = review_packet["packet_id"]
        if actor in pending:
            if pending[actor] != dict(review_packet):
                raise ReviewRoundError("reviewer resubmission changed immutable content")
            return len(pending) == 3
        pending[actor] = deepcopy(dict(review_packet))

        if len(pending) < 3:
            return False
        profiles = {packet["review_profile"] for packet in pending.values()}
        if len(profiles) != 3:
            raise ReviewRoundError("three distinct review profiles are required")
        for packet in pending.values():
            attestation = packet["independence_attestation"]
            if not all(attestation.values()):
                raise ReviewRoundError("all independence attestations must be true")
        packet_ids: list[str] = []
        for role_id in REVIEWER_ORDER:
            packet = pending[role_id]
            self.feedback_firewall.retain_review(role_id, packet)
            packet_ids.append(packet["packet_id"])
        self._completed_review_packet_ids[round_id] = tuple(packet_ids)
        return True

    def collect_reviews(
        self, *, actor: str, round_id: str
    ) -> tuple[dict[str, Any], ...]:
        if actor != MAIN_AGENT_ID:
            raise ReviewRoundError("raw review collection is main-agent-only")
        try:
            packet_ids = self._completed_review_packet_ids[round_id]
        except KeyError as exc:
            raise ReviewRoundError("review round is not complete") from exc
        return tuple(
            self.feedback_firewall.read_review(actor, packet_id)
            for packet_id in packet_ids
        )

    def restart_from_revision(
        self,
        *,
        actor: str,
        revision_decision: Mapping[str, Any],
        objective: str,
        prompt: PromptRef,
        promoted_inputs: Sequence[PromotedInput],
    ) -> RevisionRestart:
        """Restart only the current answer from a task-bound RevisionDecision.

        DTP and MAP are intentionally excluded because they are Agent-body-only
        protocols and cannot truthfully represent real-task answer feedback.
        """

        if actor != MAIN_AGENT_ID:
            raise DispatchRejected("only main_agent may authorize answer revision")
        packet_ids = revision_decision.get("source_review_packet_ids")
        if not isinstance(packet_ids, list):
            raise DispatchRejected("RevisionDecision source review IDs are required")
        try:
            source_reviews = tuple(
                self.review_vault.read(MAIN_AGENT_ID, packet_id)
                for packet_id in packet_ids
            )
            self.packet_validator.validate(
                "RevisionDecision",
                revision_decision,
                context=PacketContext(source_reviews=source_reviews),
            )
        except (GovernanceError, PacketError, KeyError) as exc:
            raise DispatchRejected(str(exc)) from exc
        if revision_decision.get("task_id") != self.task_id:
            raise DispatchRejected("RevisionDecision is bound to a different task")
        if revision_decision.get("run_id") != self.run_id:
            raise DispatchRejected("RevisionDecision is bound to a different run")
        target_role = revision_decision.get("target_role_id")
        if target_role not in self.router.subagent_ids:
            raise DispatchRejected("RevisionDecision targets an unknown role")
        if self._roles[target_role].get("kind") != "author":
            raise DispatchRejected(
                "RevisionDecision may target only the current answer author"
            )
        prior_attempt_id = str(revision_decision.get("prior_attempt_id", ""))
        if prior_attempt_id not in {
            record.attempt_id for record in self._dispatches.values()
        }:
            raise DispatchRejected("prior attempt is unknown")
        earliest_impact = str(revision_decision.get("earliest_impact", ""))
        new_attempt = _opaque_id("attempt")
        record = self._prepare_dispatch(
            actor=actor,
            role_id=target_role,
            objective=objective,
            prompt=prompt,
            promoted_inputs=promoted_inputs,
            attempt_id=new_attempt,
            parent_packet_id=revision_decision.get("packet_id"),
            review_context=None,
            required_deliverables=None,
            acceptance_criteria=None,
            allowed_tools=(),
            task_purpose="task-answer",
        )
        revision_hash = sha256_json(revision_decision)
        self._sealed_attempts.add(prior_attempt_id)
        if earliest_impact in EARLIEST_IMPACT_EVENT:
            event = EARLIEST_IMPACT_EVENT[earliest_impact]
            spec = self.ledger.definition.transition_for(self.ledger.state, event)
            guard_hashes = {guard: revision_hash for guard in spec.guards}
            emitted_hashes = {
                name: (
                    sha256_json(record.task_packet)
                    if "task_packet" in name or "task_packets" in name
                    else revision_hash
                )
                for name in spec.emits
            }
            self.ledger.transition(
                actor=actor,
                event=event,
                attempt_id=new_attempt,
                evidence=TransitionEvidence(
                    guard_results={guard: True for guard in spec.guards},
                    guard_evidence_hashes=guard_hashes,
                    emitted_artifact_hashes=emitted_hashes,
                    input_manifest_hashes=tuple(
                        item.manifest_sha256 for item in promoted_inputs
                    ),
                ),
            )
        elif earliest_impact == "validation":
            self.ledger.rollback_validation(
                actor=actor,
                prior_attempt_id=prior_attempt_id,
                new_attempt_id=new_attempt,
                authorization_hash=revision_hash,
            )
        else:
            raise DispatchRejected(
                "earliest_impact must be definition, understanding, creation, or validation"
            )
        self._activate_dispatch(record)
        return RevisionRestart(
            earliest_impact=earliest_impact,
            target_state=self.ledger.state,
            dispatch=record,
            prior_attempt_id=prior_attempt_id,
        )

    def fail_attempt(
        self,
        *,
        attempt_id: str,
        trigger: str,
        failure_event_sha256: str,
    ) -> None:
        source_state = self.ledger.state
        count = self._failed_stage_attempts.get(source_state, 0) + 1
        self._failed_stage_attempts[source_state] = count
        self._sealed_attempts.add(attempt_id)
        self.ledger.fail_recoverable(
            actor="orchestrator",
            trigger=trigger,
            attempt_id=attempt_id,
            failure_event_hash=failure_event_sha256,
        )

    def retry_dispatch(
        self,
        *,
        actor: str,
        failed_role_id: str,
        objective: str,
        prompt: PromptRef,
        promoted_inputs: Sequence[PromotedInput],
        recovery_record_sha256: str,
    ) -> DispatchRecord:
        if actor != MAIN_AGENT_ID:
            raise DispatchRejected("only main_agent may authorize retry")
        if self.ledger.state != "FAILED_RECOVERABLE":
            raise DispatchRejected("retry requires FAILED_RECOVERABLE state")
        failed_event = self.ledger.events[-1]
        failed_stage = failed_event.source_state
        used = self._failed_stage_attempts.get(failed_stage, 0)
        if used >= self.ledger.definition.retry_limit:
            raise RetryBudgetExhausted(
                f"retry budget exhausted for stage {failed_stage}"
            )
        if failed_event.attempt_id not in self._sealed_attempts:
            raise DispatchRejected("failed workspace must be sealed before retry")
        new_attempt = _opaque_id("attempt")
        record = self._prepare_dispatch(
            actor=actor,
            role_id=failed_role_id,
            objective=objective,
            prompt=prompt,
            promoted_inputs=promoted_inputs,
            attempt_id=new_attempt,
            parent_packet_id=None,
            review_context=None,
            required_deliverables=None,
            acceptance_criteria=None,
            allowed_tools=(),
        )
        spec = self.ledger.definition.transition_for(
            "FAILED_RECOVERABLE", "recovery.retry_authorized"
        )
        evidence_hashes = {guard: recovery_record_sha256 for guard in spec.guards}
        self.ledger.transition(
            actor=actor,
            event="recovery.retry_authorized",
            attempt_id=new_attempt,
            evidence=TransitionEvidence(
                guard_results={guard: True for guard in spec.guards},
                guard_evidence_hashes=evidence_hashes,
                emitted_artifact_hashes={
                    "recovery_record": recovery_record_sha256,
                    "task_packet": sha256_json(record.task_packet),
                },
                input_manifest_hashes=tuple(
                    item.manifest_sha256 for item in promoted_inputs
                ),
            ),
        )
        self._activate_dispatch(record)
        return record

    def cancel(
        self, *, actor: str, attempt_id: str, cancellation_record_sha256: str
    ) -> None:
        self.ledger.cancel(
            actor=actor,
            attempt_id=attempt_id,
            cancellation_hash=cancellation_record_sha256,
        )
        self._cancelled = True
        self._sealed_attempts.update(
            record.attempt_id for record in self._dispatches.values()
        )

    def release_agent_body_candidate(
        self,
        *,
        actor: str,
        merge: AgentBodyMergeV1,
        receipt: OpaqueEvaluationReceiptV1,
    ) -> ReleaseGateDisposition:
        """Consume one opaque receipt in the disjoint release-gate ledger."""

        if actor != "release_gate":
            raise ReleaseRejected(
                "only the release_gate control-plane role may consume opaque receipts"
            )
        try:
            disposition = self._agent_body_release_ledger.record(merge, receipt)
            self._opaque_evaluation_receipt_ledger.record(receipt)
            return disposition
        except AgentBodyBoundaryError as exc:
            raise ReleaseRejected(str(exc)) from exc

    def record_agent_body_construction(
        self,
        *,
        actor: str,
        provenance: AgentBodyProvenanceV1,
    ) -> ConstructionLineageEntry:
        """Store closed construction lineage outside all evaluation ledgers."""

        if actor != "construction_gate":
            raise ReleaseRejected(
                "only the construction_gate role may store Agent-body lineage"
            )
        try:
            return self._agent_body_construction_ledger.record(provenance)
        except AgentBodyBoundaryError as exc:
            raise ReleaseRejected(str(exc)) from exc

    def _verify_all_dispatch_attestations(self) -> None:
        if set(self._execution_records) != set(self._dispatches):
            raise ReleaseRejected(
                "every historical dispatch requires an execution attestation"
            )
        for key, dispatch in self._dispatches.items():
            try:
                require_production_backend(dispatch.sandbox_plan)
            except Exception as exc:
                raise ReleaseRejected(
                    "historical dispatch used a non-production sandbox"
                ) from exc
            execution = self._execution_records[key]
            try:
                self._validate_production_execution(dispatch, execution)
            except DispatchRejected as exc:
                raise ReleaseRejected(str(exc)) from exc

    def export_ledger(self) -> tuple[dict[str, Any], ...]:
        return self.ledger.export()

    def resume_from_ledger(self, events: Sequence[Mapping[str, Any]]) -> None:
        """Replace in-memory lifecycle position only after full hash-chain replay."""

        self.ledger = RunLedger.replay(
            self.ledger.definition,
            run_id=self.run_id,
            initial_attempt_id=self.ledger.initial_attempt_id,
            events=events,
        )
