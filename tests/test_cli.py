from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from modeling_harness import cli
from modeling_harness.packets import sha256_json
from modeling_harness.runtime import DockerPreflightReport


ROOT = Path(__file__).parents[1]
H_A = "a" * 64
H_B = "b" * 64
NOW = "2026-01-02T03:04:05Z"


def task_packet() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "packet_id": "packet-task-001",
        "task_id": "task-001",
        "run_id": "run-001",
        "attempt_id": "attempt-001",
        "parent_packet_id": None,
        "role_id": "problem_definition_router",
        "task_purpose": "task-answer",
        "objective": "Produce a generic definition.",
        "scope": {"in": ["generic work"], "out": ["peer workspaces"]},
        "input_artifacts": [],
        "allowed_tools": [],
        "required_deliverables": [
            {
                "deliverable_id": "deliverable-001",
                "description": "Definition artifact",
                "media_type": "application/json",
            }
        ],
        "acceptance_criteria": [
            {
                "criterion_id": "criterion-001",
                "description": "Contract passes.",
                "evidence_type": "test",
                "hard_gate": True,
            }
        ],
        "workspace": {
            "workspace_id": "workspace-001",
            "container_id": "container-001",
            "write_root": (
                "/runs/project-001/task-001/problem_definition_router/attempt-001/"
            ),
            "read_only_mounts": [],
            "fresh_environment_required": True,
        },
        "prompt": {
            "prompt_id": "prompt-001",
            "version": "1.0.0",
            "sha256": H_A,
        },
        "benchmark_access_level": "none",
        "execution_limits": {
            "wall_time_seconds": 30,
            "cpu_cores": 1,
            "memory_mb": 256,
            "disk_mb": 256,
            "network_policy": "deny",
        },
        "created_at": NOW,
    }


def result_packet(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "packet_id": "packet-result-001",
        "task_id": task["task_id"],
        "run_id": task["run_id"],
        "attempt_id": task["attempt_id"],
        "role_id": task["role_id"],
        "task_packet_sha256": sha256_json(task),
        "status": "pass",
        "summary": "Completed.",
        "artifact_manifest": {
            "artifact_id": "artifact-001",
            "manifest_sha256": H_B,
            "location": "/runs/project-001/manifest.json",
        },
        "claims_and_sources": [],
        "assumptions": [],
        "validation_performed": [],
        "metrics": {},
        "known_failures": [],
        "uncertainties": [],
        "provenance": {
            "prompt_sha256": H_A,
            "container_image_digest": "sha256:" + "c" * 64,
            "input_manifest_sha256s": [],
            "random_seeds": [],
        },
        "completed_at": NOW,
    }


def write_json(path: Path, value: Any) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def ready() -> DockerPreflightReport:
    return DockerPreflightReport(
        "docker",
        True,
        "27.0.0",
        True,
        "27.0.0",
        (),
    )


def unavailable() -> DockerPreflightReport:
    return DockerPreflightReport(
        "docker",
        False,
        None,
        False,
        None,
        ("Docker binary was not found.",),
    )


def test_validate_config_and_packet_modes(
    tmp_path: Path, capsys: Any
) -> None:
    assert cli.main(["validate-config", "--project-root", str(ROOT)]) == 0
    capsys.readouterr()

    assert cli.main(["validate-codex-adapter", "--project-root", str(ROOT)]) == 0
    capsys.readouterr()

    assert (
        cli.main(
            [
                "codex-agent-entrypoint",
                "model_architect",
                "--project-root",
                str(ROOT),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == [
        "--role",
        "model_architect",
        "--charter",
        "/opt/modeling-harness/prompts/08_model_architect.md",
    ]

    task_path = write_json(tmp_path / "task.json", task_packet())
    assert (
        cli.main(
            [
                "validate-packet",
                "TaskPacket",
                str(task_path),
                "--project-root",
                str(ROOT),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        cli.main(
            [
                "validate-packet",
                "TaskPacket",
                str(task_path),
                "--project-root",
                str(ROOT),
                "--schema-only",
            ]
        )
        == 0
    )
    capsys.readouterr()


def test_validate_packet_default_strict_requires_context(
    tmp_path: Path, capsys: Any
) -> None:
    task = task_packet()
    result_path = write_json(tmp_path / "result.json", result_packet(task))
    code = cli.main(
        [
            "validate-packet",
            "ResultPacket",
            str(result_path),
            "--project-root",
            str(ROOT),
        ]
    )
    assert code != 0
    capsys.readouterr()


def test_production_preflight_and_doctor_exit_codes(
    monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr(cli, "docker_preflight", lambda _: unavailable())
    assert cli.main(["production-preflight"]) == 2
    capsys.readouterr()

    monkeypatch.setattr(cli, "_verify_benchmark_files", lambda _: 11)
    assert cli.main(["doctor", "--project-root", str(ROOT)]) != 0
    capsys.readouterr()

    monkeypatch.setattr(cli, "docker_preflight", lambda _: ready())
    assert cli.main(["doctor", "--project-root", str(ROOT)]) == 0
    capsys.readouterr()


def test_verify_benchmarks_and_ledger_commands(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr(cli, "_verify_benchmark_files", lambda _: 11)
    assert (
        cli.main(["verify-benchmarks", "--project-root", str(ROOT)]) == 0
    )
    capsys.readouterr()

    ledger_path = write_json(
        tmp_path / "ledger.json",
        {
            "run_id": "run-001",
            "initial_attempt_id": "attempt-001",
            "events": [],
        },
    )
    assert (
        cli.main(
            [
                "verify-ledger",
                str(ledger_path),
                "--project-root",
                str(ROOT),
            ]
        )
        == 0
    )
    capsys.readouterr()


def test_cli_returns_nonzero_for_invalid_artifacts(
    tmp_path: Path, capsys: Any
) -> None:
    invalid = write_json(tmp_path / "invalid.json", {})
    assert (
        cli.main(
            [
                "validate-packet",
                "TaskPacket",
                str(invalid),
                "--project-root",
                str(ROOT),
                "--schema-only",
            ]
        )
        != 0
    )
    capsys.readouterr()
