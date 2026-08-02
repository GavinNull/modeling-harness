"""Immutable, content-addressed staging and promotion of harness artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from typing import Any

from modeling_harness.governance import (
    ProvenanceError,
    SourceProvenanceLedger,
    validate_artifact_provenance,
)
from modeling_harness.packets import (
    PacketContext,
    PacketSemanticError,
    PacketValidator,
    canonical_json_bytes,
    sha256_json,
)


class ArtifactStoreError(ValueError):
    """Base class for rejected artifact-store operations."""


class ArtifactIntegrityError(ArtifactStoreError):
    """Raised when content, size, manifest, or lineage does not verify."""


class ArtifactExistsError(ArtifactStoreError):
    """Raised when an immutable destination already exists."""


class ArtifactPathError(ArtifactStoreError):
    """Raised for traversal, symlink, or root-escape attempts."""


@dataclass(frozen=True)
class PromotionRecord:
    record_version: str
    source_manifest_sha256: str
    promoted_manifest_sha256: str
    approved_by: str
    approved_at: str
    artifact_sha256s: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        value = asdict(self)
        value["artifact_sha256s"] = list(self.artifact_sha256s)
        return value


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


_WINDOWS_REPARSE_POINT = 0x0400


def _absolute_without_resolving_links(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
    )


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.fspath(left)) == os.path.normcase(os.fspath(right))


def _is_within(path: Path, root: Path) -> bool:
    try:
        common = Path(os.path.commonpath((os.fspath(path), os.fspath(root))))
    except ValueError:
        return False
    return _same_path(common, root)


def _reject_link_components(path: Path) -> None:
    absolute = _absolute_without_resolving_links(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if _is_link_or_reparse(current):
            raise ArtifactPathError(f"path uses a symlink or reparse point: {path}")


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_nlink,
    )


def _directory_inventory(root: Path) -> tuple[tuple[Any, ...], ...]:
    """Snapshot directory structure and file identities without following links."""

    _reject_link_components(root)
    entries: list[tuple[Any, ...]] = []
    canonical_names: set[str] = set()

    def visit(directory: Path) -> None:
        for child in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
            _reject_link_components(child)
            relative = child.relative_to(root).as_posix()
            canonical = relative.casefold()
            if canonical in canonical_names:
                raise ArtifactPathError(
                    f"case-insensitive path collision in source tree: {relative}"
                )
            canonical_names.add(canonical)
            metadata = os.lstat(child)
            if stat.S_ISDIR(metadata.st_mode):
                entries.append(("directory", relative, *_file_identity(metadata)))
                visit(child)
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise ArtifactPathError(
                        f"hard-linked artifact inputs are forbidden: {relative}"
                    )
                entries.append(("file", relative, *_file_identity(metadata)))
            else:
                raise ArtifactPathError(
                    f"special artifact inputs are forbidden: {relative}"
                )

    visit(root)
    return tuple(entries)


def _write_bytes_fsync(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        # Windows does not consistently allow opening directories for fsync.
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            with path.open("r+b") as handle:
                os.fsync(handle.fileno())
    for directory in sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        _fsync_directory(directory)
    _fsync_directory(root)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_artifact_path(root: Path, logical_path: str, *, must_exist: bool) -> Path:
    relative = PurePosixPath(logical_path)
    if relative.is_absolute() or not relative.parts:
        raise ArtifactPathError(f"artifact path must be relative: {logical_path!r}")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ArtifactPathError(f"unsafe artifact path: {logical_path!r}")
    if "\\" in logical_path or ":" in logical_path:
        raise ArtifactPathError(
            f"artifact path is not canonical POSIX form: {logical_path!r}"
        )

    root_declared = _absolute_without_resolving_links(root)
    _reject_link_components(root_declared)
    root_resolved = root_declared.resolve(strict=True)
    candidate = root.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as exc:
        raise ArtifactPathError(
            f"artifact path cannot be resolved: {logical_path!r}: {exc}"
        ) from exc
    if not _is_within(resolved, root_resolved):
        raise ArtifactPathError(f"artifact path escapes its root: {logical_path!r}")

    current = root_declared
    for part in relative.parts:
        current = current / part
        if current.exists() and _is_link_or_reparse(current):
            raise ArtifactPathError(
                f"artifact path uses a symlink or reparse point: {logical_path!r}"
            )
    return resolved


def _atomic_create_bytes(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ArtifactExistsError(f"immutable destination exists: {destination}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise ArtifactExistsError(
                f"immutable destination exists: {destination}"
            ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _copy_regular_file_verified(source: Path, destination: Path) -> None:
    """Copy one regular single-link file and detect replacement during the copy."""

    _reject_link_components(source)
    before = os.lstat(source)
    if not stat.S_ISREG(before.st_mode):
        raise ArtifactPathError(f"artifact input is not a regular file: {source}")
    if before.st_nlink != 1:
        raise ArtifactPathError(f"hard-linked artifact input is forbidden: {source}")

    binary = getattr(os, "O_BINARY", 0)
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | binary
    source_fd = os.open(source, source_flags)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | binary
    destination_fd = os.open(destination, destination_flags, 0o600)
    try:
        opened = os.fstat(source_fd)
        if _file_identity(opened) != _file_identity(before):
            raise ArtifactIntegrityError("artifact source changed before copy")
        while True:
            block = os.read(source_fd, 1024 * 1024)
            if not block:
                break
            view = memoryview(block)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
        os.fsync(destination_fd)
        after_open = os.fstat(source_fd)
    finally:
        os.close(source_fd)
        os.close(destination_fd)

    after_path = os.lstat(source)
    if (
        _file_identity(after_open) != _file_identity(before)
        or _file_identity(after_path) != _file_identity(before)
    ):
        raise ArtifactIntegrityError("artifact source changed during copy")
    copied = os.lstat(destination)
    if not stat.S_ISREG(copied.st_mode) or copied.st_nlink != 1:
        raise ArtifactIntegrityError("private artifact snapshot is not a regular file")


@contextmanager
def _exclusive_claim(path: Path):
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ArtifactExistsError(f"operation is already in progress: {path.name}") from exc
    try:
        yield
    finally:
        os.close(descriptor)
        path.unlink(missing_ok=True)


class ArtifactStore:
    """A local reference implementation of staging -> immutable promotion."""

    def __init__(
        self,
        root: str | Path,
        validator: PacketValidator,
        provenance_ledger: SourceProvenanceLedger,
    ) -> None:
        self.root = Path(root).resolve()
        self.validator = validator
        self.provenance_ledger = provenance_ledger
        self.staging_root = self.root / "staging"
        self.promoted_root = self.root / "promoted" / "sha256"
        self.records_root = self.root / "promotion-records"
        for directory in (
            self.staging_root,
            self.promoted_root,
            self.records_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def stage(
        self,
        manifest: Mapping[str, Any],
        source_root: str | Path,
        *,
        task_packet: Mapping[str, Any] | None = None,
    ) -> str:
        """Verify and atomically stage a candidate manifest plus its content."""

        input_manifests = self._verify_lineage(
            manifest["input_manifest_sha256s"]
        )
        self.validator.validate(
            "ArtifactManifest",
            manifest,
            context=PacketContext(
                task_packet=task_packet,
                input_manifests=input_manifests,
            ),
        )
        try:
            validate_artifact_provenance(
                manifest,
                provenance_ledger=self.provenance_ledger,
                input_manifests=input_manifests,
            )
        except ProvenanceError as exc:
            raise ArtifactIntegrityError(str(exc)) from exc
        if manifest["promotion"]["status"] != "candidate":
            raise ArtifactStoreError("only candidate manifests may enter staging")

        source_declared = _absolute_without_resolving_links(Path(source_root))
        _reject_link_components(source_declared)
        source = source_declared.resolve(strict=True)
        if not source.is_dir():
            raise ArtifactPathError(f"source root is not a directory: {source}")
        inventory_before = _directory_inventory(source)

        manifest_hash = sha256_json(manifest)
        destination = self.staging_root / manifest_hash
        if destination.exists():
            raise ArtifactExistsError(
                f"candidate manifest is already staged: {manifest_hash}"
            )

        temporary = Path(
            tempfile.mkdtemp(prefix=f".{manifest_hash}.", dir=self.staging_root)
        )
        try:
            artifact_root = temporary / "artifacts"
            artifact_root.mkdir()
            for artifact in manifest["artifacts"]:
                source_path = _safe_artifact_path(
                    source, artifact["logical_path"], must_exist=True
                )
                destination_path = _safe_artifact_path(
                    artifact_root, artifact["logical_path"], must_exist=False
                )
                self._copy_file(source_path, destination_path)
            if _directory_inventory(source) != inventory_before:
                raise ArtifactIntegrityError("artifact source directory changed during copy")
            self._verify_artifact_files(manifest, artifact_root)
            _write_bytes_fsync(
                temporary / "manifest.json", canonical_json_bytes(manifest)
            )
            if task_packet is None:
                raise ArtifactStoreError(
                    "staging requires the originating TaskPacket snapshot"
                )
            _write_bytes_fsync(
                temporary / "task-packet.json",
                canonical_json_bytes(task_packet),
            )
            _fsync_tree(temporary)
            claim = self.staging_root / f".{manifest_hash}.lock"
            with _exclusive_claim(claim):
                if destination.exists():
                    raise ArtifactExistsError(
                        f"candidate manifest is already staged: {manifest_hash}"
                    )
                os.replace(temporary, destination)
                _fsync_directory(self.staging_root)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return manifest_hash

    def _copy_file(self, source: Path, destination: Path) -> None:
        _copy_regular_file_verified(source, destination)

    def promote(
        self,
        source_manifest_sha256: str,
        *,
        approved_by: str,
        approved_at: str | None = None,
    ) -> PromotionRecord:
        """Reverify a staged candidate and create a promoted immutable snapshot."""

        if approved_by != "main_agent":
            raise ArtifactStoreError("only main_agent may approve promotion")
        self._require_sha256(source_manifest_sha256)

        record_path = self.records_root / f"{source_manifest_sha256}.json"
        if record_path.exists():
            raise ArtifactExistsError(
                f"candidate was already promoted: {source_manifest_sha256}"
            )
        staged = self.staging_root / source_manifest_sha256
        if not staged.is_dir():
            raise ArtifactStoreError(
                f"staged candidate does not exist: {source_manifest_sha256}"
            )
        candidate = self._load_manifest(staged)
        task_packet = self._load_task_packet(staged)
        if sha256_json(candidate) != source_manifest_sha256:
            raise ArtifactIntegrityError("staged candidate manifest was modified")
        input_manifests = self._verify_lineage(
            candidate["input_manifest_sha256s"]
        )
        self.validator.validate(
            "ArtifactManifest",
            candidate,
            context=PacketContext(
                task_packet=task_packet,
                input_manifests=input_manifests,
            ),
        )
        if candidate["promotion"]["status"] != "candidate":
            raise ArtifactIntegrityError("staged manifest is no longer a candidate")
        try:
            validate_artifact_provenance(
                candidate,
                provenance_ledger=self.provenance_ledger,
                input_manifests=input_manifests,
            )
        except ProvenanceError as exc:
            raise ArtifactIntegrityError(str(exc)) from exc
        self._verify_artifact_files(candidate, staged / "artifacts")

        timestamp = approved_at or _utc_now()
        promoted_manifest = deepcopy(candidate)
        promoted_manifest["manifest_version"] += 1
        promoted_manifest["promotion"] = {
            "status": "promoted",
            "approved_by": approved_by,
            "approved_at": timestamp,
            "source_manifest_sha256": source_manifest_sha256,
        }
        try:
            self.validator.validate(
                "ArtifactManifest",
                promoted_manifest,
                context=PacketContext(
                    task_packet=task_packet,
                    source_manifest=candidate,
                    input_manifests=input_manifests,
                ),
            )
        except PacketSemanticError as exc:
            raise ArtifactStoreError(f"invalid promoted manifest: {exc}") from exc

        promoted_hash = sha256_json(promoted_manifest)
        record = PromotionRecord(
            record_version="1.0.0",
            source_manifest_sha256=source_manifest_sha256,
            promoted_manifest_sha256=promoted_hash,
            approved_by=approved_by,
            approved_at=timestamp,
            artifact_sha256s=tuple(
                artifact["content_sha256"]
                for artifact in promoted_manifest["artifacts"]
            ),
        )
        destination = self.promoted_root / promoted_hash
        if destination.exists():
            raise ArtifactExistsError(
                f"promoted manifest destination exists: {promoted_hash}"
            )
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{promoted_hash}.", dir=self.promoted_root)
        )
        destination_committed = False
        try:
            staged_artifacts = staged / "artifacts"
            inventory_before = _directory_inventory(staged_artifacts)
            artifact_root = temporary / "artifacts"
            artifact_root.mkdir()
            for artifact in promoted_manifest["artifacts"]:
                source_path = _safe_artifact_path(
                    staged_artifacts,
                    artifact["logical_path"],
                    must_exist=True,
                )
                destination_path = _safe_artifact_path(
                    artifact_root,
                    artifact["logical_path"],
                    must_exist=False,
                )
                self._copy_file(source_path, destination_path)
            if _directory_inventory(staged_artifacts) != inventory_before:
                raise ArtifactIntegrityError(
                    "staged artifact directory changed during promotion"
                )
            _write_bytes_fsync(
                temporary / "manifest.json",
                canonical_json_bytes(promoted_manifest),
            )
            _write_bytes_fsync(
                temporary / "task-packet.json",
                canonical_json_bytes(task_packet),
            )
            _write_bytes_fsync(
                temporary / "source-manifest.json",
                canonical_json_bytes(candidate),
            )
            _write_bytes_fsync(
                temporary / "promotion-record.json",
                canonical_json_bytes(record.as_json()),
            )
            self._verify_artifact_files(promoted_manifest, artifact_root)
            _fsync_tree(temporary)
            self._make_read_only(temporary)
            _fsync_directory(temporary)

            claim = self.promoted_root / f".{source_manifest_sha256}.lock"
            with _exclusive_claim(claim):
                if destination.exists() or record_path.exists():
                    raise ArtifactExistsError(
                        f"candidate was already promoted: {source_manifest_sha256}"
                    )
                os.replace(temporary, destination)
                destination_committed = True
                _fsync_directory(self.promoted_root)
                _atomic_create_bytes(
                    record_path,
                    canonical_json_bytes(record.as_json()),
                )
                _fsync_directory(self.records_root)
        except Exception:
            if destination_committed and destination.exists():
                self._make_writable(destination)
                shutil.rmtree(destination)
                _fsync_directory(self.promoted_root)
            record_path.unlink(missing_ok=True)
            raise
        finally:
            if temporary.exists():
                self._make_writable(temporary)
                shutil.rmtree(temporary)
        return record

    def verify_staged(self, source_manifest_sha256: str) -> Mapping[str, Any]:
        self._require_sha256(source_manifest_sha256)
        directory = self.staging_root / source_manifest_sha256
        manifest = self._load_manifest(directory)
        if sha256_json(manifest) != source_manifest_sha256:
            raise ArtifactIntegrityError("staged manifest hash mismatch")
        task_packet = self._load_task_packet(directory)
        input_manifests = self._verify_lineage(
            manifest["input_manifest_sha256s"]
        )
        self.validator.validate(
            "ArtifactManifest",
            manifest,
            context=PacketContext(
                task_packet=task_packet,
                input_manifests=input_manifests,
            ),
        )
        try:
            validate_artifact_provenance(
                manifest,
                provenance_ledger=self.provenance_ledger,
                input_manifests=input_manifests,
            )
        except ProvenanceError as exc:
            raise ArtifactIntegrityError(str(exc)) from exc
        self._verify_artifact_files(manifest, directory / "artifacts")
        return manifest

    def verify_promoted(self, promoted_manifest_sha256: str) -> Mapping[str, Any]:
        self._require_sha256(promoted_manifest_sha256)
        directory = self.promoted_root / promoted_manifest_sha256
        manifest = self._load_manifest(directory)
        if sha256_json(manifest) != promoted_manifest_sha256:
            raise ArtifactIntegrityError("promoted manifest hash mismatch")
        task_packet = self._load_task_packet(directory)
        source_manifest = self._load_json_object(
            directory / "source-manifest.json", "source manifest"
        )
        input_manifests = self._verify_lineage(
            manifest["input_manifest_sha256s"]
        )
        self.validator.validate(
            "ArtifactManifest",
            manifest,
            context=PacketContext(
                task_packet=task_packet,
                source_manifest=source_manifest,
                input_manifests=input_manifests,
            ),
        )
        try:
            validate_artifact_provenance(
                manifest,
                provenance_ledger=self.provenance_ledger,
                input_manifests=input_manifests,
            )
        except ProvenanceError as exc:
            raise ArtifactIntegrityError(str(exc)) from exc
        if manifest["promotion"]["status"] != "promoted":
            raise ArtifactIntegrityError("content-addressed snapshot is not promoted")
        record = self._load_json_object(
            directory / "promotion-record.json", "promotion record"
        )
        if record.get("promoted_manifest_sha256") != promoted_manifest_sha256:
            raise ArtifactIntegrityError("promotion record does not bind the snapshot")
        external_record = (
            self.records_root
            / f"{manifest['promotion']['source_manifest_sha256']}.json"
        )
        if not external_record.is_file() or self._load_json_object(
            external_record, "external promotion record"
        ) != record:
            raise ArtifactIntegrityError("external promotion record mismatch")
        self._verify_artifact_files(manifest, directory / "artifacts")
        return manifest

    def resolve_promoted_artifact(
        self,
        promoted_manifest_sha256: str,
        artifact_id: str,
    ) -> Path:
        """Resolve one artifact through the verified immutable promotion root."""

        manifest = self.verify_promoted(promoted_manifest_sha256)
        matches = [
            artifact
            for artifact in manifest["artifacts"]
            if artifact["artifact_id"] == artifact_id
        ]
        if len(matches) != 1:
            raise ArtifactIntegrityError(
                "artifact_id must identify exactly one promoted artifact"
            )
        return _safe_artifact_path(
            self.promoted_root / promoted_manifest_sha256 / "artifacts",
            matches[0]["logical_path"],
            must_exist=True,
        )

    def read_promoted_artifact(
        self,
        promoted_manifest_sha256: str,
        artifact_id: str,
    ) -> bytes:
        path = self.resolve_promoted_artifact(
            promoted_manifest_sha256,
            artifact_id,
        )
        try:
            return path.read_bytes()
        except OSError as exc:
            raise ArtifactIntegrityError(
                f"promoted artifact cannot be read: {artifact_id}"
            ) from exc

    def _load_manifest(self, directory: Path) -> dict[str, Any]:
        return self._load_json_object(directory / "manifest.json", "manifest")

    def _load_task_packet(self, directory: Path) -> dict[str, Any]:
        return self._load_json_object(
            directory / "task-packet.json", "TaskPacket snapshot"
        )

    @staticmethod
    def _load_json_object(path: Path, description: str) -> dict[str, Any]:
        _reject_link_components(path)
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise ArtifactIntegrityError(
                f"{description} is missing: {path}"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ArtifactIntegrityError(f"{description} is missing: {path}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactIntegrityError(
                f"{description} cannot be read as UTF-8 JSON: {path}"
            ) from exc
        if not isinstance(value, dict):
            raise ArtifactIntegrityError(f"{description} JSON must be an object")
        return value

    def _verify_artifact_files(
        self,
        manifest: Mapping[str, Any],
        content_root: Path,
    ) -> None:
        for artifact in manifest["artifacts"]:
            path = _safe_artifact_path(
                content_root, artifact["logical_path"], must_exist=True
            )
            metadata = os.lstat(path)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ArtifactIntegrityError(
                    f"declared artifact is not a regular file: "
                    f"{artifact['logical_path']}"
                )
            actual_size = metadata.st_size
            if actual_size != artifact["size_bytes"]:
                raise ArtifactIntegrityError(
                    f"artifact size mismatch for {artifact['logical_path']!r}: "
                    f"expected {artifact['size_bytes']}, got {actual_size}"
                )
            actual_hash = sha256_file(path)
            if actual_hash != artifact["content_sha256"]:
                raise ArtifactIntegrityError(
                    f"artifact hash mismatch for {artifact['logical_path']!r}: "
                    f"expected {artifact['content_sha256']}, got {actual_hash}"
                )

    def _verify_lineage(
        self, input_manifest_sha256s: list[str]
    ) -> tuple[Mapping[str, Any], ...]:
        verified: set[str] = set()
        return tuple(
            self._verify_promoted_tree(parent_hash, verified)
            for parent_hash in input_manifest_sha256s
        )

    def _verify_promoted_tree(
        self,
        parent_hash: str,
        verified: set[str],
    ) -> Mapping[str, Any]:
        if parent_hash in verified:
            return self._load_manifest(self.promoted_root / parent_hash)
        self._require_sha256(parent_hash)
        parent_dir = self.promoted_root / parent_hash
        if not parent_dir.is_dir():
            raise ArtifactIntegrityError(
                f"input lineage is not a promoted manifest: {parent_hash}"
            )
        parent = self._load_manifest(parent_dir)
        if sha256_json(parent) != parent_hash:
            raise ArtifactIntegrityError(
                f"input lineage manifest hash mismatch: {parent_hash}"
            )
        task_packet = self._load_task_packet(parent_dir)
        source_manifest = self._load_json_object(
            parent_dir / "source-manifest.json", "source manifest"
        )
        ancestor_manifests = tuple(
            self._verify_promoted_tree(ancestor_hash, verified)
            for ancestor_hash in parent["input_manifest_sha256s"]
        )
        self.validator.validate(
            "ArtifactManifest",
            parent,
            context=PacketContext(
                task_packet=task_packet,
                source_manifest=source_manifest,
                input_manifests=ancestor_manifests,
            ),
        )
        if parent.get("promotion", {}).get("status") != "promoted":
            raise ArtifactIntegrityError(
                f"input lineage manifest is not promoted: {parent_hash}"
            )
        self._verify_artifact_files(parent, parent_dir / "artifacts")
        try:
            validate_artifact_provenance(
                parent,
                provenance_ledger=self.provenance_ledger,
                input_manifests=ancestor_manifests,
            )
        except ProvenanceError as exc:
            raise ArtifactIntegrityError(str(exc)) from exc
        # Mark only after the node itself verifies.  Content hashes make a
        # genuine cycle infeasible, while this also bounds repeated ancestry.
        verified.add(parent_hash)
        return parent

    @staticmethod
    def _require_sha256(value: str) -> None:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ArtifactStoreError(f"invalid lowercase SHA-256: {value!r}")

    @staticmethod
    def _make_read_only(directory: Path) -> None:
        for path in sorted(directory.rglob("*"), reverse=True):
            try:
                path.chmod(0o555 if path.is_dir() else 0o444)
            except OSError as exc:
                raise ArtifactStoreError(
                    f"could not make promoted content read-only: {path}"
                ) from exc
        directory.chmod(0o555)

    @staticmethod
    def _make_writable(directory: Path) -> None:
        for path in sorted(directory.rglob("*")):
            try:
                path.chmod(0o755 if path.is_dir() else 0o644)
            except OSError:
                pass
        try:
            directory.chmod(0o755)
        except OSError:
            pass
