"""Schema and cross-document validation for harness protocol objects.

Schema validation establishes the shape of an object.  The checks in this
module establish relationships that JSON Schema cannot express, such as a
ResultPacket belonging to the TaskPacket that launched it and a reviewer being
independent from the producer of the candidate under review.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import sqlite3
from threading import Lock
from typing import Any, Protocol

from jsonschema import Draft202012Validator, FormatChecker

from modeling_harness.config import load_json, load_yaml
from modeling_harness.schemas import SchemaRegistry, load_schema_registry


PACKET_TYPES = (
    "TaskPacket",
    "ResultPacket",
    "ReviewPacket",
    "RevisionDecision",
    "ArtifactManifest",
    "RoleCharter",
    "SourceProvenanceManifest",
)
REVIEW_PROFILE_BY_ROLE = {
    "mathematical_reviewer": "mathematical-logic",
    "reproducibility_reviewer": "numerical-reproducibility",
    "evidence_communication_reviewer": "evidence-communication",
}


class PacketError(ValueError):
    """Base class for rejected protocol objects."""


class PacketSchemaError(PacketError):
    """Raised when an object does not satisfy its Draft 2020-12 schema."""


class PacketSemanticError(PacketError):
    """Raised when valid-looking objects violate a cross-object invariant."""


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Return the stable UTF-8 representation used by protocol hashes."""

    try:
        text = json.dumps(
            _plain(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PacketSemanticError(f"object is not canonical JSON: {exc}") from exc
    return text.encode("utf-8")


def sha256_json(value: Mapping[str, Any]) -> str:
    """Hash one protocol object using its canonical JSON representation."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class PacketIdentityLedger(Protocol):
    """Append-only packet identity sink used after complete validation."""

    @property
    def durable(self) -> bool: ...

    def record(self, packet_id: str, content_sha256: str) -> None: ...


class InMemoryPacketIdentityLedger:
    """Thread-safe process-local ledger, primarily for tests."""

    def __init__(self) -> None:
        self._identities: dict[str, str] = {}
        self._lock = Lock()

    @property
    def durable(self) -> bool:
        return False

    def record(self, packet_id: str, content_sha256: str) -> None:
        with self._lock:
            previous = self._identities.get(packet_id)
            if previous is not None and previous != content_sha256:
                raise PacketSemanticError(
                    f"packet_id {packet_id!r} was already observed with different content"
                )
            self._identities.setdefault(packet_id, content_sha256)


class SQLitePacketIdentityLedger:
    """Persistent append-only packet identity ledger.

    Rows are inserted once under a primary key and are never updated. SQLite's
    immediate transaction provides cross-process serialization.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS packet_identity ("
                "packet_id TEXT PRIMARY KEY, content_sha256 TEXT NOT NULL)"
            )
            connection.commit()

    @property
    def durable(self) -> bool:
        return True

    def record(self, packet_id: str, content_sha256: str) -> None:
        with sqlite3.connect(self.path, timeout=30.0, isolation_level=None) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT content_sha256 FROM packet_identity WHERE packet_id = ?",
                (packet_id,),
            ).fetchone()
            if row is not None:
                connection.rollback()
                if row[0] != content_sha256:
                    raise PacketSemanticError(
                        f"packet_id {packet_id!r} was already observed with "
                        "different content"
                    )
                return
            connection.execute(
                "INSERT INTO packet_identity(packet_id, content_sha256) VALUES (?, ?)",
                (packet_id, content_sha256),
            )
            connection.commit()


@dataclass(frozen=True)
class PacketContext:
    """Related immutable objects required for cross-document validation."""

    task_packet: Mapping[str, Any] | None = None
    artifact_manifest: Mapping[str, Any] | None = None
    candidate_manifest: Mapping[str, Any] | None = None
    source_manifest: Mapping[str, Any] | None = None
    source_charter: Mapping[str, Any] | None = None
    input_manifests: Sequence[Mapping[str, Any]] = ()
    source_reviews: Sequence[Mapping[str, Any]] = ()
    enforce_parent: bool = False
    expected_parent_packet_id: str | None = None


class PacketValidator:
    """Validate the approved protocol object types."""

    def __init__(
        self,
        schemas: SchemaRegistry,
        role_registry: Mapping[str, Any],
        identity_ledger: PacketIdentityLedger | None = None,
    ) -> None:
        self.schemas = schemas
        entries = role_registry.get("roles")
        if not isinstance(entries, list) or not all(
            isinstance(entry, dict) for entry in entries
        ):
            raise PacketSemanticError("role registry must contain a roles list")

        self.roles: dict[str, Mapping[str, Any]] = {}
        for entry in entries:
            role_id = entry.get("id")
            if not isinstance(role_id, str):
                raise PacketSemanticError("role registry contains an invalid role ID")
            if role_id in self.roles:
                raise PacketSemanticError(f"duplicate registered role {role_id!r}")
            self.roles[role_id] = entry
        if "main_agent" not in self.roles:
            raise PacketSemanticError("role registry is missing main_agent")

        self.identity_ledger = identity_ledger or InMemoryPacketIdentityLedger()

    @classmethod
    def from_project_root(
        cls,
        project_root: str | Path,
        *,
        identity_ledger: PacketIdentityLedger | None = None,
    ) -> "PacketValidator":
        root = Path(project_root)
        schemas = load_schema_registry(root / "workspaces/architect/schemas")
        registry = load_yaml(root / "workspaces/architect/role_registry.yaml")
        if not isinstance(registry, dict):
            raise PacketSemanticError("role registry must be a YAML mapping")
        return cls(schemas, registry, identity_ledger=identity_ledger)

    def validate_file(
        self,
        packet_type: str,
        path: str | Path,
        *,
        context: PacketContext | None = None,
    ) -> dict[str, Any]:
        document = load_json(path)
        if not isinstance(document, dict):
            raise PacketSchemaError(f"{packet_type} must be a JSON object")
        self.validate(packet_type, document, context=context)
        return document

    def validate_schema_file(
        self, packet_type: str, path: str | Path
    ) -> dict[str, Any]:
        document = load_json(path)
        if not isinstance(document, dict):
            raise PacketSchemaError(f"{packet_type} must be a JSON object")
        self.validate_schema(packet_type, document)
        return document

    def validate_schema(
        self,
        packet_type: str,
        document: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Validate shape only; this does not establish trust or record identity."""

        if packet_type not in PACKET_TYPES:
            raise PacketSchemaError(
                f"unknown packet type {packet_type!r}; expected one of "
                f"{', '.join(PACKET_TYPES)}"
            )
        if not isinstance(document, Mapping):
            raise PacketSchemaError(f"{packet_type} must be a JSON object")

        validator = Draft202012Validator(
            self.schemas[packet_type],
            format_checker=FormatChecker(),
        )
        errors = sorted(
            validator.iter_errors(_plain(document)),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            error = errors[0]
            location = "/".join(str(part) for part in error.absolute_path)
            where = f" at /{location}" if location else ""
            raise PacketSchemaError(
                f"{packet_type} schema violation{where}: {error.message}"
            )
        return document

    def validate(
        self,
        packet_type: str,
        document: Mapping[str, Any],
        *,
        context: PacketContext | None = None,
    ) -> Mapping[str, Any]:
        """Validate schema and all required cross-document relationships."""

        self.validate_schema(packet_type, document)
        semantic = getattr(self, f"_validate_{packet_type}")
        semantic(document, context or PacketContext())
        self._record_packet_identity(document)
        return document

    def validate_bundle(
        self,
        packet_type: str,
        document: Mapping[str, Any],
        *,
        context: PacketContext,
    ) -> Mapping[str, Any]:
        """Explicit name for strict schema-plus-context validation."""

        return self.validate(packet_type, document, context=context)

    def _record_packet_identity(self, document: Mapping[str, Any]) -> None:
        packet_id = document.get("packet_id")
        if not isinstance(packet_id, str):
            return
        digest = sha256_json(document)
        self.identity_ledger.record(packet_id, digest)

    def _role(self, role_id: Any, *, allow_main: bool = False) -> Mapping[str, Any]:
        if not isinstance(role_id, str) or role_id not in self.roles:
            raise PacketSemanticError(f"unknown role_id {role_id!r}")
        if not allow_main and role_id == "main_agent":
            raise PacketSemanticError("main_agent cannot be a Subagent packet role")
        return self.roles[role_id]

    @staticmethod
    def _require_equal(
        actual: Any,
        expected: Any,
        description: str,
    ) -> None:
        if actual != expected:
            raise PacketSemanticError(
                f"{description} mismatch: expected {expected!r}, got {actual!r}"
            )

    def _validate_TaskPacket(
        self,
        document: Mapping[str, Any],
        context: PacketContext,
    ) -> None:
        role = self._role(document["role_id"])
        from modeling_harness.governance import (
            GovernanceError,
            validate_role_data_boundary,
            validate_task_packet_narrative,
        )

        try:
            validate_role_data_boundary(
                role_id=document["role_id"],
                role_kind=str(role.get("kind", "")),
                task_purpose=document["task_purpose"],
                content_kinds=(
                    item["content_kind"] for item in document["input_artifacts"]
                ),
            )
            validate_task_packet_narrative(
                document,
                role_id=document["role_id"],
                task_purpose=document["task_purpose"],
            )
        except GovernanceError as exc:
            raise PacketSemanticError(str(exc)) from exc
        workspace = document["workspace"]
        path = PurePosixPath(workspace["write_root"])
        parts = path.parts
        if len(parts) != 6 or parts[0] != "/" or parts[1] != "runs":
            raise PacketSemanticError("workspace.write_root is not a canonical run path")
        self._require_equal(parts[3], document["task_id"], "write_root task_id")
        self._require_equal(parts[4], document["role_id"], "write_root role_id")
        self._require_equal(parts[5], document["attempt_id"], "write_root attempt_id")

        input_mounts = {item["mount_path"] for item in document["input_artifacts"]}
        declared_mounts = set(workspace["read_only_mounts"])
        for mount_path in input_mounts | declared_mounts:
            mount = PurePosixPath(mount_path)
            if (
                not mount.is_absolute()
                or len(mount.parts) < 3
                or mount.parts[1] != "inputs"
                or any(part in {".", ".."} for part in mount.parts)
            ):
                raise PacketSemanticError(f"unsafe read-only mount {mount_path!r}")
        if input_mounts != declared_mounts:
            raise PacketSemanticError(
                "workspace.read_only_mounts must exactly match input artifact mounts"
            )

        role_network = role.get("network_policy")
        self._require_equal(
            document["execution_limits"]["network_policy"],
            role_network,
            "role network policy",
        )

        hidden_inputs = any(
            item["classification"] == "benchmark-hidden"
            for item in document["input_artifacts"]
        )
        hidden_level = document["benchmark_access_level"] == "hidden-benchmark-admin"
        is_curator = document["role_id"] == "benchmark_curator"
        if (hidden_inputs or hidden_level) and not is_curator:
            raise PacketSemanticError(
                "benchmark-hidden input/access is restricted to benchmark_curator"
            )
        if is_curator and role.get("hidden_benchmark_access") is not True:
            raise PacketSemanticError(
                "benchmark_curator registry entry lacks hidden benchmark permission"
            )

        if document["input_artifacts"]:
            if not context.input_manifests:
                raise PacketSemanticError(
                    "TaskPacket input artifacts require promoted manifest context"
                )
            expected = sorted(
                item["manifest_sha256"] for item in document["input_artifacts"]
            )
            actual = sorted(
                sha256_json(manifest) for manifest in context.input_manifests
            )
            self._require_equal(actual, expected, "TaskPacket input manifest lineage")
            manifest_by_hash: dict[str, Mapping[str, Any]] = {}
            for manifest in context.input_manifests:
                self.validate_schema("ArtifactManifest", manifest)
                if manifest.get("promotion", {}).get("status") != "promoted":
                    raise PacketSemanticError(
                        "TaskPacket inputs must be promoted immutable manifests"
                    )
                manifest_by_hash[sha256_json(manifest)] = manifest
            for reference in document["input_artifacts"]:
                manifest = manifest_by_hash.get(reference["manifest_sha256"])
                if manifest is None:
                    raise PacketSemanticError(
                        "TaskPacket input has no trusted promoted manifest"
                    )
                matches = [
                    artifact
                    for artifact in manifest["artifacts"]
                    if artifact["artifact_id"] == reference["artifact_id"]
                ]
                if len(matches) != 1:
                    raise PacketSemanticError(
                        "TaskPacket artifact_id must bind exactly one promoted "
                        "manifest artifact"
                    )
                artifact = matches[0]
                for field in (
                    "content_sha256",
                    "logical_path",
                    "classification",
                    "content_kind",
                ):
                    if reference[field] != artifact[field]:
                        raise PacketSemanticError(
                            f"TaskPacket input {field} does not match promoted manifest"
                        )
                for field in (
                    "source_provenance_sha256s",
                    "real_task_derived",
                ):
                    if reference[field] != manifest[field]:
                        raise PacketSemanticError(
                            f"TaskPacket input {field} does not match promoted manifest"
                        )

        if context.enforce_parent:
            self._require_equal(
                document.get("parent_packet_id"),
                context.expected_parent_packet_id,
                "parent_packet_id",
            )

    def _validate_ResultPacket(
        self,
        document: Mapping[str, Any],
        context: PacketContext,
    ) -> None:
        self._role(document["role_id"])
        location = PurePosixPath(document["artifact_manifest"]["location"])
        if (
            not location.is_absolute()
            or len(location.parts) < 3
            or location.parts[1] != "runs"
            or any(part in {".", ".."} for part in location.parts)
        ):
            raise PacketSemanticError("unsafe ResultPacket manifest location")
        task = context.task_packet
        if task is None:
            raise PacketSemanticError("ResultPacket requires its originating TaskPacket")
        manifest = context.artifact_manifest
        if manifest is None:
            raise PacketSemanticError(
                "ResultPacket requires its referenced ArtifactManifest"
            )
        for field in ("task_id", "run_id", "attempt_id", "role_id"):
            self._require_equal(document[field], task[field], f"ResultPacket {field}")
        self._require_equal(
            document["task_packet_sha256"],
            sha256_json(task),
            "ResultPacket task_packet_sha256",
        )
        self._require_equal(
            document["provenance"]["prompt_sha256"],
            task["prompt"]["sha256"],
            "ResultPacket prompt_sha256",
        )
        expected_inputs = sorted(
            item["manifest_sha256"] for item in task["input_artifacts"]
        )
        self._require_equal(
            sorted(document["provenance"]["input_manifest_sha256s"]),
            expected_inputs,
            "ResultPacket input manifest lineage",
        )
        self._require_equal(
            document["artifact_manifest"]["manifest_sha256"],
            sha256_json(manifest),
            "ResultPacket artifact manifest hash",
        )
        self._require_equal(
            document["artifact_manifest"]["artifact_id"],
            manifest["manifest_id"],
            "ResultPacket artifact manifest ID",
        )
        for field in ("task_id", "run_id", "attempt_id"):
            self._require_equal(
                manifest[field], document[field], f"Result/Manifest {field}"
            )
        self._require_equal(
            manifest["producer"]["role_id"],
            document["role_id"],
            "Result/Manifest producer role",
        )

    def _validate_ReviewPacket(
        self,
        document: Mapping[str, Any],
        context: PacketContext,
    ) -> None:
        reviewer = document["reviewer_role_id"]
        role = self._role(reviewer)
        if role.get("kind") != "reviewer":
            raise PacketSemanticError(f"{reviewer!r} is not a registered reviewer")
        self._require_equal(
            document["review_profile"],
            REVIEW_PROFILE_BY_ROLE[reviewer],
            "review profile",
        )

        task = context.task_packet
        if task is None:
            raise PacketSemanticError("ReviewPacket requires its reviewer TaskPacket")
        for field in ("task_id", "run_id"):
            self._require_equal(
                document[field], task[field], f"ReviewPacket {field}"
            )
        self._require_equal(
            reviewer, task["role_id"], "ReviewPacket reviewer task role"
        )

        candidate = context.candidate_manifest
        if candidate is None:
            raise PacketSemanticError("ReviewPacket requires its candidate manifest")
        self._require_equal(
            document["candidate_manifest_sha256"],
            sha256_json(candidate),
            "review candidate manifest hash",
        )
        for field in ("task_id", "run_id"):
            self._require_equal(
                document[field], candidate[field], f"candidate {field}"
            )
        if task["attempt_id"] == candidate["attempt_id"]:
            raise PacketSemanticError("reviewer cannot inherit the author attempt")
        if (
            task["workspace"]["container_id"]
            == candidate["producer"]["container_id"]
        ):
            raise PacketSemanticError("reviewer cannot inherit the author container")
        author = candidate["producer"]["role_id"]
        if author == reviewer:
            raise PacketSemanticError("reviewer cannot review its own artifact")
        author_role = self._role(author)
        if author_role.get("kind") == "reviewer":
            raise PacketSemanticError(
                "a review candidate cannot be authored by a reviewer role"
            )

    def _validate_RevisionDecision(
        self,
        document: Mapping[str, Any],
        context: PacketContext,
    ) -> None:
        role = self._role(document["target_role_id"])
        if role.get("kind") != "author":
            raise PacketSemanticError(
                "RevisionDecision may target only a current-answer author"
            )
        if not context.source_reviews:
            raise PacketSemanticError(
                "RevisionDecision requires retained source reviews"
            )
        expected_ids = [review.get("packet_id") for review in context.source_reviews]
        expected_hashes = [sha256_json(review) for review in context.source_reviews]
        self._require_equal(
            sorted(document["source_review_packet_ids"]),
            sorted(expected_ids),
            "RevisionDecision source review IDs",
        )
        self._require_equal(
            sorted(document["source_review_hashes"]),
            sorted(expected_hashes),
            "RevisionDecision source review hashes",
        )
        for review in context.source_reviews:
            if review.get("task_id") != document["task_id"]:
                raise PacketSemanticError(
                    "RevisionDecision review belongs to a different task"
                )
            if review.get("run_id") != document["run_id"]:
                raise PacketSemanticError(
                    "RevisionDecision review belongs to a different run"
                )
            if review.get("visibility") != "main-agent-only":
                raise PacketSemanticError(
                    "RevisionDecision reviews must remain main-agent-only"
                )

    def _validate_SourceProvenanceManifest(
        self,
        document: Mapping[str, Any],
        context: PacketContext,
    ) -> None:
        del context
        real_source = document["source_class"] in {
            "public-real-task",
            "private-real-task",
        }
        if real_source and document["real_task_derived"] is not True:
            raise PacketSemanticError(
                "real source provenance must carry real-task-derived taint"
            )

    def _validate_ArtifactManifest(
        self,
        document: Mapping[str, Any],
        context: PacketContext,
    ) -> None:
        producer = document["producer"]
        self._role(producer["role_id"])
        artifact_ids = [item["artifact_id"] for item in document["artifacts"]]
        paths = [item["logical_path"] for item in document["artifacts"]]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise PacketSemanticError("artifact_id values must be unique")
        if len(paths) != len(set(paths)):
            raise PacketSemanticError("artifact logical_path values must be unique")
        canonical_paths = [path.casefold() for path in paths]
        if len(canonical_paths) != len(set(canonical_paths)):
            raise PacketSemanticError(
                "artifact logical_path values must be unique on "
                "case-insensitive filesystems"
            )
        for logical_path in paths:
            path = PurePosixPath(logical_path)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "." in path.parts
                or "\\" in logical_path
                or ":" in logical_path
            ):
                raise PacketSemanticError(
                    f"unsafe artifact logical_path {logical_path!r}"
                )

        promotion = document["promotion"]
        if promotion["status"] == "promoted":
            if (
                promotion["approved_by"] != "main_agent"
                or promotion["approved_at"] is None
                or promotion["source_manifest_sha256"] is None
            ):
                raise PacketSemanticError(
                    "promoted manifests require complete main_agent approval"
                )
        elif any(
            promotion[field] is not None
            for field in ("approved_by", "approved_at", "source_manifest_sha256")
        ):
            raise PacketSemanticError(
                "non-promoted manifests cannot carry promotion approval fields"
            )

        task = context.task_packet
        if task is None:
            raise PacketSemanticError(
                "ArtifactManifest requires its originating TaskPacket"
            )
        for field in ("task_id", "run_id", "attempt_id"):
            self._require_equal(document[field], task[field], f"manifest {field}")
        self._require_equal(
            producer["role_id"], task["role_id"], "manifest producer role"
        )
        self._require_equal(
            producer["workspace_id"],
            task["workspace"]["workspace_id"],
            "manifest workspace",
        )
        self._require_equal(
            producer["container_id"],
            task["workspace"]["container_id"],
            "manifest container",
        )
        self._require_equal(
            document["task_packet_sha256"],
            sha256_json(task),
            "manifest task_packet_sha256",
        )
        self._require_equal(
            document["prompt_sha256"],
            task["prompt"]["sha256"],
            "manifest prompt_sha256",
        )
        expected_inputs = sorted(
            item["manifest_sha256"] for item in task["input_artifacts"]
        )
        self._require_equal(
            sorted(document["input_manifest_sha256s"]),
            expected_inputs,
            "manifest input lineage",
        )
        if context.input_manifests:
            self._require_equal(
                sorted(sha256_json(item) for item in context.input_manifests),
                expected_inputs,
                "manifest verified input ancestry",
            )
            parent_provenance = {
                provenance_hash
                for item in context.input_manifests
                for provenance_hash in item["source_provenance_sha256s"]
            }
            if any(
                item["real_task_derived"] is True
                for item in context.input_manifests
            ) and document["real_task_derived"] is not True:
                raise PacketSemanticError(
                    "ArtifactManifest cannot clear input real-task-derived taint"
                )
            if parent_provenance and not document["source_provenance_sha256s"]:
                raise PacketSemanticError(
                    "ArtifactManifest cannot omit input source provenance"
                )
        if promotion["status"] == "promoted":
            source = context.source_manifest
            if source is None:
                raise PacketSemanticError(
                    "promoted ArtifactManifest requires its candidate source manifest"
                )
            self._require_equal(
                promotion["source_manifest_sha256"],
                sha256_json(source),
                "promotion source manifest hash",
            )
            if source.get("promotion", {}).get("status") != "candidate":
                raise PacketSemanticError(
                    "promotion source manifest must be a candidate"
                )
            for field in ("task_id", "run_id", "attempt_id", "producer"):
                self._require_equal(
                    document[field], source[field], f"promotion source {field}"
                )
            self._require_equal(
                document["manifest_version"],
                source["manifest_version"] + 1,
                "promoted manifest version",
            )

    def _validate_RoleCharter(
        self,
        document: Mapping[str, Any],
        context: PacketContext,
    ) -> None:
        role = self._role(document["role_id"])
        for charter_field, registry_field in (
            ("role_name", "name"),
            ("phase", "phase"),
            ("mission", "mission"),
        ):
            self._require_equal(
                document[charter_field],
                role.get(registry_field),
                f"RoleCharter {charter_field}",
            )

        for charter_field, registry_field in (
            ("responsibilities", "responsibilities"),
            ("prohibited_actions", "prohibited"),
        ):
            registered = set(role.get(registry_field, []))
            chartered = set(document[charter_field])
            if not registered.issubset(chartered):
                missing = ", ".join(sorted(registered - chartered))
                raise PacketSemanticError(
                    f"RoleCharter {charter_field} omits registered items: {missing}"
                )

        information = document["information_policy"]
        self._require_equal(
            information["write_scope"], role.get("workspace"), "charter write scope"
        )
        self._require_equal(
            information["hidden_benchmark_access"],
            role.get("hidden_benchmark_access"),
            "charter hidden benchmark access",
        )

        approval = document["approval"]
        if approval["status"] == "approved":
            if (
                approval["approved_by"] != "main_agent"
                or approval["approved_at"] is None
                or approval["charter_sha256"] is None
            ):
                raise PacketSemanticError(
                    "approved RoleCharter requires complete main_agent approval"
                )
            source = context.source_charter
            if source is None:
                raise PacketSemanticError(
                    "approved RoleCharter requires its draft source charter"
                )
            self._require_equal(
                approval["charter_sha256"],
                sha256_json(source),
                "RoleCharter source hash",
            )
            if source.get("approval", {}).get("status") != "draft":
                raise PacketSemanticError(
                    "approved RoleCharter source must be a draft"
                )
            for field in ("charter_id", "role_id", "charter_version"):
                self._require_equal(
                    document[field], source[field], f"RoleCharter source {field}"
                )
        elif any(
            approval[field] is not None
            for field in ("approved_by", "approved_at", "charter_sha256")
        ):
            raise PacketSemanticError(
                "non-approved RoleCharter cannot carry approval values"
            )
