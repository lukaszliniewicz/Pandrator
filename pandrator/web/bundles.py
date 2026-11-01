"""Versioned, checksum-verified Pandrator session bundles."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import uuid
import zipfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from sqlalchemy import case, literal, or_, select, update
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .access.artifact_visibility import resource_visibility_clause
from .access.contracts import AccessDenied, ProjectAction
from .access.project_permissions import ACTION_SCOPES
from .access.session_bundle_policy import (
    BundleExportInput,
    BundleImportInput,
    require_bundle_export_access,
    require_bundle_import_execution_access,
)
from .accounts.execution_actor import HostActor, UserActor, current_execution_actor
from .accounts.job_authorization import require_execution_actor_in_session
from .artifacts import ArtifactService, PreparedArtifactRegistration, sha256_file
from .credentials import SecretRedactor
from .database import Database
from .job_execution import current_job_lease
from .models import Artifact, ArtifactEdge, SessionRecord, SessionSource, SourceAsset
from .projects.models import Project
from .sessions import SessionService

BUNDLE_VERSION = 1
_MAX_BUNDLE_MANIFEST_BYTES = 64 * 1024 * 1024
_RESERVED_BUNDLE_BASENAMES = frozenset({
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "123456789¹²³"),
})
ProgressCallback = Callable[[float, str | None], None]


def _report_progress(
    callback: ProgressCallback | None,
    value: float,
    detail: str,
) -> None:
    if callback is not None:
        callback(max(0.0, min(1.0, float(value))), detail)


def _check_export_cancellation(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("Session bundle export was canceled.")


def _check_import_cancellation(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("Session bundle import was canceled.")


class SessionBundleService:
    def __init__(self, database: Database, paths: DataPaths):
        self.database = database
        self.paths = paths
        self.artifacts = ArtifactService(database, paths)
        self.sessions = SessionService(database)
        self.secret_redactor = SecretRedactor(database, paths)

    def _prove_export(self, session_id: str, inputs: tuple[BundleExportInput, ...]) -> None:
        if current_execution_actor.get() is None:
            return
        with self.database.snapshot_session() as unit:
            require_bundle_export_access(unit, session_id, inputs=inputs)

    def _capture_export(
        self,
        session_id: str,
        include_sources: bool,
    ) -> tuple[
        str,
        dict[str, Any],
        list[dict[str, Any]],
        list[dict[str, Any]],
        tuple[BundleExportInput, ...],
    ]:
        actor = current_execution_actor.get()
        read_unit = self.database.session if actor is None else self.database.snapshot_session
        with read_unit() as unit:
            require_bundle_export_access(unit, session_id)
            record = unit.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            storage_key = record.storage_key
            session_payload = {
                "name": record.name,
                "workflow_kind": record.workflow_kind,
                "interface_mode": record.interface_mode,
                "source_language": record.source_language,
                "target_language": record.target_language,
                "workflow_preset": record.workflow_preset,
                "included_stages": record.included_stages_json,
                "status": record.status,
                "source_session_id": record.id,
            }
            attached_ids = set(
                unit.scalars(
                    select(SourceAsset.artifact_id)
                    .join(SessionSource, SessionSource.source_asset_id == SourceAsset.id)
                    .where(
                        SessionSource.session_id == session_id, SessionSource.is_current.is_(True)
                    )
                )
            )
            filters = (
                (Artifact.session_id == session_id) | Artifact.id.in_(attached_ids),
                Artifact.state != "deleted",
                Artifact.role != "session_bundle",
            )
            if not include_sources:
                filters += (Artifact.role != "upload", Artifact.id.not_in(attached_ids))
            if actor is None:
                captured = [
                    {
                        "id": item.id,
                        "kind": item.kind,
                        "role": item.role,
                        "mime_type": item.mime_type,
                        "state": item.state,
                        "relative_path": item.relative_path,
                        "content_hash": item.content_hash,
                        "settings_hash": item.settings_hash,
                        "metadata": item.metadata_json,
                        "ordinary_visibility": True,
                    }
                    for item in unit.scalars(
                        select(Artifact).where(*filters).order_by(Artifact.created_at)
                    )
                ]
            else:
                ordinary = (
                    resource_visibility_clause(Artifact, actor)
                    if type(actor) is UserActor
                    else literal(True)
                )
                captured = [
                    dict(row._mapping)
                    for row in unit.execute(
                        select(
                            Artifact.id,
                            Artifact.kind,
                            Artifact.role,
                            Artifact.mime_type,
                            Artifact.state,
                            Artifact.relative_path,
                            Artifact.content_hash,
                            case((ordinary, Artifact.settings_hash), else_=None).label(
                                "settings_hash"
                            ),
                            case((ordinary, Artifact.metadata_json), else_=None).label("metadata"),
                            ordinary.label("ordinary_visibility"),
                        )
                        .where(*filters)
                        .order_by(Artifact.created_at)
                    )
                ]
            included = [
                item
                for item in captured
                if include_sources or (item["role"] != "upload" and item["id"] not in attached_ids)
            ]
            included_ids = {item["id"] for item in included}
            edge_payloads = (
                [
                    {
                        "parent": row.parent_artifact_id,
                        "child": row.child_artifact_id,
                        "relation": row.relation,
                    }
                    for row in unit.execute(
                        select(
                            ArtifactEdge.parent_artifact_id,
                            ArtifactEdge.child_artifact_id,
                            ArtifactEdge.relation,
                        ).where(ArtifactEdge.child_artifact_id.in_(included_ids))
                    )
                    if row.parent_artifact_id in included_ids
                    and row.child_artifact_id in included_ids
                ]
                if included_ids
                else []
            )
            identities = tuple(
                BundleExportInput(
                    item["id"],
                    item["relative_path"],
                    item["content_hash"],
                    bool(item["ordinary_visibility"]),
                )
                for item in included
            )
        return storage_key, session_payload, included, edge_payloads, identities

    def _export_path_is_registered(self, path: Path) -> bool:
        try:
            relative = self.paths.relative_managed_path(path)
        except ValueError:
            return False
        with self.database.session() as unit:
            return (
                unit.scalar(select(Artifact.id).where(Artifact.relative_path == relative).limit(1))
                is not None
            )

    def _cleanup_export_output(self, path: Path) -> None:
        if not self._export_path_is_registered(path):
            path.unlink(missing_ok=True)

    def export_bundle(
        self,
        session_id: str,
        destination: Path | None,
        *,
        include_sources: bool = True,
        progress_callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        publish_callback: Callable[[Path, str, tuple[BundleExportInput, ...]], dict[str, Any]]
        | None = None,
    ) -> dict[str, Any]:
        actor = current_execution_actor.get()
        if actor is None and destination is not None:
            destination = destination.expanduser().resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
        if actor is None:
            _report_progress(progress_callback, 0.02, "Collecting session artifacts")
        storage_key, session_payload, included, edge_payloads, identities = self._capture_export(
            session_id, include_sources
        )
        if actor is not None:
            _report_progress(progress_callback, 0.02, "Collecting session artifacts")
        self._prove_export(session_id, identities)
        _check_export_cancellation(cancel_event)
        operation_dir: Path | None = None
        temporary: Path | None = None
        created_final = False
        preexisting_destination = False
        try:
            if destination is None:
                operation_dir = (
                    self.paths.sessions
                    / storage_key
                    / "stage-runs"
                    / f"session-bundle-{uuid.uuid4()}"
                )
                operation_dir.mkdir(parents=True, exist_ok=False)
                destination = operation_dir / f"{storage_key}.pandrator-session"
            else:
                destination = destination.expanduser().resolve()
                if type(actor) is UserActor:
                    self.paths.relative_managed_path(destination)
                    session_dir = (self.paths.sessions / storage_key).resolve()
                    if not destination.is_relative_to(session_dir):
                        raise ValueError(
                            "Session bundle destination must belong to the target session."
                        )
                    destination = self.artifacts.next_available_path(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
            if type(actor) is not UserActor and self._export_path_is_registered(destination):
                destination = self.artifacts.next_available_path(destination)
            preexisting_destination = destination.exists()
            artifact_payloads = []
            files = []
            for index, item in enumerate(included, start=1):
                _check_export_cancellation(cancel_event)
                path = self.paths.managed_path(item["relative_path"])
                if not path.is_file():
                    raise FileNotFoundError(f"Bundle artifact is missing: {path}")
                digest = sha256_file(path)
                _check_export_cancellation(cancel_event)
                size = path.stat().st_size
                archive_path = f"files/{item['id']}/{path.name}"
                artifact_payloads.append(
                    {
                        "source_id": item["id"],
                        "kind": item["kind"],
                        "role": item["role"],
                        "mime_type": item["mime_type"],
                        "size_bytes": size,
                        "sha256": digest,
                        "state": item["state"],
                        "settings_hash": item["settings_hash"],
                        "metadata": self.secret_redactor.redact_value(item["metadata"] or {}),
                        "archive_path": archive_path,
                    }
                )
                files.append((path, archive_path, size, digest))
                _report_progress(
                    progress_callback,
                    0.08 + 0.32 * (index / max(1, len(included))),
                    f"Verified session artifact {index} of {len(included)}",
                )
            manifest = {
                "format": "pandrator-session",
                "version": BUNDLE_VERSION,
                "session": session_payload,
                "artifacts": artifact_payloads,
                "artifact_edges": edge_payloads,
            }
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            with zipfile.ZipFile(temporary, "w", allowZip64=True) as archive:
                _report_progress(progress_callback, 0.45, "Writing session bundle manifest")
                _check_export_cancellation(cancel_event)
                archive.writestr(
                    "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
                )
                for index, (path, archive_path, expected_size, expected_hash) in enumerate(
                    files, start=1
                ):
                    _check_export_cancellation(cancel_event)
                    info = zipfile.ZipInfo.from_file(path, archive_path)
                    info.compress_type = (
                        zipfile.ZIP_DEFLATED
                        if path.suffix.lower() in {".txt", ".json", ".srt", ".ass", ".md"}
                        else zipfile.ZIP_STORED
                    )
                    actual_hash = hashlib.sha256()
                    actual_size = 0
                    with path.open("rb") as source, archive.open(info, "w") as target:
                        while True:
                            _check_export_cancellation(cancel_event)
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            target.write(chunk)
                            actual_hash.update(chunk)
                            actual_size += len(chunk)
                    if actual_size != expected_size or actual_hash.hexdigest() != expected_hash:
                        raise ValueError("Bundle artifact changed during export.")
                    _report_progress(
                        progress_callback,
                        0.45 + 0.4 * (index / max(1, len(files))),
                        f"Packed session artifact {index} of {len(files)}",
                    )
            _report_progress(progress_callback, 0.9, "Finalizing session bundle")
            self._prove_export(session_id, identities)
            _check_export_cancellation(cancel_event)
            os.replace(temporary, destination)
            created_final = True
            _report_progress(progress_callback, 0.95, "Verifying session bundle checksum")
            _check_export_cancellation(cancel_event)
            digest = sha256_file(destination)
            _check_export_cancellation(cancel_event)
            _report_progress(
                progress_callback,
                1.0,
                f"Session bundle ready with {len(artifact_payloads)} artifacts",
            )
            result = {
                "path": str(destination),
                "sha256": digest,
                "artifacts": len(artifact_payloads),
                "version": BUNDLE_VERSION,
            }
            if publish_callback is not None:
                _check_export_cancellation(cancel_event)
                result.update(publish_callback(destination, digest, identities))
            return result
        except BaseException:
            if created_final and not preexisting_destination and destination is not None:
                self._cleanup_export_output(destination)
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if operation_dir is not None:
                try:
                    operation_dir.rmdir()
                except OSError:
                    pass

    @staticmethod
    def _safe_member(name: str) -> PurePosixPath:
        path = PurePosixPath(name)
        unsafe = (
            not name
            or path.is_absolute()
            or not path.parts
            or any(ord(char) < 32 or char in '\\:<>"|?*' for char in name)
            or any(
                part == ".."
                or part.endswith((".", " "))
                or part.split(".", 1)[0].rstrip(" ").upper() in _RESERVED_BUNDLE_BASENAMES
                for part in name.split("/")
            )
        )
        if unsafe:
            raise ValueError(f"Unsafe bundle member: {name}")
        return path

    def _import_access_in_session(
        self,
        unit: Session,
        source_artifact_id: str | None,
        expected: BundleImportInput | None = None,
    ) -> BundleImportInput | None:
        actor = current_execution_actor.get()
        if actor is None:
            return None
        if type(actor) not in (UserActor, HostActor):
            raise AccessDenied()
        if source_artifact_id:
            return require_bundle_import_execution_access(
                unit, source_artifact_id, expected=expected
            )
        if type(actor) is UserActor or current_job_lease.get() is not None:
            raise AccessDenied()
        try:
            require_execution_actor_in_session(
                unit,
                actor,
                required_scopes=ACTION_SCOPES[ProjectAction.EDIT]
                | ACTION_SCOPES[ProjectAction.RUN],
                allow_host=True,
            )
        except PermissionError:
            raise AccessDenied() from None
        return None

    def _prove_import(
        self,
        source_artifact_id: str | None,
        expected: BundleImportInput | None = None,
    ) -> BundleImportInput | None:
        if current_execution_actor.get() is None:
            return None
        with self.database.snapshot_session() as unit:
            return self._import_access_in_session(unit, source_artifact_id, expected)

    @staticmethod
    def _hash_import_archive(source: Path, cancel_event: threading.Event | None) -> str:
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            while True:
                _check_import_cancellation(cancel_event)
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _read_import_manifest(
        self,
        archive: zipfile.ZipFile,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        members: dict[str, zipfile.ZipInfo] = {}
        canonical_members: set[tuple[str, ...]] = set()
        for info in archive.infolist():
            safe = self._safe_member(info.orig_filename)
            key = tuple(part.casefold() for part in safe.parts)
            if info.filename in members or key in canonical_members:
                raise ValueError("Session bundle contains duplicate archive members.")
            members[info.filename] = info
            canonical_members.add(key)
        if "manifest.json" not in members:
            raise ValueError("Session bundle is missing manifest.json.")
        if members["manifest.json"].file_size > _MAX_BUNDLE_MANIFEST_BYTES:
            raise ValueError("Session bundle manifest is too large.")
        try:
            manifest = json.loads(archive.read("manifest.json"))
        except (ValueError, UnicodeError):
            raise ValueError("Invalid session bundle manifest.") from None
        if not isinstance(manifest, dict):
            raise ValueError("Invalid session bundle manifest.")
        try:
            version = int(manifest.get("version") or 0)
        except (TypeError, ValueError):
            raise ValueError("Unsupported Pandrator session bundle version.") from None
        if manifest.get("format") != "pandrator-session" or version != BUNDLE_VERSION:
            raise ValueError("Unsupported Pandrator session bundle version.")
        session_data = manifest.get("session", {})
        artifacts = manifest.get("artifacts", [])
        edges = manifest.get("artifact_edges", [])
        if (
            not isinstance(session_data, dict)
            or not isinstance(artifacts, list)
            or not isinstance(edges, list)
        ):
            raise ValueError("Invalid session bundle manifest.")
        if any(
            value is not None and not isinstance(value, str)
            for key, value in session_data.items()
            if key
            in {
                "name",
                "workflow_kind",
                "interface_mode",
                "source_language",
                "target_language",
                "workflow_preset",
            }
        ):
            raise ValueError("Invalid session bundle manifest.")
        stages = session_data.get("included_stages", [])
        if not isinstance(stages, list) or any(not isinstance(stage, str) for stage in stages):
            raise ValueError("Invalid session bundle manifest.")
        identifiers: set[str] = set()
        destinations: set[tuple[str, ...]] = set()
        declared_size = 0
        for item in artifacts:
            if not isinstance(item, dict):
                raise ValueError("Invalid session bundle manifest.")
            source_id = item.get("source_id")
            if not isinstance(source_id, str) or not source_id or source_id in identifiers:
                raise ValueError(
                    "Session bundle contains duplicate or invalid artifact identities."
                )
            identifiers.add(source_id)
            archive_path = item.get("archive_path")
            if not isinstance(archive_path, str):
                raise ValueError("Invalid session bundle manifest.")
            safe = self._safe_member(archive_path)
            key = tuple(part.casefold() for part in safe.parts)
            if key in destinations:
                raise ValueError("Session bundle contains duplicate archive members.")
            destinations.add(key)
            if archive_path not in members or members[archive_path].is_dir():
                raise ValueError(f"Bundle artifact is missing: {archive_path}")
            expected_size = item.get("size_bytes")
            if type(expected_size) is not int or expected_size < 0:
                raise ValueError("Invalid session bundle manifest.")
            expected_hash = item.get("sha256")
            if (
                not isinstance(expected_hash, str)
                or len(expected_hash) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in expected_hash)
                or members[archive_path].file_size != expected_size
            ):
                raise ValueError(f"Bundle checksum validation failed: {archive_path}")
            if any(
                item.get(key) is not None and not isinstance(item[key], str)
                for key in ("kind", "role", "state", "settings_hash", "mime_type")
            ):
                raise ValueError("Invalid session bundle manifest.")
            if item.get("metadata") is not None and not isinstance(item["metadata"], dict):
                raise ValueError("Invalid session bundle manifest.")
            declared_size += expected_size
        for edge in edges:
            if not isinstance(edge, dict) or any(
                edge.get(key) is not None and not isinstance(edge[key], str)
                for key in ("parent", "child", "relation")
            ):
                raise ValueError("Invalid session bundle manifest.")
        if declared_size > shutil.disk_usage(self.paths.temporary).free:
            raise ValueError("Session bundle exceeds available temporary storage.")
        return manifest, artifacts

    def _cleanup_import_target(self, planned_id: str, target_dir: Path) -> None:
        prefix = self.paths.relative_managed_path(target_dir) + "/"
        with self.database.session() as unit:
            committed = unit.scalar(select(SessionRecord.id).where(SessionRecord.id == planned_id))
            registered = unit.scalar(
                select(Artifact.id)
                .where(Artifact.relative_path.startswith(prefix, autoescape=True))
                .limit(1)
            )
        if committed is None and registered is None:
            shutil.rmtree(target_dir, ignore_errors=True)

    def import_bundle(
        self,
        source: Path | None,
        *,
        name: str | None = None,
        workspace_id: str | None = None,
        progress_callback: ProgressCallback | None = None,
        source_artifact_id: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        captured = self._prove_import(source_artifact_id)
        if captured is not None:
            if workspace_id is not None and workspace_id != captured.workspace_id:
                raise AccessDenied()
            workspace_id = captured.workspace_id
            try:
                managed_source = self.paths.managed_path(captured.relative_path)
            except ValueError:
                raise KeyError("Bundle artifact not found.") from None
            if source is not None and source.expanduser().resolve() != managed_source:
                raise AccessDenied()
            source = managed_source
        elif source is None:
            raise AccessDenied()
        source = source.expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        _check_import_cancellation(cancel_event)
        initial_hash = (
            captured.content_hash
            if captured is not None and captured.content_hash
            else self._hash_import_archive(source, cancel_event)
        )
        _report_progress(progress_callback, 0.02, "Inspecting session bundle")
        planned_id, storage_key = str(uuid.uuid4()), str(uuid.uuid4())
        target_dir = self.paths.sessions / storage_key
        target_owned = False
        try:
            with zipfile.ZipFile(source, "r") as archive:
                manifest, manifest_artifacts = self._read_import_manifest(archive)
                with tempfile.TemporaryDirectory(
                    prefix="pandrator-bundle-", dir=self.paths.temporary
                ) as temporary_directory:
                    staging = Path(temporary_directory)
                    staged: dict[str, Path] = {}
                    for index, item in enumerate(manifest_artifacts, start=1):
                        _check_import_cancellation(cancel_event)
                        archive_path = item["archive_path"]
                        safe = self._safe_member(archive_path)
                        destination = staging.joinpath(*safe.parts)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        actual_size = 0
                        actual_hash = hashlib.sha256()
                        with (
                            archive.open(archive_path) as input_handle,
                            destination.open("xb") as output_handle,
                        ):
                            while True:
                                _check_import_cancellation(cancel_event)
                                chunk = input_handle.read(1024 * 1024)
                                if not chunk:
                                    break
                                actual_size += len(chunk)
                                if actual_size > item["size_bytes"]:
                                    raise ValueError(
                                        f"Bundle checksum validation failed: {archive_path}"
                                    )
                                output_handle.write(chunk)
                                actual_hash.update(chunk)
                        if (
                            actual_size != item["size_bytes"]
                            or actual_hash.hexdigest() != item["sha256"].lower()
                        ):
                            raise ValueError(f"Bundle checksum validation failed: {archive_path}")
                        staged[item["source_id"]] = destination
                        _report_progress(
                            progress_callback,
                            0.08 + 0.42 * (index / max(1, len(manifest_artifacts))),
                            f"Verified bundled artifact {index} of {len(manifest_artifacts)}",
                        )
                    self._prove_import(source_artifact_id, captured)
                    _check_import_cancellation(cancel_event)
                    with self.database.snapshot_session() as unit:
                        if (
                            unit.scalar(
                                select(SessionRecord.id).where(
                                    or_(
                                        SessionRecord.id == planned_id,
                                        SessionRecord.storage_key == storage_key,
                                    )
                                )
                            )
                            is not None
                        ):
                            raise FileExistsError(target_dir)
                    _report_progress(progress_callback, 0.55, "Creating imported session")
                    target_dir.mkdir(parents=True, exist_ok=False)
                    target_owned = True
                    prepared_files: list[
                        tuple[dict[str, Any], Path, PreparedArtifactRegistration]
                    ] = []
                    for index, item in enumerate(manifest_artifacts, start=1):
                        _check_import_cancellation(cancel_event)
                        staged_path = staged[item["source_id"]]
                        destination = target_dir / f"{uuid.uuid4()}-{staged_path.name}"
                        shutil.move(str(staged_path), destination)
                        prepared = self.artifacts.prepare_registration(
                            destination, calculate_hash=False
                        )
                        if prepared.size_bytes != item["size_bytes"]:
                            raise ValueError(
                                f"Bundle checksum validation failed: {item['archive_path']}"
                            )
                        prepared = replace(
                            prepared,
                            content_hash=item["sha256"].lower(),
                            size_bytes=item["size_bytes"],
                        )
                        prepared_files.append((item, destination, prepared))
                        _report_progress(
                            progress_callback,
                            0.55 + 0.35 * (index / max(1, len(manifest_artifacts))),
                            f"Imported session artifact {index} of {len(manifest_artifacts)}",
                        )
            session_data = manifest.get("session") or {}
            _report_progress(progress_callback, 0.92, "Restoring artifact relationships")
            _report_progress(progress_callback, 0.96, "Verifying source bundle checksum")
            digest = self._hash_import_archive(source, cancel_event)
            if digest != initial_hash.lower():
                raise ValueError(f"Bundle checksum validation failed: {source}")
            self._prove_import(source_artifact_id, captured)
            _check_import_cancellation(cancel_event)
            with self.database.immediate_session() as unit:
                current = self._import_access_in_session(unit, source_artifact_id, captured)
                if current is not None and current.workspace_id != workspace_id:
                    raise AccessDenied()
                _check_import_cancellation(cancel_event)
                imported = self.sessions.create(
                    str(name or session_data.get("name") or "Imported session"),
                    workflow_kind=str(session_data.get("workflow_kind") or "audiobook"),
                    interface_mode=session_data.get("interface_mode", "advanced"),
                    source_language=str(session_data.get("source_language") or "auto"),
                    target_language=str(session_data.get("target_language") or "") or None,
                    workflow_preset=str(session_data.get("workflow_preset") or "custom"),
                    seed_voice_mode=False,
                    workspace_id=workspace_id,
                    included_stages=list(session_data.get("included_stages") or []),
                    record_id=planned_id,
                    storage_key=storage_key,
                    db_session=unit,
                )
                actor = current_execution_actor.get()
                if type(actor) is UserActor:
                    unit.execute(
                        update(Project)
                        .where(Project.id == imported.project_id)
                        .values(created_by_user_id=actor.user_id)
                    )
                id_map: dict[str, str] = {}
                for item, destination, prepared in prepared_files:
                    artifact = self.artifacts.register_in_session(
                        unit,
                        destination,
                        kind=str(item.get("kind") or "artifact"),
                        role=str(item.get("role") or "artifact"),
                        session_id=imported.id,
                        metadata={
                            **dict(item.get("metadata") or {}),
                            "bundle_source_artifact_id": item["source_id"],
                        },
                        _prepared=prepared,
                    )
                    artifact.state = str(item.get("state") or "current")
                    artifact.settings_hash = item.get("settings_hash")
                    id_map[item["source_id"]] = artifact.id
                for edge in manifest.get("artifact_edges") or []:
                    parent_id = id_map.get(str(edge.get("parent") or ""))
                    child_id = id_map.get(str(edge.get("child") or ""))
                    if parent_id and child_id:
                        unit.add(
                            ArtifactEdge(
                                parent_artifact_id=parent_id,
                                child_artifact_id=child_id,
                                relation=str(edge.get("relation") or "derived_from"),
                            )
                        )
                result = {
                    "session_id": imported.id,
                    "name": imported.name,
                    "artifacts": len(id_map),
                    "source_bundle_sha256": digest,
                }
            _report_progress(
                progress_callback, 1.0, f"Imported session with {len(id_map)} artifacts"
            )
            return result
        except BaseException:
            if target_owned:
                self._cleanup_import_target(planned_id, target_dir)
            raise
