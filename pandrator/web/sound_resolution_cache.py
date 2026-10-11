"""Verified session-local alignment cache and private publication."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import replace
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.logic.book_timing import AlignedBookText
from pandrator.runtime import DataPaths

from .artifacts import ArtifactService, PreparedArtifactRegistration, sha256_file
from .models import Artifact, ArtifactEdge
from .sound_generation_worker_support import _remove_if_same, _rollback_cleanup
from .sound_resolution_snapshot import REFINEMENT_SETTINGS, WorkItem, digest


def cache_key(item: WorkItem) -> str:
    return digest(
        {
            "take_hash": item.take_hash,
            "transcript_hash": item.transcript_hash,
            "language": item.language,
            "alignment_identity": item.identity,
            "refinement_settings": REFINEMENT_SETTINGS,
        }
    )


def find_cache(
    session: Session, paths: DataPaths, session_id: str, item: WorkItem
) -> AlignedBookText | None:
    assert item.artifact_id is not None and item.transcript is not None
    candidates = session.scalars(
        select(Artifact)
        .where(
            Artifact.session_id == session_id,
            Artifact.kind == "json",
            Artifact.role == "sound_alignment",
            Artifact.state == "current",
            Artifact.metadata_json["cache_key"].as_string() == cache_key(item),
        )
        .order_by(Artifact.created_at.desc())
        .limit(5)
    ).all()
    for artifact in candidates:
        if not artifact.content_hash or not session.get(
            ArtifactEdge, (item.artifact_id, artifact.id)
        ):
            continue
        try:
            path = paths.managed_path(artifact.relative_path)
            if not path.is_file() or sha256_file(path) != artifact.content_hash:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != 1 or payload.get("cache_key") != cache_key(item):
                continue
            aligned = AlignedBookText.from_dict(payload["alignment"])
            if (
                aligned.text == item.transcript
                and aligned.engine == (item.identity or {}).get("engine")
                and aligned.diagnostics.get("identity") == item.identity
            ):
                return aligned
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return None


def stage_cache(paths: DataPaths, item: WorkItem, aligned: AlignedBookText, workdir: Path) -> Path:
    content = {"schema_version": 1, "cache_key": cache_key(item), "alignment": aligned.as_dict()}
    path = workdir / f"alignment-{uuid.uuid4()}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(content, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return path


def publish_cache(
    session: Session,
    paths: DataPaths,
    artifacts: ArtifactService,
    session_id: str,
    item: WorkItem,
    staged: Path,
    prepared: PreparedArtifactRegistration,
) -> str:
    assert item.artifact_id is not None
    directory = paths.root / "sounds" / "alignment"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for _ in range(8):
        destination = directory / f"{uuid.uuid4()}.json"
        if (
            session.scalar(
                select(Artifact.id).where(
                    Artifact.relative_path == paths.relative_managed_path(destination)
                )
            )
            is not None
        ):
            continue
        try:
            os.link(staged, destination)
        except FileExistsError:
            continue
        stat = destination.lstat()
        identity = (stat.st_dev, stat.st_ino)
        try:
            registration = replace(prepared, relative_path=paths.relative_managed_path(destination))
            artifact = artifacts.register_in_session(
                session,
                destination,
                kind="json",
                role="sound_alignment",
                session_id=session_id,
                parent_ids=[item.artifact_id],
                calculate_hash=False,
                metadata={"cache_key": cache_key(item), "schema_version": 1},
                _prepared=registration,
            )
            _rollback_cleanup(session, destination, identity)
            return artifact.id
        except BaseException:
            _remove_if_same(destination, identity)
            raise
    raise RuntimeError("Could not allocate a private sound alignment cache path.")
