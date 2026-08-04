from __future__ import annotations

import ast
from dataclasses import fields as dataclass_fields
import hashlib
import inspect
import json
from pathlib import Path
import textwrap
import tomllib

import pytest
import yaml

import modeling_harness.agent_body as agent_body_module
import modeling_harness.config as config_module
import modeling_harness.orchestrator as orchestrator_module
import modeling_harness.packets as packets_module
from modeling_harness.agent_body import (
    AGENT_BODY_AUTHORIZATION_PERMISSIONS,
    AGENT_ID,
    BOUNDARY_CARRIER_FIELD_SETS,
    BOUNDARY_ENUM_VALUES,
    MAIN_AGENT_ACTIONS,
    MAIN_AGENT_FORBIDDEN_ARTIFACTS,
    MAIN_AGENT_OUTPUTS,
    MAIN_AGENT_WRITES,
    PACKAGE_VERSION,
    AdmissionDecision,
    AgentBodyAdmissionV1,
    AgentBodyBoundaryError,
    AgentBodyCandidateSourceV1,
    AgentBodyControlAuthorizationV1,
    AgentBodyMergeV1,
    AgentBodyPermission,
    AgentBodyProposalV1,
    AgentBodyProvenanceV1,
    BoundaryErrorCode,
    BoundaryTaint,
    BuilderRole,
    CapabilityTaxonomy,
    ChangeClass,
    ConstructionLedgerState,
    ConstructionLineageEntry,
    ConstructionLineageLedger,
    ConstructionState,
    ControlSource,
    DTPV1,
    DirectUserExecutableInstructionV1,
    DirectUserInstruction,
    EvaluationDecision,
    GovernancePurpose,
    GovernanceStage,
    ImpactClass,
    IndependentGeneralResearchManifestV1,
    MAPV1,
    MainAgentAction,
    MergeState,
    OpaqueEvaluationReceiptEntry,
    OpaqueEvaluationReceiptLedger,
    OpaqueEvaluationReceiptV1,
    OpaqueReceiptLedgerState,
    ReleaseGateDisposition,
    ReleaseGateLedger,
    ReleaseStatus,
    RollbackClass,
    VerificationPlanClass,
    advance_construction,
    boundary_taint,
    canonical_sha256,
    enforce_main_agent_action,
    enforce_main_agent_boundary,
    enforce_main_agent_output,
    enforce_main_agent_write,
    join_boundary_taint,
    validate_boundary_carrier,
)
from modeling_harness.codex_adapter import (
    codex_agent_entrypoint,
    validate_codex_adapter,
)
from modeling_harness.config import (
    AGENT_BODY_PROVENANCE_POLICY,
    AGENT_VERSION,
    POLICY_PROJECTIONS,
    SOUND_BOUNDARY_PROJECTION,
    validate_config,
)
from modeling_harness.packets import PACKET_TYPES, PacketSchemaError, PacketValidator
from modeling_harness.roles import load_role_registry


ROOT = Path(__file__).parents[1]
SHA_PATTERN = "^[a-f0-9]{64}$"


def digest(index: int) -> str:
    return hashlib.sha256(index.to_bytes(8, "big")).hexdigest()


def assert_boundary_code(
    code: BoundaryErrorCode,
    operation: object,
    *args: object,
    **kwargs: object,
) -> None:
    with pytest.raises(AgentBodyBoundaryError) as raised:
        operation(*args, **kwargs)  # type: ignore[operator]
    assert raised.value.code is code


def direct_document(seed: int = 1) -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": DirectUserExecutableInstructionV1.SCHEMA_VERSION,
        "instruction": (
            DirectUserInstruction.APPLY_REVIEWED_GENERIC_GOVERNANCE_CHANGE.value
        ),
        "governance_document_sha256": digest(seed),
        "independent_review_sha256": digest(seed + 1),
        "agent_id": AGENT_ID,
        "package_version": PACKAGE_VERSION,
    }
    return {**body, "instruction_sha256": canonical_sha256(body)}


def direct_authorization(seed: int = 1) -> AgentBodyControlAuthorizationV1:
    instruction = DirectUserExecutableInstructionV1.from_mapping(
        direct_document(seed)
    )
    return AgentBodyControlAuthorizationV1.from_direct_user(instruction)


def research_document(seed: int = 20) -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": IndependentGeneralResearchManifestV1.SCHEMA_VERSION,
        "agent_id": AGENT_ID,
        "package_version": PACKAGE_VERSION,
        "independent_review_sha256": digest(seed),
        "capability": CapabilityTaxonomy.DEFINITION.value,
        "change_class": ChangeClass.CONTROL_SCHEMA.value,
        "impact": ImpactClass.AGENT_BODY.value,
        "rollback": RollbackClass.RESTORE_PARENT_AGENT_BODY.value,
        "verification_plan": (
            VerificationPlanClass.PREREGISTERED_CONTENT_INDEPENDENT.value
        ),
        "stage": GovernanceStage.PROPOSAL.value,
        "purpose": GovernancePurpose.AGENT_BODY_CHANGE.value,
    }
    return {**body, "manifest_sha256": canonical_sha256(body)}


