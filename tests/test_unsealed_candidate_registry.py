from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest
from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = (
    REPO_ROOT / "benchmarks" / "scripts" / "validate_unsealed_candidate_catalog.py"
)
CATALOG_SCHEMA = (
    REPO_ROOT / "benchmarks" / "schemas" / "unsealed_candidate_catalog.schema.json"
)
PAYLOAD_SCHEMA = (
    REPO_ROOT
    / "benchmarks"
    / "schemas"
    / "unsealed_candidate_payload_manifest.schema.json"
)
REVIEW_SCHEMA = (
    REPO_ROOT
    / "benchmarks"
    / "schemas"
    / "unsealed_candidate_review_attestation.schema.json"
)

spec = importlib.util.spec_from_file_location("unsealed_registry_validator", VALIDATOR_PATH)
assert spec and spec.loader
VALIDATOR = importlib.util.module_from_spec(spec)
spec.loader.exec_module(VALIDATOR)


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


SCHEMAS = {
    "catalog": _load(CATALOG_SCHEMA),
    "payload": _load(PAYLOAD_SCHEMA),
    "review": _load(REVIEW_SCHEMA),
}


def _digest(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _canonical_hash(value: dict) -> str:
    data = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )


def _empty_catalog() -> dict:
    return {
        "catalog_version": "3.0.0",
        "catalog_kind": "unsealed_evaluator_only_candidate_staging",
        "generated_at": "2026-01-01T00:00:00Z",
        "policy": copy.deepcopy(VALIDATOR._EXPECTED_CATALOG_POLICY),
        "candidates": [],
    }


def _governance() -> dict:
    return copy.deepcopy(VALIDATOR._EXPECTED_CANDIDATE_GOVERNANCE)


def _build_valid_registry(
    tmp_path: Path, candidate_id: str = "uc-a0a0a0a0a0a0a0a0"
) -> tuple[Path, Path, dict, dict, dict]:
    root = tmp_path / "repository"
    package_relative = (
        f"benchmarks/evaluator_only/unsealed_candidates/{candidate_id}/"
    )
    review_relative = f"benchmarks/evaluator_only/unsealed_reviews/{candidate_id}/"
    package = root.joinpath(*PurePosixPath(package_relative).parts)
    review_root = root.joinpath(*PurePosixPath(review_relative).parts)
    payload = package / "visible" / "opaque.bin"
    control = package / "controls" / "opaque.bin"
    payload.parent.mkdir(parents=True)
    control.parent.mkdir(parents=True)
    review_root.mkdir(parents=True)
    payload.write_bytes(b"opaque-solver-payload\n")
    control.write_bytes(b"opaque-evaluator-control\n")

    payload_manifest = {
        "manifest_version": "3.0.0",
        "manifest_kind": "unsealed_candidate_payload",
        "candidate_id": candidate_id,
        "package_root": package_relative,
        "manifest_self_registered": False,
        "files": [
            {
                "relative_path": "visible/opaque.bin",
                **_digest(payload),
                "file_role": "solver_payload",
                "audience": "solver_visible",
            },
            {
                "relative_path": "controls/opaque.bin",
                **_digest(control),
                "file_role": "evaluator_control",
                "audience": "evaluator_only",
            },
        ],
    }
    manifest_path = package / "payload_manifest.json"
    _write_json(manifest_path, payload_manifest)
    manifest_digest = _digest(manifest_path)

    attestation_payload = {
        "attestation_version": "1.0.0",
        "attestation_kind": "unsealed_candidate_independent_review",
        "candidate_id": candidate_id,
        "reviewed_payload_manifest_sha256": manifest_digest["sha256"],
        "reviewer_opaque_id": "reviewer-b1b1b1b1b1b1b1b1",
        "disposition": "approved_for_diagnostic_execution_only",
        "independence": True,
        "binding_method": "canonical_attestation_payload_sha256",
        "authenticity_scope": (
            "integrity_binding_only_external_reviewer_control_required"
        ),
    }
    attestation = {
        **attestation_payload,
        "attestation_payload_sha256": _canonical_hash(attestation_payload),
    }
    attestation_path = review_root / "review_attestation.json"
    _write_json(attestation_path, attestation)
    attestation_digest = _digest(attestation_path)

    candidate = {
        "candidate_id": candidate_id,
        "layer": "private_novel",
        "package_root": package_relative,
        "review_root": review_relative,
        "payload_manifest": {
            "path": package_relative + "payload_manifest.json",
            **manifest_digest,
        },
        "review_attestation": {
            "path": review_relative + "review_attestation.json",
            **attestation_digest,
            "reviewed_payload_manifest_sha256": manifest_digest["sha256"],
        },
        "mount_contract": {
            "source_root": package_relative,
            "mount_mode": "exact_file_allowlist",
            "mount_namespace": "/evaluation/input",
            "solver_visible_allowlist": [
                {
                    "source_relative_path": "visible/opaque.bin",
                    "target_path": "/evaluation/input/opaque.bin",
                    **_digest(payload),
                    "access": "read_only",
                }
            ],
            "solver_hidden_paths": [
                "controls/opaque.bin",
                "payload_manifest.json",
            ],
        },
        "governance": _governance(),
    }
    catalog = _empty_catalog()
    catalog["candidates"] = [candidate]
    catalog_path = root / "benchmarks" / "manifests" / "unsealed.json"
    _write_json(catalog_path, catalog)
    return root, catalog_path, catalog, payload_manifest, attestation


