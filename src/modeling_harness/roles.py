"""Cross-validation for architect roles and their system prompts."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping

from modeling_harness.config import (
    AGENT_BODY_PROVENANCE_POLICY,
    AGENT_VERSION,
    ConfigError,
    load_yaml,
)
from modeling_harness.agent_body import (
    MAIN_AGENT_ACTIONS,
    MAIN_AGENT_OUTPUTS,
    MAIN_AGENT_WRITES,
)


MAIN_AGENT_ID = "main_agent"
ROLE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
EXPECTED_SUBAGENT_COUNT = 16
EXPECTED_EFFECTIVE_PROMPT_COUNT = 17
ABSOLUTE_TEST_ONLY_MARKERS = (
    "Revision 3E sound construction boundary",
    "Agent-body authorization sources are exactly DIRECT_USER and INDEPENDENT_GENERAL_RESEARCH",
    "DirectUserExecutableInstructionV1 has one executable instruction enum",
    "IndependentGeneralResearchManifestV1, DTPV1 and MAPV1 carry only",
    "AgentBodyControlAuthorizationV1",
    "AgentBodyCandidateSourceV1",
    "AgentBodyProposalV1",
    "AgentBodyAdmissionV1",
    "AgentBodyMergeV1",
    "AgentBodyProvenanceV1",
    "Evaluation and verification taint is permanent",
    "Only OpaqueEvaluationReceiptV1 may enter the independent release-gate ledger",
    "Release evidence is never an Agent-body authorization",
    "There are no release-to-construction transitions",
    "Agent-body rejection is terminal; it cannot authorize rollback/report",
    "never trigger, justify, prove or optimize an Agent-body change",
    "enforcement uses closed enums, exact schemas, positive carrier types, hashes, unreachable states and exact permission sets",
    "main_agent actions are exactly dispatch, write_prompts, review, approve and reject",
    "main_agent outputs are exactly dispatch_records, task_packets, prompt_registry, review_decisions, approval_decisions and rejection_decisions",
    "Authorization permissions are exactly CONSTRUCT_CANDIDATE, PROPOSE_CANDIDATE, ADMIT_CANDIDATE and PROMOTE_CANDIDATE",
    "ConstructionLineageLedger, OpaqueEvaluationReceiptLedger and ReleaseGateLedger are nominally disjoint",
    "Authorization, admission and promotion never inspect natural language or task/evaluation content",
    "main_agent never creates or emits DirectUserExecutableInstructionV1, DTPV1, MAPV1",
)
class RoleRegistryError(ConfigError):
    """Raised when role registries or prompt references disagree."""


@dataclass(frozen=True)
class RoleRegistry:
    """Validated controller/subagent prompt paths and canonical subagent IDs."""

    canonical_ids: tuple[str, ...]
    prompt_files: Mapping[str, Path]

    def prompt_for(self, role_id: str) -> Path:
        try:
            return self.prompt_files[role_id]
        except KeyError as exc:
            raise KeyError(f"unknown registered prompt role {role_id!r}") from exc


def _role_entries(document: Any, source: Path) -> list[dict[str, Any]]:
    if not isinstance(document, dict):
        raise RoleRegistryError(f"registry must be a YAML mapping: {source}")
    entries = document.get("roles")
    if not isinstance(entries, list):
        raise RoleRegistryError(f"registry 'roles' must be a list: {source}")
    if not all(isinstance(entry, dict) for entry in entries):
        raise RoleRegistryError(f"every role entry must be a mapping: {source}")
    return entries


def _role_ids(entries: list[dict[str, Any]], source: Path) -> list[str]:
    role_ids: list[str] = []
    for index, entry in enumerate(entries):
        role_id = entry.get("id")
        if not isinstance(role_id, str) or not ROLE_ID_PATTERN.fullmatch(role_id):
            raise RoleRegistryError(
                f"invalid role id at roles[{index}] in {source}: {role_id!r}"
            )
        role_ids.append(role_id)

    duplicates = sorted(
        role_id for role_id, count in Counter(role_ids).items() if count > 1
    )
    if duplicates:
        raise RoleRegistryError(
            f"duplicate role IDs in {source}: {', '.join(duplicates)}"
        )
    return role_ids


def canonical_subagent_ids(architect_registry: Any, source: Path) -> tuple[str, ...]:
    """Extract architect role IDs in canonical order, excluding main_agent."""

    entries = _role_entries(architect_registry, source)
    role_ids = _role_ids(entries, source)
    if MAIN_AGENT_ID not in role_ids:
        raise RoleRegistryError(
            f"architect registry must contain the controller role {MAIN_AGENT_ID!r}"
        )
    subagents = tuple(role_id for role_id in role_ids if role_id != MAIN_AGENT_ID)
    if not subagents:
        raise RoleRegistryError("architect registry contains no subagent roles")
    if len(subagents) != EXPECTED_SUBAGENT_COUNT:
        raise RoleRegistryError(
            "architect registry must contain exactly "
            f"{EXPECTED_SUBAGENT_COUNT} subagent roles"
        )
    return subagents


def _resolve_prompt_path(prompt_root: Path, reference: Any, role_id: str) -> Path:
    if not isinstance(reference, str) or not reference.strip():
        raise RoleRegistryError(f"role {role_id!r} has no valid prompt_file")

    relative = Path(reference)
    if relative.is_absolute():
        raise RoleRegistryError(
            f"role {role_id!r} prompt_file must be relative: {reference!r}"
        )

    root = prompt_root.resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise RoleRegistryError(
            f"role {role_id!r} prompt_file escapes the prompt registry: "
            f"{reference!r}"
        )
    if not resolved.is_file():
        raise RoleRegistryError(
            f"prompt file for role {role_id!r} does not exist: {resolved}"
        )
    return resolved


def load_role_registry(
    architect_registry_path: str | Path,
    prompt_registry_path: str | Path,
) -> RoleRegistry:
    """Validate canonical subagent IDs and all corresponding prompt files."""

    architect_source = Path(architect_registry_path)
    prompt_source = Path(prompt_registry_path)

    architect_document = load_yaml(architect_source)
    prompt_document = load_yaml(prompt_source)
    for source, document in (
        (architect_source, architect_document),
        (prompt_source, prompt_document),
    ):
        if not isinstance(document, dict):
            raise RoleRegistryError(f"registry must be a YAML mapping: {source}")
        if document.get("agent_version") != AGENT_VERSION:
            raise RoleRegistryError(f"{source} must target {AGENT_VERSION}")
        if document.get("agent_body_provenance_policy") != (
            AGENT_BODY_PROVENANCE_POLICY
        ):
            raise RoleRegistryError(
                f"{source} does not enforce the canonical Agent-body "
                "source-provenance policy"
            )
    canonical_ids = canonical_subagent_ids(architect_document, architect_source)
    architect_entries = _role_entries(architect_document, architect_source)
    architect_main = next(
        entry for entry in architect_entries if entry.get("id") == MAIN_AGENT_ID
    )
    boundary = architect_main.get("role_boundary")
    if not isinstance(boundary, dict):
        raise RoleRegistryError("main_agent requires a closed role_boundary")
    expected_actions = {item.value for item in MAIN_AGENT_ACTIONS}
    if set(boundary.get("allowed", ())) != expected_actions:
        raise RoleRegistryError("main_agent actions must equal the canonical set")
    if set(architect_main.get("writes", ())) != set(MAIN_AGENT_WRITES):
        raise RoleRegistryError("main_agent writes must equal the canonical set")
    if set(boundary.get("outputs", ())) != set(MAIN_AGENT_OUTPUTS):
        raise RoleRegistryError("main_agent outputs must equal the canonical set")
    required_forbidden = {
        "create_direct_user_instruction",
        "create_dtp",
        "create_map",
        "create_agent_body_candidate",
        "create_build_artifacts",
        "create_solution_artifacts",
    }
    if not required_forbidden <= set(boundary.get("forbidden", ())):
        raise RoleRegistryError("main_agent forbidden outputs are incomplete")

    prompt_entries = _role_entries(prompt_document, prompt_source)
    prompt_ids = _role_ids(prompt_entries, prompt_source)

    canonical_set = set(canonical_ids) | {MAIN_AGENT_ID}
    prompt_set = set(prompt_ids)
    missing = sorted(canonical_set - prompt_set)
    unexpected = sorted(prompt_set - canonical_set)
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append(f"missing prompt roles: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected prompt roles: {', '.join(unexpected)}")
        raise RoleRegistryError(
            "prompt role IDs must exactly match main_agent and canonical subagent IDs "
            f"({'; '.join(details)})"
        )

    prompt_root = prompt_source.parent
    entry_by_id = {entry["id"]: entry for entry in prompt_entries}
    prompt_main_outputs = set(entry_by_id[MAIN_AGENT_ID].get("required_outputs", ()))
    if prompt_main_outputs != set(MAIN_AGENT_WRITES):
        raise RoleRegistryError(
            "prompt projection main_agent outputs must equal canonical writes"
        )
    prompt_files = {
        role_id: _resolve_prompt_path(
            prompt_root,
            entry_by_id[role_id].get("prompt_file"),
            role_id,
        )
        for role_id in (MAIN_AGENT_ID, *canonical_ids)
    }
    all_prompt_files = {
        path.resolve()
        for path in (prompt_root / "system_prompts").glob("*.md")
        if path.is_file()
    }
    if len(all_prompt_files) != EXPECTED_EFFECTIVE_PROMPT_COUNT:
        raise RoleRegistryError(
            "prompt registry must contain exactly "
            f"{EXPECTED_EFFECTIVE_PROMPT_COUNT} effective system prompts"
        )
    if set(prompt_files.values()) != all_prompt_files:
        raise RoleRegistryError(
            "the 17 effective prompts must be exactly the 17 registered role prompts"
        )
    for role_id, prompt_path in prompt_files.items():
        try:
            text = prompt_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RoleRegistryError(
                f"could not read prompt for role {role_id!r}: {exc}"
            ) from exc
        missing_markers = [
            marker for marker in ABSOLUTE_TEST_ONLY_MARKERS if marker not in text
        ]
        if missing_markers:
            raise RoleRegistryError(
                f"prompt for role {role_id!r} omits absolute test-only policy: "
                + ", ".join(missing_markers)
            )
    return RoleRegistry(
        canonical_ids=canonical_ids,
        prompt_files=MappingProxyType(prompt_files),
    )