def research_authority(
    seed: int = 20,
) -> tuple[
    IndependentGeneralResearchManifestV1,
    DTPV1,
    MAPV1,
    AgentBodyControlAuthorizationV1,
]:
    manifest = IndependentGeneralResearchManifestV1.from_mapping(
        research_document(seed)
    )
    dtp = DTPV1.from_manifest(manifest)
    map_packet = MAPV1.from_dtp(dtp)
    authorization = AgentBodyControlAuthorizationV1.from_research(
        manifest, dtp, map_packet
    )
    return manifest, dtp, map_packet, authorization


def construction_chain(
    authorization: AgentBodyControlAuthorizationV1,
    seed: int,
) -> tuple[
    AgentBodyCandidateSourceV1,
    AgentBodyProposalV1,
    AgentBodyAdmissionV1,
    AgentBodyMergeV1,
    AgentBodyProvenanceV1,
]:
    candidate_sha256 = digest(seed)
    source = AgentBodyCandidateSourceV1.bind(
        authorization,
        candidate_sha256=candidate_sha256,
        parent_agent_body_sha256=digest(seed + 1),
        builder_role=BuilderRole.SYSTEM_ARCHITECT,
    )
    proposal = AgentBodyProposalV1.bind(source, authorization)
    admission = AgentBodyAdmissionV1.decide(
        proposal, authorization, AdmissionDecision.ADMIT
    )
    merge = AgentBodyMergeV1.merge(
        admission,
        authorization,
        resulting_agent_body_sha256=candidate_sha256,
    )
    provenance = AgentBodyProvenanceV1.bind(
        authorization, source, proposal, admission, merge
    )
    return source, proposal, admission, merge, provenance


def opaque_receipt(
    merge: AgentBodyMergeV1,
    decision: EvaluationDecision = EvaluationDecision.PASS,
    seed: int = 400,
) -> OpaqueEvaluationReceiptV1:
    body: dict[str, object] = {
        "schema_version": OpaqueEvaluationReceiptV1.SCHEMA_VERSION,
        "agent_id": AGENT_ID,
        "package_version": PACKAGE_VERSION,
        "candidate_sha256": merge.candidate_sha256,
        "verification_plan_sha256": digest(seed),
        "decision": decision.value,
    }
    return OpaqueEvaluationReceiptV1.from_mapping(
        {**body, "opaque_receipt_sha256": canonical_sha256(body)}
    )


EXPECTED_CARRIER_FIELDS = {
    "DirectUserExecutableInstructionV1": frozenset(
        {
            "schema_version",
            "instruction",
            "governance_document_sha256",
            "instruction_sha256",
            "independent_review_sha256",
            "agent_id",
            "package_version",
        }
    ),
    "IndependentGeneralResearchManifestV1": frozenset(
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
    ),
    "DTPV1": frozenset(
        {
            "schema_version",
            "agent_id",
            "package_version",
            "research_manifest_sha256",
            "independent_review_sha256",
            "capability",
            "change_class",
            "impact",
            "rollback",
            "verification_plan",
            "stage",
            "purpose",
            "dtp_sha256",
        }
    ),
    "MAPV1": frozenset(
        {
            "schema_version",
            "agent_id",
            "package_version",
            "research_manifest_sha256",
            "independent_review_sha256",
            "capability",
            "change_class",
            "impact",
            "rollback",
            "verification_plan",
            "stage",
            "purpose",
            "dtp_sha256",
            "map_sha256",
        }
    ),
    "AgentBodyControlAuthorizationV1": frozenset(
        {
            "schema_version",
            "source",
            "source_sha256",
            "direct_user_instruction_sha256",
            "research_manifest_sha256",
            "dtp_sha256",
            "map_sha256",
            "permissions",
            "authorization_sha256",
            "agent_id",
            "package_version",
        }
    ),
    "AgentBodyCandidateSourceV1": frozenset(
        {
            "schema_version",
            "agent_id",
            "package_version",
            "candidate_sha256",
            "parent_agent_body_sha256",
            "builder_role",
            "control_authorization_sha256",
            "candidate_source_sha256",
        }
    ),
    "AgentBodyProposalV1": frozenset(
        {
            "schema_version",
            "agent_id",
            "package_version",
            "candidate_source_sha256",
            "control_authorization_sha256",
            "candidate_sha256",
            "parent_agent_body_sha256",
            "proposal_sha256",
        }
    ),
    "AgentBodyAdmissionV1": frozenset(
        {
            "schema_version",
            "agent_id",
            "package_version",
            "proposal_sha256",
            "control_authorization_sha256",
            "candidate_sha256",
            "parent_agent_body_sha256",
            "decision",
            "admission_sha256",
        }
    ),
    "AgentBodyMergeV1": frozenset(
        {
            "schema_version",
            "agent_id",
            "package_version",
            "admission_sha256",
            "candidate_sha256",
            "previous_agent_body_sha256",
            "resulting_agent_body_sha256",
            "state",
            "merge_sha256",
        }
    ),
    "AgentBodyProvenanceV1": frozenset(
        {
            "schema_version",
            "agent_id",
            "package_version",
            "control_source",
            "control_source_sha256",
            "control_authorization_sha256",
            "candidate_sha256",
            "candidate_source_sha256",
            "proposal_sha256",
            "admission_sha256",
            "merge_sha256",
            "provenance_sha256",
        }
    ),
    "OpaqueEvaluationReceiptV1": frozenset(
        {
            "schema_version",
            "agent_id",
            "package_version",
            "candidate_sha256",
            "verification_plan_sha256",
            "opaque_receipt_sha256",
            "decision",
        }
    ),
    "ReleaseGateDisposition": frozenset(
        {
            "candidate_sha256",
            "verification_plan_sha256",
            "opaque_receipt_sha256",
            "status",
        }
    ),
    "ConstructionLineageEntry": frozenset(
        {"candidate_sha256", "provenance_sha256", "state", "entry_sha256"}
    ),
    "OpaqueEvaluationReceiptEntry": frozenset(
        {"candidate_sha256", "opaque_receipt_sha256", "state", "entry_sha256"}
    ),
}


