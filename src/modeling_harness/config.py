"""Project discovery and top-level configuration validation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import yaml


PROJECT_MARKERS = (
    Path("workspaces/architect/role_registry.yaml"),
    Path("workspaces/architect/schemas"),
    Path("workspaces/prompts/roles.yaml"),
    Path("workspaces/prompts/system_prompts"),
)
AGENT_VERSION = "generic-agent-v2.5"
AGENT_BODY_PROVENANCE_POLICY = {
    "boundary_revision": "sound-by-construction-r3e",
    "agent_identity": "generic-agent-v2.5",
    "package_version": "2.5.0",
    "authorization_sources": [
        "DIRECT_USER",
        "INDEPENDENT_GENERAL_RESEARCH",
    ],
    "direct_user_carrier": "DirectUserExecutableInstructionV1",
    "direct_user_instruction_enum": [
        "APPLY_REVIEWED_GENERIC_GOVERNANCE_CHANGE",
    ],
    "research_carrier": "IndependentGeneralResearchManifestV1",
    "research_manifest_payload": "hashes-and-closed-enums-only",
    "dtp_carrier": "DTPV1",
    "map_carrier": "MAPV1",
    "authorization_permissions": [
        "CONSTRUCT_CANDIDATE",
        "PROPOSE_CANDIDATE",
        "ADMIT_CANDIDATE",
        "PROMOTE_CANDIDATE",
    ],
    "closed_carriers_additional_properties": False,
    "free_text_in_construction_control": False,
    "candidate_source_carrier": "AgentBodyCandidateSourceV1",
    "proposal_carrier": "AgentBodyProposalV1",
    "admission_carrier": "AgentBodyAdmissionV1",
    "merge_carrier": "AgentBodyMergeV1",
    "provenance_carrier": "AgentBodyProvenanceV1",
    "construction_ledger_entry": "ConstructionLineageEntry",
    "construction_ledger": "ConstructionLineageLedger",
    "direct_user_construction_lineage": [
        "DirectUserExecutableInstructionV1",
        "AgentBodyControlAuthorizationV1",
        "AgentBodyCandidateSourceV1",
        "AgentBodyProposalV1",
        "AgentBodyAdmissionV1",
        "AgentBodyMergeV1",
    ],
    "research_construction_lineage": [
        "IndependentGeneralResearchManifestV1",
        "DTPV1",
        "MAPV1",
        "AgentBodyControlAuthorizationV1",
        "AgentBodyCandidateSourceV1",
        "AgentBodyProposalV1",
        "AgentBodyAdmissionV1",
        "AgentBodyMergeV1",
    ],
    "evaluation_taint": "permanent",
    "evaluation_carrier": "OpaqueEvaluationReceiptV1",
    "opaque_evaluation_ledger_entry": "OpaqueEvaluationReceiptEntry",
    "opaque_evaluation_ledger": "OpaqueEvaluationReceiptLedger",
    "evaluation_destination": "independent-release-gate-ledger-only",
    "release_ledger": "ReleaseGateLedger",
    "release_terminal_states": ["RELEASED", "RELEASE_REJECTED"],
    "release_rejection_actions": [],
    "release_to_construction_edges": [],
    "evaluation_construction_parent_allowed": False,
    "task_derived_construction_parent_allowed": False,
    "content_independent_tests": "verification-only-accept-or-reject-candidate",
    "verification_failure_successor_change_allowed": False,
    "content_or_tool_strategy_branching_allowed": False,
    "authorization_admission_promotion_content_inspection": False,
    "main_agent_actions": [
        "dispatch",
        "write_prompts",
        "review",
        "approve",
        "reject",
    ],
    "main_agent_writes": [
        "dispatch_records",
        "task_packets",
        "prompt_registry",
        "review_decisions",
        "approval_decisions",
        "rejection_decisions",
    ],
    "main_agent_outputs": [
        "dispatch_records",
        "task_packets",
        "prompt_registry",
        "review_decisions",
        "approval_decisions",
        "rejection_decisions",
    ],
    "main_agent_forbidden_outputs": [
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
    ],
    "effective_prompt_count": 17,
    "subagent_profile_count": 16,
}
SOUND_BOUNDARY_PROJECTION = {
    "control_sources": ["DIRECT_USER", "INDEPENDENT_GENERAL_RESEARCH"],
    "control_carriers": [
        "DirectUserExecutableInstructionV1",
        "IndependentGeneralResearchManifestV1",
        "DTPV1",
        "MAPV1",
        "AgentBodyControlAuthorizationV1",
    ],
    "authorization_permissions": [
        "CONSTRUCT_CANDIDATE",
        "PROPOSE_CANDIDATE",
        "ADMIT_CANDIDATE",
        "PROMOTE_CANDIDATE",
    ],
    "construction_carriers": [
        "AgentBodyCandidateSourceV1",
        "AgentBodyProposalV1",
        "AgentBodyAdmissionV1",
        "AgentBodyMergeV1",
        "AgentBodyProvenanceV1",
    ],
    "construction_ledger": "ConstructionLineageLedger",
    "opaque_evaluation_ledger": "OpaqueEvaluationReceiptLedger",
    "release_ledger": "ReleaseGateLedger",
    "release_gate_input": "OpaqueEvaluationReceiptV1",
    "release_gate_outputs": [
        "RELEASED",
        "RELEASE_REJECTED",
    ],
    "release_to_construction_edges": [],
    "main_agent_actions": [
        "dispatch",
        "write_prompts",
        "review",
        "approve",
        "reject",
    ],
    "main_agent_writes": [
        "dispatch_records",
        "task_packets",
        "prompt_registry",
        "review_decisions",
        "approval_decisions",
        "rejection_decisions",
    ],
    "main_agent_outputs": [
        "dispatch_records",
        "task_packets",
        "prompt_registry",
        "review_decisions",
        "approval_decisions",
        "rejection_decisions",
    ],
    "main_agent_forbidden_outputs": [
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
    ],
}
POLICY_PROJECTIONS = (
    Path("workspaces/architect/role_registry.yaml"),
    Path("workspaces/architect/state_machine.yaml"),
    Path("workspaces/evaluation/benchmark_protocol.yaml"),
    Path("workspaces/evaluation/capability_taxonomy.yaml"),
    Path("workspaces/prompts/roles.yaml"),
)


class ConfigError(ValueError):
    """Base exception for invalid or unreadable harness configuration."""


@dataclass(frozen=True)
class ValidationReport:
    """Summary returned after all Stage 1 configuration checks pass."""

    project_root: Path
    schema_names: tuple[str, ...]
    role_ids: tuple[str, ...]

    @property
    def schema_count(self) -> int:
        return len(self.schema_names)

    @property
    def role_count(self) -> int:
        return len(self.role_ids)


def _is_project_root(path: Path) -> bool:
    return all((path / marker).exists() for marker in PROJECT_MARKERS)


def locate_project_root(start: str | Path | None = None) -> Path:
    """Find the nearest ancestor containing the approved configuration layout."""

    candidate = Path.cwd() if start is None else Path(start)
    candidate = candidate.expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent

    for directory in (candidate, *candidate.parents):
        if _is_project_root(directory):
            return directory

    raise ConfigError(
        f"could not locate project root from {candidate}; "
        "expected architect and prompt registries under workspaces/"
    )


def load_yaml(path: str | Path) -> Any:
    """Load one UTF-8 YAML document with configuration-focused errors."""

    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"could not read UTF-8 YAML {source}: {exc}") from exc

    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {source}: {exc}") from exc


def load_json(path: str | Path) -> Any:
    """Load one UTF-8 JSON document with configuration-focused errors."""

    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"could not read UTF-8 JSON {source}: {exc}") from exc

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"invalid JSON in {source} at line {exc.lineno}, "
            f"column {exc.colno}: {exc.msg}"
        ) from exc


def validate_config(project_root: str | Path | None = None) -> ValidationReport:
    """Validate all Stage 1 schemas, role IDs, and prompt references."""

    root = locate_project_root(project_root)

    # Imported here to keep the low-level loaders free of module cycles.
    from modeling_harness.roles import load_role_registry
    from modeling_harness.schemas import load_schema_registry

    schemas = load_schema_registry(root / "workspaces/architect/schemas")
    roles = load_role_registry(
        root / "workspaces/architect/role_registry.yaml",
        root / "workspaces/prompts/roles.yaml",
    )
    for relative in POLICY_PROJECTIONS:
        document = load_yaml(root / relative)
        if not isinstance(document, dict):
            raise ConfigError(f"policy projection must be a YAML mapping: {relative}")
        if document.get("agent_version") != AGENT_VERSION:
            raise ConfigError(
                f"{relative} must target {AGENT_VERSION}"
            )
        if document.get("agent_body_provenance_policy") != (
            AGENT_BODY_PROVENANCE_POLICY
        ):
            raise ConfigError(
                f"{relative} does not match the canonical Agent-body "
                "source-provenance policy"
            )
        if document.get("sound_boundary_r3e") != SOUND_BOUNDARY_PROJECTION:
            raise ConfigError(
                f"{relative} does not match the canonical sound boundary projection"
            )
    return ValidationReport(
        project_root=root,
        schema_names=tuple(schemas),
        role_ids=roles.canonical_ids,
    )
