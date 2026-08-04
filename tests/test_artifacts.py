from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

import pytest

import modeling_harness.artifacts as artifact_module
from modeling_harness.artifacts import (
    ArtifactExistsError,
    ArtifactIntegrityError,
    ArtifactPathError,
    ArtifactStore,
)
from modeling_harness.governance import (
    SourceProvenanceLedger,
    TRUSTED_PROVENANCE_INGRESS_ID,
    bootstrap_source_provenance_control_plane,
)
from modeling_harness.packets import (
    PacketSchemaError,
    PacketSemanticError,
    PacketValidator,
    sha256_json,
)


ROOT = Path(__file__).parents[1]
H_A = "a" * 64
H_B = "b" * 64
IMAGE = f"sha256:{'c' * 64}"
NOW = "2026-01-02T03:04:05Z"
TEST_PROVENANCE_CONTROL = bootstrap_source_provenance_control_plane()
TEST_PROVENANCE_LEDGER = TEST_PROVENANCE_CONTROL.ledger
TEST_PROVENANCE_REGISTRAR = TEST_PROVENANCE_CONTROL.registrar


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def task_packet() -> dict:
    role_id = "sandbox_platform_engineer"
    attempt_id = "attempt-001"
    return {
        "schema_version": "1.0.0",
        "packet_id": "packet-task-store-001",
        "task_id": "task-generic-001",
        "run_id": "run-generic-001",
        "attempt_id": attempt_id,
        "role_id": role_id,
        "task_purpose": "generic-governance",
        "objective": "Produce the role-scoped protocol artifact.",
        "scope": {"in": ["protocol"], "out": ["unrelated work"]},
        "input_artifacts": [],
        "allowed_tools": [],
        "required_deliverables": [
            {
                "deliverable_id": "deliverable-001",
                "description": "One protocol artifact",
                "media_type": "application/json",
            }
        ],
        "acceptance_criteria": [
            {
                "criterion_id": "criterion-001",
                "description": "The artifact validates",
                "evidence_type": "test",
                "hard_gate": True,
            }
        ],
        "workspace": {
            "workspace_id": "workspace-001",
            "container_id": "container-001",
            "write_root": (
                f"/runs/project-generic/task-generic-001/{role_id}/{attempt_id}/"
            ),
            "read_only_mounts": [],
            "fresh_environment_required": True,
        },
        "prompt": {
            "prompt_id": "prompt-generic-001",
            "version": "1.0.0",
            "sha256": H_A,
        },
        "benchmark_access_level": "none",
        "execution_limits": {
            "wall_time_seconds": 60,
            "cpu_cores": 1,
            "memory_mb": 256,
            "disk_mb": 256,
            "network_policy": "allowlisted-readonly",
        },
        "created_at": NOW,
    }


def candidate_manifest(
    content: bytes,
    *,
    logical_path: str = "outputs/result.bin",
    lineage: list[str] | None = None,
    task: dict | None = None,
    provenance_ledger: SourceProvenanceLedger = TEST_PROVENANCE_LEDGER,
) -> dict:
    task = task or task_packet()
    content_sha256 = digest(content)
    provenance_manifest = {
        "schema_version": "1.0.0",
        "manifest_id": f"provenance-{content_sha256}",
        "subject_id": f"artifact-{content_sha256}",
        "source_class": "independent-general-research",
        "source_content_sha256": content_sha256,
        "parent_manifest_sha256s": [],
        "real_task_derived": False,
        "issued_by": TRUSTED_PROVENANCE_INGRESS_ID,
        "issued_at": NOW,
    }
    assert provenance_ledger is TEST_PROVENANCE_LEDGER
    provenance_sha256 = TEST_PROVENANCE_REGISTRAR.register(provenance_manifest)
    return {
        "schema_version": "2.0.0",
        "manifest_id": "manifest-store-001",
        "manifest_version": 1,
        "task_id": task["task_id"],
        "run_id": task["run_id"],
        "attempt_id": task["attempt_id"],
        "producer": {
            "role_id": task["role_id"],
            "workspace_id": task["workspace"]["workspace_id"],
            "container_id": task["workspace"]["container_id"],
        },
        "task_packet_sha256": sha256_json(task),
        "prompt_sha256": task["prompt"]["sha256"],
        "container_image_digest": IMAGE,
        "input_manifest_sha256s": lineage or [],
        "source_provenance_sha256s": [provenance_sha256],
        "real_task_derived": False,
        "artifacts": [
            {
                "artifact_id": "artifact-store-001",
                "logical_path": logical_path,
                "content_sha256": content_sha256,
                "size_bytes": len(content),
                "media_type": "application/octet-stream",
                "classification": "internal",
                "content_kind": "generic-core",
                "executable": False,
            }
        ],
        "promotion": {
            "status": "candidate",
            "approved_by": None,
            "approved_at": None,
            "source_manifest_sha256": None,
        },
        "created_at": NOW,
    }