EXPECTED_ENUM_VALUES = {
    "BoundaryErrorCode": frozenset(
        {
            "UNKNOWN_KEY",
            "MISSING_KEY",
            "WRONG_TYPE",
            "UNKNOWN_VALUE",
            "NONCANONICAL_VALUE",
            "HASH_MISMATCH",
            "LINEAGE_MISMATCH",
            "FAMILY_MISMATCH",
            "TAINT_VIOLATION",
            "STATE_VIOLATION",
            "PERMISSION_VIOLATION",
            "CARDINALITY_VIOLATION",
            "EQUALITY_VIOLATION",
        }
    ),
    "DirectUserInstruction": frozenset(
        {"APPLY_REVIEWED_GENERIC_GOVERNANCE_CHANGE"}
    ),
    "CapabilityTaxonomy": frozenset(
        {
            "DEFINITION",
            "UNDERSTANDING",
            "CREATION",
            "VALIDATION",
            "RUNTIME_ORCHESTRATION",
        }
    ),
    "ChangeClass": frozenset(
        {
            "CONTROL_SCHEMA",
            "ROLE_PERMISSION",
            "STATE_TRANSITION",
            "RUNTIME_ENFORCEMENT",
            "PROMPT_PROJECTION",
            "GENERIC_CAPABILITY",
        }
    ),
    "ImpactClass": frozenset({"CONTROL_PLANE", "AGENT_BODY"}),
    "RollbackClass": frozenset({"RESTORE_PARENT_AGENT_BODY"}),
    "VerificationPlanClass": frozenset(
        {"PREREGISTERED_CONTENT_INDEPENDENT"}
    ),
    "GovernanceStage": frozenset({"PROPOSAL", "MERGE"}),
    "GovernancePurpose": frozenset({"AGENT_BODY_CHANGE"}),
    "BuilderRole": frozenset(
        {
            "system_architect",
            "sandbox_platform_engineer",
            "standards_delivery_manager",
        }
    ),
    "AgentBodyPermission": frozenset(
        {
            "CONSTRUCT_CANDIDATE",
            "PROPOSE_CANDIDATE",
            "ADMIT_CANDIDATE",
            "PROMOTE_CANDIDATE",
        }
    ),
    "ControlSource": frozenset(
        {"DIRECT_USER", "INDEPENDENT_GENERAL_RESEARCH"}
    ),
    "AdmissionDecision": frozenset({"ADMIT", "REJECT"}),
    "MergeState": frozenset({"MERGED"}),
    "EvaluationDecision": frozenset({"PASS", "REJECT"}),
    "ReleaseStatus": frozenset({"RELEASED", "RELEASE_REJECTED"}),
    "ConstructionLedgerState": frozenset({"CONSTRUCTION_RECORDED"}),
    "OpaqueReceiptLedgerState": frozenset({"OPAQUE_RECEIPT_RECEIVED"}),
    "ConstructionState": frozenset(
        {
            "CONTROL_AUTHORIZED",
            "CANDIDATE_SOURCED",
            "PROPOSED",
            "ADMITTED",
            "REJECTED",
            "MERGED",
        }
    ),
    "MainAgentAction": frozenset(
        {"dispatch", "write_prompts", "review", "approve", "reject"}
    ),
    "BoundaryTaint": frozenset({"CLEAN", "TAINTED"}),
}


SCHEMA_FIELDS = {
    "DirectUserExecutableInstructionV1": (
        "schema_version",
        "instruction",
        "governance_document_sha256",
        "instruction_sha256",
        "independent_review_sha256",
        "agent_id",
        "package_version",
    ),
    "IndependentGeneralResearchManifestV1": (
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
    ),
    "DTPV1": (
        "schema_version",
        "agent_id",
        "package_version",
        "research_manifest_sha256",
        "independent_review_sha256",
        "capability",
        "change_class",
        "impact",
        "rollback",
        "verification_plan",
        "stage",
        "purpose",
        "dtp_sha256",
    ),
    "MAPV1": (
        "schema_version",
        "agent_id",
        "package_version",
        "research_manifest_sha256",
        "independent_review_sha256",
        "capability",
        "change_class",
        "impact",
        "rollback",
        "verification_plan",
        "stage",
        "purpose",
        "dtp_sha256",
        "map_sha256",
    ),
    "OpaqueEvaluationReceiptV1": (
        "schema_version",
        "agent_id",
        "package_version",
        "candidate_sha256",
        "verification_plan_sha256",
        "opaque_receipt_sha256",
        "decision",
    ),
}