def _path(root: Path, posix: str) -> Path:
    return root.joinpath(*PurePosixPath(posix).parts)


def _rewrite_attestation(root: Path, catalog: dict, attestation: dict) -> None:
    binding_payload = dict(attestation)
    binding_payload.pop("attestation_payload_sha256", None)
    attestation["attestation_payload_sha256"] = _canonical_hash(binding_payload)
    registration = catalog["candidates"][0]["review_attestation"]
    path = _path(root, registration["path"])
    _write_json(path, attestation)
    registration.update(_digest(path))


def _rewrite_manifest_and_review(
    root: Path, catalog: dict, payload_manifest: dict, attestation: dict
) -> None:
    candidate = catalog["candidates"][0]
    manifest_path = _path(root, candidate["payload_manifest"]["path"])
    _write_json(manifest_path, payload_manifest)
    manifest_digest = _digest(manifest_path)
    candidate["payload_manifest"].update(manifest_digest)
    candidate["review_attestation"]["reviewed_payload_manifest_sha256"] = (
        manifest_digest["sha256"]
    )
    attestation["reviewed_payload_manifest_sha256"] = manifest_digest["sha256"]
    _rewrite_attestation(root, catalog, attestation)


def _validate(root: Path, catalog_path: Path) -> dict[str, int]:
    return VALIDATOR.validate_registry(
        root, catalog_path, CATALOG_SCHEMA, PAYLOAD_SCHEMA, REVIEW_SCHEMA
    )


def _validate_with_schemas(
    tmp_path: Path, root: Path, catalog_path: Path, schemas: dict[str, dict]
) -> dict[str, int]:
    schema_root = tmp_path / "injected_schemas"
    paths = {}
    for name, value in schemas.items():
        paths[name] = schema_root / f"{name}.json"
        _write_json(paths[name], value)
    return VALIDATOR.validate_registry(
        root,
        catalog_path,
        paths["catalog"],
        paths["payload"],
        paths["review"],
    )


def _different(value: object) -> object:
    if isinstance(value, bool):
        return not value
    if isinstance(value, str):
        return value + "-changed"
    raise AssertionError(value)


def _at(value: object, path: tuple[object, ...]) -> object:
    current = value
    for component in path:
        current = current[component]  # type: ignore[index]
    return current


def test_three_canonical_schemas_are_valid_and_self_validate() -> None:
    for schema in SCHEMAS.values():
        Draft202012Validator.check_schema(schema)
    VALIDATOR._self_validate_schemas(
        SCHEMAS["catalog"], SCHEMAS["payload"], SCHEMAS["review"]
    )


def test_committed_catalog_is_empty_and_valid_without_case_enumeration() -> None:
    catalog_path = (
        REPO_ROOT / "benchmarks" / "manifests" / "unsealed_candidate_catalog.json"
    )
    assert _load(catalog_path)["candidates"] == []
    assert _validate(REPO_ROOT, catalog_path) == {
        "candidate_count": 0,
        "registered_file_count": 0,
    }


def test_valid_three_part_acyclic_registry(tmp_path: Path) -> None:
    root, catalog_path, catalog, manifest, attestation = _build_valid_registry(tmp_path)
    candidate = catalog["candidates"][0]
    assert {entry["file_role"] for entry in manifest["files"]} == {
        "solver_payload",
        "evaluator_control",
    }
    assert not candidate["review_attestation"]["path"].startswith(
        candidate["package_root"]
    )
    assert attestation["reviewed_payload_manifest_sha256"] == candidate[
        "payload_manifest"
    ]["sha256"]
    assert _validate(root, catalog_path) == {
        "candidate_count": 1,
        "registered_file_count": 4,
    }