def write_source(root: Path, content: bytes) -> None:
    path = root / "outputs" / "result.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)


def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(
        tmp_path / "store",
        PacketValidator.from_project_root(ROOT),
        TEST_PROVENANCE_LEDGER,
    )


def test_artifact_store_receives_only_read_only_provenance_facade(
    tmp_path: Path,
) -> None:
    artifact_store = store(tmp_path)
    assert artifact_store.provenance_ledger is TEST_PROVENANCE_LEDGER
    assert not hasattr(artifact_store, "provenance_registrar")
    assert not hasattr(artifact_store.provenance_ledger, "register")
    assert not hasattr(artifact_store.provenance_ledger, "trusted_ingress")


def test_stage_promote_verify_and_audit_record(tmp_path: Path) -> None:
    content = b"generic artifact\n"
    source = tmp_path / "source"
    write_source(source, content)
    task = task_packet()
    manifest = candidate_manifest(content, task=task)
    artifacts = store(tmp_path)

    source_hash = artifacts.stage(manifest, source, task_packet=task)
    assert source_hash == sha256_json(manifest)
    assert artifacts.verify_staged(source_hash) == manifest

    record = artifacts.promote(
        source_hash,
        approved_by="main_agent",
        approved_at=NOW,
    )
    promoted = artifacts.verify_promoted(record.promoted_manifest_sha256)
    assert promoted["promotion"]["source_manifest_sha256"] == source_hash
    assert promoted["promotion"]["approved_by"] == "main_agent"
    assert promoted["manifest_version"] == 2

    record_path = artifacts.records_root / f"{source_hash}.json"
    on_disk = json.loads(record_path.read_text(encoding="utf-8"))
    assert on_disk == record.as_json()


def test_hash_mismatch_rejected_before_staging(tmp_path: Path) -> None:
    declared = b"declared content"
    source = tmp_path / "source"
    write_source(source, b"different content")
    task = task_packet()
    manifest = candidate_manifest(declared, task=task)

    with pytest.raises(ArtifactIntegrityError):
        store(tmp_path).stage(manifest, source, task_packet=task)


def test_staged_tampering_is_rejected_at_promotion(tmp_path: Path) -> None:
    content = b"stable content"
    source = tmp_path / "source"
    write_source(source, content)
    artifacts = store(tmp_path)
    task = task_packet()
    source_hash = artifacts.stage(
        candidate_manifest(content, task=task),
        source,
        task_packet=task,
    )
    staged_file = (
        artifacts.staging_root / source_hash / "artifacts" / "outputs" / "result.bin"
    )
    staged_file.write_bytes(b"tampered content")

    with pytest.raises(ArtifactIntegrityError):
        artifacts.promote(source_hash, approved_by="main_agent", approved_at=NOW)


def test_immutable_overwrite_and_second_promotion_are_rejected(
    tmp_path: Path,
) -> None:
    content = b"one candidate"
    source = tmp_path / "source"
    write_source(source, content)
    artifacts = store(tmp_path)
    task = task_packet()
    manifest = candidate_manifest(content, task=task)
    source_hash = artifacts.stage(manifest, source, task_packet=task)

    with pytest.raises(ArtifactExistsError):
        artifacts.stage(manifest, source, task_packet=task)

    artifacts.promote(source_hash, approved_by="main_agent", approved_at=NOW)
    with pytest.raises(ArtifactExistsError):
        artifacts.promote(source_hash, approved_by="main_agent", approved_at=NOW)


def test_path_traversal_is_rejected_by_manifest_contract(tmp_path: Path) -> None:
    content = b"content"
    source = tmp_path / "source"
    write_source(source, content)
    task = task_packet()
    manifest = candidate_manifest(
        content, logical_path="../outside.bin", task=task
    )

    with pytest.raises(PacketSchemaError):
        store(tmp_path).stage(manifest, source, task_packet=task)


def test_unknown_or_modified_lineage_is_rejected(tmp_path: Path) -> None:
    content = b"child content"
    source = tmp_path / "source"
    write_source(source, content)
    task = task_packet()
    task["input_artifacts"] = [
        {
            "artifact_id": "input-001",
            "manifest_sha256": "d" * 64,
            "mount_path": "/inputs/input-001",
            "classification": "internal",
        }
    ]
    task["workspace"]["read_only_mounts"] = ["/inputs/input-001"]
    manifest = candidate_manifest(content, lineage=["d" * 64], task=task)
    with pytest.raises(ArtifactIntegrityError):
        store(tmp_path).stage(manifest, source, task_packet=task)

    no_lineage = deepcopy(manifest)
    no_lineage["input_manifest_sha256s"] = []
    no_lineage_task = task_packet()
    no_lineage["task_packet_sha256"] = sha256_json(no_lineage_task)
    artifacts = store(tmp_path / "second")
    source_hash = artifacts.stage(
        no_lineage, source, task_packet=no_lineage_task
    )
    staged_manifest = artifacts.staging_root / source_hash / "manifest.json"
    changed = json.loads(staged_manifest.read_text(encoding="utf-8"))
    changed["input_manifest_sha256s"] = ["e" * 64]
    staged_manifest.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError):
        artifacts.promote(source_hash, approved_by="main_agent", approved_at=NOW)


