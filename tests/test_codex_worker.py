from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from modeling_harness.codex_adapter import codex_agent_entrypoint
from modeling_harness.isolation import DockerSandboxBackend, ResourceLimits, SandboxRequest


ROOT = Path(__file__).parents[1]
WORKER_PATH = ROOT / "containers/codex-agent/codex_worker.py"
SPEC = importlib.util.spec_from_file_location("codex_worker", WORKER_PATH)
assert SPEC is not None and SPEC.loader is not None
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)
CHARTER = ROOT / "workspaces/prompts/system_prompts/08_model_architect.md"


def task_packet() -> dict[str, Any]:
    return {
        "task_id": "task-001",
        "run_id": "run-001",
        "attempt_id": "attempt-001",
        "role_id": "model_architect",
        "prompt": {
            "sha256": hashlib.sha256(CHARTER.read_bytes()).hexdigest()
        },
    }


def bound_result(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "run_id": task["run_id"],
        "attempt_id": task["attempt_id"],
        "role_id": task["role_id"],
        "task_packet_sha256": worker._task_hash(task),
        "provenance": {"prompt_sha256": task["prompt"]["sha256"]},
    }


def test_command_is_ephemeral_json_and_shell_free() -> None:
    command = worker.build_codex_command("perform generic work")
    assert command[:2] == ["codex", "exec"]
    assert "--ephemeral" in command
    assert "--json" in command
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert "--ignore-user-config" in command
    assert all(item not in {"sh", "bash", "-c", "/c"} for item in command)


def test_worker_accepts_only_bound_result_in_fixed_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Path("/workspace")
    monkeypatch.setattr(worker, "WORKSPACE", tmp_path)
    monkeypatch.setattr(worker, "TASK_PACKET", tmp_path / "task_packet.json")
    monkeypatch.setattr(worker, "RESULT_PACKET", tmp_path / "result_packet.json")
    task = task_packet()
    (tmp_path / "task_packet.json").write_text(json.dumps(task), encoding="utf-8")
    charter = CHARTER
    # The production worker only accepts an immutable charter location. Patch
    # its root for this filesystem-level unit test.
    monkeypatch.setattr(worker, "_validate_charter", lambda path: charter)

    seen: dict[str, Any] = {}

    def fake_runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["command"] = command
        seen["env"] = kwargs["env"]
        (tmp_path / "result_packet.json").write_text(
            json.dumps(bound_result(task)), encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    assert worker.run_worker(
        role_id="model_architect",
        charter=charter,
        workspace=tmp_path,
        environ={"PATH": "/bin", "CODEX_API_KEY": "not-written"},
        runner=fake_runner,
    ) == 0
    assert seen["env"] == {
        "PATH": "/bin",
        "HOME": "/tmp/codex-home",
        "CODEX_HOME": "/tmp/codex-home",
        "LANG": "C.UTF-8",
        "CODEX_API_KEY": "not-written",
    }
    assert isinstance(seen["command"][-1], str)
    assert seen["command"][-1]


def test_worker_rejects_unbound_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "WORKSPACE", tmp_path)
    monkeypatch.setattr(worker, "TASK_PACKET", tmp_path / "task_packet.json")
    monkeypatch.setattr(worker, "RESULT_PACKET", tmp_path / "result_packet.json")
    task = task_packet()
    (tmp_path / "task_packet.json").write_text(json.dumps(task), encoding="utf-8")
    charter = CHARTER
    monkeypatch.setattr(worker, "_validate_charter", lambda path: charter)

    def fake_runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        invalid = bound_result(task)
        invalid["role_id"] = "optimization_decision_modeler"
        (tmp_path / "result_packet.json").write_text(json.dumps(invalid), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(worker.CodexWorkerError):
        worker.run_worker(
            role_id="model_architect",
            charter=charter,
            workspace=tmp_path,
            runner=fake_runner,
        )


def test_worker_rejects_charter_byte_tampering_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "WORKSPACE", tmp_path)
    monkeypatch.setattr(worker, "TASK_PACKET", tmp_path / "task_packet.json")
    monkeypatch.setattr(worker, "RESULT_PACKET", tmp_path / "result_packet.json")
    charter = tmp_path / "role-charter.md"
    original = b"approved immutable charter\n"
    charter.write_bytes(original)
    task = task_packet()
    task["prompt"]["sha256"] = hashlib.sha256(original).hexdigest()
    (tmp_path / "task_packet.json").write_text(json.dumps(task), encoding="utf-8")
    charter.write_bytes(original + b"tampered")
    monkeypatch.setattr(worker, "_validate_charter", lambda path: charter)

    launched = False

    def fake_runner(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        nonlocal launched
        launched = True
        raise AssertionError("worker must reject before launch")

    with pytest.raises(worker.CodexWorkerError):
        worker.run_worker(
            role_id="model_architect",
            charter=charter,
            workspace=tmp_path,
            runner=fake_runner,
        )
    assert launched is False


def test_codex_worker_arguments_survive_the_strict_docker_planner(
    tmp_path: Path,
) -> None:
    request = SandboxRequest(
        project_id="project-001",
        task_id="task-001",
        role_id="model_architect",
        attempt_id="attempt-001",
        session_id="session-001",
        host_write_root=tmp_path / "isolated-workspace",
        image="registry.example/codex-agent@sha256:" + "a" * 64,
        limits=ResourceLimits(
            cpus=1,
            memory_bytes=256 * 1024 * 1024,
            disk_bytes=512 * 1024 * 1024,
            wall_time_seconds=60,
        ),
        entrypoint=codex_agent_entrypoint("model_architect", project_root=ROOT),
    )
    plan = DockerSandboxBackend().plan(request)
    image_index = plan.command.index(request.image)
    assert plan.command[image_index + 1 :] == request.entrypoint
    assert "--read-only" in plan.command
    assert plan.command[plan.command.index("--network") + 1] == "none"
