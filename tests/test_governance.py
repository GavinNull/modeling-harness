from __future__ import annotations

import hashlib

import pytest

from modeling_harness.agent_body import (
    AGENT_ID,
    PACKAGE_VERSION,
    AgentBodyBoundaryError,
    AgentBodyControlAuthorizationV1,
    DirectUserExecutableInstructionV1,
    DirectUserInstruction,
    canonical_sha256,
)
from modeling_harness.governance import (
    DataBoundaryError,
    FeedbackFirewall,
    ProvenanceError,
    ReviewAccessError,
    ReviewEvidenceVault,
    TRUSTED_PROVENANCE_INGRESS_ID,
    UnsafeTranslationError,
    assess_immediate_safety_containment,
    bootstrap_source_provenance_control_plane,
    validate_artifact_provenance,
    validate_role_data_boundary,
)


def digest(index: int) -> str:
    return hashlib.sha256(index.to_bytes(4, "big")).hexdigest()


def source_manifest(
    index: int,
    *,
    source_class: str = "independent-general-research",
    parents: list[str] | None = None,
    real_task_derived: bool = False,
) -> dict:
    return {
        "schema_version": "1.0.0",
        "manifest_id": f"manifest-{index:03d}",
        "subject_id": f"subject-{index:03d}",
        "source_class": source_class,
        "source_content_sha256": digest(index),
        "parent_manifest_sha256s": list(parents or ()),
        "real_task_derived": real_task_derived,
        "issued_by": TRUSTED_PROVENANCE_INGRESS_ID,
        "issued_at": "2026-01-01T00:00:00Z",
    }


def direct_authorization() -> AgentBodyControlAuthorizationV1:
    body = {
        "schema_version": DirectUserExecutableInstructionV1.SCHEMA_VERSION,
        "instruction": (
            DirectUserInstruction.APPLY_REVIEWED_GENERIC_GOVERNANCE_CHANGE.value
        ),
        "governance_document_sha256": digest(20),
        "independent_review_sha256": digest(21),
        "agent_id": AGENT_ID,
        "package_version": PACKAGE_VERSION,
    }
    instruction = DirectUserExecutableInstructionV1.from_mapping(
        {**body, "instruction_sha256": canonical_sha256(body)}
    )
    return AgentBodyControlAuthorizationV1.from_direct_user(instruction)


def review_packet() -> dict:
    return {
        "schema_version": "1.0.0",
        "packet_id": "review-001",
        "reviewer_role_id": "mathematical_reviewer",
        "visibility": "main-agent-only",
    }


def test_provenance_control_plane_is_the_only_ledger_writer() -> None:
    control = bootstrap_source_provenance_control_plane()
    manifest = source_manifest(1)
    manifest_hash = control.registrar.register(manifest)
    assert control.ledger.get(manifest_hash).source_class == (
        "independent-general-research"
    )
    assert control.ledger.verify() == 1
    assert control.registrar.register(manifest) == manifest_hash
    with pytest.raises(ProvenanceError):
        type(control.ledger)(_bootstrap_authority=object())


def test_provenance_taint_is_monotonic_and_identity_is_immutable() -> None:
    control = bootstrap_source_provenance_control_plane()
    parent = control.registrar.register(
        source_manifest(
            2, source_class="public-real-task", real_task_derived=True
        )
    )
    with pytest.raises(ProvenanceError):
        control.registrar.register(
            source_manifest(3, parents=[parent], real_task_derived=False)
        )
    child = control.registrar.register(
        source_manifest(3, parents=[parent], real_task_derived=True)
    )
    assert control.ledger.get(child).real_task_derived is True

    rebound = source_manifest(4)
    rebound["subject_id"] = "subject-003"
    with pytest.raises(ProvenanceError):
        control.registrar.register(rebound)


