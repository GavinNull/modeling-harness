"""Sound-by-construction control and release boundaries for Agent-body changes.

This module deliberately exposes only closed, positive carrier types.  Agent-body
construction and release evaluation are separate type graphs: construction APIs
cannot accept evaluation objects, and the release ledger cannot authorize or
describe a successor Agent-body change.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields as dataclass_fields
from enum import StrEnum
import hashlib
import json
from threading import Lock
from types import MappingProxyType
from typing import Any, ClassVar, Mapping


AGENT_ID = "generic-agent-v2.5"
PACKAGE_VERSION = "2.5.0"
LOWER_HEX_DIGITS = frozenset("0123456789abcdef")


class BoundaryErrorCode(StrEnum):
    """Closed, machine-readable failures for the Agent-body trust boundary."""

    UNKNOWN_KEY = "UNKNOWN_KEY"
    MISSING_KEY = "MISSING_KEY"
    WRONG_TYPE = "WRONG_TYPE"
    UNKNOWN_VALUE = "UNKNOWN_VALUE"
    NONCANONICAL_VALUE = "NONCANONICAL_VALUE"
    HASH_MISMATCH = "HASH_MISMATCH"
    LINEAGE_MISMATCH = "LINEAGE_MISMATCH"
    FAMILY_MISMATCH = "FAMILY_MISMATCH"
    TAINT_VIOLATION = "TAINT_VIOLATION"
    STATE_VIOLATION = "STATE_VIOLATION"
    PERMISSION_VIOLATION = "PERMISSION_VIOLATION"
    CARDINALITY_VIOLATION = "CARDINALITY_VIOLATION"
    EQUALITY_VIOLATION = "EQUALITY_VIOLATION"


class AgentBodyBoundaryError(ValueError):
    """Typed failure at the Agent-body trust boundary.

    ``code`` is normative.  ``message`` is diagnostic only and must never be
    used for branching, hashing, construction, or release decisions.
    """

    def __init__(
        self,
        code: BoundaryErrorCode,
        message: str | None = None,
    ) -> None:
        if type(code) is not BoundaryErrorCode:
            raise TypeError("AgentBodyBoundaryError requires BoundaryErrorCode")
        self.code = code
        super().__init__(message if message is not None else code.value)


class DirectUserInstruction(StrEnum):
    """The sole executable direct-user governance instruction."""

    APPLY_REVIEWED_GENERIC_GOVERNANCE_CHANGE = (
        "APPLY_REVIEWED_GENERIC_GOVERNANCE_CHANGE"
    )


class CapabilityTaxonomy(StrEnum):
    DEFINITION = "DEFINITION"
    UNDERSTANDING = "UNDERSTANDING"
    CREATION = "CREATION"
    VALIDATION = "VALIDATION"
    RUNTIME_ORCHESTRATION = "RUNTIME_ORCHESTRATION"


class ChangeClass(StrEnum):
    CONTROL_SCHEMA = "CONTROL_SCHEMA"
    ROLE_PERMISSION = "ROLE_PERMISSION"
    STATE_TRANSITION = "STATE_TRANSITION"
    RUNTIME_ENFORCEMENT = "RUNTIME_ENFORCEMENT"
    PROMPT_PROJECTION = "PROMPT_PROJECTION"
    GENERIC_CAPABILITY = "GENERIC_CAPABILITY"


class ImpactClass(StrEnum):
    CONTROL_PLANE = "CONTROL_PLANE"
    AGENT_BODY = "AGENT_BODY"


class RollbackClass(StrEnum):
    RESTORE_PARENT_AGENT_BODY = "RESTORE_PARENT_AGENT_BODY"


class VerificationPlanClass(StrEnum):
    PREREGISTERED_CONTENT_INDEPENDENT = "PREREGISTERED_CONTENT_INDEPENDENT"


class GovernanceStage(StrEnum):
    PROPOSAL = "PROPOSAL"
    MERGE = "MERGE"


class GovernancePurpose(StrEnum):
    AGENT_BODY_CHANGE = "AGENT_BODY_CHANGE"


class BuilderRole(StrEnum):
    SYSTEM_ARCHITECT = "system_architect"
    SANDBOX_PLATFORM_ENGINEER = "sandbox_platform_engineer"
    STANDARDS_DELIVERY_MANAGER = "standards_delivery_manager"


class AgentBodyPermission(StrEnum):
    """Closed construction permissions carried by every authorization."""

    CONSTRUCT_CANDIDATE = "CONSTRUCT_CANDIDATE"
    PROPOSE_CANDIDATE = "PROPOSE_CANDIDATE"
    ADMIT_CANDIDATE = "ADMIT_CANDIDATE"
    PROMOTE_CANDIDATE = "PROMOTE_CANDIDATE"


class ControlSource(StrEnum):
    DIRECT_USER = "DIRECT_USER"
    INDEPENDENT_GENERAL_RESEARCH = "INDEPENDENT_GENERAL_RESEARCH"


class AdmissionDecision(StrEnum):
    ADMIT = "ADMIT"
    REJECT = "REJECT"


class MergeState(StrEnum):
    MERGED = "MERGED"


class EvaluationDecision(StrEnum):
    PASS = "PASS"
    REJECT = "REJECT"


class ReleaseStatus(StrEnum):
    RELEASED = "RELEASED"
    RELEASE_REJECTED = "RELEASE_REJECTED"


class ConstructionLedgerState(StrEnum):
    RECORDED = "CONSTRUCTION_RECORDED"


class OpaqueReceiptLedgerState(StrEnum):
    RECEIVED = "OPAQUE_RECEIPT_RECEIVED"


class ConstructionState(StrEnum):
    CONTROL_AUTHORIZED = "CONTROL_AUTHORIZED"
    CANDIDATE_SOURCED = "CANDIDATE_SOURCED"
    PROPOSED = "PROPOSED"
    ADMITTED = "ADMITTED"
    REJECTED = "REJECTED"
    MERGED = "MERGED"


class MainAgentAction(StrEnum):
    DISPATCH = "dispatch"
    WRITE_PROMPTS = "write_prompts"
    REVIEW = "review"
    APPROVE = "approve"
    REJECT = "reject"


class BoundaryTaint(StrEnum):
    CLEAN = "CLEAN"
    TAINTED = "TAINTED"


MAIN_AGENT_ACTIONS = frozenset(MainAgentAction)
MAIN_AGENT_WRITES = frozenset(
    {
        "dispatch_records",
        "task_packets",
        "prompt_registry",
        "review_decisions",
        "approval_decisions",
        "rejection_decisions",
    }
)
MAIN_AGENT_OUTPUTS = frozenset(MAIN_AGENT_WRITES)
MAIN_AGENT_FORBIDDEN_ARTIFACTS = frozenset(
    {
        "DirectUserExecutableInstructionV1",
        "IndependentGeneralResearchManifestV1",
        "DTPV1",
        "MAPV1",
        "AgentBodyControlAuthorizationV1",
        "AgentBodyCandidateSourceV1",
        "AgentBodyProposalV1",
        "AgentBodyAdmissionV1",
        "AgentBodyMergeV1",
        "AgentBodyProvenanceV1",
        "BuildArtifact",
        "SolutionArtifact",
    }
)
AGENT_BODY_AUTHORIZATION_PERMISSIONS = tuple(AgentBodyPermission)


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    if not isinstance(value, Mapping):
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.WRONG_TYPE,
            "canonical carrier must be a mapping",
        )
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.NONCANONICAL_VALUE,
            "carrier is not canonical JSON",
        ) from exc


def canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _require_sha256(value: Any, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in LOWER_HEX_DIGITS for character in value)
    ):
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.NONCANONICAL_VALUE,
            f"{field} must be a lowercase SHA-256",
        )
    return value


def _require_identity(document: Mapping[str, Any]) -> None:
    if document.get("agent_id") != AGENT_ID:
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.UNKNOWN_VALUE, "agent_id mismatch"
        )
    if document.get("package_version") != PACKAGE_VERSION:
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.UNKNOWN_VALUE, "package_version mismatch"
        )


def _require_typed_enum(value: Any, enum_type: type[StrEnum], field: str) -> None:
    if type(value) is not enum_type:
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.WRONG_TYPE,
            f"{field} must be a typed closed enum",
        )


def _require_exact_keys(
    document: Mapping[str, Any], expected: frozenset[str], carrier: str
) -> None:
    if not isinstance(document, Mapping):
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.WRONG_TYPE,
            f"{carrier} must be a mapping",
        )
    actual = frozenset(document)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if unknown:
            detail.append("unknown=" + ",".join(unknown))
        code = (
            BoundaryErrorCode.UNKNOWN_KEY
            if unknown
            else BoundaryErrorCode.MISSING_KEY
        )
        raise AgentBodyBoundaryError(
            code,
            f"{carrier} has a non-exact field set ({'; '.join(detail)})",
        )


def _enum(enum_type: type[StrEnum], value: Any, field: str) -> StrEnum:
    if not isinstance(value, str):
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.WRONG_TYPE,
            f"{field} must be a closed enum value",
        )
    try:
        return enum_type(value)
    except ValueError as exc:
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.UNKNOWN_VALUE, f"unknown {field}"
        ) from exc


def _require_nominal_carrier(value: Any, carrier_type: type[Any]) -> None:
    """Reject cross-family/subclass values and revalidate frozen parents."""

    if type(value) is not carrier_type:
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.FAMILY_MISMATCH,
            f"expected nominal {carrier_type.__name__}",
        )
    carrier_type.__post_init__(value)


def _strict_frozen_dataclass(cls: type[Any]) -> type[Any]:
    """Create an exact-key frozen carrier with typed constructor failures."""

    processed = dataclass(frozen=True, init=False)(cls)
    names = tuple(field.name for field in dataclass_fields(processed))

    def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
        if type(self) is not processed:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.FAMILY_MISMATCH,
                "boundary carrier subclasses are not accepted",
            )
        if len(args) > len(names):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.CARDINALITY_VIOLATION,
                "too many positional carrier fields",
            )
        supplied = dict(zip(names, args))
        duplicate = frozenset(supplied).intersection(kwargs)
        if duplicate:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.UNKNOWN_KEY,
                "carrier field supplied more than once",
            )
        unknown = frozenset(kwargs) - frozenset(names)
        if unknown:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.UNKNOWN_KEY,
                "carrier has an unknown constructor field",
            )
        supplied.update(kwargs)
        missing = frozenset(names) - frozenset(supplied)
        if missing:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.MISSING_KEY,
                "carrier is missing a constructor field",
            )
        for name in names:
            object.__setattr__(self, name, supplied[name])
        self.__post_init__()

    def _reject_mutation(self: Any, name: str, value: Any = None) -> None:
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.STATE_VIOLATION,
            "boundary carriers are frozen",
        )

    processed.__init__ = __init__
    processed.__setattr__ = _reject_mutation
    processed.__delattr__ = _reject_mutation
    return processed


@_strict_frozen_dataclass
class DirectUserExecutableInstructionV1:
    schema_version: str
    instruction: DirectUserInstruction
    governance_document_sha256: str
    instruction_sha256: str
    independent_review_sha256: str
    agent_id: str
    package_version: str

    SCHEMA_VERSION: ClassVar[str] = "agent-body.direct-user.v1"
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema_version",
            "instruction",
            "governance_document_sha256",
            "instruction_sha256",
            "independent_review_sha256",
            "agent_id",
            "package_version",
        }
    )

    def __post_init__(self) -> None:
        _require_identity(
            {"agent_id": self.agent_id, "package_version": self.package_version}
        )
        if self.schema_version != self.SCHEMA_VERSION:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.UNKNOWN_VALUE,
                "unknown DirectUser schema_version",
            )
        _require_typed_enum(self.instruction, DirectUserInstruction, "instruction")
        _require_sha256(
            self.governance_document_sha256, "governance_document_sha256"
        )
        _require_sha256(self.independent_review_sha256, "independent_review_sha256")
        _require_sha256(self.instruction_sha256, "instruction_sha256")
        bound = self.as_mapping()
        del bound["instruction_sha256"]
        if self.instruction_sha256 != canonical_sha256(bound):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.HASH_MISMATCH,
                "instruction_sha256 binding mismatch",
            )

    @classmethod
    def from_mapping(
        cls, document: Mapping[str, Any]
    ) -> "DirectUserExecutableInstructionV1":
        _require_exact_keys(document, cls.FIELDS, cls.__name__)
        _require_identity(document)
        if document["schema_version"] != cls.SCHEMA_VERSION:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.UNKNOWN_VALUE,
                "unknown DirectUser schema_version",
            )
        instruction = _enum(
            DirectUserInstruction, document["instruction"], "instruction"
        )
        governance_hash = _require_sha256(
            document["governance_document_sha256"], "governance_document_sha256"
        )
        review_hash = _require_sha256(
            document["independent_review_sha256"], "independent_review_sha256"
        )
        bound = {
            "schema_version": cls.SCHEMA_VERSION,
            "instruction": instruction.value,
            "governance_document_sha256": governance_hash,
            "independent_review_sha256": review_hash,
            "agent_id": AGENT_ID,
            "package_version": PACKAGE_VERSION,
        }
        instruction_hash = _require_sha256(
            document["instruction_sha256"], "instruction_sha256"
        )
        if instruction_hash != canonical_sha256(bound):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.HASH_MISMATCH,
                "instruction_sha256 binding mismatch",
            )
        return cls(
            schema_version=cls.SCHEMA_VERSION,
            instruction=instruction,
            governance_document_sha256=governance_hash,
            instruction_sha256=instruction_hash,
            independent_review_sha256=review_hash,
            agent_id=AGENT_ID,
            package_version=PACKAGE_VERSION,
        )

    def as_mapping(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "instruction": self.instruction.value,
            "governance_document_sha256": self.governance_document_sha256,
            "instruction_sha256": self.instruction_sha256,
            "independent_review_sha256": self.independent_review_sha256,
            "agent_id": self.agent_id,
            "package_version": self.package_version,
        }


@_strict_frozen_dataclass
class IndependentGeneralResearchManifestV1:
    schema_version: str
    agent_id: str
    package_version: str
    manifest_sha256: str
    independent_review_sha256: str
    capability: CapabilityTaxonomy
    change_class: ChangeClass
    impact: ImpactClass
    rollback: RollbackClass
    verification_plan: VerificationPlanClass
    stage: GovernanceStage
    purpose: GovernancePurpose

    SCHEMA_VERSION: ClassVar[str] = "agent-body.research-manifest.v1"
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema_version",
            "agent_id",
            "package_version",
            "manifest_sha256",
            "independent_review_sha256",
            "capability",
            "change_class",
            "impact",
            "rollback",
            "verification_plan",
            "stage",
            "purpose",
        }
    )

    def __post_init__(self) -> None:
        _require_identity(
            {"agent_id": self.agent_id, "package_version": self.package_version}
        )
        if self.schema_version != self.SCHEMA_VERSION:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.UNKNOWN_VALUE,
                "unknown research manifest schema_version",
            )
        for field, enum_type in (
            ("capability", CapabilityTaxonomy),
            ("change_class", ChangeClass),
            ("impact", ImpactClass),
            ("rollback", RollbackClass),
            ("verification_plan", VerificationPlanClass),
            ("stage", GovernanceStage),
            ("purpose", GovernancePurpose),
        ):
            _require_typed_enum(getattr(self, field), enum_type, field)
        mapping = self.as_mapping()
        _require_sha256(self.independent_review_sha256, "independent_review_sha256")
        _require_sha256(self.manifest_sha256, "manifest_sha256")
        del mapping["manifest_sha256"]
        if self.manifest_sha256 != canonical_sha256(mapping):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.HASH_MISMATCH,
                "manifest_sha256 binding mismatch",
            )

    @classmethod
    def from_mapping(
        cls, document: Mapping[str, Any]
    ) -> "IndependentGeneralResearchManifestV1":
        _require_exact_keys(document, cls.FIELDS, cls.__name__)
        _require_identity(document)
        if document["schema_version"] != cls.SCHEMA_VERSION:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.UNKNOWN_VALUE,
                "unknown research manifest schema_version",
            )
        values = {
            "capability": _enum(
                CapabilityTaxonomy, document["capability"], "capability"
            ),
            "change_class": _enum(
                ChangeClass, document["change_class"], "change_class"
            ),
            "impact": _enum(ImpactClass, document["impact"], "impact"),
            "rollback": _enum(RollbackClass, document["rollback"], "rollback"),
            "verification_plan": _enum(
                VerificationPlanClass,
                document["verification_plan"],
                "verification_plan",
            ),
            "stage": _enum(GovernanceStage, document["stage"], "stage"),
            "purpose": _enum(
                GovernancePurpose, document["purpose"], "purpose"
            ),
        }
        review_hash = _require_sha256(
            document["independent_review_sha256"], "independent_review_sha256"
        )
        bound = {
            "schema_version": cls.SCHEMA_VERSION,
            "agent_id": AGENT_ID,
            "package_version": PACKAGE_VERSION,
            "independent_review_sha256": review_hash,
            **{key: value.value for key, value in values.items()},
        }
        manifest_hash = _require_sha256(
            document["manifest_sha256"], "manifest_sha256"
        )
        if manifest_hash != canonical_sha256(bound):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.HASH_MISMATCH,
                "manifest_sha256 binding mismatch",
            )
        return cls(
            schema_version=cls.SCHEMA_VERSION,
            agent_id=AGENT_ID,
            package_version=PACKAGE_VERSION,
            manifest_sha256=manifest_hash,
            independent_review_sha256=review_hash,
            **values,
        )

    def as_mapping(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "agent_id": self.agent_id,
            "package_version": self.package_version,
            "manifest_sha256": self.manifest_sha256,
            "independent_review_sha256": self.independent_review_sha256,
            "capability": self.capability.value,
            "change_class": self.change_class.value,
            "impact": self.impact.value,
            "rollback": self.rollback.value,
            "verification_plan": self.verification_plan.value,
            "stage": self.stage.value,
            "purpose": self.purpose.value,
        }


_RESEARCH_CONTROL_FIELDS = (
    "research_manifest_sha256",
    "independent_review_sha256",
    "capability",
    "change_class",
    "impact",
    "rollback",
    "verification_plan",
    "stage",
    "purpose",
)


@_strict_frozen_dataclass
class DTPV1:
    schema_version: str
    agent_id: str
    package_version: str
    research_manifest_sha256: str
    independent_review_sha256: str
    capability: CapabilityTaxonomy
    change_class: ChangeClass
    impact: ImpactClass
    rollback: RollbackClass
    verification_plan: VerificationPlanClass
    stage: GovernanceStage
    purpose: GovernancePurpose
    dtp_sha256: str

    SCHEMA_VERSION: ClassVar[str] = "agent-body.dtp.v1"

    def __post_init__(self) -> None:
        mapping = self.as_mapping()
        _require_identity(mapping)
        if self.schema_version != self.SCHEMA_VERSION:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.UNKNOWN_VALUE, "unknown DTP schema_version"
            )
        _validate_research_control_enums(self)
        for field in (
            "research_manifest_sha256",
            "independent_review_sha256",
            "dtp_sha256",
        ):
            _require_sha256(getattr(self, field), field)
        del mapping["dtp_sha256"]
        if self.dtp_sha256 != canonical_sha256(mapping):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.HASH_MISMATCH,
                "dtp_sha256 binding mismatch",
            )
        manifest_body = dict(mapping)
        manifest_body["schema_version"] = IndependentGeneralResearchManifestV1.SCHEMA_VERSION
        manifest_body["manifest_sha256"] = manifest_body.pop(
            "research_manifest_sha256"
        )
        claimed_manifest_hash = manifest_body.pop("manifest_sha256")
        if claimed_manifest_hash != canonical_sha256(manifest_body):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.LINEAGE_MISMATCH,
                "DTP research manifest binding mismatch",
            )

    @classmethod
    def from_manifest(cls, manifest: IndependentGeneralResearchManifestV1) -> "DTPV1":
        _require_nominal_carrier(
            manifest, IndependentGeneralResearchManifestV1
        )
        body = _research_control_body(cls.SCHEMA_VERSION, manifest)
        return cls(
            **body,
            capability=manifest.capability,
            change_class=manifest.change_class,
            impact=manifest.impact,
            rollback=manifest.rollback,
            verification_plan=manifest.verification_plan,
            stage=manifest.stage,
            purpose=manifest.purpose,
            dtp_sha256=canonical_sha256(_plain_enums(body, manifest)),
        )

    @classmethod
    def from_mapping(
        cls,
        document: Mapping[str, Any],
        *,
        manifest: IndependentGeneralResearchManifestV1,
    ) -> "DTPV1":
        expected = frozenset(
            {"schema_version", "agent_id", "package_version", "dtp_sha256", *_RESEARCH_CONTROL_FIELDS}
        )
        _require_exact_keys(document, expected, cls.__name__)
        candidate = cls.from_manifest(manifest)
        if document != candidate.as_mapping():
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.LINEAGE_MISMATCH,
                "DTP does not exactly bind the research manifest",
            )
        return candidate

    def as_mapping(self) -> dict[str, str]:
        return _control_mapping(asdict(self))


@_strict_frozen_dataclass
class MAPV1:
    schema_version: str
    agent_id: str
    package_version: str
    research_manifest_sha256: str
    independent_review_sha256: str
    capability: CapabilityTaxonomy
    change_class: ChangeClass
    impact: ImpactClass
    rollback: RollbackClass
    verification_plan: VerificationPlanClass
    stage: GovernanceStage
    purpose: GovernancePurpose
    dtp_sha256: str
    map_sha256: str

    SCHEMA_VERSION: ClassVar[str] = "agent-body.map.v1"

    def __post_init__(self) -> None:
        mapping = self.as_mapping()
        _require_identity(mapping)
        if self.schema_version != self.SCHEMA_VERSION:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.UNKNOWN_VALUE, "unknown MAP schema_version"
            )
        _validate_research_control_enums(self)
        for field in (
            "research_manifest_sha256",
            "independent_review_sha256",
            "dtp_sha256",
            "map_sha256",
        ):
            _require_sha256(getattr(self, field), field)
        del mapping["map_sha256"]
        if self.map_sha256 != canonical_sha256(mapping):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.HASH_MISMATCH,
                "map_sha256 binding mismatch",
            )
        dtp_body = dict(mapping)
        dtp_body["schema_version"] = DTPV1.SCHEMA_VERSION
        claimed_dtp_hash = dtp_body.pop("dtp_sha256")
        if claimed_dtp_hash != canonical_sha256(dtp_body):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.LINEAGE_MISMATCH,
                "MAP DTP binding mismatch",
            )

    @classmethod
    def from_dtp(cls, dtp: DTPV1) -> "MAPV1":
        _require_nominal_carrier(dtp, DTPV1)
        body = {
            "schema_version": cls.SCHEMA_VERSION,
            "agent_id": AGENT_ID,
            "package_version": PACKAGE_VERSION,
            **{field: getattr(dtp, field) for field in _RESEARCH_CONTROL_FIELDS},
            "dtp_sha256": dtp.dtp_sha256,
        }
        plain = _control_mapping(body)
        return cls(**body, map_sha256=canonical_sha256(plain))

    @classmethod
    def from_mapping(
        cls, document: Mapping[str, Any], *, dtp: DTPV1
    ) -> "MAPV1":
        expected = frozenset(
            {
                "schema_version",
                "agent_id",
                "package_version",
                "dtp_sha256",
                "map_sha256",
                *_RESEARCH_CONTROL_FIELDS,
            }
        )
        _require_exact_keys(document, expected, cls.__name__)
        candidate = cls.from_dtp(dtp)
        if document != candidate.as_mapping():
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.LINEAGE_MISMATCH,
                "MAP does not exactly bind the DTP",
            )
        return candidate

    def as_mapping(self) -> dict[str, str]:
        return _control_mapping(asdict(self))


def _research_control_body(
    schema_version: str, manifest: IndependentGeneralResearchManifestV1
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "agent_id": AGENT_ID,
        "package_version": PACKAGE_VERSION,
        "research_manifest_sha256": manifest.manifest_sha256,
        "independent_review_sha256": manifest.independent_review_sha256,
    }


def _validate_research_control_enums(value: Any) -> None:
    for field, enum_type in (
        ("capability", CapabilityTaxonomy),
        ("change_class", ChangeClass),
        ("impact", ImpactClass),
        ("rollback", RollbackClass),
        ("verification_plan", VerificationPlanClass),
        ("stage", GovernanceStage),
        ("purpose", GovernancePurpose),
    ):
        _require_typed_enum(getattr(value, field), enum_type, field)


def _plain_enums(
    body: Mapping[str, Any], manifest: IndependentGeneralResearchManifestV1
) -> dict[str, str]:
    return {
        **_control_mapping(body),
        **{
            field: getattr(manifest, field).value
            for field in _RESEARCH_CONTROL_FIELDS
            if field not in body
        },
    }


def _control_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    def plain(item: Any) -> Any:
        if isinstance(item, StrEnum):
            return item.value
        if isinstance(item, tuple):
            return [plain(child) for child in item]
        if isinstance(item, list):
            return [plain(child) for child in item]
        if isinstance(item, Mapping):
            return {key: plain(child) for key, child in item.items()}
        return item

    return {key: plain(item) for key, item in value.items()}


def _require_exact_authorization_permissions(
    permissions: Any,
) -> tuple[AgentBodyPermission, ...]:
    if type(permissions) is not tuple:
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.WRONG_TYPE,
            "authorization permissions must be a tuple of closed values",
        )
    if any(type(item) is not AgentBodyPermission for item in permissions):
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.WRONG_TYPE,
            "authorization permission is not a closed value",
        )
    if permissions != AGENT_BODY_AUTHORIZATION_PERMISSIONS:
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.PERMISSION_VIOLATION,
            "authorization permissions must equal the canonical set and order",
        )
    return permissions


@_strict_frozen_dataclass
class AgentBodyControlAuthorizationV1:
    schema_version: str
    source: ControlSource
    source_sha256: str
    direct_user_instruction_sha256: str | None
    research_manifest_sha256: str | None
    dtp_sha256: str | None
    map_sha256: str | None
    permissions: tuple[AgentBodyPermission, ...]
    authorization_sha256: str
    agent_id: str
    package_version: str

    SCHEMA_VERSION: ClassVar[str] = "agent-body.authorization.v1"

    def __post_init__(self) -> None:
        mapping = self.as_mapping()
        _require_identity(mapping)
        if self.schema_version != self.SCHEMA_VERSION:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.UNKNOWN_VALUE,
                "unknown authorization schema_version",
            )
        _require_typed_enum(self.source, ControlSource, "source")
        _require_sha256(self.source_sha256, "source_sha256")
        _require_exact_authorization_permissions(self.permissions)
        branch = (
            self.direct_user_instruction_sha256,
            self.research_manifest_sha256,
            self.dtp_sha256,
            self.map_sha256,
        )
        if self.source is ControlSource.DIRECT_USER:
            if branch != (self.source_sha256, None, None, None):
                raise AgentBodyBoundaryError(
                    BoundaryErrorCode.LINEAGE_MISMATCH,
                    "invalid DirectUser authorization branch",
                )
        elif (
            self.direct_user_instruction_sha256 is not None
            or self.research_manifest_sha256 != self.source_sha256
            or self.dtp_sha256 is None
            or self.map_sha256 is None
        ):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.LINEAGE_MISMATCH,
                "invalid research authorization branch",
            )
        for field in (
            "direct_user_instruction_sha256",
            "research_manifest_sha256",
            "dtp_sha256",
            "map_sha256",
            "authorization_sha256",
        ):
            item = getattr(self, field)
            if item is not None:
                _require_sha256(item, field)
        del mapping["authorization_sha256"]
        if self.authorization_sha256 != canonical_sha256(mapping):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.HASH_MISMATCH,
                "authorization_sha256 binding mismatch",
            )

    @classmethod
    def from_direct_user(
        cls, instruction: DirectUserExecutableInstructionV1
    ) -> "AgentBodyControlAuthorizationV1":
        _require_nominal_carrier(
            instruction, DirectUserExecutableInstructionV1
        )
        body = cls._body(
            ControlSource.DIRECT_USER,
            instruction.instruction_sha256,
            instruction.instruction_sha256,
            None,
            None,
            None,
            AGENT_BODY_AUTHORIZATION_PERMISSIONS,
        )
        return cls(**body, authorization_sha256=canonical_sha256(_control_mapping(body)))

    @classmethod
    def from_research(
        cls,
        manifest: IndependentGeneralResearchManifestV1,
        dtp: DTPV1,
        map_packet: MAPV1,
    ) -> "AgentBodyControlAuthorizationV1":
        _require_nominal_carrier(
            manifest, IndependentGeneralResearchManifestV1
        )
        _require_nominal_carrier(dtp, DTPV1)
        _require_nominal_carrier(map_packet, MAPV1)
        if dtp.research_manifest_sha256 != manifest.manifest_sha256:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.LINEAGE_MISMATCH,
                "DTP research lineage mismatch",
            )
        if map_packet.dtp_sha256 != dtp.dtp_sha256:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.LINEAGE_MISMATCH,
                "MAP DTP lineage mismatch",
            )
        body = cls._body(
            ControlSource.INDEPENDENT_GENERAL_RESEARCH,
            manifest.manifest_sha256,
            None,
            manifest.manifest_sha256,
            dtp.dtp_sha256,
            map_packet.map_sha256,
            AGENT_BODY_AUTHORIZATION_PERMISSIONS,
        )
        return cls(**body, authorization_sha256=canonical_sha256(_control_mapping(body)))

    @classmethod
    def _body(
        cls,
        source: ControlSource,
        source_sha256: str,
        direct_user_instruction_sha256: str | None,
        research_manifest_sha256: str | None,
        dtp_sha256: str | None,
        map_sha256: str | None,
        permissions: tuple[AgentBodyPermission, ...],
    ) -> dict[str, Any]:
        for field, value in {
            "source_sha256": source_sha256,
            "direct_user_instruction_sha256": direct_user_instruction_sha256,
            "research_manifest_sha256": research_manifest_sha256,
            "dtp_sha256": dtp_sha256,
            "map_sha256": map_sha256,
        }.items():
            if value is not None:
                _require_sha256(value, field)
        _require_exact_authorization_permissions(permissions)
        return {
            "schema_version": cls.SCHEMA_VERSION,
            "source": source,
            "source_sha256": source_sha256,
            "direct_user_instruction_sha256": direct_user_instruction_sha256,
            "research_manifest_sha256": research_manifest_sha256,
            "dtp_sha256": dtp_sha256,
            "map_sha256": map_sha256,
            "permissions": permissions,
            "agent_id": AGENT_ID,
            "package_version": PACKAGE_VERSION,
        }

    def as_mapping(self) -> dict[str, Any]:
        return _control_mapping(asdict(self))


@_strict_frozen_dataclass
class AgentBodyCandidateSourceV1:
    schema_version: str
    agent_id: str
    package_version: str
    candidate_sha256: str
    parent_agent_body_sha256: str
    builder_role: BuilderRole
    control_authorization_sha256: str
    candidate_source_sha256: str

    SCHEMA_VERSION: ClassVar[str] = "agent-body.candidate-source.v1"

    def __post_init__(self) -> None:
        mapping = _control_mapping(asdict(self))
        _require_identity(mapping)
        if self.schema_version != self.SCHEMA_VERSION:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.UNKNOWN_VALUE,
                "unknown candidate-source schema_version",
            )
        _require_typed_enum(self.builder_role, BuilderRole, "builder_role")
        for field in (
            "candidate_sha256",
            "parent_agent_body_sha256",
            "control_authorization_sha256",
            "candidate_source_sha256",
        ):
            _require_sha256(getattr(self, field), field)
        del mapping["candidate_source_sha256"]
        if self.candidate_source_sha256 != canonical_sha256(mapping):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.HASH_MISMATCH,
                "candidate_source_sha256 binding mismatch",
            )

    @classmethod
    def bind(
        cls,
        authorization: AgentBodyControlAuthorizationV1,
        *,
        candidate_sha256: str,
        parent_agent_body_sha256: str,
        builder_role: BuilderRole,
    ) -> "AgentBodyCandidateSourceV1":
        _require_nominal_carrier(
            authorization, AgentBodyControlAuthorizationV1
        )
        if AgentBodyPermission.CONSTRUCT_CANDIDATE not in authorization.permissions:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.PERMISSION_VIOLATION,
                "authorization does not grant candidate construction",
            )
        _require_sha256(candidate_sha256, "candidate_sha256")
        _require_sha256(parent_agent_body_sha256, "parent_agent_body_sha256")
        if not isinstance(builder_role, BuilderRole):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.WRONG_TYPE,
                "builder_role must be a closed BuilderRole",
            )
        body = {
            "schema_version": cls.SCHEMA_VERSION,
            "agent_id": AGENT_ID,
            "package_version": PACKAGE_VERSION,
            "candidate_sha256": candidate_sha256,
            "parent_agent_body_sha256": parent_agent_body_sha256,
            "builder_role": builder_role,
            "control_authorization_sha256": authorization.authorization_sha256,
        }
        return cls(
            **body,
            candidate_source_sha256=canonical_sha256(_control_mapping(body)),
        )


@_strict_frozen_dataclass
class AgentBodyProposalV1:
    schema_version: str
    agent_id: str
    package_version: str
    candidate_source_sha256: str
    control_authorization_sha256: str
    candidate_sha256: str
    parent_agent_body_sha256: str
    proposal_sha256: str

    SCHEMA_VERSION: ClassVar[str] = "agent-body.proposal.v1"

    def __post_init__(self) -> None:
        mapping = _control_mapping(asdict(self))
        _require_identity(mapping)
        if self.schema_version != self.SCHEMA_VERSION:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.UNKNOWN_VALUE,
                "unknown proposal schema_version",
            )
        for field in (
            "candidate_source_sha256",
            "control_authorization_sha256",
            "candidate_sha256",
            "parent_agent_body_sha256",
            "proposal_sha256",
        ):
            _require_sha256(getattr(self, field), field)
        del mapping["proposal_sha256"]
        if self.proposal_sha256 != canonical_sha256(mapping):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.HASH_MISMATCH,
                "proposal_sha256 binding mismatch",
            )

    @classmethod
    def bind(
        cls,
        source: AgentBodyCandidateSourceV1,
        authorization: AgentBodyControlAuthorizationV1,
    ) -> "AgentBodyProposalV1":
        _require_nominal_carrier(source, AgentBodyCandidateSourceV1)
        _require_nominal_carrier(
            authorization, AgentBodyControlAuthorizationV1
        )
        if AgentBodyPermission.PROPOSE_CANDIDATE not in authorization.permissions:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.PERMISSION_VIOLATION,
                "authorization does not grant candidate proposal",
            )
        if source.control_authorization_sha256 != authorization.authorization_sha256:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.LINEAGE_MISMATCH,
                "candidate source authorization mismatch",
            )
        body = {
            "schema_version": cls.SCHEMA_VERSION,
            "agent_id": AGENT_ID,
            "package_version": PACKAGE_VERSION,
            "candidate_source_sha256": source.candidate_source_sha256,
            "control_authorization_sha256": authorization.authorization_sha256,
            "candidate_sha256": source.candidate_sha256,
            "parent_agent_body_sha256": source.parent_agent_body_sha256,
        }
        return cls(**body, proposal_sha256=canonical_sha256(body))


@_strict_frozen_dataclass
class AgentBodyAdmissionV1:
    schema_version: str
    agent_id: str
    package_version: str
    proposal_sha256: str
    control_authorization_sha256: str
    candidate_sha256: str
    parent_agent_body_sha256: str
    decision: AdmissionDecision
    admission_sha256: str

    SCHEMA_VERSION: ClassVar[str] = "agent-body.admission.v1"

    def __post_init__(self) -> None:
        mapping = _control_mapping(asdict(self))
        _require_identity(mapping)
        if self.schema_version != self.SCHEMA_VERSION:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.UNKNOWN_VALUE,
                "unknown admission schema_version",
            )
        _require_typed_enum(self.decision, AdmissionDecision, "decision")
        for field in (
            "proposal_sha256",
            "control_authorization_sha256",
            "candidate_sha256",
            "parent_agent_body_sha256",
            "admission_sha256",
        ):
            _require_sha256(getattr(self, field), field)
        del mapping["admission_sha256"]
        if self.admission_sha256 != canonical_sha256(mapping):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.HASH_MISMATCH,
                "admission_sha256 binding mismatch",
            )

    @classmethod
    def decide(
        cls,
        proposal: AgentBodyProposalV1,
        authorization: AgentBodyControlAuthorizationV1,
        decision: AdmissionDecision,
    ) -> "AgentBodyAdmissionV1":
        _require_nominal_carrier(proposal, AgentBodyProposalV1)
        _require_nominal_carrier(
            authorization, AgentBodyControlAuthorizationV1
        )
        if proposal.control_authorization_sha256 != authorization.authorization_sha256:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.LINEAGE_MISMATCH,
                "admission authorization does not match proposal",
            )
        if AgentBodyPermission.ADMIT_CANDIDATE not in authorization.permissions:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.PERMISSION_VIOLATION,
                "authorization does not grant candidate admission",
            )
        if type(decision) is not AdmissionDecision:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.WRONG_TYPE,
                "decision must be a closed AdmissionDecision",
            )
        body = {
            "schema_version": cls.SCHEMA_VERSION,
            "agent_id": AGENT_ID,
            "package_version": PACKAGE_VERSION,
            "proposal_sha256": proposal.proposal_sha256,
            "control_authorization_sha256": proposal.control_authorization_sha256,
            "candidate_sha256": proposal.candidate_sha256,
            "parent_agent_body_sha256": proposal.parent_agent_body_sha256,
            "decision": decision,
        }
        return cls(**body, admission_sha256=canonical_sha256(_control_mapping(body)))


@_strict_frozen_dataclass
class AgentBodyMergeV1:
    schema_version: str
    agent_id: str
    package_version: str
    admission_sha256: str
    candidate_sha256: str
    previous_agent_body_sha256: str
    resulting_agent_body_sha256: str
    state: MergeState
    merge_sha256: str

    SCHEMA_VERSION: ClassVar[str] = "agent-body.merge.v1"

    def __post_init__(self) -> None:
        mapping = _control_mapping(asdict(self))
        _require_identity(mapping)
        if self.schema_version != self.SCHEMA_VERSION:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.UNKNOWN_VALUE,
                "unknown merge schema_version",
            )
        _require_typed_enum(self.state, MergeState, "state")
        for field in (
            "admission_sha256",
            "candidate_sha256",
            "previous_agent_body_sha256",
            "resulting_agent_body_sha256",
            "merge_sha256",
        ):
            _require_sha256(getattr(self, field), field)
        del mapping["merge_sha256"]
        if self.merge_sha256 != canonical_sha256(mapping):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.HASH_MISMATCH,
                "merge_sha256 binding mismatch",
            )

    @classmethod
    def merge(
        cls,
        admission: AgentBodyAdmissionV1,
        authorization: AgentBodyControlAuthorizationV1,
        *,
        resulting_agent_body_sha256: str,
    ) -> "AgentBodyMergeV1":
        _require_nominal_carrier(admission, AgentBodyAdmissionV1)
        _require_nominal_carrier(
            authorization, AgentBodyControlAuthorizationV1
        )
        if admission.control_authorization_sha256 != authorization.authorization_sha256:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.LINEAGE_MISMATCH,
                "promotion authorization does not match admission",
            )
        if AgentBodyPermission.PROMOTE_CANDIDATE not in authorization.permissions:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.PERMISSION_VIOLATION,
                "authorization does not grant candidate promotion",
            )
        if admission.decision is not AdmissionDecision.ADMIT:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.STATE_VIOLATION,
                "only an admitted proposal can merge",
            )
        _require_sha256(resulting_agent_body_sha256, "resulting_agent_body_sha256")
        body = {
            "schema_version": cls.SCHEMA_VERSION,
            "agent_id": AGENT_ID,
            "package_version": PACKAGE_VERSION,
            "admission_sha256": admission.admission_sha256,
            "candidate_sha256": admission.candidate_sha256,
            "previous_agent_body_sha256": admission.parent_agent_body_sha256,
            "resulting_agent_body_sha256": resulting_agent_body_sha256,
            "state": MergeState.MERGED,
        }
        return cls(**body, merge_sha256=canonical_sha256(_control_mapping(body)))


@_strict_frozen_dataclass
class AgentBodyProvenanceV1:
    """Closed construction provenance; it has no generic parent/artifact field."""

    schema_version: str
    agent_id: str
    package_version: str
    control_source: ControlSource
    control_source_sha256: str
    control_authorization_sha256: str
    candidate_sha256: str
    candidate_source_sha256: str
    proposal_sha256: str
    admission_sha256: str
    merge_sha256: str
    provenance_sha256: str

    SCHEMA_VERSION: ClassVar[str] = "agent-body.provenance.v1"

    def __post_init__(self) -> None:
        mapping = _control_mapping(asdict(self))
        _require_identity(mapping)
        if self.schema_version != self.SCHEMA_VERSION:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.UNKNOWN_VALUE,
                "unknown provenance schema_version",
            )
        _require_typed_enum(self.control_source, ControlSource, "control_source")
        for field in (
            "control_source_sha256",
            "control_authorization_sha256",
            "candidate_sha256",
            "candidate_source_sha256",
            "proposal_sha256",
            "admission_sha256",
            "merge_sha256",
            "provenance_sha256",
        ):
            _require_sha256(getattr(self, field), field)
        del mapping["provenance_sha256"]
        if self.provenance_sha256 != canonical_sha256(mapping):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.HASH_MISMATCH,
                "provenance_sha256 binding mismatch",
            )

    @classmethod
    def bind(
        cls,
        authorization: AgentBodyControlAuthorizationV1,
        source: AgentBodyCandidateSourceV1,
        proposal: AgentBodyProposalV1,
        admission: AgentBodyAdmissionV1,
        merge: AgentBodyMergeV1,
    ) -> "AgentBodyProvenanceV1":
        _require_nominal_carrier(
            authorization, AgentBodyControlAuthorizationV1
        )
        _require_nominal_carrier(source, AgentBodyCandidateSourceV1)
        _require_nominal_carrier(proposal, AgentBodyProposalV1)
        _require_nominal_carrier(admission, AgentBodyAdmissionV1)
        _require_nominal_carrier(merge, AgentBodyMergeV1)
        expected = authorization.authorization_sha256
        if source.control_authorization_sha256 != expected:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.LINEAGE_MISMATCH,
                "candidate provenance authorization mismatch",
            )
        if proposal.control_authorization_sha256 != expected:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.LINEAGE_MISMATCH,
                "proposal provenance authorization mismatch",
            )
        if proposal.candidate_source_sha256 != source.candidate_source_sha256:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.LINEAGE_MISMATCH,
                "proposal provenance source mismatch",
            )
        if admission.proposal_sha256 != proposal.proposal_sha256:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.LINEAGE_MISMATCH,
                "admission provenance proposal mismatch",
            )
        if admission.control_authorization_sha256 != expected:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.LINEAGE_MISMATCH,
                "admission provenance authorization mismatch",
            )
        if merge.admission_sha256 != admission.admission_sha256:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.LINEAGE_MISMATCH,
                "merge provenance admission mismatch",
            )
        if len(
            {
                source.candidate_sha256,
                proposal.candidate_sha256,
                admission.candidate_sha256,
                merge.candidate_sha256,
            }
        ) != 1:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.LINEAGE_MISMATCH,
                "candidate provenance hash mismatch",
            )
        body = {
            "schema_version": cls.SCHEMA_VERSION,
            "agent_id": AGENT_ID,
            "package_version": PACKAGE_VERSION,
            "control_source": authorization.source,
            "control_source_sha256": authorization.source_sha256,
            "control_authorization_sha256": expected,
            "candidate_sha256": source.candidate_sha256,
            "candidate_source_sha256": source.candidate_source_sha256,
            "proposal_sha256": proposal.proposal_sha256,
            "admission_sha256": admission.admission_sha256,
            "merge_sha256": merge.merge_sha256,
        }
        return cls(**body, provenance_sha256=canonical_sha256(_control_mapping(body)))


@_strict_frozen_dataclass
class OpaqueEvaluationReceiptV1:
    """Minimal evaluation-tainted receipt accepted only by the release ledger."""

    schema_version: str
    agent_id: str
    package_version: str
    candidate_sha256: str
    verification_plan_sha256: str
    opaque_receipt_sha256: str
    decision: EvaluationDecision

    SCHEMA_VERSION: ClassVar[str] = "release-gate.opaque-receipt.v1"
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema_version",
            "agent_id",
            "package_version",
            "candidate_sha256",
            "verification_plan_sha256",
            "opaque_receipt_sha256",
            "decision",
        }
    )

    def __post_init__(self) -> None:
        mapping = _control_mapping(asdict(self))
        _require_identity(mapping)
        if self.schema_version != self.SCHEMA_VERSION:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.UNKNOWN_VALUE,
                "unknown opaque receipt schema_version",
            )
        _require_typed_enum(self.decision, EvaluationDecision, "decision")
        for field in (
            "candidate_sha256",
            "verification_plan_sha256",
            "opaque_receipt_sha256",
        ):
            _require_sha256(getattr(self, field), field)
        claimed = mapping.pop("opaque_receipt_sha256")
        if claimed != canonical_sha256(mapping):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.HASH_MISMATCH,
                "opaque_receipt_sha256 binding mismatch",
            )

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any]) -> "OpaqueEvaluationReceiptV1":
        _require_exact_keys(document, cls.FIELDS, cls.__name__)
        _require_identity(document)
        if document["schema_version"] != cls.SCHEMA_VERSION:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.UNKNOWN_VALUE,
                "unknown opaque receipt schema_version",
            )
        return cls(
            schema_version=cls.SCHEMA_VERSION,
            agent_id=AGENT_ID,
            package_version=PACKAGE_VERSION,
            candidate_sha256=_require_sha256(
                document["candidate_sha256"], "candidate_sha256"
            ),
            verification_plan_sha256=_require_sha256(
                document["verification_plan_sha256"], "verification_plan_sha256"
            ),
            opaque_receipt_sha256=_require_sha256(
                document["opaque_receipt_sha256"], "opaque_receipt_sha256"
            ),
            decision=_enum(EvaluationDecision, document["decision"], "decision"),
        )


@_strict_frozen_dataclass
class ReleaseGateDisposition:
    candidate_sha256: str
    verification_plan_sha256: str
    opaque_receipt_sha256: str
    status: ReleaseStatus

    def __post_init__(self) -> None:
        for field in (
            "candidate_sha256",
            "verification_plan_sha256",
            "opaque_receipt_sha256",
        ):
            _require_sha256(getattr(self, field), field)
        _require_typed_enum(self.status, ReleaseStatus, "status")


@_strict_frozen_dataclass
class ConstructionLineageEntry:
    """Nominal construction-only ledger entry."""

    candidate_sha256: str
    provenance_sha256: str
    state: ConstructionLedgerState
    entry_sha256: str

    def __post_init__(self) -> None:
        _require_typed_enum(self.state, ConstructionLedgerState, "state")
        mapping = _control_mapping(asdict(self))
        for field in ("candidate_sha256", "provenance_sha256", "entry_sha256"):
            _require_sha256(getattr(self, field), field)
        claimed = mapping.pop("entry_sha256")
        if claimed != canonical_sha256(mapping):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.HASH_MISMATCH,
                "construction ledger entry hash mismatch",
            )


@_strict_frozen_dataclass
class OpaqueEvaluationReceiptEntry:
    """Nominal evaluation-only ledger entry with permanently tainted identity."""

    candidate_sha256: str
    opaque_receipt_sha256: str
    state: OpaqueReceiptLedgerState
    entry_sha256: str

    def __post_init__(self) -> None:
        _require_typed_enum(self.state, OpaqueReceiptLedgerState, "state")
        mapping = _control_mapping(asdict(self))
        for field in ("candidate_sha256", "opaque_receipt_sha256", "entry_sha256"):
            _require_sha256(getattr(self, field), field)
        claimed = mapping.pop("entry_sha256")
        if claimed != canonical_sha256(mapping):
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.HASH_MISMATCH,
                "opaque receipt ledger entry hash mismatch",
            )


class ConstructionLineageLedger:
    """Append-only construction storage; evaluation values are nominally invalid."""

    def __init__(self) -> None:
        self._entries: dict[str, ConstructionLineageEntry] = {}
        self._lock = Lock()

    def record(self, provenance: AgentBodyProvenanceV1) -> ConstructionLineageEntry:
        _require_nominal_carrier(provenance, AgentBodyProvenanceV1)
        body = {
            "candidate_sha256": provenance.candidate_sha256,
            "provenance_sha256": provenance.provenance_sha256,
            "state": ConstructionLedgerState.RECORDED,
        }
        entry = ConstructionLineageEntry(
            **body,
            entry_sha256=canonical_sha256(_control_mapping(body)),
        )
        with self._lock:
            previous = self._entries.get(provenance.candidate_sha256)
            if previous is not None and previous != entry:
                raise AgentBodyBoundaryError(
                    BoundaryErrorCode.STATE_VIOLATION,
                    "construction lineage is immutable",
                )
            self._entries.setdefault(provenance.candidate_sha256, entry)
        return entry

    def entry(self, candidate_sha256: str) -> ConstructionLineageEntry:
        _require_sha256(candidate_sha256, "candidate_sha256")
        try:
            return self._entries[candidate_sha256]
        except KeyError as exc:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.STATE_VIOLATION,
                "candidate has no construction lineage entry",
            ) from exc


class OpaqueEvaluationReceiptLedger:
    """Append-only opaque evaluation storage; construction values cannot enter."""

    def __init__(self) -> None:
        self._entries: dict[str, OpaqueEvaluationReceiptEntry] = {}
        self._lock = Lock()

    def record(
        self, receipt: OpaqueEvaluationReceiptV1
    ) -> OpaqueEvaluationReceiptEntry:
        _require_nominal_carrier(receipt, OpaqueEvaluationReceiptV1)
        body = {
            "candidate_sha256": receipt.candidate_sha256,
            "opaque_receipt_sha256": receipt.opaque_receipt_sha256,
            "state": OpaqueReceiptLedgerState.RECEIVED,
        }
        entry = OpaqueEvaluationReceiptEntry(
            **body,
            entry_sha256=canonical_sha256(_control_mapping(body)),
        )
        with self._lock:
            previous = self._entries.get(receipt.opaque_receipt_sha256)
            if previous is not None and previous != entry:
                raise AgentBodyBoundaryError(
                    BoundaryErrorCode.STATE_VIOLATION,
                    "opaque evaluation receipt is immutable",
                )
            self._entries.setdefault(receipt.opaque_receipt_sha256, entry)
        return entry

    def entry(self, opaque_receipt_sha256: str) -> OpaqueEvaluationReceiptEntry:
        _require_sha256(opaque_receipt_sha256, "opaque_receipt_sha256")
        try:
            return self._entries[opaque_receipt_sha256]
        except KeyError as exc:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.STATE_VIOLATION,
                "receipt has no opaque evaluation entry",
            ) from exc


class ReleaseGateLedger:
    """Candidate-wide write-once terminal decisions without a feedback surface."""

    def __init__(self) -> None:
        self._entries: dict[str, ReleaseGateDisposition] = {}
        self._lock = Lock()

    def record(
        self,
        merge: AgentBodyMergeV1,
        receipt: OpaqueEvaluationReceiptV1,
    ) -> ReleaseGateDisposition:
        _require_nominal_carrier(merge, AgentBodyMergeV1)
        _require_nominal_carrier(receipt, OpaqueEvaluationReceiptV1)
        if receipt.candidate_sha256 != merge.candidate_sha256:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.LINEAGE_MISMATCH,
                "release receipt candidate mismatch",
            )
        with self._lock:
            if receipt.candidate_sha256 in self._entries:
                raise AgentBodyBoundaryError(
                    BoundaryErrorCode.STATE_VIOLATION,
                    "candidate already has an irreversible terminal decision",
                )
            status = (
                ReleaseStatus.RELEASED
                if receipt.decision is EvaluationDecision.PASS
                else ReleaseStatus.RELEASE_REJECTED
            )
            disposition = ReleaseGateDisposition(
                candidate_sha256=receipt.candidate_sha256,
                verification_plan_sha256=receipt.verification_plan_sha256,
                opaque_receipt_sha256=receipt.opaque_receipt_sha256,
                status=status,
            )
            self._entries[receipt.candidate_sha256] = disposition
        return disposition

    def disposition(
        self,
        candidate_sha256: str,
        verification_plan_sha256: str,
    ) -> ReleaseGateDisposition:
        _require_sha256(candidate_sha256, "candidate_sha256")
        _require_sha256(verification_plan_sha256, "verification_plan_sha256")
        try:
            disposition = self._entries[candidate_sha256]
        except KeyError as exc:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.STATE_VIOLATION,
                "candidate has no release disposition",
            ) from exc
        if disposition.verification_plan_sha256 != verification_plan_sha256:
            raise AgentBodyBoundaryError(
                BoundaryErrorCode.STATE_VIOLATION,
                "candidate release disposition is bound to a different verification plan",
            )
        return disposition


CONSTRUCTION_TRANSITIONS = frozenset(
    {
        (ConstructionState.CONTROL_AUTHORIZED, ConstructionState.CANDIDATE_SOURCED),
        (ConstructionState.CANDIDATE_SOURCED, ConstructionState.PROPOSED),
        (ConstructionState.PROPOSED, ConstructionState.ADMITTED),
        (ConstructionState.PROPOSED, ConstructionState.REJECTED),
        (ConstructionState.ADMITTED, ConstructionState.MERGED),
    }
)


def advance_construction(
    current: ConstructionState, target: ConstructionState
) -> ConstructionState:
    if not isinstance(current, ConstructionState) or not isinstance(
        target, ConstructionState
    ):
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.WRONG_TYPE,
            "construction states must be closed enums",
        )
    if (current, target) not in CONSTRUCTION_TRANSITIONS:
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.STATE_VIOLATION,
            "unreachable Agent-body construction transition",
        )
    return target


def enforce_main_agent_action(action: MainAgentAction) -> None:
    if type(action) is not MainAgentAction or action not in MAIN_AGENT_ACTIONS:
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.PERMISSION_VIOLATION,
            "main_agent action is outside its exact permission set",
        )


def enforce_main_agent_write(artifact_type: str) -> None:
    if type(artifact_type) is not str or artifact_type not in MAIN_AGENT_WRITES:
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.PERMISSION_VIOLATION,
            "main_agent write is outside its exact permission set",
        )


def enforce_main_agent_output(artifact_type: str) -> None:
    if type(artifact_type) is not str or artifact_type not in MAIN_AGENT_OUTPUTS:
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.PERMISSION_VIOLATION,
            "main_agent output is outside its exact permission set",
        )


def enforce_main_agent_boundary(
    *,
    actions: frozenset[MainAgentAction],
    writes: frozenset[str],
    outputs: frozenset[str],
) -> None:
    """Require the complete main-role capability projection by exact equality."""

    if type(actions) is not frozenset or actions != MAIN_AGENT_ACTIONS:
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.PERMISSION_VIOLATION,
            "main_agent actions do not equal the canonical set",
        )
    if type(writes) is not frozenset or writes != MAIN_AGENT_WRITES:
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.PERMISSION_VIOLATION,
            "main_agent writes do not equal the canonical set",
        )
    if type(outputs) is not frozenset or outputs != MAIN_AGENT_OUTPUTS:
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.PERMISSION_VIOLATION,
            "main_agent outputs do not equal the canonical set",
        )


CONSTRUCTION_CARRIER_TYPES = (
    DirectUserExecutableInstructionV1,
    IndependentGeneralResearchManifestV1,
    DTPV1,
    MAPV1,
    AgentBodyControlAuthorizationV1,
    AgentBodyCandidateSourceV1,
    AgentBodyProposalV1,
    AgentBodyAdmissionV1,
    AgentBodyMergeV1,
    AgentBodyProvenanceV1,
    ConstructionLineageEntry,
)
OPAQUE_EVALUATION_CARRIER_TYPES = (
    OpaqueEvaluationReceiptV1,
    OpaqueEvaluationReceiptEntry,
)
RELEASE_CARRIER_TYPES = (ReleaseGateDisposition,)
EVALUATION_CARRIER_TYPES = OPAQUE_EVALUATION_CARRIER_TYPES + RELEASE_CARRIER_TYPES
BOUNDARY_CARRIER_TYPES = CONSTRUCTION_CARRIER_TYPES + EVALUATION_CARRIER_TYPES

if set(CONSTRUCTION_CARRIER_TYPES) & set(EVALUATION_CARRIER_TYPES):
    raise RuntimeError("construction and evaluation carrier families overlap")

BOUNDARY_CARRIER_FIELD_SETS = MappingProxyType(
    {
        carrier.__name__: frozenset(field.name for field in dataclass_fields(carrier))
        for carrier in BOUNDARY_CARRIER_TYPES
    }
)

BOUNDARY_ENUM_VALUES = MappingProxyType(
    {
        enum_type.__name__: frozenset(member.value for member in enum_type)
        for enum_type in (
            BoundaryErrorCode,
            DirectUserInstruction,
            CapabilityTaxonomy,
            ChangeClass,
            ImpactClass,
            RollbackClass,
            VerificationPlanClass,
            GovernanceStage,
            GovernancePurpose,
            BuilderRole,
            AgentBodyPermission,
            ControlSource,
            AdmissionDecision,
            MergeState,
            EvaluationDecision,
            ReleaseStatus,
            ConstructionLedgerState,
            OpaqueReceiptLedgerState,
            ConstructionState,
            MainAgentAction,
            BoundaryTaint,
        )
    }
)


def validate_boundary_carrier(value: Any) -> None:
    """Revalidate one exact nominal boundary carrier after construction."""

    carrier_type = type(value)
    if carrier_type not in BOUNDARY_CARRIER_TYPES:
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.FAMILY_MISMATCH,
            "value is not an exact Agent-body boundary carrier",
        )
    carrier_type.__post_init__(value)


def boundary_taint(value: Any) -> BoundaryTaint:
    """Return the fixed taint of a validated nominal boundary carrier."""

    validate_boundary_carrier(value)
    if type(value) in EVALUATION_CARRIER_TYPES:
        return BoundaryTaint.TAINTED
    return BoundaryTaint.CLEAN


def join_boundary_taint(*values: BoundaryTaint) -> BoundaryTaint:
    """Monotone join; there is deliberately no declassification operation."""

    if not values:
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.CARDINALITY_VIOLATION,
            "taint join requires at least one value",
        )
    if any(type(value) is not BoundaryTaint for value in values):
        raise AgentBodyBoundaryError(
            BoundaryErrorCode.TAINT_VIOLATION,
            "taint join accepts only closed BoundaryTaint values",
        )
    if BoundaryTaint.TAINTED in values:
        return BoundaryTaint.TAINTED
    return BoundaryTaint.CLEAN


__all__ = (
    "AGENT_ID",
    "PACKAGE_VERSION",
    "BoundaryErrorCode",
    "AgentBodyBoundaryError",
    "DirectUserInstruction",
    "CapabilityTaxonomy",
    "ChangeClass",
    "ImpactClass",
    "RollbackClass",
    "VerificationPlanClass",
    "GovernanceStage",
    "GovernancePurpose",
    "BuilderRole",
    "AgentBodyPermission",
    "ControlSource",
    "AdmissionDecision",
    "MergeState",
    "EvaluationDecision",
    "ReleaseStatus",
    "ConstructionLedgerState",
    "OpaqueReceiptLedgerState",
    "ConstructionState",
    "MainAgentAction",
    "BoundaryTaint",
    "MAIN_AGENT_ACTIONS",
    "MAIN_AGENT_WRITES",
    "MAIN_AGENT_OUTPUTS",
    "MAIN_AGENT_FORBIDDEN_ARTIFACTS",
    "AGENT_BODY_AUTHORIZATION_PERMISSIONS",
    "canonical_sha256",
    "DirectUserExecutableInstructionV1",
    "IndependentGeneralResearchManifestV1",
    "DTPV1",
    "MAPV1",
    "AgentBodyControlAuthorizationV1",
    "AgentBodyCandidateSourceV1",
    "AgentBodyProposalV1",
    "AgentBodyAdmissionV1",
    "AgentBodyMergeV1",
    "AgentBodyProvenanceV1",
    "OpaqueEvaluationReceiptV1",
    "ReleaseGateDisposition",
    "ConstructionLineageEntry",
    "OpaqueEvaluationReceiptEntry",
    "ConstructionLineageLedger",
    "OpaqueEvaluationReceiptLedger",
    "ReleaseGateLedger",
    "CONSTRUCTION_TRANSITIONS",
    "CONSTRUCTION_CARRIER_TYPES",
    "EVALUATION_CARRIER_TYPES",
    "OPAQUE_EVALUATION_CARRIER_TYPES",
    "RELEASE_CARRIER_TYPES",
    "BOUNDARY_CARRIER_TYPES",
    "BOUNDARY_CARRIER_FIELD_SETS",
    "BOUNDARY_ENUM_VALUES",
    "advance_construction",
    "enforce_main_agent_action",
    "enforce_main_agent_write",
    "enforce_main_agent_output",
    "enforce_main_agent_boundary",
    "validate_boundary_carrier",
    "boundary_taint",
    "join_boundary_taint",
)