@pytest.mark.parametrize(
    ("scope", "field", "expected"),
    [
        ("policy", key, value)
        for key, value in VALIDATOR._EXPECTED_CATALOG_POLICY.items()
    ]
    + [
        ("governance", key, value)
        for key, value in VALIDATOR._EXPECTED_CANDIDATE_GOVERNANCE.items()
    ],
)
@pytest.mark.parametrize("mutation", ["remove", "reverse"])
def test_closed_governance_is_required_and_immutable(
    tmp_path: Path,
    scope: str,
    field: str,
    expected: object,
    mutation: str,
) -> None:
    root, catalog_path, catalog, _, _ = _build_valid_registry(tmp_path)
    target = (
        catalog["policy"]
        if scope == "policy"
        else catalog["candidates"][0]["governance"]
    )
    if mutation == "remove":
        assert target.pop(field) == expected
    else:
        target[field] = _different(expected)
    _write_json(catalog_path, catalog)
    with pytest.raises(VALIDATOR.RegistryValidationError):
        _validate(root, catalog_path)


@pytest.mark.parametrize(
    ("schema_name", "closed_objects"),
    [
        ("catalog", VALIDATOR._CATALOG_CLOSED_OBJECTS),
        ("payload", VALIDATOR._PAYLOAD_CLOSED_OBJECTS),
        ("review", VALIDATOR._REVIEW_CLOSED_OBJECTS),
    ],
)
def test_every_closed_schema_object_is_pinned(
    tmp_path: Path, schema_name: str, closed_objects: dict
) -> None:
    root, catalog_path, _, _, _ = _build_valid_registry(tmp_path)
    for object_path, required_fields in closed_objects.items():
        for mode in ("additional", "required"):
            schemas = copy.deepcopy(SCHEMAS)
            node = _at(schemas[schema_name], object_path)
            if mode == "additional":
                node["additionalProperties"] = True
            else:
                node["required"].remove(required_fields[0])
            Draft202012Validator.check_schema(schemas[schema_name])
            with pytest.raises(VALIDATOR.RegistryValidationError):
                _validate_with_schemas(tmp_path, root, catalog_path, schemas)


@pytest.mark.parametrize(
    ("schema_name", "consts"),
    [
        ("catalog", VALIDATOR._CATALOG_CONSTS),
        ("payload", VALIDATOR._PAYLOAD_CONSTS),
        ("review", VALIDATOR._REVIEW_CONSTS),
    ],
)
def test_every_schema_const_is_pinned(
    tmp_path: Path, schema_name: str, consts: dict
) -> None:
    root, catalog_path, _, _, _ = _build_valid_registry(tmp_path)
    for const_path, expected in consts.items():
        schemas = copy.deepcopy(SCHEMAS)
        node = _at(schemas[schema_name], const_path)
        node["const"] = _different(expected)
        Draft202012Validator.check_schema(schemas[schema_name])
        with pytest.raises(VALIDATOR.RegistryValidationError):
            _validate_with_schemas(tmp_path, root, catalog_path, schemas)


@pytest.mark.parametrize("schema_name", ["catalog", "payload", "review"])
def test_metaschema_valid_schema_replacement_is_rejected(
    tmp_path: Path, schema_name: str
) -> None:
    root, catalog_path, _, _, _ = _build_valid_registry(tmp_path)
    schemas = copy.deepcopy(SCHEMAS)
    schemas[schema_name] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
    }
    Draft202012Validator.check_schema(schemas[schema_name])
    with pytest.raises(VALIDATOR.RegistryValidationError):
        _validate_with_schemas(tmp_path, root, catalog_path, schemas)


def test_hardcoded_governance_precedes_instance_schema(tmp_path: Path, monkeypatch) -> None:
    root, catalog_path, catalog, _, _ = _build_valid_registry(tmp_path)
    catalog["policy"]["agent_body_input_forbidden"] = False
    _write_json(catalog_path, catalog)

    def must_not_run(*args, **kwargs):
        raise AssertionError("instance schema ran first")

    monkeypatch.setattr(VALIDATOR, "_validate_schema", must_not_run)
    with pytest.raises(VALIDATOR.RegistryValidationError):
        _validate(root, catalog_path)