def test_artifact_provenance_closes_sources_and_rejects_task_taint_for_agent_body() -> None:
    control = bootstrap_source_provenance_control_plane()
    source_hash = control.registrar.register(source_manifest(5))
    manifest = {
        "source_provenance_sha256s": [source_hash],
        "real_task_derived": False,
    }
    validate_artifact_provenance(
        manifest,
        provenance_ledger=control.ledger,
        require_authorized_agent_body_source=True,
    )
    with pytest.raises(ProvenanceError):
        validate_artifact_provenance(
            manifest,
            provenance_ledger=control.ledger,
            input_manifests=(
                {
                    "source_provenance_sha256s": [digest(100)],
                    "real_task_derived": False,
                },
            ),
        )

    real_hash = control.registrar.register(
        source_manifest(
            6, source_class="private-real-task", real_task_derived=True
        )
    )
    with pytest.raises(ProvenanceError):
        validate_artifact_provenance(
            {
                "source_provenance_sha256s": [real_hash],
                "real_task_derived": True,
            },
            provenance_ledger=control.ledger,
            require_authorized_agent_body_source=True,
        )


def test_raw_review_vault_is_main_only_and_not_a_builder_input() -> None:
    control = bootstrap_source_provenance_control_plane()
    vault = ReviewEvidenceVault(control.ledger)
    packet = review_packet()
    vault.store("mathematical_reviewer", packet)
    assert vault.read("main_agent", "review-001") == packet
    with pytest.raises(ReviewAccessError):
        vault.read("system_architect", "review-001")
    with pytest.raises(ReviewAccessError):
        vault.store("main_agent", packet)


def test_firewall_dispatch_accepts_only_exact_closed_authorization() -> None:
    control = bootstrap_source_provenance_control_plane()
    firewall = FeedbackFirewall(ReviewEvidenceVault(control.ledger))
    authorization = direct_authorization()
    assert firewall.dispatch_control_authorization(
        actor="main_agent",
        recipient_role_id="system_architect",
        authorization=authorization,
    ) is authorization
    with pytest.raises(UnsafeTranslationError):
        firewall.dispatch_control_authorization(
            actor="main_agent",
            recipient_role_id="system_architect",
            authorization=authorization.as_mapping(),
        )
    with pytest.raises(DataBoundaryError):
        firewall.dispatch_control_authorization(
            actor="main_agent",
            recipient_role_id="model_architect",
            authorization=authorization,
        )
    with pytest.raises(ReviewAccessError):
        firewall.dispatch_control_authorization(
            actor="system_architect",
            recipient_role_id="system_architect",
            authorization=authorization,
        )


@pytest.mark.parametrize(
    ("role_id", "role_kind", "purpose", "kinds"),
    (
        (
            "system_architect",
            "builder",
            "agent-core-modification",
            ("generic-core", "agent-body-control-authorization"),
        ),
        (
            "problem_definition_router",
            "author",
            "task-answer",
            ("generic-core", "task-text"),
        ),
        (
            "mathematical_reviewer",
            "reviewer",
            "evaluation",
            ("generic-core", "evaluation-rubric"),
        ),
    ),
)
def test_role_data_boundary_accepts_only_declared_positive_cases(
    role_id: str,
    role_kind: str,
    purpose: str,
    kinds: tuple[str, ...],
) -> None:
    validate_role_data_boundary(
        role_id=role_id,
        role_kind=role_kind,
        task_purpose=purpose,
        content_kinds=kinds,
    )


def test_role_data_boundary_rejects_evaluation_to_core_builder() -> None:
    with pytest.raises(DataBoundaryError):
        validate_role_data_boundary(
            role_id="system_architect",
            role_kind="builder",
            task_purpose="agent-core-modification",
            content_kinds=("generic-core", "raw-review"),
        )
    with pytest.raises(DataBoundaryError):
        validate_role_data_boundary(
            role_id="system_architect",
            role_kind="builder",
            task_purpose="agent-core-modification",
            content_kinds=("generic-core",),
        )


def test_containment_never_authorizes_capability_modification() -> None:
    decision = assess_immediate_safety_containment(
        severity="P0", defect_kind="hash-mismatch"
    )
    assert decision.quarantine is True
    assert decision.authorizes_capability_modification is False
    ordinary = assess_immediate_safety_containment(
        severity="P1", defect_kind="hash-mismatch"
    )
    assert ordinary.quarantine is False
    assert ordinary.authorizes_capability_modification is False
