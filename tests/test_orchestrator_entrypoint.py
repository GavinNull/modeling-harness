from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from modeling_harness.isolation import LocalSandboxBackend, ResourceLimits
from modeling_harness.orchestrator import Orchestrator, PromotedInput, PromptRef


ROOT = Path(__file__).parents[1]


class _Router:
    subagent_ids = frozenset({"data_assumption_analyst", "model_architect"})

    @staticmethod
    def authorize_dispatch(actor: str) -> None:
        assert actor == "main_agent"

    @staticmethod
    def authorize(sender: str, recipient: str, packet_type: str) -> None:
        assert (sender, packet_type) == ("main_agent", "TaskPacket")
        assert recipient in _Router.subagent_ids


class _PacketValidator:
    @staticmethod
    def validate(*args: object, **kwargs: object) -> None:
        return None


def _orchestrator(tmp_path: Path) -> Orchestrator:
    orchestrator = object.__new__(Orchestrator)
    orchestrator.project_root = ROOT
    orchestrator.runtime_root = tmp_path / "runtime"
    orchestrator.project_id = "project-001"
    orchestrator.task_id = "task-001"
    orchestrator.run_id = "run-001"
    orchestrator.container_image = "registry.example/codex-agent@sha256:" + "a" * 64
    orchestrator.limits = ResourceLimits(
        cpus=1,
        memory_bytes=256 * 1024 * 1024,
        disk_bytes=512 * 1024 * 1024,
        wall_time_seconds=60,
    )
    orchestrator.network_proxy_url = None
    orchestrator.allowed_domains = ()
    orchestrator.egress_attestation_id = None
    orchestrator._backend = LocalSandboxBackend()
    orchestrator.packet_validator = _PacketValidator()
    orchestrator.router = _Router()
    orchestrator._roles = {
        role_id: {"kind": "author", "network_policy": "deny"}
        for role_id in _Router.subagent_ids
    }
    orchestrator._cancelled = False
    orchestrator._dispatches = {}
    orchestrator._sealed_attempts = set()
    orchestrator._session_ids = set()
    orchestrator._workspace_roots = set()
    orchestrator._verify_promoted_input = lambda item, *args, **kwargs: {
        "artifacts": [
            {"artifact_id": item.artifact_id, "logical_path": "input.json"}
        ]
    }
    return orchestrator


@pytest.mark.parametrize(
    ("role_id", "prompt_name"),
    (
        ("model_architect", "08_model_architect.md"),
        ("data_assumption_analyst", "07_data_assumption_analyst.md"),
    ),
)
def test_orchestrator_uses_worker_entrypoint_arguments(
    tmp_path: Path,
    role_id: str,
    prompt_name: str,
) -> None:
    digest = hashlib.sha256(b"input").hexdigest()
    record = _orchestrator(tmp_path)._prepare_dispatch(
        actor="main_agent",
        role_id=role_id,
        objective="Perform the role-scoped task.",
        prompt=PromptRef(prompt_id="prompt-001", version="1.0.0", sha256=digest),
        promoted_inputs=(
            PromotedInput(
                artifact_id="input-001",
                manifest_sha256="b" * 64,
                content_sha256=digest,
                source_path=tmp_path / "input.json",
                producer_task_id="producer-task",
                producer_run_id="producer-run",
                producer_attempt_id="producer-attempt",
                producer_role_id="problem_definition_router",
                source_provenance_sha256s=("c" * 64,),
                real_task_derived=True,
            ),
        ),
        attempt_id="attempt-001",
        parent_packet_id=None,
        review_context=None,
        required_deliverables=None,
        acceptance_criteria=None,
        allowed_tools=(),
    )

    assert record.sandbox_request.entrypoint == (
        "--role",
        role_id,
        "--charter",
        f"/opt/modeling-harness/prompts/{prompt_name}",
    )
    assert record.sandbox_request.entrypoint[:2] != (
        "modeling-harness-agent",
        role_id,
    )