def test_direct_user_authority_constructs_complete_receipt_free_lineage() -> None:
    authorization = direct_authorization()
    source, proposal, admission, merge, provenance = construction_chain(
        authorization, 100
    )
    assert authorization.source is ControlSource.DIRECT_USER
    assert authorization.direct_user_instruction_sha256 == (
        authorization.source_sha256
    )
    assert (
        authorization.research_manifest_sha256,
        authorization.dtp_sha256,
        authorization.map_sha256,
    ) == (None, None, None)
    assert source.control_authorization_sha256 == authorization.authorization_sha256
    assert proposal.candidate_source_sha256 == source.candidate_source_sha256
    assert admission.proposal_sha256 == proposal.proposal_sha256
    assert merge.admission_sha256 == admission.admission_sha256
    assert provenance.merge_sha256 == merge.merge_sha256
    assert provenance.control_source is ControlSource.DIRECT_USER


def test_research_authority_constructs_complete_receipt_free_lineage() -> None:
    manifest, dtp, map_packet, authorization = research_authority()
    source, proposal, admission, merge, provenance = construction_chain(
        authorization, 120
    )
    assert authorization.source is ControlSource.INDEPENDENT_GENERAL_RESEARCH
    assert authorization.source_sha256 == manifest.manifest_sha256
    assert authorization.dtp_sha256 == dtp.dtp_sha256
    assert authorization.map_sha256 == map_packet.map_sha256
    assert proposal.control_authorization_sha256 == (
        authorization.authorization_sha256
    )
    assert admission.candidate_sha256 == source.candidate_sha256
    assert merge.resulting_agent_body_sha256 == source.candidate_sha256
    assert provenance.control_source is ControlSource.INDEPENDENT_GENERAL_RESEARCH


def test_authority_paths_are_nominally_closed_and_cannot_cross() -> None:
    instruction = DirectUserExecutableInstructionV1.from_mapping(direct_document())
    manifest_a, dtp_a, _, _ = research_authority(40)
    _, _, map_b, _ = research_authority(60)
    assert_boundary_code(
        BoundaryErrorCode.FAMILY_MISMATCH,
        AgentBodyControlAuthorizationV1.from_direct_user,
        manifest_a,
    )
    assert_boundary_code(
        BoundaryErrorCode.FAMILY_MISMATCH,
        AgentBodyControlAuthorizationV1.from_research,
        instruction,
        dtp_a,
        map_b,
    )
    assert_boundary_code(
        BoundaryErrorCode.LINEAGE_MISMATCH,
        AgentBodyControlAuthorizationV1.from_research,
        manifest_a,
        dtp_a,
        map_b,
    )
    assert_boundary_code(
        BoundaryErrorCode.FAMILY_MISMATCH,
        AgentBodyControlAuthorizationV1.from_direct_user,
        instruction.as_mapping(),
    )


def test_construction_api_signatures_have_no_evaluation_input() -> None:
    signatures = {
        AgentBodyCandidateSourceV1.bind: (
            "authorization",
            "candidate_sha256",
            "parent_agent_body_sha256",
            "builder_role",
        ),
        AgentBodyProposalV1.bind: ("source", "authorization"),
        AgentBodyAdmissionV1.decide: (
            "proposal",
            "authorization",
            "decision",
        ),
        AgentBodyMergeV1.merge: (
            "admission",
            "authorization",
            "resulting_agent_body_sha256",
        ),
        AgentBodyProvenanceV1.bind: (
            "authorization",
            "source",
            "proposal",
            "admission",
            "merge",
        ),
        ConstructionLineageLedger.record: ("self", "provenance"),
    }
    for operation, expected in signatures.items():
        assert tuple(inspect.signature(operation).parameters) == expected


def test_three_ledgers_reject_every_cross_family_substitution() -> None:
    authorization = direct_authorization()
    _, _, _, merge, provenance = construction_chain(authorization, 140)
    receipt = opaque_receipt(merge)
    construction_ledger = ConstructionLineageLedger()
    opaque_ledger = OpaqueEvaluationReceiptLedger()
    release_ledger = ReleaseGateLedger()
    construction_entry = construction_ledger.record(provenance)
    opaque_entry = opaque_ledger.record(receipt)
    disposition = release_ledger.record(merge, receipt)
    assert type(construction_entry) is ConstructionLineageEntry
    assert type(opaque_entry) is OpaqueEvaluationReceiptEntry
    assert type(disposition) is ReleaseGateDisposition
    assert_boundary_code(
        BoundaryErrorCode.FAMILY_MISMATCH,
        construction_ledger.record,
        receipt,
    )
    assert_boundary_code(
        BoundaryErrorCode.FAMILY_MISMATCH,
        opaque_ledger.record,
        provenance,
    )
    assert_boundary_code(
        BoundaryErrorCode.FAMILY_MISMATCH,
        release_ledger.record,
        construction_entry,
        receipt,
    )
    assert_boundary_code(
        BoundaryErrorCode.FAMILY_MISMATCH,
        release_ledger.record,
        merge,
        opaque_entry,
    )