def test_stage_rejects_hardlinked_source_file(tmp_path: Path) -> None:
    content = b"hardlink source"
    source = tmp_path / "source"
    write_source(source, content)
    alias = source / "outputs" / "alias.bin"
    try:
        os.link(source / "outputs" / "result.bin", alias)
    except OSError as exc:
        pytest.skip(f"filesystem cannot create a hardlink: {exc}")
    task = task_packet()

    with pytest.raises(ArtifactPathError):
        store(tmp_path).stage(
            candidate_manifest(content, task=task),
            source,
            task_packet=task,
        )


def test_stage_rejects_symlink_or_reparse_source(tmp_path: Path) -> None:
    content = b"linked source"
    real_source = tmp_path / "real-source"
    write_source(real_source, content)
    linked_source = tmp_path / "linked-source"
    try:
        linked_source.symlink_to(real_source, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"filesystem cannot create a directory symlink: {exc}")
    task = task_packet()

    with pytest.raises(ArtifactPathError):
        store(tmp_path).stage(
            candidate_manifest(content, task=task),
            linked_source,
            task_packet=task,
        )


def test_stage_detects_source_directory_mutation_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"stable before copy"
    source = tmp_path / "source"
    write_source(source, content)
    artifacts = store(tmp_path)
    task = task_packet()
    original = artifacts._copy_file

    def copy_then_mutate(source_path: Path, destination_path: Path) -> None:
        original(source_path, destination_path)
        (source / "outputs" / "unexpected.bin").write_bytes(b"mutation")

    monkeypatch.setattr(artifacts, "_copy_file", copy_then_mutate)
    with pytest.raises(ArtifactIntegrityError):
        artifacts.stage(
            candidate_manifest(content, task=task),
            source,
            task_packet=task,
        )
    assert not [
        path for path in artifacts.staging_root.iterdir() if not path.name.startswith(".")
    ]


def test_stage_reverifies_private_snapshot_after_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"declared snapshot"
    source = tmp_path / "source"
    write_source(source, content)
    artifacts = store(tmp_path)
    task = task_packet()
    original = artifacts._copy_file

    def copy_then_tamper(source_path: Path, destination_path: Path) -> None:
        original(source_path, destination_path)
        destination_path.write_bytes(b"private snapshot tampered")

    monkeypatch.setattr(artifacts, "_copy_file", copy_then_tamper)
    with pytest.raises(ArtifactIntegrityError):
        artifacts.stage(
            candidate_manifest(content, task=task),
            source,
            task_packet=task,
        )


def test_case_insensitive_logical_path_collision_is_rejected(
    tmp_path: Path,
) -> None:
    content = b"content"
    source = tmp_path / "source"
    write_source(source, content)
    task = task_packet()
    manifest = candidate_manifest(content, task=task)
    duplicate = deepcopy(manifest["artifacts"][0])
    duplicate["artifact_id"] = "artifact-store-002"
    duplicate["logical_path"] = "outputs/RESULT.bin"
    manifest["artifacts"].append(duplicate)

    with pytest.raises(PacketSemanticError):
        store(tmp_path).stage(manifest, source, task_packet=task)


def test_failed_promotion_has_no_visible_snapshot_and_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"atomic promotion"
    source = tmp_path / "source"
    write_source(source, content)
    artifacts = store(tmp_path)
    task = task_packet()
    source_hash = artifacts.stage(
        candidate_manifest(content, task=task),
        source,
        task_packet=task,
    )
    original_atomic_create = artifact_module._atomic_create_bytes

    def fail_record_commit(destination: Path, data: bytes) -> None:
        raise OSError("injected record commit failure")

    monkeypatch.setattr(
        artifact_module, "_atomic_create_bytes", fail_record_commit
    )
    with pytest.raises(OSError):
        artifacts.promote(
            source_hash, approved_by="main_agent", approved_at=NOW
        )
    assert not [
        path for path in artifacts.promoted_root.iterdir()
        if not path.name.startswith(".")
    ]
    assert not (artifacts.records_root / f"{source_hash}.json").exists()

    monkeypatch.setattr(
        artifact_module, "_atomic_create_bytes", original_atomic_create
    )
    record = artifacts.promote(
        source_hash, approved_by="main_agent", approved_at=NOW
    )
    assert artifacts.verify_promoted(record.promoted_manifest_sha256)


@pytest.mark.skipif(os.name != "nt", reason="Windows drive semantics")
def test_cross_drive_path_is_never_within_root() -> None:
    assert not artifact_module._is_within(
        Path(r"C:\outside\artifact.bin"),
        Path(r"D:\store"),
    )
