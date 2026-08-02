"""Command-line operations for configuration, trust, and runtime readiness."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from jsonschema import Draft202012Validator

from modeling_harness.config import (
    ConfigError,
    load_json,
    locate_project_root,
    validate_config,
)
from modeling_harness.codex_adapter import codex_agent_entrypoint, validate_codex_adapter
from modeling_harness.packets import PACKET_TYPES, PacketContext, PacketValidator
from modeling_harness.runtime import docker_preflight
from modeling_harness.state_machine import LifecycleDefinition, RunLedger


class CliValidationError(ValueError):
    """Raised for invalid operational artifacts."""


def _add_project_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project-root",
        type=Path,
        help="project root or a path beneath it (defaults to current directory)",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="modeling-harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate-config",
        help="validate schemas, roles, prompts, and policy configuration",
    )
    _add_project_root(validate_parser)

    codex_parser = subparsers.add_parser(
        "validate-codex-adapter",
        help="validate project-scoped Codex profiles and governance markers",
    )
    _add_project_root(codex_parser)

    entrypoint_parser = subparsers.add_parser(
        "codex-agent-entrypoint",
        help="print the safe argument vector for one canonical Codex image role",
    )
    entrypoint_parser.add_argument("role_id")
    _add_project_root(entrypoint_parser)

    packet_parser = subparsers.add_parser(
        "validate-packet",
        help="strictly validate one protocol packet and its related documents",
    )
    packet_parser.add_argument("packet_type", choices=PACKET_TYPES)
    packet_parser.add_argument("path", type=Path)
    _add_project_root(packet_parser)
    packet_parser.add_argument(
        "--schema-only",
        action="store_true",
        help="explicitly validate JSON Schema only; establishes no trust",
    )
    packet_parser.add_argument("--task-packet", type=Path)
    packet_parser.add_argument("--artifact-manifest", type=Path)
    packet_parser.add_argument("--candidate-manifest", type=Path)
    packet_parser.add_argument("--source-manifest", type=Path)
    packet_parser.add_argument("--source-charter", type=Path)
    packet_parser.add_argument(
        "--input-manifest",
        type=Path,
        action="append",
        default=[],
    )
    packet_parser.add_argument(
        "--source-review",
        type=Path,
        action="append",
        default=[],
    )
    packet_parser.add_argument("--expected-parent-packet-id")

    benchmark_parser = subparsers.add_parser(
        "verify-benchmarks",
        help="verify registered benchmark schemas, paths, sizes, and SHA-256",
    )
    _add_project_root(benchmark_parser)

    ledger_parser = subparsers.add_parser(
        "verify-ledger",
        help="replay and verify an exported lifecycle ledger hash chain",
    )
    ledger_parser.add_argument("path", type=Path)
    _add_project_root(ledger_parser)

    preflight_parser = subparsers.add_parser(
        "production-preflight",
        help="check Docker client and daemon production prerequisites",
    )
    preflight_parser.add_argument("--docker-executable", default="docker")
    preflight_parser.add_argument("--json", action="store_true")

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="run configuration, benchmark, and Docker production checks",
    )
    _add_project_root(doctor_parser)
    doctor_parser.add_argument("--docker-executable", default="docker")
    return parser


def _object(path: Path | None, description: str) -> Mapping[str, Any] | None:
    if path is None:
        return None
    value = load_json(path)
    if not isinstance(value, dict):
        raise CliValidationError(f"{description} must be a JSON object")
    return value


def _objects(paths: Sequence[Path], description: str) -> tuple[Mapping[str, Any], ...]:
    values: list[Mapping[str, Any]] = []
    for path in paths:
        value = _object(path, description)
        assert value is not None
        values.append(value)
    return tuple(values)


def _packet_context(args: argparse.Namespace) -> PacketContext:
    return PacketContext(
        task_packet=_object(args.task_packet, "task packet"),
        artifact_manifest=_object(args.artifact_manifest, "artifact manifest"),
        candidate_manifest=_object(args.candidate_manifest, "candidate manifest"),
        source_manifest=_object(args.source_manifest, "source manifest"),
        source_charter=_object(args.source_charter, "source charter"),
        input_manifests=_objects(args.input_manifest, "input manifest"),
        source_reviews=_objects(args.source_review, "source review"),
        enforce_parent=args.expected_parent_packet_id is not None,
        expected_parent_packet_id=args.expected_parent_packet_id,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_benchmark_files(project_root: Path) -> int:
    benchmark_root = project_root / "benchmarks"
    documents = (
        (
            benchmark_root / "schemas/public_catalog.schema.json",
            benchmark_root / "manifests/public_catalog.json",
        ),
        (
            benchmark_root / "schemas/scorer_mapping.schema.json",
            benchmark_root / "manifests/scorer_mapping.json",
        ),
    )
    loaded: dict[Path, Mapping[str, Any]] = {}
    for schema_path, document_path in documents:
        schema = load_json(schema_path)
        document = load_json(document_path)
        if not isinstance(schema, dict) or not isinstance(document, dict):
            raise CliValidationError("benchmark schema and manifest must be objects")
        Draft202012Validator.check_schema(schema)
        errors = sorted(
            Draft202012Validator(schema).iter_errors(document),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            raise CliValidationError(
                f"{document_path.name} violates its schema: {errors[0].message}"
            )
        loaded[document_path] = document

    catalog = loaded[benchmark_root / "manifests/public_catalog.json"]
    artifacts = catalog.get("artifacts")
    if not isinstance(artifacts, list):
        raise CliValidationError("public benchmark catalog has no artifacts list")
    root = project_root.resolve()
    verified = 0
    for entry in artifacts:
        if not isinstance(entry, dict):
            raise CliValidationError("benchmark artifact entry must be an object")
        relative = entry.get("local_path")
        if not isinstance(relative, str):
            raise CliValidationError("benchmark artifact local_path is invalid")
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise CliValidationError("benchmark artifact path escapes project root")
        if not path.is_file():
            raise CliValidationError(f"benchmark artifact is missing: {relative}")
        if path.stat().st_size != entry.get("size_bytes"):
            raise CliValidationError(f"benchmark size mismatch: {relative}")
        if _sha256_file(path) != entry.get("sha256"):
            raise CliValidationError(f"benchmark SHA-256 mismatch: {relative}")
        verified += 1
    return verified


def _verify_exported_ledger(path: Path, project_root: Path) -> RunLedger:
    document = load_json(path)
    if not isinstance(document, dict):
        raise CliValidationError("ledger export must be a JSON object")
    run_id = document.get("run_id")
    initial_attempt_id = document.get("initial_attempt_id")
    events = document.get("events")
    if (
        not isinstance(run_id, str)
        or not isinstance(initial_attempt_id, str)
        or not isinstance(events, list)
    ):
        raise CliValidationError(
            "ledger export requires run_id, initial_attempt_id, and events"
        )
    definition = LifecycleDefinition.load(
        project_root / "workspaces/architect/state_machine.yaml",
        project_root / "workspaces/architect/role_registry.yaml",
    )
    return RunLedger.replay(
        definition,
        run_id=run_id,
        initial_attempt_id=initial_attempt_id,
        events=events,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-config":
            report = validate_config(args.project_root)
            print(
                "configuration valid: "
                f"{report.schema_count} schemas, "
                f"{report.role_count} subagent roles "
                f"({report.project_root})"
            )
            return 0

        if args.command == "validate-codex-adapter":
            report = validate_codex_adapter(args.project_root)
            print(
                "Codex adapter valid: "
                f"{report.profile_count} profiles, "
                f"at most {report.max_concurrent_threads} concurrent role sessions "
                f"({report.project_root})"
            )
            return 0

        if args.command == "codex-agent-entrypoint":
            print(
                json.dumps(
                    list(
                        codex_agent_entrypoint(
                            args.role_id,
                            project_root=args.project_root,
                        )
                    ),
                    ensure_ascii=False,
                )
            )
            return 0

        if args.command == "validate-packet":
            root = locate_project_root(args.project_root)
            validator = PacketValidator.from_project_root(root)
            packet = _object(args.path, args.packet_type)
            assert packet is not None
            if args.schema_only:
                validator.validate_schema(args.packet_type, packet)
                print(
                    f"{args.packet_type} schema valid (schema-only; no trust established)"
                )
            else:
                validator.validate(
                    args.packet_type,
                    packet,
                    context=_packet_context(args),
                )
                print(f"{args.packet_type} strictly valid")
            return 0

        if args.command == "verify-benchmarks":
            root = locate_project_root(args.project_root)
            count = _verify_benchmark_files(root)
            print(f"benchmark assets verified: {count}")
            return 0

        if args.command == "verify-ledger":
            root = locate_project_root(args.project_root)
            ledger = _verify_exported_ledger(args.path, root)
            print(
                f"ledger valid: {len(ledger.events)} events, "
                f"state={ledger.state}, head={ledger.head_hash}"
            )
            return 0

        if args.command == "production-preflight":
            report = docker_preflight(args.docker_executable)
            if args.json:
                print(json.dumps(report.as_json(), ensure_ascii=False, sort_keys=True))
            elif report.production_available:
                print(
                    "production available: "
                    f"Docker client {report.client_version}, "
                    f"server {report.server_version}"
                )
            else:
                print(
                    "production unavailable: " + "; ".join(report.errors),
                    file=sys.stderr,
                )
            return 0 if report.production_available else 2

        if args.command == "doctor":
            report = validate_config(args.project_root)
            codex_report = validate_codex_adapter(report.project_root)
            count = _verify_benchmark_files(report.project_root)
            preflight = docker_preflight(args.docker_executable)
            if not preflight.production_available:
                raise CliValidationError(
                    "production unavailable: " + "; ".join(preflight.errors)
                )
            print(
                "doctor passed: "
                f"{report.role_count} roles / {codex_report.profile_count} Codex profiles, "
                f"{count} benchmark assets, "
                f"Docker server {preflight.server_version}"
            )
            return 0

        raise AssertionError(f"unhandled command: {args.command}")
    except (ConfigError, CliValidationError, OSError, ValueError) as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