@pytest.mark.parametrize(
    ("decision", "status"),
    (
        (EvaluationDecision.PASS, ReleaseStatus.RELEASED),
        (EvaluationDecision.REJECT, ReleaseStatus.RELEASE_REJECTED),
    ),
)
def test_opaque_receipt_produces_only_terminal_release_status(
    decision: EvaluationDecision, status: ReleaseStatus
) -> None:
    _, _, _, merge, _ = construction_chain(direct_authorization(), 160)
    receipt = opaque_receipt(merge, decision)
    disposition = ReleaseGateLedger().record(merge, receipt)
    assert disposition.status is status
    assert set(ReleaseStatus) == {
        ReleaseStatus.RELEASED,
        ReleaseStatus.RELEASE_REJECTED,
    }
    assert frozenset(disposition.__dict__) == EXPECTED_CARRIER_FIELDS[
        "ReleaseGateDisposition"
    ]


def test_release_terminal_is_candidate_wide_across_plans_and_decisions() -> None:
    _, _, _, merge, _ = construction_chain(direct_authorization(), 180)
    passed = opaque_receipt(merge, EvaluationDecision.PASS, 500)
    rejected = opaque_receipt(merge, EvaluationDecision.REJECT, 501)
    ledger = ReleaseGateLedger()
    assert ledger.record(merge, passed).status is ReleaseStatus.RELEASED
    assert_boundary_code(
        BoundaryErrorCode.STATE_VIOLATION, ledger.record, merge, passed
    )
    assert_boundary_code(
        BoundaryErrorCode.STATE_VIOLATION, ledger.record, merge, rejected
    )
    assert ledger.disposition(
        merge.candidate_sha256, passed.verification_plan_sha256
    ).status is ReleaseStatus.RELEASED
    assert_boundary_code(
        BoundaryErrorCode.STATE_VIOLATION,
        ledger.disposition,
        merge.candidate_sha256,
        rejected.verification_plan_sha256,
    )


def test_release_terminal_does_not_lock_a_different_candidate() -> None:
    authorization = direct_authorization()
    _, _, _, merge_a, _ = construction_chain(authorization, 182)
    _, _, _, merge_b, _ = construction_chain(authorization, 184)
    receipt_a = opaque_receipt(merge_a, EvaluationDecision.PASS, 510)
    receipt_b = opaque_receipt(merge_b, EvaluationDecision.REJECT, 511)
    ledger = ReleaseGateLedger()

    assert ledger.record(merge_a, receipt_a).status is ReleaseStatus.RELEASED
    assert ledger.record(merge_b, receipt_b).status is ReleaseStatus.RELEASE_REJECTED
    assert ledger.disposition(
        merge_a.candidate_sha256, receipt_a.verification_plan_sha256
    ).candidate_sha256 == merge_a.candidate_sha256
    assert ledger.disposition(
        merge_b.candidate_sha256, receipt_b.verification_plan_sha256
    ).candidate_sha256 == merge_b.candidate_sha256


def test_release_requires_exact_merged_frozen_candidate_binding() -> None:
    _, _, _, merge_a, _ = construction_chain(direct_authorization(), 200)
    _, _, _, merge_b, _ = construction_chain(direct_authorization(5), 220)
    receipt = opaque_receipt(merge_a)
    assert_boundary_code(
        BoundaryErrorCode.LINEAGE_MISMATCH,
        ReleaseGateLedger().record,
        merge_b,
        receipt,
    )
    object.__setattr__(merge_a, "candidate_sha256", digest(999))
    assert_boundary_code(
        BoundaryErrorCode.HASH_MISMATCH,
        ReleaseGateLedger().record,
        merge_a,
        receipt,
    )


def test_exact_carrier_fields_and_closed_enum_values() -> None:
    assert dict(BOUNDARY_CARRIER_FIELD_SETS) == EXPECTED_CARRIER_FIELDS
    assert dict(BOUNDARY_ENUM_VALUES) == EXPECTED_ENUM_VALUES
    assert DirectUserExecutableInstructionV1.FIELDS == (
        EXPECTED_CARRIER_FIELDS["DirectUserExecutableInstructionV1"]
    )
    assert IndependentGeneralResearchManifestV1.FIELDS == (
        EXPECTED_CARRIER_FIELDS["IndependentGeneralResearchManifestV1"]
    )
    assert OpaqueEvaluationReceiptV1.FIELDS == (
        EXPECTED_CARRIER_FIELDS["OpaqueEvaluationReceiptV1"]
    )


