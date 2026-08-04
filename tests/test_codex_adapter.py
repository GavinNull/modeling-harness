from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from modeling_harness.codex_adapter import (
    CodexAdapterError,
    codex_agent_entrypoint,
    validate_codex_adapter,
)


ROOT = Path(__file__).parents[1]


def copied_project(tmp_path: Path) -> Path:
    for relative in ("workspaces", ".codex", "containers/codex-agent"):
        shutil.copytree(ROOT / relative, tmp_path / relative)
    return tmp_path


def test_project_codex_profiles_match_canonical_roles() -> None:
    report = validate_codex_adapter(ROOT)
    assert report.profile_count == 16
    assert report.max_concurrent_threads == 3
    assert report.profile_names[0] == "system_architect"


def test_codex_image_entrypoint_is_derived_from_canonical_role() -> None:
    assert codex_agent_entrypoint("model_architect", project_root=ROOT) == (
        "--role",
        "model_architect",
        "--charter",
        "/opt/modeling-harness/prompts/08_model_architect.md",
    )
    unknown_role = validate_codex_adapter(ROOT).profile_names[0] + "__unknown"
    with pytest.raises(CodexAdapterError):
        codex_agent_entrypoint(unknown_role, project_root=ROOT)


def test_adapter_rejects_missing_profile(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    (project / ".codex/agents/model_architect.toml").unlink()
    with pytest.raises(CodexAdapterError):
        validate_codex_adapter(project)


def test_adapter_rejects_shared_workspace_write_access(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    profile = project / ".codex/agents/mathematical_reviewer.toml"
    profile.write_text(
        profile.read_text(encoding="utf-8").replace(
            'sandbox_mode = "read-only"', 'sandbox_mode = "workspace-write"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(CodexAdapterError):
        validate_codex_adapter(project)


def test_adapter_rejects_missing_non_specialization_marker(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    profile = project / ".codex/agents/model_architect.toml"
    profile.write_text(
        profile.read_text(encoding="utf-8").replace("task-specific", "single-task"),
        encoding="utf-8",
    )
    with pytest.raises(CodexAdapterError):
        validate_codex_adapter(project)


def test_adapter_rejects_incomplete_codex_worker_contract(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    worker = project / "containers/codex-agent/codex_worker.py"
    worker.write_text(
        worker.read_text(encoding="utf-8").replace("--ephemeral", "--persistent"),
        encoding="utf-8",
    )
    with pytest.raises(CodexAdapterError):
        validate_codex_adapter(project)
