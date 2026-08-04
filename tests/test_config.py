from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil

import pytest
import yaml

from modeling_harness.config import (
    AGENT_BODY_PROVENANCE_POLICY,
    AGENT_VERSION,
    POLICY_PROJECTIONS,
    SOUND_BOUNDARY_PROJECTION,
    ConfigError,
    validate_config,
)


ROOT = Path(__file__).parents[1]


def copy_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    for relative in ("workspaces/architect", "workspaces/evaluation", "workspaces/prompts"):
        shutil.copytree(ROOT / relative, project / relative)
    return project


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def write_yaml(path: Path, document: dict) -> None:
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def test_validate_config_success() -> None:
    report = validate_config(ROOT)
    assert report.schema_count == 12
    assert report.role_count == 16
    assert "main_agent" not in report.role_ids
    assert report.role_ids[0] == "system_architect"
    assert report.role_ids[-1] == "evidence_communication_reviewer"


def test_five_governance_projections_equal_the_canonical_constants() -> None:
    assert len(POLICY_PROJECTIONS) == 5
    for relative in POLICY_PROJECTIONS:
        document = read_yaml(ROOT / relative)
        assert document["agent_version"] == AGENT_VERSION
        assert document["agent_body_provenance_policy"] == (
            AGENT_BODY_PROVENANCE_POLICY
        )
        assert document["sound_boundary_r3e"] == SOUND_BOUNDARY_PROJECTION


def test_all_seventeen_prompts_share_one_exact_sound_boundary_block() -> None:
    paths = sorted((ROOT / "workspaces/prompts/system_prompts").glob("*.md"))
    marker = "Revision 3E sound construction boundary"
    blocks = []
    assert len(paths) == 17
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert text.count(marker) == 1, path.name
        blocks.append(text[text.index(marker) :].strip())
    assert len(set(blocks)) == 1
    block = blocks[0]
    for required in (
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
        "no release-to-construction transition",
    ):
        assert required in block


def test_state_machine_contains_disjoint_closed_subprotocols() -> None:
    state = read_yaml(ROOT / "workspaces/architect/state_machine.yaml")
    protocol = state["agent_body_modification_subprotocol"]
    construction = protocol["construction_machine"]
    release = protocol["release_gate_machine"]
    assert construction["evaluation_parent_edges"] == []
    assert construction["task_derived_parent_edges"] == []
    assert construction["generic_parent_arrays"] == "forbidden"
    assert release["input_carrier"] == "OpaqueEvaluationReceiptV1"
    assert release["construction_authorization_edges"] == []
    assert release["builder_feedback_fields"] == []
    assert set(release["terminal_states"]) == {"RELEASED", "RELEASE_REJECTED"}


def test_registry_main_agent_permissions_are_exact() -> None:
    registry = read_yaml(ROOT / "workspaces/architect/role_registry.yaml")
    main = next(role for role in registry["roles"] if role["id"] == "main_agent")
    assert main["role_boundary"]["allowed"] == [
        "dispatch",
        "write_prompts",
        "review",
        "approve",
        "reject",
    ]
    assert main["writes"] == [
        "dispatch_records",
        "task_packets",
        "prompt_registry",
        "review_decisions",
        "approval_decisions",
        "rejection_decisions",
    ]
    forbidden = set(main["role_boundary"]["forbidden"])
    assert {
        "construct_task_artifacts",
        "implement_solutions",
        "solve_tasks",
        "create_direct_user_instruction",
        "create_dtp",
        "create_map",
        "create_agent_body_candidate",
        "create_build_artifacts",
        "create_solution_artifacts",
    } <= forbidden


def test_config_rejects_one_projection_diverging_from_canonical(
    tmp_path: Path,
) -> None:
    project = copy_project(tmp_path)
    path = project / "workspaces/evaluation/capability_taxonomy.yaml"
    document = read_yaml(path)
    document["sound_boundary_r3e"] = deepcopy(SOUND_BOUNDARY_PROJECTION)
    document["sound_boundary_r3e"]["release_to_construction_edges"] = [
        ["RELEASE_REJECTED", "PROPOSED"]
    ]
    write_yaml(path, document)
    with pytest.raises(ConfigError):
        validate_config(project)


def test_config_rejects_unknown_schema_file(tmp_path: Path) -> None:
    project = copy_project(tmp_path)
    (project / "workspaces/architect/schemas/X.json").write_text(
        "{}", encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        validate_config(project)
