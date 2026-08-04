from __future__ import annotations

import hashlib
import inspect

import pytest

from modeling_harness.agent_body import (
    AGENT_ID,
    PACKAGE_VERSION,
    AdmissionDecision,
    AgentBodyAdmissionV1,
    AgentBodyCandidateSourceV1,
    AgentBodyControlAuthorizationV1,
    AgentBodyMergeV1,
    AgentBodyProposalV1,
    BuilderRole,
    DirectUserExecutableInstructionV1,
    DirectUserInstruction,
    EvaluationDecision,
    OpaqueEvaluationReceiptLedger,
    OpaqueEvaluationReceiptV1,
    ReleaseGateLedger,
    ReleaseStatus,
    canonical_sha256,
)
from modeling_harness.orchestrator import Orchestrator, ReleaseRejected


def digest(index: int) -> str:
    return hashlib.sha256(index.to_bytes(4, "big")).hexdigest()


def merge() -> AgentBodyMergeV1:
    instruction_body = {
        "schema_version": DirectUserExecutableInstructionV1.SCHEMA_VERSION,
        "instruction": (
            DirectUserInstruction.APPLY_REVIEWED_GENERIC_GOVERNANCE_CHANGE.value
        ),
        "governance_document_sha256": digest(1),
        "independent_review_sha256": digest(2),
        "agent_id": AGENT_ID,
        "package_version": PACKAGE_VERSION,
    }
    instruction = DirectUserExecutableInstructionV1.from_mapping(
        {
            **instruction_body,
            "instruction_sha256": canonical_sha256(instruction_body),
        }
    )
    authorization = AgentBodyControlAuthorizationV1.from_direct_user(instruction)
    candidate_sha256 = digest(3)
    source = AgentBodyCandidateSourceV1.bind(
        authorization,
        candidate_sha256=candidate_sha256,
        parent_agent_body_sha256=digest(4),
        builder_role=BuilderRole.SYSTEM_ARCHITECT,
    )
    proposal = AgentBodyProposalV1.bind(source, authorization)
    admission = AgentBodyAdmissionV1.decide(
        proposal, authorization, AdmissionDecision.ADMIT
    )
    return AgentBodyMergeV1.merge(
        admission,
        authorization,
        resulting_agent_body_sha256=candidate_sha256,
    )


def receipt(
    merge_packet: AgentBodyMergeV1,
    decision: EvaluationDecision = EvaluationDecision.PASS,
) -> OpaqueEvaluationReceiptV1:
    body = {
        "schema_version": OpaqueEvaluationReceiptV1.SCHEMA_VERSION,
        "agent_id": AGENT_ID,
        "package_version": PACKAGE_VERSION,
        "candidate_sha256": merge_packet.candidate_sha256,
        "verification_plan_sha256": digest(6),
        "decision": decision.value,
    }
    return OpaqueEvaluationReceiptV1.from_mapping(
        {**body, "opaque_receipt_sha256": canonical_sha256(body)}
    )


def release_only_orchestrator() -> Orchestrator:
    orchestrator = object.__new__(Orchestrator)
    orchestrator._opaque_evaluation_receipt_ledger = (
        OpaqueEvaluationReceiptLedger()
    )
    orchestrator._agent_body_release_ledger = ReleaseGateLedger()
    return orchestrator


def test_release_gate_public_interface_is_exact_and_typed() -> None:
    parameters = inspect.signature(
        Orchestrator.release_agent_body_candidate
    ).parameters
    assert tuple(parameters) == ("self", "actor", "merge", "receipt")
    assert parameters["merge"].annotation == "AgentBodyMergeV1"
    assert parameters["receipt"].annotation == "OpaqueEvaluationReceiptV1"
    dispatch = inspect.signature(Orchestrator.dispatch).parameters
    assert dispatch["control_authorization"].annotation == (
        "AgentBodyControlAuthorizationV1 | None"
    )


@pytest.mark.parametrize(
    ("decision", "status"),
    (
        (EvaluationDecision.PASS, ReleaseStatus.RELEASED),
        (EvaluationDecision.REJECT, ReleaseStatus.RELEASE_REJECTED),
    ),
)
def test_release_gate_has_only_two_terminal_outcomes(
    decision: EvaluationDecision, status: ReleaseStatus
) -> None:
    orchestrator = release_only_orchestrator()
    merge_packet = merge()
    disposition = orchestrator.release_agent_body_candidate(
        actor="release_gate",
        merge=merge_packet,
        receipt=receipt(merge_packet, decision),
    )
    assert disposition.status is status
    assert set(disposition.__dict__) == {
        "candidate_sha256",
        "verification_plan_sha256",
        "opaque_receipt_sha256",
        "status",
    }


def test_release_gate_rejects_actor_candidate_and_nominal_substitution() -> None:
    orchestrator = release_only_orchestrator()
    merge_packet = merge()
    release_receipt = receipt(merge_packet)
    with pytest.raises(ReleaseRejected):
        orchestrator.release_agent_body_candidate(
            actor="main_agent",
            merge=merge_packet,
            receipt=release_receipt,
        )
    body = {
        "schema_version": OpaqueEvaluationReceiptV1.SCHEMA_VERSION,
        "agent_id": AGENT_ID,
        "package_version": PACKAGE_VERSION,
        "candidate_sha256": digest(99),
        "verification_plan_sha256": digest(6),
        "decision": EvaluationDecision.PASS.value,
    }
    wrong_candidate = OpaqueEvaluationReceiptV1.from_mapping(
        {**body, "opaque_receipt_sha256": canonical_sha256(body)}
    )
    with pytest.raises(ReleaseRejected):
        orchestrator.release_agent_body_candidate(
            actor="release_gate",
            merge=merge_packet,
            receipt=wrong_candidate,
        )
    with pytest.raises(ReleaseRejected):
        orchestrator.release_agent_body_candidate(
            actor="release_gate",
            merge={},  # type: ignore[arg-type]
            receipt=release_receipt,
        )


def test_release_gate_rejects_duplicate_terminal_decision() -> None:
    orchestrator = release_only_orchestrator()
    merge_packet = merge()
    release_receipt = receipt(merge_packet)
    orchestrator.release_agent_body_candidate(
        actor="release_gate",
        merge=merge_packet,
        receipt=release_receipt,
    )
    with pytest.raises(ReleaseRejected):
        orchestrator.release_agent_body_candidate(
            actor="release_gate",
            merge=merge_packet,
            receipt=release_receipt,
        )