def test_json_schemas_have_exact_fields_and_fail_closed_shape() -> None:
    for title, ordered_fields in SCHEMA_FIELDS.items():
        schema = json.loads(
            (
                ROOT / "workspaces/architect/schemas" / f"{title}.json"
            ).read_text(encoding="utf-8")
        )
        assert set(schema) == {
            "$schema",
            "$id",
            "title",
            "type",
            "additionalProperties",
            "required",
            "properties",
        }
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"] == (
            f"https://modeling-harness.invalid/schemas/{title}.json"
        )
        assert schema["title"] == title
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert tuple(schema["required"]) == ordered_fields
        assert tuple(schema["properties"]) == ordered_fields
        for field in ordered_fields:
            rule = schema["properties"][field]
            if field.endswith("_sha256"):
                assert rule == {"type": "string", "pattern": SHA_PATTERN}
    direct_schema = json.loads(
        (
            ROOT
            / "workspaces/architect/schemas/DirectUserExecutableInstructionV1.json"
        ).read_text(encoding="utf-8")
    )
    assert direct_schema["properties"]["instruction"] == {
        "const": next(iter(EXPECTED_ENUM_VALUES["DirectUserInstruction"]))
    }
    receipt_schema = json.loads(
        (
            ROOT / "workspaces/architect/schemas/OpaqueEvaluationReceiptV1.json"
        ).read_text(encoding="utf-8")
    )
    assert frozenset(receipt_schema["properties"]["decision"]["enum"]) == (
        EXPECTED_ENUM_VALUES["EvaluationDecision"]
    )


def test_unknown_missing_surplus_wrong_type_and_hash_are_typed() -> None:
    document = direct_document()
    assert_boundary_code(
        BoundaryErrorCode.UNKNOWN_KEY,
        DirectUserExecutableInstructionV1.from_mapping,
        {**document, "surplus": digest(700)},
    )
    missing = dict(document)
    missing.pop("instruction")
    assert_boundary_code(
        BoundaryErrorCode.MISSING_KEY,
        DirectUserExecutableInstructionV1.from_mapping,
        missing,
    )
    assert_boundary_code(
        BoundaryErrorCode.WRONG_TYPE,
        DirectUserExecutableInstructionV1.from_mapping,
        [],
    )
    assert_boundary_code(
        BoundaryErrorCode.UNKNOWN_VALUE,
        DirectUserExecutableInstructionV1.from_mapping,
        {**document, "instruction": "UNKNOWN"},
    )
    assert_boundary_code(
        BoundaryErrorCode.HASH_MISMATCH,
        DirectUserExecutableInstructionV1.from_mapping,
        {**document, "instruction_sha256": digest(701)},
    )
    assert_boundary_code(
        BoundaryErrorCode.CARDINALITY_VIOLATION,
        DirectUserExecutableInstructionV1,
        *(tuple(document.values()) + ("surplus",)),
    )


def test_post_construction_parent_mutation_is_detected() -> None:
    instruction = DirectUserExecutableInstructionV1.from_mapping(direct_document())
    object.__setattr__(
        instruction, "governance_document_sha256", digest(750)
    )
    assert_boundary_code(
        BoundaryErrorCode.HASH_MISMATCH,
        AgentBodyControlAuthorizationV1.from_direct_user,
        instruction,
    )
    authorization = direct_authorization()
    _, proposal, _, _, _ = construction_chain(authorization, 240)
    object.__setattr__(proposal, "candidate_sha256", digest(751))
    assert_boundary_code(
        BoundaryErrorCode.HASH_MISMATCH,
        AgentBodyAdmissionV1.decide,
        proposal,
        authorization,
        AdmissionDecision.ADMIT,
    )


def test_wrong_nominal_and_duck_mapping_values_fail_closed() -> None:
    authorization = direct_authorization()
    source, proposal, _, _, _ = construction_chain(authorization, 260)
    assert_boundary_code(
        BoundaryErrorCode.FAMILY_MISMATCH,
        AgentBodyProposalV1.bind,
        source.__dict__,
        authorization,
    )
    assert_boundary_code(
        BoundaryErrorCode.FAMILY_MISMATCH,
        AgentBodyAdmissionV1.decide,
        proposal.__dict__,
        authorization,
        AdmissionDecision.ADMIT,
    )
    assert_boundary_code(
        BoundaryErrorCode.FAMILY_MISMATCH,
        validate_boundary_carrier,
        {
            field.name: getattr(proposal, field.name)
            for field in dataclass_fields(proposal)
        },
    )


def test_boundary_taint_is_nominal_permanent_and_monotone() -> None:
    authorization = direct_authorization()
    _, _, _, merge, provenance = construction_chain(authorization, 280)
    receipt = opaque_receipt(merge)
    construction_entry = ConstructionLineageLedger().record(provenance)
    opaque_entry = OpaqueEvaluationReceiptLedger().record(receipt)
    disposition = ReleaseGateLedger().record(merge, receipt)
    assert boundary_taint(authorization) is BoundaryTaint.CLEAN
    assert boundary_taint(construction_entry) is BoundaryTaint.CLEAN
    assert boundary_taint(receipt) is BoundaryTaint.TAINTED
    assert boundary_taint(opaque_entry) is BoundaryTaint.TAINTED
    assert boundary_taint(disposition) is BoundaryTaint.TAINTED
    assert (
        join_boundary_taint(BoundaryTaint.CLEAN, BoundaryTaint.TAINTED)
        is BoundaryTaint.TAINTED
    )
    assert_boundary_code(
        BoundaryErrorCode.TAINT_VIOLATION,
        join_boundary_taint,
        BoundaryTaint.CLEAN,
        "CLEAN",
    )
    assert_boundary_code(
        BoundaryErrorCode.FAMILY_MISMATCH,
        AgentBodyAdmissionV1.decide,
        receipt,
        authorization,
        AdmissionDecision.ADMIT,
    )