def test_cli_exposes_no_schema_override() -> None:
    help_result = subprocess.run(
        [sys.executable, str(VALIDATOR_PATH), "--help"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0
    for option in ("--catalog-schema", "--payload-schema", "--review-schema"):
        assert option not in help_result.stdout
        rejected = subprocess.run(
            [sys.executable, str(VALIDATOR_PATH), option, "opaque.json"],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert rejected.returncode != 0


@pytest.mark.parametrize("forbidden", ["sealed", "year", "historical_case_count"])
def test_formal_or_public_history_claims_are_rejected(
    tmp_path: Path, forbidden: str
) -> None:
    root, catalog_path, catalog, _, _ = _build_valid_registry(tmp_path)
    catalog["candidates"][0][forbidden] = True
    _write_json(catalog_path, catalog)
    with pytest.raises(VALIDATOR.RegistryValidationError):
        _validate(root, catalog_path)


@pytest.mark.parametrize("party", ["payload_file", "manifest_catalog", "review_catalog", "attestation"])
def test_changing_any_bound_party_is_rejected(tmp_path: Path, party: str) -> None:
    root, catalog_path, catalog, _, attestation = _build_valid_registry(tmp_path)
    candidate = catalog["candidates"][0]
    if party == "payload_file":
        _path(root, candidate["package_root"] + "visible/opaque.bin").write_bytes(b"x\n")
    elif party == "manifest_catalog":
        candidate["payload_manifest"]["sha256"] = "0" * 64
    elif party == "review_catalog":
        candidate["review_attestation"]["reviewed_payload_manifest_sha256"] = "0" * 64
    else:
        attestation["reviewed_payload_manifest_sha256"] = "0" * 64
        _rewrite_attestation(root, catalog, attestation)
    _write_json(catalog_path, catalog)
    with pytest.raises(VALIDATOR.RegistryValidationError):
        _validate(root, catalog_path)


def test_attestation_canonical_payload_binding_is_required(tmp_path: Path) -> None:
    root, catalog_path, catalog, _, attestation = _build_valid_registry(tmp_path)
    registration = catalog["candidates"][0]["review_attestation"]
    attestation["attestation_payload_sha256"] = "0" * 64
    path = _path(root, registration["path"])
    _write_json(path, attestation)
    registration.update(_digest(path))
    _write_json(catalog_path, catalog)
    with pytest.raises(VALIDATOR.RegistryValidationError):
        _validate(root, catalog_path)


def test_review_file_inside_package_is_rejected_by_package_closure(tmp_path: Path) -> None:
    root, catalog_path, catalog, _, _ = _build_valid_registry(tmp_path)
    candidate = catalog["candidates"][0]
    source = _path(root, candidate["review_attestation"]["path"])
    destination = _path(root, candidate["package_root"] + "review_attestation.json")
    destination.write_bytes(source.read_bytes())
    with pytest.raises(VALIDATOR.RegistryValidationError):
        _validate(root, catalog_path)


def test_review_role_in_payload_manifest_is_rejected(tmp_path: Path) -> None:
    root, catalog_path, catalog, manifest, attestation = _build_valid_registry(tmp_path)
    candidate = catalog["candidates"][0]
    source = _path(root, candidate["review_attestation"]["path"])
    copied = _path(root, candidate["package_root"] + "controls/review.bin")
    copied.write_bytes(source.read_bytes())
    manifest["files"].append(
        {
            "relative_path": "controls/review.bin",
            **_digest(copied),
            "file_role": "independent_review_evidence",
            "audience": "evaluator_only",
        }
    )
    _rewrite_manifest_and_review(root, catalog, manifest, attestation)
    _write_json(catalog_path, catalog)
    with pytest.raises(VALIDATOR.RegistryValidationError):
        _validate(root, catalog_path)


@pytest.mark.parametrize("audience", ["solver_visible", "evaluator_only"])
def test_review_bytes_cannot_enter_solver_or_hidden_package_closure(
    tmp_path: Path, audience: str
) -> None:
    root, catalog_path, catalog, manifest, attestation = _build_valid_registry(tmp_path)
    candidate = catalog["candidates"][0]
    source = _path(root, candidate["review_attestation"]["path"])
    relative = "visible/review_attestation.json" if audience == "solver_visible" else "controls/review_attestation.json"
    copied = _path(root, candidate["package_root"] + relative)
    copied.parent.mkdir(parents=True, exist_ok=True)
    copied.write_bytes(source.read_bytes())
    role = "solver_payload" if audience == "solver_visible" else "evaluator_control"
    manifest["files"].append(
        {
            "relative_path": relative,
            **_digest(copied),
            "file_role": role,
            "audience": audience,
        }
    )
    if audience == "solver_visible":
        candidate["mount_contract"]["solver_visible_allowlist"].append(
            {
                "source_relative_path": relative,
                "target_path": "/evaluation/input/review_attestation.json",
                **_digest(copied),
                "access": "read_only",
            }
        )
    else:
        candidate["mount_contract"]["solver_hidden_paths"].append(relative)
    _rewrite_manifest_and_review(root, catalog, manifest, attestation)
    _write_json(catalog_path, catalog)
    with pytest.raises(VALIDATOR.RegistryValidationError):
        _validate(root, catalog_path)


def test_review_root_extra_file_is_rejected(tmp_path: Path) -> None:
    root, catalog_path, catalog, _, _ = _build_valid_registry(tmp_path)
    extra = _path(root, catalog["candidates"][0]["review_root"] + "extra.bin")
    extra.write_bytes(b"extra\n")
    with pytest.raises(VALIDATOR.RegistryValidationError):
        _validate(root, catalog_path)


def test_payload_manifest_self_registration_is_rejected(tmp_path: Path) -> None:
    root, catalog_path, catalog, manifest, attestation = _build_valid_registry(tmp_path)
    candidate = catalog["candidates"][0]
    manifest_path = _path(root, candidate["payload_manifest"]["path"])
    manifest["files"].append(
        {
            "relative_path": "payload_manifest.json",
            **_digest(manifest_path),
            "file_role": "evaluator_control",
            "audience": "evaluator_only",
        }
    )
    _rewrite_manifest_and_review(root, catalog, manifest, attestation)
    _write_json(catalog_path, catalog)
    with pytest.raises(VALIDATOR.RegistryValidationError):
        _validate(root, catalog_path)


@pytest.mark.parametrize(
    "mutation",
    ["traversal", "duplicate_manifest", "duplicate_mount", "wrong_size", "undeclared"],
)
def test_paths_digests_and_closures_fail_closed(tmp_path: Path, mutation: str) -> None:
    root, catalog_path, catalog, manifest, attestation = _build_valid_registry(tmp_path)
    candidate = catalog["candidates"][0]
    if mutation == "traversal":
        candidate["mount_contract"]["solver_hidden_paths"][0] = "../opaque.bin"
    elif mutation == "duplicate_manifest":
        duplicate = copy.deepcopy(manifest["files"][0])
        duplicate["relative_path"] = "Visible/Opaque.bin"
        manifest["files"].append(duplicate)
        _rewrite_manifest_and_review(root, catalog, manifest, attestation)
    elif mutation == "duplicate_mount":
        duplicate = copy.deepcopy(
            candidate["mount_contract"]["solver_visible_allowlist"][0]
        )
        duplicate["target_path"] = "/evaluation/input/other.bin"
        candidate["mount_contract"]["solver_visible_allowlist"].append(duplicate)
    elif mutation == "wrong_size":
        candidate["payload_manifest"]["size_bytes"] += 1
    else:
        _path(root, candidate["package_root"] + "controls/extra.bin").write_bytes(b"x")
    _write_json(catalog_path, catalog)
    with pytest.raises(VALIDATOR.RegistryValidationError):
        _validate(root, catalog_path)


def test_cross_candidate_review_path_reuse_is_rejected(tmp_path: Path) -> None:
    root, catalog_path, catalog, _, _ = _build_valid_registry(tmp_path)
    second = copy.deepcopy(catalog["candidates"][0])
    second["candidate_id"] = "uc-c2c2c2c2c2c2c2c2"
    catalog["candidates"].append(second)
    _write_json(catalog_path, catalog)
    with pytest.raises(VALIDATOR.RegistryValidationError):
        _validate(root, catalog_path)


def test_symlinked_registered_payload_is_rejected(tmp_path: Path) -> None:
    root, catalog_path, catalog, _, _ = _build_valid_registry(tmp_path)
    payload = _path(root, catalog["candidates"][0]["package_root"] + "visible/opaque.bin")
    outside = root / "outside.bin"
    outside.write_bytes(payload.read_bytes())
    payload.unlink()
    try:
        payload.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(VALIDATOR.RegistryValidationError):
        _validate(root, catalog_path)


def test_windows_reparse_and_symlink_modes_are_unsafe() -> None:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    class ReparseMetadata:
        st_mode = stat.S_IFREG
        st_file_attributes = reparse_flag

    class LinkMetadata:
        st_mode = stat.S_IFLNK
        st_file_attributes = 0

    class FakePath:
        def __init__(self, metadata):
            self.metadata = metadata

        def lstat(self):
            return self.metadata

    assert VALIDATOR._is_link_or_reparse(FakePath(ReparseMetadata())) is True
    assert VALIDATOR._is_link_or_reparse(FakePath(LinkMetadata())) is True
