"""Loading and meta-validation for the approved packet schemas."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from modeling_harness.config import ConfigError, load_json


DRAFT_2020_12_URI = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_FILENAMES = (
    "ArtifactManifest.json",
    "DTPV1.json",
    "DirectUserExecutableInstructionV1.json",
    "IndependentGeneralResearchManifestV1.json",
    "MAPV1.json",
    "OpaqueEvaluationReceiptV1.json",
    "ResultPacket.json",
    "ReviewPacket.json",
    "RevisionDecision.json",
    "RoleCharter.json",
    "SourceProvenanceManifest.json",
    "TaskPacket.json",
)
BOUNDARY_SCHEMA_FILENAMES = (
    "DirectUserExecutableInstructionV1.json",
    "IndependentGeneralResearchManifestV1.json",
    "DTPV1.json",
    "MAPV1.json",
    "OpaqueEvaluationReceiptV1.json",
)
BOUNDARY_EXACT_FIELDS = MappingProxyType(
    {
        "DirectUserExecutableInstructionV1": frozenset(
            {
                "schema_version",
                "instruction",
                "governance_document_sha256",
                "instruction_sha256",
                "independent_review_sha256",
                "agent_id",
                "package_version",
            }
        ),
        "IndependentGeneralResearchManifestV1": frozenset(
            {
                "schema_version",
                "agent_id",
                "package_version",
                "manifest_sha256",
                "independent_review_sha256",
                "capability",
                "change_class",
                "impact",
                "rollback",
                "verification_plan",
                "stage",
                "purpose",
            }
        ),
        "DTPV1": frozenset(
            {
                "schema_version",
                "agent_id",
                "package_version",
                "research_manifest_sha256",
                "independent_review_sha256",
                "capability",
                "change_class",
                "impact",
                "rollback",
                "verification_plan",
                "stage",
                "purpose",
                "dtp_sha256",
            }
        ),
        "MAPV1": frozenset(
            {
                "schema_version",
                "agent_id",
                "package_version",
                "research_manifest_sha256",
                "independent_review_sha256",
                "capability",
                "change_class",
                "impact",
                "rollback",
                "verification_plan",
                "stage",
                "purpose",
                "dtp_sha256",
                "map_sha256",
            }
        ),
        "OpaqueEvaluationReceiptV1": frozenset(
            {
                "schema_version",
                "agent_id",
                "package_version",
                "candidate_sha256",
                "verification_plan_sha256",
                "opaque_receipt_sha256",
                "decision",
            }
        ),
    }
)


class SchemaRegistryError(ConfigError):
    """Raised when the approved JSON Schema registry is invalid."""


@dataclass(frozen=True)
class SchemaRegistry(Mapping[str, Mapping[str, Any]]):
    """Read-only mapping from schema title to schema document."""

    _schemas: Mapping[str, Mapping[str, Any]]
    _schema_hashes: Mapping[str, str]

    def __getitem__(self, name: str) -> Mapping[str, Any]:
        return self._schemas[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._schemas)

    def __len__(self) -> int:
        return len(self._schemas)

    def validator(self, name: str) -> Draft202012Validator:
        """Build a Draft 2020-12 validator for one registered schema."""

        try:
            schema = self._schemas[name]
        except KeyError as exc:
            raise KeyError(f"unknown schema {name!r}") from exc
        return Draft202012Validator(schema)

    @property
    def schema_hashes(self) -> Mapping[str, str]:
        return self._schema_hashes


def canonical_schema_sha256(schema: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SchemaRegistryError("schema is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _validate_exact_root_schema(
    document: Mapping[str, Any],
    *,
    path: Path,
) -> None:
    if document.get("type") != "object":
        raise SchemaRegistryError(f"{path} must describe an object at its root")
    if document.get("additionalProperties") is not False:
        raise SchemaRegistryError(f"{path} must reject additional root properties")
    properties = document.get("properties")
    required = document.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise SchemaRegistryError(f"{path} requires properties and required fields")
    if not all(type(field) is str for field in required):
        raise SchemaRegistryError(f"{path} required fields must be strings")
    if len(required) != len(set(required)):
        raise SchemaRegistryError(f"{path} contains duplicate required fields")
    if not set(required) <= set(properties):
        raise SchemaRegistryError(
            f"{path} required fields must be declared as root properties"
        )


def load_schema_registry(schema_directory: str | Path) -> SchemaRegistry:
    """Load and meta-check the complete approved Draft 2020-12 schema set."""

    directory = Path(schema_directory)
    if not directory.is_dir():
        raise SchemaRegistryError(f"schema directory does not exist: {directory}")

    expected = set(SCHEMA_FILENAMES)
    actual = {path.name for path in directory.glob("*.json") if path.is_file()}
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected: {', '.join(unexpected)}")
        raise SchemaRegistryError(
            "schema registry must contain exactly the approved schemas "
            f"({'; '.join(details)})"
        )

    loaded: dict[str, Mapping[str, Any]] = {}
    for filename in SCHEMA_FILENAMES:
        path = directory / filename
        document = load_json(path)
        if not isinstance(document, dict):
            raise SchemaRegistryError(f"schema must be a JSON object: {path}")
        if document.get("$schema") != DRAFT_2020_12_URI:
            raise SchemaRegistryError(
                f"{path} must declare JSON Schema Draft 2020-12 via "
                f"$schema={DRAFT_2020_12_URI!r}"
            )

        try:
            Draft202012Validator.check_schema(document)
        except SchemaError as exc:
            location = "/".join(str(part) for part in exc.path)
            suffix = f" at {location}" if location else ""
            raise SchemaRegistryError(
                f"invalid Draft 2020-12 schema {path}{suffix}: {exc.message}"
            ) from exc

        _validate_exact_root_schema(document, path=path)

        expected_name = Path(filename).stem
        title = document.get("title")
        if title != expected_name:
            raise SchemaRegistryError(
                f"{path} must have title {expected_name!r}, got {title!r}"
            )
        loaded[expected_name] = document

        if filename in BOUNDARY_SCHEMA_FILENAMES:
            actual_fields = frozenset(document["properties"])
            if actual_fields != BOUNDARY_EXACT_FIELDS[expected_name]:
                raise SchemaRegistryError(
                    f"{path} does not match its exact boundary field set"
                )
            if frozenset(document["required"]) != actual_fields:
                raise SchemaRegistryError(
                    f"{path} must require every exact boundary field"
                )

    hashes = {
        name: canonical_schema_sha256(document)
        for name, document in loaded.items()
    }
    return SchemaRegistry(MappingProxyType(loaded), MappingProxyType(hashes))
