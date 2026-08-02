"""Validation for the project-scoped Codex role adapter.

The adapter is intentionally a control-plane integration. Codex profiles are
read-only and may only route or review work; task artifacts continue to be
created through the isolated runtime described by the role registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any, Mapping

from modeling_harness.config import ConfigError, locate_project_root
from modeling_harness.roles import load_role_registry


class CodexAdapterError(ConfigError):
    """Raised when Codex profiles bypass the harness governance contract."""


@dataclass(frozen=True)
class CodexAdapterReport:
    """Summary of a validated project-scoped Codex adapter."""

    project_root: Path
    profile_names: tuple[str, ...]
    max_concurrent_threads: int

    @property
    def profile_count(self) -> int:
        return len(self.profile_names)


def _load_toml(path: Path) -> Mapping[str, Any]:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise CodexAdapterError(f"could not read TOML {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise CodexAdapterError(f"TOML document must be a mapping: {path}")
    return document


def _required_string(document: Mapping[str, Any], key: str, path: Path) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CodexAdapterError(f"{path.name} requires a non-empty {key!r}")
    return value


def _validate_codex_worker_image(root: Path) -> None:
    """Verify the checked-in image contract without claiming it was built."""

    image_root = root / "containers" / "codex-agent"
    dockerfile = image_root / "Dockerfile"
    worker = image_root / "codex_worker.py"
    if not dockerfile.is_file() or not worker.is_file():
        raise CodexAdapterError("bundled Codex worker image files are missing")
    try:
        dockerfile_text = dockerfile.read_text(encoding="utf-8")
        worker_text = worker.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CodexAdapterError(f"could not read bundled Codex worker image: {exc}") from exc
    docker_markers = (
        "ARG CODEX_CLI_VERSION",
        "@openai/codex@${CODEX_CLI_VERSION}",
        'ENTRYPOINT ["python3", "/opt/modeling-harness/codex_worker.py"]',
    )
    missing = [marker for marker in docker_markers if marker not in dockerfile_text]
    if missing:
        raise CodexAdapterError(
            "bundled Codex Dockerfile omits required contract: " + ", ".join(missing)
        )
    worker_markers = (
        "--ephemeral",
        "--json",
        "--ignore-user-config",
        "task-specific optimization rules",
        "Agent-body authorization sources are exactly DIRECT_USER and INDEPENDENT_GENERAL_RESEARCH",
        "DirectUserExecutableInstructionV1",
        "IndependentGeneralResearchManifestV1, DTPV1 and MAPV1",
        "OpaqueEvaluationReceiptV1",
        "release-to-construction transitions",
        "main_agent actions are exactly dispatch, write_prompts, review, approve and reject",
        "main_agent writes and outputs are exactly dispatch_records, task_packets, prompt_registry, review_decisions, approval_decisions and rejection_decisions",
        "Authorization permissions are exactly CONSTRUCT_CANDIDATE, PROPOSE_CANDIDATE, ADMIT_CANDIDATE and PROMOTE_CANDIDATE",
        "ConstructionLineageLedger, OpaqueEvaluationReceiptLedger and ReleaseGateLedger use disjoint nominal entries, stores and states",
        "Authorization, admission and promotion inspect only nominal carriers",
        "main_agent never creates or emits",
        "hashlib.sha256(charter.read_bytes()).hexdigest()",
    )
    missing = [marker for marker in worker_markers if marker not in worker_text]
    if missing:
        raise CodexAdapterError(
            "bundled Codex worker omits required governance: " + ", ".join(missing)
        )


def validate_codex_adapter(
    project_root: str | Path | None = None,
) -> CodexAdapterReport:
    """Validate that every canonical role has one safe Codex profile.

    The checks deliberately reject a profile that can write the shared project
    directory. This keeps the project's star topology meaningful even when
    roles are launched from the Codex UI.
    """

    root = locate_project_root(project_root)
    _validate_codex_worker_image(root)
    codex_root = root / ".codex"
    config_path = codex_root / "config.toml"
    profiles_root = codex_root / "agents"
    if not config_path.is_file():
        raise CodexAdapterError(f"Codex project config is missing: {config_path}")
    if not profiles_root.is_dir():
        raise CodexAdapterError(f"Codex profile directory is missing: {profiles_root}")

    config = _load_toml(config_path)
    agents = config.get("agents")
    if not isinstance(agents, dict):
        raise CodexAdapterError(".codex/config.toml requires an [agents] table")
    max_threads = agents.get("max_concurrent_threads_per_session")
    if isinstance(max_threads, bool) or not isinstance(max_threads, int) or max_threads < 1:
        raise CodexAdapterError(
            "[agents].max_concurrent_threads_per_session must be a positive integer"
        )

    registry = load_role_registry(
        root / "workspaces/architect/role_registry.yaml",
        root / "workspaces/prompts/roles.yaml",
    )
    expected = set(registry.canonical_ids)
    profile_paths = {path.stem: path for path in profiles_root.glob("*.toml")}
    found = set(profile_paths)
    missing = sorted(expected - found)
    unexpected = sorted(found - expected)
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append("missing profiles: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected profiles: " + ", ".join(unexpected))
        raise CodexAdapterError(
            "Codex profiles must match canonical roles (" + "; ".join(details) + ")"
        )

    for role_id in registry.canonical_ids:
        path = profile_paths[role_id]
        profile = _load_toml(path)
        name = _required_string(profile, "name", path)
        _required_string(profile, "description", path)
        instructions = _required_string(profile, "developer_instructions", path)
        if name != role_id:
            raise CodexAdapterError(
                f"{path.name} declares name {name!r}; expected {role_id!r}"
            )
        if profile.get("sandbox_mode") != "read-only":
            raise CodexAdapterError(
                f"{path.name} must use sandbox_mode = 'read-only'; "
                "task writes belong in an isolated runtime workspace"
            )
        prompt_path = registry.prompt_for(role_id).relative_to(root).as_posix()
        required_markers = (
            prompt_path,
            "main_agent",
            "task-specific",
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
            "OpaqueEvaluationReceiptV1 enters only the independent release-gate ledger",
            "Release evidence is never authorization, DTP/MAP input, proposal input, admission input, merge parent or source-provenance parent",
            "there are no release-to-construction transitions",
            "rejection is terminal and cannot authorize rollback/report",
            "main_agent actions are exactly dispatch, write_prompts, review, approve and reject",
            "main_agent outputs are exactly dispatch_records, task_packets, prompt_registry, review_decisions, approval_decisions and rejection_decisions",
            "Authorization permissions are exactly CONSTRUCT_CANDIDATE, PROPOSE_CANDIDATE, ADMIT_CANDIDATE and PROMOTE_CANDIDATE",
            "ConstructionLineageLedger, OpaqueEvaluationReceiptLedger and ReleaseGateLedger are nominally disjoint",
            "Authorization, admission and promotion never inspect natural language or task/evaluation content",
            "main_agent never creates or emits DirectUserExecutableInstructionV1, DTPV1, MAPV1",
        )
        absent = [marker for marker in required_markers if marker not in instructions]
        if absent:
            raise CodexAdapterError(
                f"{path.name} omits required governance markers: {', '.join(absent)}"
            )
    return CodexAdapterReport(
        project_root=root,
        profile_names=registry.canonical_ids,
        max_concurrent_threads=max_threads,
    )


def codex_agent_entrypoint(
    role_id: str,
    *,
    project_root: str | Path | None = None,
) -> tuple[str, ...]:
    """Return the safe argument vector for the bundled Codex agent image.

    The image's Docker ``ENTRYPOINT`` is the worker itself. These arguments are
    intentionally derived only from the canonical registry, never from a task
    title or benchmark identity.
    """

    root = locate_project_root(project_root)
    registry = load_role_registry(
        root / "workspaces/architect/role_registry.yaml",
        root / "workspaces/prompts/roles.yaml",
    )
    if role_id not in registry.canonical_ids:
        raise CodexAdapterError(f"unknown Codex agent role {role_id!r}")
    prompt_name = registry.prompt_for(role_id).name
    return (
        "--role",
        role_id,
        "--charter",
        f"/opt/modeling-harness/prompts/{prompt_name}",
    )
