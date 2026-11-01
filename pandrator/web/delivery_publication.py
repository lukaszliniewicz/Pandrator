"""Publish caller-rendered delivery files as immutable managed artifacts."""

from __future__ import annotations

import os
import stat
import threading
from dataclasses import replace
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService, PreparedArtifactRegistration
from .database import Database
from .models import Artifact


def _require_nonempty_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")


def _lstat_if_present(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _regular_file_identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Expected a regular file: {path}")
    return info.st_dev, info.st_ino


def _raise_if_cancelled(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise InterruptedError("Delivery publication was canceled.")


def _remove_owned_unregistered_file(
    database: Database,
    destination: Path,
    *,
    destination_identity: tuple[int, int] | None,
    relative_path: str | None,
    content_hash: str | None,
) -> None:
    """Remove a failed attempt's bytes only after a durable absence check."""
    if destination_identity is None or relative_path is None or content_hash is None:
        return

    try:
        with database.snapshot_session() as session:
            registered = session.scalar(
                select(Artifact.id)
                .where(
                    Artifact.relative_path == relative_path,
                    Artifact.content_hash == content_hash,
                )
                .limit(1)
            )
    except Exception:
        # A failed or uncertain commit check means the output bytes must stay.
        return

    if registered is not None:
        return

    try:
        current = destination.lstat()
        if not stat.S_ISREG(current.st_mode):
            return
        if (current.st_dev, current.st_ino) != destination_identity:
            return
        destination.unlink()
    except OSError:
        # Cleanup is best effort; never turn a publication failure into a
        # deletion of bytes whose current identity cannot be proven.
        return


def publish_delivery_artifact(
    database: Database,
    paths: DataPaths,
    artifacts: ArtifactService,
    rendered: Path,
    requested_destination: Path,
    *,
    session_id: str,
    role: str = "export",
    kind: str = "export",
    parent_ids: list[str] | None = None,
    settings: dict | None = None,
    metadata: dict | None = None,
    before_io: Callable[[], None],
    authorization_guard: Callable[[Session], None],
    cancel_event: threading.Event,
) -> Artifact:
    """Atomically move and register one rendered delivery artifact.

    The supplied guards define current caller authority. Byte hashing happens
    before the immediate writer; the final move and artifact lineage transaction
    then revalidate authority and cancellation before committing.
    """
    if not isinstance(database, Database):
        raise TypeError("database must be a Database.")
    if not isinstance(paths, DataPaths):
        raise TypeError("paths must be DataPaths.")
    if not isinstance(artifacts, ArtifactService):
        raise TypeError("artifacts must be an ArtifactService.")
    if artifacts.database.path != database.path or artifacts.paths != paths:
        raise ValueError("ArtifactService must use the supplied database and paths.")
    if not isinstance(rendered, Path) or not isinstance(requested_destination, Path):
        raise TypeError("rendered and requested_destination must be Path objects.")
    if rendered.suffix.casefold() != requested_destination.suffix.casefold():
        raise ValueError("Rendered and requested destination suffixes must match.")
    _require_nonempty_text(session_id, "session_id")
    _require_nonempty_text(role, "role")
    _require_nonempty_text(kind, "kind")
    if parent_ids is not None and (
        not isinstance(parent_ids, list)
        or any(not isinstance(parent_id, str) or not parent_id.strip() for parent_id in parent_ids)
    ):
        raise ValueError("parent_ids must be a list of non-empty strings or None.")
    if settings is not None and not isinstance(settings, dict):
        raise TypeError("settings must be a dictionary or None.")
    if metadata is not None and not isinstance(metadata, dict):
        raise TypeError("metadata must be a dictionary or None.")
    if not callable(before_io):
        raise TypeError("before_io must be callable.")
    if not callable(authorization_guard):
        raise TypeError("authorization_guard must be callable.")
    if not isinstance(cancel_event, threading.Event):
        raise TypeError("cancel_event must be a threading.Event.")

    _raise_if_cancelled(cancel_event)
    before_io()

    # Resolve both paths through DataPaths after the caller's read guard, then
    # reject final-component symlinks before ArtifactService follows any path.
    rendered_relative = paths.relative_managed_path(rendered)
    requested_relative = paths.relative_managed_path(requested_destination)
    if rendered_relative == requested_relative:
        raise ValueError("Rendered source and requested destination must differ.")
    source_identity = _regular_file_identity(rendered)
    requested_info = _lstat_if_present(requested_destination)
    if requested_info is not None and stat.S_ISLNK(requested_info.st_mode):
        raise ValueError("Requested destination must not be a symlink.")

    prepared: PreparedArtifactRegistration = artifacts.prepare_registration(
        rendered, settings=settings
    )
    if prepared.content_hash is None:
        raise ValueError("Delivery artifact preparation must include a content hash.")
    if _regular_file_identity(rendered) != source_identity:
        raise RuntimeError("Rendered source changed during artifact preparation.")

    destination: Path | None = None
    destination_relative: str | None = None
    destination_identity: tuple[int, int] | None = None
    try:
        with database.immediate_session() as session:
            _raise_if_cancelled(cancel_event)
            authorization_guard(session)
            _raise_if_cancelled(cancel_event)

            requested_destination.parent.mkdir(parents=True, exist_ok=True)
            destination = artifacts.next_available_path(requested_destination)
            destination_relative = paths.relative_managed_path(destination)
            if destination_relative == rendered_relative:
                raise ValueError("Allocated destination must differ from rendered source.")
            if _lstat_if_present(destination) is not None:
                # Reject paths already occupied at the guarded allocation
                # boundary, including dangling symlinks. The managed directory
                # is private to this application, not hostile OS writers.
                raise FileExistsError(f"Allocated delivery path already exists: {destination}")
            if _regular_file_identity(rendered) != source_identity:
                raise RuntimeError("Rendered source identity changed before publication.")

            os.replace(rendered, destination)
            moved_identity = _regular_file_identity(destination)
            if moved_identity != source_identity:
                raise RuntimeError("Moved delivery file is not the prepared source inode.")
            destination_identity = moved_identity

            prepared_for_destination = replace(prepared, relative_path=destination_relative)
            artifact = artifacts.register_in_session(
                session,
                destination,
                kind=kind,
                role=role,
                session_id=session_id,
                parent_ids=parent_ids,
                settings=settings,
                metadata=metadata,
                _prepared=prepared_for_destination,
            )
            _raise_if_cancelled(cancel_event)

        return artifact
    except BaseException:
        if destination is not None:
            _remove_owned_unregistered_file(
                database,
                destination,
                destination_identity=destination_identity,
                relative_path=destination_relative,
                content_hash=prepared.content_hash,
            )
        raise
