"""Versioned, checksum-verified Pandrator session bundles."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from sqlalchemy import select

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService, sha256_file
from .database import Database
from .models import Artifact, ArtifactEdge, SessionRecord
from .sessions import SessionService


BUNDLE_VERSION = 1
ProgressCallback = Callable[[float, str | None], None]


def _report_progress(
    callback: ProgressCallback | None,
    value: float,
    detail: str,
) -> None:
    if callback is not None:
        callback(max(0.0, min(1.0, float(value))), detail)


class SessionBundleService:
    def __init__(self, database: Database, paths: DataPaths):
        self.database = database
        self.paths = paths
        self.artifacts = ArtifactService(database, paths)
        self.sessions = SessionService(database)

    def export_bundle(
        self,
        session_id: str,
        destination: Path,
        *,
        include_sources: bool = True,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        _report_progress(progress_callback, 0.02, "Collecting session artifacts")
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            artifacts = list(session.scalars(select(Artifact).where(Artifact.session_id == session_id).order_by(Artifact.created_at)).all())
            artifact_ids = [item.id for item in artifacts]
            edges = list(session.scalars(select(ArtifactEdge).where(ArtifactEdge.child_artifact_id.in_(artifact_ids))).all()) if artifact_ids else []
            session_payload = {
                "name": record.name,
                "workflow_kind": record.workflow_kind,
                "source_language": record.source_language,
                "target_language": record.target_language,
                "workflow_preset": record.workflow_preset,
                "included_stages": record.included_stages_json,
                "status": record.status,
                "source_session_id": record.id,
            }
            artifact_payloads = []
            files = []
            included_artifacts = [
                artifact
                for artifact in artifacts
                if include_sources or artifact.role != "upload"
            ]
            for index, artifact in enumerate(included_artifacts, start=1):
                path = self.paths.managed_path(artifact.relative_path)
                if not path.is_file():
                    raise FileNotFoundError(f"Bundle artifact is missing: {path}")
                digest = sha256_file(path)
                archive_path = f"files/{artifact.id}/{path.name}"
                artifact_payloads.append({
                    "source_id": artifact.id,
                    "kind": artifact.kind,
                    "role": artifact.role,
                    "mime_type": artifact.mime_type,
                    "size_bytes": path.stat().st_size,
                    "sha256": digest,
                    "state": artifact.state,
                    "settings_hash": artifact.settings_hash,
                    "metadata": artifact.metadata_json,
                    "archive_path": archive_path,
                })
                files.append((path, archive_path))
                _report_progress(
                    progress_callback,
                    0.08 + 0.32 * (index / max(1, len(included_artifacts))),
                    f"Verified session artifact {index} of {len(included_artifacts)}",
                )
            included_ids = {item["source_id"] for item in artifact_payloads}
            edge_payloads = [
                {"parent": edge.parent_artifact_id, "child": edge.child_artifact_id, "relation": edge.relation}
                for edge in edges
                if edge.parent_artifact_id in included_ids and edge.child_artifact_id in included_ids
            ]

        manifest = {
            "format": "pandrator-session",
            "version": BUNDLE_VERSION,
            "session": session_payload,
            "artifacts": artifact_payloads,
            "artifact_edges": edge_payloads,
        }
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with zipfile.ZipFile(temporary, "w", allowZip64=True) as archive:
                _report_progress(progress_callback, 0.45, "Writing session bundle manifest")
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
                for index, (path, archive_path) in enumerate(files, start=1):
                    compress = zipfile.ZIP_DEFLATED if path.suffix.lower() in {".txt", ".json", ".srt", ".ass", ".md"} else zipfile.ZIP_STORED
                    archive.write(path, archive_path, compress_type=compress)
                    _report_progress(
                        progress_callback,
                        0.45 + 0.4 * (index / max(1, len(files))),
                        f"Packed session artifact {index} of {len(files)}",
                    )
            _report_progress(progress_callback, 0.9, "Finalizing session bundle")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        _report_progress(progress_callback, 0.95, "Verifying session bundle checksum")
        digest = sha256_file(destination)
        _report_progress(
            progress_callback,
            1.0,
            f"Session bundle ready with {len(artifact_payloads)} artifacts",
        )
        return {"path": str(destination), "sha256": digest, "artifacts": len(artifact_payloads), "version": BUNDLE_VERSION}

    @staticmethod
    def _safe_member(name: str) -> PurePosixPath:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError(f"Unsafe bundle member: {name}")
        return path

    def import_bundle(
        self,
        source: Path,
        *,
        name: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        source = source.expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        _report_progress(progress_callback, 0.02, "Inspecting session bundle")
        with tempfile.TemporaryDirectory(prefix="pandrator-bundle-", dir=self.paths.temporary) as temporary_directory:
            staging = Path(temporary_directory)
            with zipfile.ZipFile(source, "r") as archive:
                members = {item.filename: item for item in archive.infolist()}
                if "manifest.json" not in members:
                    raise ValueError("Session bundle is missing manifest.json.")
                manifest = json.loads(archive.read("manifest.json"))
                if manifest.get("format") != "pandrator-session" or int(manifest.get("version") or 0) != BUNDLE_VERSION:
                    raise ValueError("Unsupported Pandrator session bundle version.")
                staged: dict[str, Path] = {}
                manifest_artifacts = list(manifest.get("artifacts") or [])
                for index, item in enumerate(manifest_artifacts, start=1):
                    archive_path = str(item.get("archive_path") or "")
                    safe = self._safe_member(archive_path)
                    if archive_path not in members or members[archive_path].is_dir():
                        raise ValueError(f"Bundle artifact is missing: {archive_path}")
                    destination = staging.joinpath(*safe.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(archive_path) as input_handle, destination.open("xb") as output_handle:
                        shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
                    expected_size = int(item.get("size_bytes") or 0)
                    expected_hash = str(item.get("sha256") or "")
                    if destination.stat().st_size != expected_size or not expected_hash or sha256_file(destination) != expected_hash:
                        raise ValueError(f"Bundle checksum validation failed: {archive_path}")
                    staged[str(item.get("source_id") or "")] = destination
                    _report_progress(
                        progress_callback,
                        0.08 + 0.42 * (index / max(1, len(manifest_artifacts))),
                        f"Verified bundled artifact {index} of {len(manifest_artifacts)}",
                    )

            session_data = dict(manifest.get("session") or {})
            _report_progress(progress_callback, 0.55, "Creating imported session")
            imported = self.sessions.create(
                str(name or session_data.get("name") or "Imported session"),
                workflow_kind=str(session_data.get("workflow_kind") or "audiobook"),
                source_language=str(session_data.get("source_language") or "auto"),
                target_language=str(session_data.get("target_language") or "") or None,
                workflow_preset=str(session_data.get("workflow_preset") or "custom"),
                included_stages=list(session_data.get("included_stages") or []),
            )
            target_dir = self.paths.sessions / imported.storage_key
            target_dir.mkdir(parents=True, exist_ok=False)
            id_map: dict[str, str] = {}
            try:
                for index, item in enumerate(manifest_artifacts, start=1):
                    source_id = str(item.get("source_id") or "")
                    staged_path = staged[source_id]
                    destination = target_dir / f"{uuid.uuid4()}-{staged_path.name}"
                    shutil.move(str(staged_path), destination)
                    artifact = self.artifacts.register(
                        destination,
                        kind=str(item.get("kind") or "artifact"),
                        role=str(item.get("role") or "artifact"),
                        session_id=imported.id,
                        metadata={**dict(item.get("metadata") or {}), "bundle_source_artifact_id": source_id},
                    )
                    with self.database.session() as session:
                        managed = session.get(Artifact, artifact.id)
                        managed.state = str(item.get("state") or "current")
                        managed.settings_hash = item.get("settings_hash")
                    id_map[source_id] = artifact.id
                    _report_progress(
                        progress_callback,
                        0.55 + 0.35 * (index / max(1, len(manifest_artifacts))),
                        f"Imported session artifact {index} of {len(manifest_artifacts)}",
                    )
                _report_progress(progress_callback, 0.92, "Restoring artifact relationships")
                with self.database.session() as session:
                    for edge in manifest.get("artifact_edges") or []:
                        parent_id = id_map.get(str(edge.get("parent") or ""))
                        child_id = id_map.get(str(edge.get("child") or ""))
                        if parent_id and child_id:
                            session.add(ArtifactEdge(parent_artifact_id=parent_id, child_artifact_id=child_id, relation=str(edge.get("relation") or "derived_from")))
            except Exception:
                with self.database.session() as session:
                    failed = session.get(SessionRecord, imported.id)
                    if failed is not None:
                        session.delete(failed)
                shutil.rmtree(target_dir, ignore_errors=True)
                raise
        _report_progress(progress_callback, 0.96, "Verifying source bundle checksum")
        digest = sha256_file(source)
        _report_progress(
            progress_callback,
            1.0,
            f"Imported session with {len(id_map)} artifacts",
        )
        return {"session_id": imported.id, "name": imported.name, "artifacts": len(id_map), "source_bundle_sha256": digest}
