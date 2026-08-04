from __future__ import annotations

from pathlib import Path

import pytest

from modeling_harness.packets import (
    PACKET_TYPES,
    PacketSchemaError,
    PacketValidator,
)


ROOT = Path(__file__).parents[1]
TASK_PLANE_PACKET_TYPES = (
    "TaskPacket",
    "ResultPacket",
    "ReviewPacket",
    "RevisionDecision",
    "ArtifactManifest",
    "RoleCharter",
    "SourceProvenanceManifest",
)


def validator() -> PacketValidator:
    return PacketValidator.from_project_root(ROOT)


def test_packet_registry_is_exactly_the_task_plane_surface() -> None:
    assert PACKET_TYPES == TASK_PLANE_PACKET_TYPES
    check = validator()
    assert set(check.schemas) == set(TASK_PLANE_PACKET_TYPES) | {
        "DirectUserExecutableInstructionV1",
        "IndependentGeneralResearchManifestV1",
        "DTPV1",
        "MAPV1",
        "OpaqueEvaluationReceiptV1",
    }
    for packet_type in TASK_PLANE_PACKET_TYPES:
        assert check.schemas[packet_type]["additionalProperties"] is False


def test_unknown_and_retired_packet_types_are_unreachable() -> None:
    check = validator()
    retired_aliases = (
        "DirectUser" + "GovernancePacket",
        "IndependentGeneralResearch" + "Manifest",
        "DefectTranslation" + "Packet",
        "ModificationAdmission" + "Packet",
        "OpaqueEvaluation" + "Receipt",
    )
    for packet_type in ("UnknownPacket", *retired_aliases):
        with pytest.raises(PacketSchemaError):
            check.validate(packet_type, {})