def test_main_agent_action_write_and_output_sets_are_exact_equalities() -> None:
    expected_actions = frozenset(MainAgentAction)
    expected_writes = frozenset(
        {
            "dispatch_records",
            "task_packets",
            "prompt_registry",
            "review_decisions",
            "approval_decisions",
            "rejection_decisions",
        }
    )
    assert MAIN_AGENT_ACTIONS == expected_actions
    assert MAIN_AGENT_WRITES == expected_writes
    assert MAIN_AGENT_OUTPUTS == expected_writes
    assert set(AGENT_BODY_PROVENANCE_POLICY["main_agent_actions"]) == {
        item.value for item in expected_actions
    }
    assert set(AGENT_BODY_PROVENANCE_POLICY["main_agent_writes"]) == (
        expected_writes
    )
    assert set(AGENT_BODY_PROVENANCE_POLICY["main_agent_outputs"]) == (
        expected_writes
    )
    assert set(
        AGENT_BODY_PROVENANCE_POLICY["main_agent_forbidden_outputs"]
    ) == MAIN_AGENT_FORBIDDEN_ARTIFACTS
    assert AGENT_BODY_AUTHORIZATION_PERMISSIONS == tuple(AgentBodyPermission)
    enforce_main_agent_boundary(
        actions=expected_actions,
        writes=expected_writes,
        outputs=expected_writes,
    )
    for action in expected_actions:
        enforce_main_agent_action(action)
    for artifact_type in expected_writes:
        enforce_main_agent_write(artifact_type)
        enforce_main_agent_output(artifact_type)
    assert_boundary_code(
        BoundaryErrorCode.PERMISSION_VIOLATION,
        enforce_main_agent_boundary,
        actions=frozenset(),
        writes=expected_writes,
        outputs=expected_writes,
    )


def test_construction_state_graph_has_no_release_return_edge() -> None:
    state = ConstructionState.CONTROL_AUTHORIZED
    for target in (
        ConstructionState.CANDIDATE_SOURCED,
        ConstructionState.PROPOSED,
        ConstructionState.ADMITTED,
        ConstructionState.MERGED,
    ):
        state = advance_construction(state, target)
    assert state is ConstructionState.MERGED
    assert_boundary_code(
        BoundaryErrorCode.STATE_VIOLATION,
        advance_construction,
        ConstructionState.MERGED,
        ConstructionState.PROPOSED,
    )


def test_five_named_policy_projections_are_exact_and_identical() -> None:
    assert POLICY_PROJECTIONS == (
        Path("workspaces/architect/role_registry.yaml"),
        Path("workspaces/architect/state_machine.yaml"),
        Path("workspaces/evaluation/benchmark_protocol.yaml"),
        Path("workspaces/evaluation/capability_taxonomy.yaml"),
        Path("workspaces/prompts/roles.yaml"),
    )
    projection_hashes = set()
    legacy_key = "sound_boundary_" + "r3" + "b"
    for relative in POLICY_PROJECTIONS:
        document = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
        assert document["agent_version"] == AGENT_VERSION
        assert document["agent_body_provenance_policy"] == (
            AGENT_BODY_PROVENANCE_POLICY
        )
        assert document["sound_boundary_r3e"] == SOUND_BOUNDARY_PROJECTION
        assert legacy_key not in document
        projection_hashes.add(
            canonical_sha256(document["sound_boundary_r3e"])
        )
    assert len(projection_hashes) == 1


EXPECTED_PROMPTS = (
    "00_main_agent.md",
    "01_system_architect.md",
    "02_sandbox_platform_builder.md",
    "03_benchmark_curator.md",
    "04_standards_delivery_manager.md",
    "05_problem_definition_router.md",
    "06_domain_evidence_researcher.md",
    "07_data_assumption_analyst.md",
    "08_model_architect.md",
    "09_mechanism_numerical_modeler.md",
    "10_statistical_learning_modeler.md",
    "11_optimization_decision_modeler.md",
    "12_computation_reproducibility_engineer.md",
    "13_visualization_report_writer.md",
    "14_mathematical_logic_reviewer.md",
    "15_numerical_reproducibility_reviewer.md",
    "16_expression_evidence_reviewer.md",
)


EXPECTED_PROFILES = (
    "benchmark_curator",
    "compute_reproducibility_engineer",
    "data_assumption_analyst",
    "domain_evidence_researcher",
    "evidence_communication_reviewer",
    "mathematical_reviewer",
    "mechanistic_numerical_modeler",
    "model_architect",
    "optimization_decision_modeler",
    "problem_definition_router",
    "reproducibility_reviewer",
    "sandbox_platform_engineer",
    "standards_delivery_manager",
    "statistical_learning_modeler",
    "system_architect",
    "visualization_report_author",
)


def test_seventeen_named_prompts_share_one_exact_boundary_projection() -> None:
    paths = sorted((ROOT / "workspaces/prompts/system_prompts").glob("*.md"))
    assert tuple(path.name for path in paths) == EXPECTED_PROMPTS
    marker = "## Revision 3E sound construction boundary (mandatory)"
    blocks = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert source.count(marker) == 1
        blocks.append(source.split(marker, 1)[1].strip())
    assert len(
        {hashlib.sha256(block.encode("utf-8")).hexdigest() for block in blocks}
    ) == 1


