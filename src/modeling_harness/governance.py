"""Closed governance boundaries for source provenance and Agent-body dispatch.

The module deliberately keeps three concerns separate:

* trusted-ingress source provenance for all artifacts;
* a main-agent-only vault for raw current-answer reviews; and
* positive, typed Agent-body control authorization for core builders.

Raw reviews, evaluation results, and legacy translation/admission mappings are
not Agent-body construction inputs.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from threading import Lock
from typing import Any, Iterable, Mapping


MAIN_AGENT_ID = "main_agent"
TRUSTED_PROVENANCE_INGRESS_ID = "trusted_provenance_ingress"

REVIEWER_ROLES = frozenset(
    {
        "mathematical_reviewer",
        "reproducibility_reviewer",
        "evidence_communication_reviewer",
    }
)
CORE_BUILDER_ROLES = frozenset(
    {
        "system_architect",
        "sandbox_platform_engineer",
        "standards_delivery_manager",
    }
)
TASK_PURPOSES = frozenset(
    {
        "task-answer",
        "evaluation",
        "generic-governance",
        "role-identity",
        "agent-core-modification",
    }
)
INPUT_CONTENT_KINDS = frozenset(
    {
        "generic-core",
        "agent-body-control-authorization",
        "agent-body-candidate-source",
        "task-text",
        "task-attachment",
        "task-artifact",
        "evaluation-rubric",
        "raw-review",
        "score",
        "critique",
        "reference-solution",
        "test-failure",
        "benchmark-secret",
        "content-independent-verification",
    }
)

_IDENTITY_SAFE_CONTENT = frozenset({"generic-core"})
_CORE_BUILDER_CONTENT = frozenset(
    {
        "generic-core",
        "agent-body-control-authorization",
        "agent-body-candidate-source",
    }
)
_TASK_AUTHOR_CONTENT = frozenset(
    {"generic-core", "task-text", "task-attachment", "task-artifact"}
)
_REVIEWER_CONTENT = frozenset(
    {
        "generic-core",
        "task-text",
        "task-attachment",
        "task-artifact",
        "evaluation-rubric",
        "test-failure",
    }
)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_EXPOSURE_SOURCE_CLASSES = frozenset(
    {
        "public-real-task",
        "private-real-task",
        "synthetic-generic-test",
        "independent-general-research",
        "direct-user-governance-requirement",
    }
)
_AGENT_BODY_SOURCE_CLASSES = frozenset(
    {
        "independent-general-research",
        "direct-user-governance-requirement",
    }
)
_REAL_TASK_SOURCE_CLASSES = frozenset(
    {"public-real-task", "private-real-task"}
)

_SENSITIVE_KEY_TOKENS = frozenset(
    {
        "taskid",
        "tasktitle",
        "problemid",
        "problemtitle",
        "score",
        "critique",
        "reviewerfeedback",
        "rawreview",
        "reviewpacket",
        "referencesolution",
        "answerkey",
        "expectedanswer",
        "testfailure",
        "failingtest",
        "benchmarksecret",
        "hiddenbenchmark",
    }
)
_SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"\b(?:task|problem)[_ -]?(?:id|title|number|no\.?)\b", re.I),
    re.compile(r"\b(?:raw review|reviewpacket|reviewer (?:critique|feedback))\b", re.I),
    re.compile(r"\b(?:score|reference solution|answer key|expected answer)\b", re.I),
    re.compile(r"\b(?:test[- ]failure|failing test|hidden benchmark|benchmark secret)\b", re.I),
)


class GovernanceError(ValueError):
    """Base class for governance boundary violations."""


class ReviewAccessError(PermissionError):
    """Raised when restricted evidence is accessed outside its control plane."""


class UnsafeTranslationError(GovernanceError):
    """Raised when an untyped or evaluation-fed builder input is attempted."""


class DataBoundaryError(GovernanceError):
    """Raised when a dispatch crosses a role data boundary."""


class ProvenanceError(GovernanceError):
    """Raised when immutable source provenance is absent or inconsistent."""


def _canonical_sha256(document: Mapping[str, Any]) -> str:
    payload = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class SourceProvenanceRecord:
    """Immutable trusted-ingress classification for one source subject."""

    manifest_sha256: str
    manifest_id: str
    subject_id: str
    source_class: str
    source_content_sha256: str
    parent_manifest_sha256s: tuple[str, ...]
    real_task_derived: bool
    sequence: int
    previous_entry_sha256: str | None
    ledger_entry_sha256: str


_CONTROL_PLANE_BOOTSTRAP_AUTHORITY = object()


class SourceProvenanceLedger:
    """Append-only, hash-chained source classification ledger."""

    def __init__(self, *, _bootstrap_authority: object) -> None:
        if _bootstrap_authority is not _CONTROL_PLANE_BOOTSTRAP_AUTHORITY:
            raise ProvenanceError(
                "SourceProvenanceLedger must be created by control-plane bootstrap"
            )
        self._registrar_token = object()
        self._by_hash: dict[str, SourceProvenanceRecord] = {}
        self._by_subject: dict[str, SourceProvenanceRecord] = {}
        self._by_manifest_id: dict[str, SourceProvenanceRecord] = {}
        self._by_content_sha256: dict[str, SourceProvenanceRecord] = {}
        self._entries: list[SourceProvenanceRecord] = []
        self._lock = Lock()

    def _register(self, token: object, manifest: Mapping[str, Any]) -> str:
        if token is not self._registrar_token:
            raise ReviewAccessError(
                "source provenance requires its control-plane registrar"
            )
        required = {
            "schema_version",
            "manifest_id",
            "subject_id",
            "source_class",
            "source_content_sha256",
            "parent_manifest_sha256s",
            "real_task_derived",
            "issued_by",
            "issued_at",
        }
        if type(manifest) is not dict or set(manifest) != required:
            raise ProvenanceError(
                "source provenance manifest fields do not match the closed contract"
            )
        if manifest["schema_version"] != "1.0.0":
            raise ProvenanceError("unsupported source provenance manifest version")
        if manifest["issued_by"] != TRUSTED_PROVENANCE_INGRESS_ID:
            raise ProvenanceError("source provenance manifest has an untrusted issuer")

        manifest_id = manifest["manifest_id"]
        subject_id = manifest["subject_id"]
        source_class = manifest["source_class"]
        source_digest = manifest["source_content_sha256"]
        parents = manifest["parent_manifest_sha256s"]
        if not isinstance(manifest_id, str) or not manifest_id:
            raise ProvenanceError("source provenance manifest_id is required")
        if not isinstance(subject_id, str) or not subject_id:
            raise ProvenanceError("source provenance subject_id is required")
        if source_class not in _EXPOSURE_SOURCE_CLASSES:
            raise ProvenanceError("unknown trusted-ingress source class")
        if not isinstance(source_digest, str) or not _SHA256.fullmatch(source_digest):
            raise ProvenanceError("source content must be bound by lowercase SHA-256")
        if (
            type(parents) is not list
            or len(parents) != len(set(parents))
            or any(not isinstance(item, str) or not _SHA256.fullmatch(item) for item in parents)
        ):
            raise ProvenanceError(
                "parent provenance manifests must be unique lowercase SHA-256 values"
            )
        if type(manifest["real_task_derived"]) is not bool:
            raise ProvenanceError("real_task_derived must be a boolean")

        snapshot = deepcopy(manifest)
        digest = _canonical_sha256(snapshot)
        with self._lock:
            if digest in self._by_hash:
                return digest
            try:
                parent_records = tuple(self._by_hash[item] for item in parents)
            except KeyError as exc:
                raise ProvenanceError(
                    "source provenance parent is not present in the trusted ledger"
                ) from exc
            expected_taint = source_class in _REAL_TASK_SOURCE_CLASSES or any(
                record.real_task_derived for record in parent_records
            )
            if manifest["real_task_derived"] is not expected_taint:
                raise ProvenanceError(
                    "real-task-derived taint must propagate monotonically"
                )
            for previous, label in (
                (self._by_subject.get(subject_id), "subject_id"),
                (self._by_manifest_id.get(manifest_id), "manifest_id"),
                (self._by_content_sha256.get(source_digest), "source_content_sha256"),
            ):
                if previous is not None:
                    raise ProvenanceError(f"source provenance {label} is already bound")

            sequence = len(self._entries) + 1
            previous_entry_sha256 = (
                self._entries[-1].ledger_entry_sha256 if self._entries else None
            )
            entry_digest = _canonical_sha256(
                {
                    "sequence": sequence,
                    "manifest_sha256": digest,
                    "previous_entry_sha256": previous_entry_sha256,
                }
            )
            record = SourceProvenanceRecord(
                manifest_sha256=digest,
                manifest_id=manifest_id,
                subject_id=subject_id,
                source_class=source_class,
                source_content_sha256=source_digest,
                parent_manifest_sha256s=tuple(parents),
                real_task_derived=expected_taint,
                sequence=sequence,
                previous_entry_sha256=previous_entry_sha256,
                ledger_entry_sha256=entry_digest,
            )
            self._entries.append(record)
            self._by_hash[digest] = record
            self._by_subject[subject_id] = record
            self._by_manifest_id[manifest_id] = record
            self._by_content_sha256[source_digest] = record
            return digest

    def get(self, manifest_sha256: str) -> SourceProvenanceRecord:
        try:
            return self._by_hash[manifest_sha256]
        except KeyError as exc:
            raise ProvenanceError("source provenance manifest is not trusted") from exc

    def get_by_content_sha256(self, content_sha256: str) -> SourceProvenanceRecord:
        try:
            return self._by_content_sha256[content_sha256]
        except KeyError as exc:
            raise ProvenanceError("source content has no trusted provenance") from exc

    def entries(self) -> tuple[SourceProvenanceRecord, ...]:
        return tuple(self._entries)

    def verify(self) -> int:
        previous: str | None = None
        for sequence, record in enumerate(self._entries, start=1):
            if record.sequence != sequence or record.previous_entry_sha256 != previous:
                raise ProvenanceError("source provenance ledger chain is inconsistent")
            expected = _canonical_sha256(
                {
                    "sequence": sequence,
                    "manifest_sha256": record.manifest_sha256,
                    "previous_entry_sha256": previous,
                }
            )
            if record.ledger_entry_sha256 != expected:
                raise ProvenanceError("source provenance ledger entry hash mismatch")
            previous = record.ledger_entry_sha256
        return len(self._entries)


class SourceProvenanceRegistrar:
    """The sole positive writer capability for a source provenance ledger."""

    def __init__(self, ledger: SourceProvenanceLedger, token: object) -> None:
        self._ledger = ledger
        self._token = token

    def register(self, manifest: Mapping[str, Any]) -> str:
        return self._ledger._register(self._token, manifest)


@dataclass(frozen=True)
class SourceProvenanceControlPlane:
    ledger: SourceProvenanceLedger
    registrar: SourceProvenanceRegistrar


def bootstrap_source_provenance_control_plane() -> SourceProvenanceControlPlane:
    ledger = SourceProvenanceLedger(
        _bootstrap_authority=_CONTROL_PLANE_BOOTSTRAP_AUTHORITY
    )
    registrar = SourceProvenanceRegistrar(ledger, ledger._registrar_token)
    return SourceProvenanceControlPlane(ledger=ledger, registrar=registrar)


def validate_artifact_provenance(
    manifest: Mapping[str, Any],
    *,
    provenance_ledger: SourceProvenanceLedger,
    input_manifests: Iterable[Mapping[str, Any]] = (),
    require_authorized_agent_body_source: bool = False,
) -> None:
    """Validate source closure and monotonic taint for an artifact manifest."""

    hashes = manifest.get("source_provenance_sha256s")
    if (
        type(hashes) is not list
        or not hashes
        or len(hashes) != len(set(hashes))
        or any(not isinstance(item, str) or not _SHA256.fullmatch(item) for item in hashes)
    ):
        raise ProvenanceError(
            "artifact source provenance must be a non-empty unique SHA-256 list"
        )
    records = tuple(provenance_ledger.get(item) for item in hashes)
    if require_authorized_agent_body_source:
        if any(record.real_task_derived for record in records):
            raise ProvenanceError("Agent-body source may not be real-task-derived")
        unauthorized = sorted(
            {record.source_class for record in records} - _AGENT_BODY_SOURCE_CLASSES
        )
        if unauthorized:
            raise ProvenanceError(
                "Agent-body source class is not authorized: " + ", ".join(unauthorized)
            )

    parents = tuple(input_manifests)
    inherited_hashes: set[str] = set()
    inherited_taint = False
    for parent in parents:
        parent_hashes = parent.get("source_provenance_sha256s")
        if type(parent_hashes) is not list:
            raise ProvenanceError("input manifest omits source provenance")
        inherited_hashes.update(parent_hashes)
        inherited_taint = inherited_taint or parent.get("real_task_derived") is True
    if not inherited_hashes.issubset(set(hashes)):
        raise ProvenanceError("artifact cannot omit inherited source provenance")
    expected_taint = inherited_taint or any(record.real_task_derived for record in records)
    if manifest.get("real_task_derived") is not expected_taint:
        raise ProvenanceError("artifact real-task-derived taint is inconsistent")


class ReviewEvidenceVault:
    """Main-agent-only immutable storage for raw evaluation ReviewPackets."""

    def __init__(self, provenance_ledger: SourceProvenanceLedger) -> None:
        self.provenance_ledger = provenance_ledger
        self._packets: dict[str, dict[str, Any]] = {}
        self._hashes: dict[str, str] = {}
        self._lock = Lock()

    def store(self, actor: str, packet: Mapping[str, Any]) -> str:
        if actor not in REVIEWER_ROLES:
            raise ReviewAccessError("only registered reviewers may store raw reviews")
        if type(packet) is not dict:
            raise GovernanceError("ReviewPacket must be a closed object")
        packet_id = packet.get("packet_id")
        if not isinstance(packet_id, str) or not packet_id:
            raise GovernanceError("ReviewPacket packet_id is required")
        if packet.get("reviewer_role_id") != actor:
            raise ReviewAccessError("reviewer identity does not match ReviewPacket")
        if packet.get("visibility") != "main-agent-only":
            raise ReviewAccessError("raw ReviewPacket visibility must be main-agent-only")
        snapshot = deepcopy(packet)
        digest = _canonical_sha256(snapshot)
        with self._lock:
            previous = self._packets.get(packet_id)
            if previous is not None and previous != snapshot:
                raise GovernanceError("ReviewPacket immutable identity was rebound")
            self._packets[packet_id] = snapshot
            self._hashes[packet_id] = digest
        return digest

    def read(self, actor: str, packet_id: str) -> dict[str, Any]:
        if actor != MAIN_AGENT_ID:
            raise ReviewAccessError("raw ReviewPacket is main-agent-only")
        try:
            return deepcopy(self._packets[packet_id])
        except KeyError as exc:
            raise ReviewAccessError("ReviewPacket is not present in the vault") from exc

    def digest(self, actor: str, packet_id: str) -> str:
        if actor != MAIN_AGENT_ID:
            raise ReviewAccessError("ReviewPacket digest is main-agent-only")
        try:
            return self._hashes[packet_id]
        except KeyError as exc:
            raise ReviewAccessError("ReviewPacket is not present in the vault") from exc


def validate_role_data_boundary(
    *,
    role_id: str,
    role_kind: str,
    task_purpose: str,
    content_kinds: Iterable[str],
) -> None:
    """Fail closed on task/evaluation material crossing into Agent-body work."""

    if task_purpose not in TASK_PURPOSES:
        raise DataBoundaryError("unknown task purpose")
    kinds = tuple(content_kinds)
    unknown = sorted(set(kinds) - INPUT_CONTENT_KINDS)
    if unknown:
        raise DataBoundaryError("unknown input content kinds: " + ", ".join(unknown))

    if task_purpose == "role-identity":
        if set(kinds) - _IDENTITY_SAFE_CONTENT:
            raise DataBoundaryError(
                "role-identity builders may read only generic-core inputs"
            )
        return

    if role_id in CORE_BUILDER_ROLES:
        if task_purpose not in {"generic-governance", "agent-core-modification"}:
            raise DataBoundaryError(
                "core builders cannot execute task-answer or evaluation work"
            )
        forbidden = sorted(set(kinds) - _CORE_BUILDER_CONTENT)
        if forbidden:
            raise DataBoundaryError(
                "core builder input contains forbidden material: "
                + ", ".join(forbidden)
            )
        if task_purpose == "agent-core-modification" and (
            "agent-body-control-authorization" not in kinds
        ):
            raise DataBoundaryError(
                "Agent-body modification requires a closed control authorization"
            )
        return

    if task_purpose == "agent-core-modification":
        raise DataBoundaryError(
            "Agent-body modification must be dispatched to a core builder"
        )
    if role_id == "benchmark_curator":
        if task_purpose != "evaluation":
            raise DataBoundaryError("benchmark_curator is evaluation-only")
        return
    if role_id in REVIEWER_ROLES or role_kind == "reviewer":
        if task_purpose != "evaluation":
            raise DataBoundaryError("reviewer roles are evaluation-only")
        forbidden = sorted(set(kinds) - _REVIEWER_CONTENT)
        if forbidden:
            raise DataBoundaryError(
                "reviewer input exceeds the evaluation boundary: "
                + ", ".join(forbidden)
            )
        return
    if role_kind == "author":
        if task_purpose != "task-answer":
            raise DataBoundaryError("task authors may only build current answer artifacts")
        forbidden = sorted(set(kinds) - _TASK_AUTHOR_CONTENT)
        if forbidden:
            raise DataBoundaryError(
                "task author input contains evaluation feedback: "
                + ", ".join(forbidden)
            )
        return
    if task_purpose != "generic-governance":
        raise DataBoundaryError("unsupported role data boundary")


def _sensitive_findings(value: Any, path: str = "$") -> tuple[str, ...]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "", str(key).lower())
            child_path = f"{path}.{key}"
            if normalized in _SENSITIVE_KEY_TOKENS:
                findings.append(child_path)
            findings.extend(_sensitive_findings(child, child_path))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            findings.extend(_sensitive_findings(child, f"{path}[{index}]"))
    elif isinstance(value, str):
        if any(pattern.search(value) for pattern in _SENSITIVE_TEXT_PATTERNS):
            findings.append(path)
    return tuple(dict.fromkeys(findings))


def task_packet_sensitive_findings(packet: Mapping[str, Any]) -> tuple[str, ...]:
    """Return content-independent forbidden-field findings for core work."""

    return _sensitive_findings(packet)


def validate_task_packet_narrative(
    packet: Mapping[str, Any], *, role_id: str, task_purpose: str
) -> None:
    """Reject sensitive narrative only for role-identity and Agent-body work."""

    if role_id not in CORE_BUILDER_ROLES and task_purpose != "role-identity":
        return
    narrative = {
        key: packet[key]
        for key in ("objective", "constraints", "deliverables", "acceptance_criteria")
        if key in packet
    }
    findings = _sensitive_findings(narrative)
    if findings:
        raise DataBoundaryError(
            "Agent-body task narrative contains task/evaluation-sensitive material at: "
            + ", ".join(findings)
        )


@dataclass(frozen=True)
class ContainmentDecision:
    quarantine: bool
    reason: str
    authorizes_capability_modification: bool = False


def assess_immediate_safety_containment(
    *, severity: str, defect_kind: str
) -> ContainmentDecision:
    """Allow quarantine without converting evaluation evidence into construction."""

    hard_safety_kinds = {
        "cross-workspace-access",
        "hidden-benchmark-disclosure",
        "hash-mismatch",
        "provenance-falsification",
        "task-specific-optimization-rule",
        "unauthorized-write",
    }
    quarantine = severity == "P0" and defect_kind in hard_safety_kinds
    return ContainmentDecision(
        quarantine=quarantine,
        reason=(
            "immediate quarantine and forensic containment"
            if quarantine
            else "not a recognized hard-safety containment event"
        ),
        authorizes_capability_modification=False,
    )


class FeedbackFirewall:
    """Raw-review vault plus typed, positive Agent-body dispatch boundary."""

    def __init__(
        self,
        vault: ReviewEvidenceVault,
    ) -> None:
        self._vault = vault

    def retain_review(self, actor: str, packet: Mapping[str, Any]) -> str:
        return self._vault.store(actor, packet)

    def read_review(self, actor: str, packet_id: str) -> dict[str, Any]:
        return self._vault.read(actor, packet_id)

    def dispatch_control_authorization(
        self,
        *,
        actor: str,
        recipient_role_id: str,
        authorization: object,
    ) -> object:
        from modeling_harness.agent_body import AgentBodyControlAuthorizationV1

        if actor != MAIN_AGENT_ID:
            raise ReviewAccessError(
                "only main_agent may dispatch a closed control authorization"
            )
        if recipient_role_id not in CORE_BUILDER_ROLES:
            raise DataBoundaryError(
                "Agent-body control authorization may target only a core builder"
            )
        if type(authorization) is not AgentBodyControlAuthorizationV1:
            raise UnsafeTranslationError(
                "builder dispatch requires AgentBodyControlAuthorizationV1"
            )
        return authorization
