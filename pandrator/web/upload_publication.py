"""Transactional publication of already validated multipart uploads."""

from __future__ import annotations

import errno
import logging
import mimetypes
import os
import uuid
from dataclasses import replace
from pathlib import Path
from shutil import copyfileobj
from typing import Any

from pandrator.runtime import DataPaths

from .access.upload_policy import require_upload_creation_access
from .artifacts import ArtifactService, sha256_file
from .database import Database
from .models import SourceRecord
from .source_library import SourceLibraryService
from .upload_activity import require_writable_upload_owner, upload_activity

logger = logging.getLogger(__name__)


def publish_multipart_upload(
    temporary: Path,
    *,
    filename: str,
    original_filename: str,
    session_id: str | None,
    purpose: str,
    database: Database,
    paths: DataPaths,
    artifacts: ArtifactService,
    sources: SourceLibraryService,
    workspace_id: str | None = None,
    attach_to_session: bool = True,
) -> dict[str, Any]:
    """Publish file and related rows, removing our link if the writer fails."""
    if type(attach_to_session) is not bool or (
        not attach_to_session and (session_id is None or purpose != "source")
    ):
        raise ValueError(
            "Staged source uploads require an explicit session and a strict boolean flag."
        )
    with database.session() as session:
        ownership = require_upload_creation_access(
            session, session_id=session_id, workspace_id=workspace_id
        )
        if not attach_to_session and ownership.project_id is None:
            raise ValueError("Staged uploads require an existing project.")
        require_writable_upload_owner(session, session_id)
    digest = sha256_file(temporary)
    destination = paths.uploads / f"{uuid.uuid4()}-{filename}"
    prepared = replace(
        artifacts.prepare_registration(temporary, calculate_hash=False),
        relative_path=destination.relative_to(paths.root).as_posix(),
        mime_type=mimetypes.guess_type(filename)[0],
        content_hash=digest,
    )
    staged = temporary.lstat()
    staged_identity = (staged.st_dev, staged.st_ino)
    upload_id = str(uuid.uuid4())
    with upload_activity(paths, session_id=session_id, upload_id=upload_id):
        published_identity: tuple[int, int] | None = None
        try:
            try:
                os.link(temporary, destination)
                published_identity = staged_identity
            except OSError as error:
                if error.errno not in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS}:
                    raise
                # Some writable filesystems cannot create hard links. Keep their
                # exclusive-copy fallback outside the SQLite write transaction.
                with destination.open("xb") as output:
                    created = os.fstat(output.fileno())
                    published_identity = (created.st_dev, created.st_ino)
                    with temporary.open("rb") as source:
                        copyfileobj(source, output)
            with database.immediate_session() as session:
                current_ownership = require_upload_creation_access(
                    session, session_id=session_id, workspace_id=ownership.workspace_id
                )
                require_writable_upload_owner(session, session_id)
                if current_ownership != ownership:
                    raise ValueError("The upload project changed before publication.")
                artifact = artifacts.register_in_session(
                    session,
                    destination,
                    kind="image" if purpose == "cover" else "source",
                    role="cover" if purpose == "cover" else "upload",
                    session_id=session_id,
                    project_id=ownership.project_id,
                    workspace_id=ownership.workspace_id,
                    calculate_hash=False,
                    metadata={"original_filename": original_filename, "purpose": purpose},
                    _prepared=prepared,
                )
                source_asset = None
                attachment = None
                if purpose == "source":
                    kind = Path(filename).suffix.lower().lstrip(".") or "file"
                    if session_id and attach_to_session:
                        session.add(
                            SourceRecord(
                                session_id=session_id,
                                kind=kind,
                                display_name=original_filename,
                                artifact_id=artifact.id,
                                content_hash=digest,
                            )
                        )
                    source_asset = sources.ensure_for_artifact_in_session(
                        session,
                        artifact.id,
                        display_name=original_filename,
                        kind=kind,
                        workspace_id=ownership.workspace_id,
                    )
                    if session_id and attach_to_session:
                        attachment = sources.attach_in_session(session, session_id, source_asset.id)
                result = {
                    "artifact_id": artifact.id,
                    "source_asset_id": source_asset.id if source_asset else None,
                    "attachment": attachment,
                    "filename": filename,
                    "size_bytes": prepared.size_bytes,
                    "sha256": digest,
                }
        except Exception:
            if published_identity is not None:
                try:
                    visible = destination.lstat()
                    if (visible.st_dev, visible.st_ino) == published_identity:
                        destination.unlink()
                except FileNotFoundError:
                    pass
                except Exception:
                    logger.warning(
                        "Could not remove failed upload publication %s", destination, exc_info=True
                    )
            raise
        return result