def test_sixteen_named_profiles_and_runtime_role_prompt_pairs_are_equivalent() -> None:
    config_report = validate_config(ROOT)
    adapter_report = validate_codex_adapter(ROOT)
    assert config_report.role_ids == adapter_report.profile_names
    assert frozenset(config_report.role_ids) == frozenset(EXPECTED_PROFILES)
    registry = load_role_registry(
        ROOT / "workspaces/architect/role_registry.yaml",
        ROOT / "workspaces/prompts/roles.yaml",
    )
    common_tail_hashes = set()
    for role_id in config_report.role_ids:
        profile_path = ROOT / ".codex/agents" / f"{role_id}.toml"
        profile = tomllib.loads(profile_path.read_text(encoding="utf-8"))
        assert profile["name"] == role_id
        lines = profile["developer_instructions"].strip().splitlines()
        common_tail_hashes.add(
            hashlib.sha256("\n".join(lines[3:]).encode("utf-8")).hexdigest()
        )
        assert codex_agent_entrypoint(role_id, project_root=ROOT) == (
            "--role",
            role_id,
            "--charter",
            f"/opt/modeling-harness/prompts/{registry.prompt_for(role_id).name}",
        )
    assert len(common_tail_hashes) == 1
    dockerfile = (
        ROOT / "containers/codex-agent/Dockerfile"
    ).read_text(encoding="utf-8")
    assert (
        "COPY workspaces/prompts/system_prompts/ /opt/modeling-harness/prompts/"
        in dockerfile
    )


def test_admission_ast_has_no_scanner_or_string_content_branch() -> None:
    source = textwrap.dedent(inspect.getsource(AgentBodyAdmissionV1.decide))
    tree = ast.parse(source)
    scanner_mechanisms = {
        "casefold",
        "compile",
        "endswith",
        "find",
        "fullmatch",
        "lower",
        "match",
        "search",
        "split",
        "startswith",
    }
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not calls & scanner_mechanisms
    predicates = [
        node.test
        for node in ast.walk(tree)
        if isinstance(node, (ast.If, ast.IfExp))
    ]
    assert predicates
    for predicate in predicates:
        assert not any(
            isinstance(node, (ast.Subscript, ast.Dict, ast.List, ast.Set))
            for node in ast.walk(predicate)
        )
        assert not any(
            isinstance(node, ast.Constant) and isinstance(node.value, str)
            for node in ast.walk(predicate)
        )


def test_retired_agent_body_boundary_and_packet_alias_surface_is_zero() -> None:
    retired_symbols = (
        "Admission" + "AuditBundle",
        "Evaluation" + "EvidenceArtifact",
        "sound_boundary_" + "r3" + "b",
        "Revision" + "3B",
    )
    inspected_paths = (
        ROOT / "src/modeling_harness/agent_body.py",
        ROOT / "src/modeling_harness/config.py",
        ROOT / "src/modeling_harness/orchestrator.py",
        ROOT / "src/modeling_harness/packets.py",
        *(ROOT / relative for relative in POLICY_PROJECTIONS),
        *sorted((ROOT / "workspaces/prompts/system_prompts").glob("*.md")),
        *sorted((ROOT / ".codex/agents").glob("*.toml")),
    )
    assert sum(
        source.count(symbol)
        for path in inspected_paths
        for source in (path.read_text(encoding="utf-8"),)
        for symbol in retired_symbols
    ) == 0
    assert PACKET_TYPES == (
        "TaskPacket",
        "ResultPacket",
        "ReviewPacket",
        "RevisionDecision",
        "ArtifactManifest",
        "RoleCharter",
        "SourceProvenanceManifest",
    )
    legacy_packet_aliases = (
        "DirectUser" + "GovernancePacket",
        "IndependentGeneralResearch" + "Manifest",
        "DefectTranslation" + "Packet",
        "ModificationAdmission" + "Packet",
        "OpaqueEvaluation" + "Receipt",
    )
    validator = PacketValidator.from_project_root(ROOT)
    for packet_type in legacy_packet_aliases:
        with pytest.raises(PacketSchemaError):
            validator.validate(packet_type, {})


def test_task_plane_protocol_imports_and_boundary_remain_available() -> None:
    assert {
        "TaskPacket",
        "ResultPacket",
        "ReviewPacket",
        "RevisionDecision",
        "ArtifactManifest",
    } < set(PACKET_TYPES)
    assert hasattr(packets_module, "PacketValidator")
    assert hasattr(orchestrator_module, "Orchestrator")
    assert config_module.AGENT_VERSION == AGENT_ID


def test_allowed_tests_have_no_human_message_or_retired_symbol_oracle() -> None:
    retired_literals = {
        "Admission" + "AuditBundle",
        "Evaluation" + "EvidenceArtifact",
        "sound_boundary_" + "r3" + "b",
        "Revision" + "3B",
    }
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        if path.name == "test_benchmarks.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                is_raises = (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "pytest"
                    and node.func.attr == "raises"
                )
                if is_raises:
                    assert all(keyword.arg != "match" for keyword in node.keywords)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in retired_literals
