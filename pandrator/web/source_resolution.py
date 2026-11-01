"""Single source of truth for a session's current primary source."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sqlalchemy import JSON, case, literal, select, union_all
from sqlalchemy.orm import Session, load_only, with_expression
from sqlalchemy.orm.interfaces import LoaderOption
from sqlalchemy.sql.elements import ColumnElement

from .models import Artifact, SessionSource, SourceAsset

VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".mpeg", ".mpg"})
AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"})
SUBTITLE_EXTENSIONS = frozenset({".srt", ".vtt", ".ass", ".ssa"})


def classify_source(*, name: str, kind: str, mime_type: str) -> str:
    """Classify a source using the same metadata at every application boundary."""

    extension = Path(name).suffix.lower()
    normalized_kind = str(kind or "").lower().lstrip(".")
    normalized_mime = str(mime_type or "").lower()
    kind_extension = f".{normalized_kind}" if normalized_kind else ""
    if (
        normalized_mime.startswith("video/")
        or extension in VIDEO_EXTENSIONS
        or kind_extension in VIDEO_EXTENSIONS
    ):
        return "video"
    if (
        normalized_mime.startswith("audio/")
        or extension in AUDIO_EXTENSIONS
        or kind_extension in AUDIO_EXTENSIONS
    ):
        return "audio"
    if extension in SUBTITLE_EXTENSIONS or kind_extension in SUBTITLE_EXTENSIONS:
        return "subtitles"
    return "document" if name else "none"


@dataclass(frozen=True, slots=True)
class PrimarySourceResolution:
    artifact: Artifact | None
    source_asset: SourceAsset | None
    attachment: SessionSource | None
    profile: str
    name: str
    kind: str
    mime_type: str
    resolution: str

    @property
    def has_video(self) -> bool:
        return self.profile == "video"

    @property
    def has_audio(self) -> bool:
        return self.profile in {"video", "audio"}


def _artifact_name(artifact: Artifact, *, include_metadata: bool = True) -> str:
    if not include_metadata:
        return Path(artifact.relative_path).name
    return str(
        (artifact.metadata_json or {}).get("original_filename") or Path(artifact.relative_path).name
    )


def _finite_source_options() -> tuple[LoaderOption, LoaderOption]:
    return (
        load_only(
            Artifact.id,
            Artifact.workspace_id,
            Artifact.project_id,
            Artifact.session_id,
            Artifact.relative_path,
            Artifact.kind,
            Artifact.role,
            Artifact.mime_type,
            Artifact.content_hash,
            Artifact.size_bytes,
            Artifact.state,
            raiseload=True,
        ),
        load_only(
            SourceAsset.id,
            SourceAsset.workspace_id,
            SourceAsset.artifact_id,
            SourceAsset.display_name,
            SourceAsset.kind,
            SourceAsset.mime_type,
            SourceAsset.state,
            raiseload=True,
        ),
    )


def resolve_primary_source(
    db_session: Session,
    session_id: str,
    *,
    authorize_input: Callable[[Session, str], None] | None = None,
    include_metadata: bool = True,
    metadata_visibility: ColumnElement[bool] | None = None,
) -> PrimarySourceResolution:
    """Resolve the attached primary source, with a bounded legacy fallback.

    Modern uploads always have a ``SourceAsset``. If such an asset is no longer
    attached, that is an intentional detach and must not be undone by looking at
    the old ``Artifact(role="upload")`` row. The fallback is therefore limited
    to pre-source-library uploads which have never been promoted to an asset.

    ``metadata_visibility`` masks metadata in SQL for an already-authorized
    caller; it grants no input access. ``include_metadata=False`` stays finite.
    """

    attached_candidates = (
        select(
            Artifact.id.label("artifact_id"),
            SourceAsset.id.label("source_asset_id"),
            SessionSource.id.label("attachment_id"),
            literal("attached").label("resolution"),
            literal(0).label("priority"),
            SessionSource.updated_at.label("source_updated_at"),
            Artifact.created_at.label("artifact_created_at"),
        )
        .select_from(SessionSource)
        .join(SourceAsset, SourceAsset.id == SessionSource.source_asset_id)
        .outerjoin(Artifact, Artifact.id == SourceAsset.artifact_id)
        .where(
            SessionSource.session_id == session_id,
            SessionSource.role == "primary",
            SessionSource.is_current.is_(True),
        )
    )
    legacy_candidates = select(
        Artifact.id.label("artifact_id"),
        literal(None).label("source_asset_id"),
        literal(None).label("attachment_id"),
        literal("legacy").label("resolution"),
        literal(1).label("priority"),
        literal(None).label("source_updated_at"),
        Artifact.created_at.label("artifact_created_at"),
    ).where(
        Artifact.session_id == session_id,
        Artifact.role == "upload",
        Artifact.state == "current",
        ~select(SourceAsset.id).where(SourceAsset.artifact_id == Artifact.id).exists(),
    )
    candidates = union_all(attached_candidates, legacy_candidates).subquery(
        "primary_source_candidates"
    )
    selected = (
        select(candidates)
        .order_by(
            candidates.c.priority,
            candidates.c.source_updated_at.desc(),
            candidates.c.artifact_created_at.desc(),
            candidates.c.artifact_id.desc(),
        )
        .limit(1)
        .subquery("primary_source")
    )
    statement = (
        select(Artifact, SourceAsset, SessionSource, selected.c.resolution)
        .select_from(selected)
        .outerjoin(Artifact, Artifact.id == selected.c.artifact_id)
        .outerjoin(SourceAsset, SourceAsset.id == selected.c.source_asset_id)
        .outerjoin(SessionSource, SessionSource.id == selected.c.attachment_id)
    )
    if not include_metadata:
        statement = statement.options(*_finite_source_options())
    elif metadata_visibility is not None:
        statement = statement.options(
            *_finite_source_options(),
            load_only(
                Artifact.created_at, Artifact.updated_at, Artifact.settings_hash,
                raiseload=True,
            ),
            with_expression(
                Artifact.metadata_json,
                case(
                    (metadata_visibility, Artifact.metadata_json),
                    else_=literal({}, type_=JSON),
                ),
            ),
            with_expression(SourceAsset.metadata_json, literal({}, type_=JSON)),
        )
    if authorize_input is not None:
        header = db_session.execute(select(selected)).first()
        if header is None:
            row = None
        else:
            authorize_input(db_session, header.artifact_id or "")
            # Pin the authorized candidate rather than allowing a subsequent
            # attachment selection to choose a different source.
            row = db_session.execute(
                statement.where(
                    selected.c.artifact_id == header.artifact_id,
                    selected.c.source_asset_id == header.source_asset_id,
                    selected.c.attachment_id == header.attachment_id,
                )
            ).first()
    else:
        row = db_session.execute(statement).first()
    if row:
        artifact, asset, attachment, resolution = row
        name = str(
            (asset.display_name if asset else "")
            or (
                (
                    _artifact_name(artifact)
                    if include_metadata
                    else _artifact_name(artifact, include_metadata=False)
                )
                if artifact
                else ""
            )
        )
        kind = str((asset.kind if asset else "") or (artifact.kind if artifact else ""))
        mime_type = str(
            (asset.mime_type if asset else "") or (artifact.mime_type if artifact else "") or ""
        )
        return PrimarySourceResolution(
            artifact=artifact,
            source_asset=asset,
            attachment=attachment,
            profile=classify_source(name=name, kind=kind, mime_type=mime_type),
            name=name,
            kind=kind,
            mime_type=mime_type,
            resolution=str(resolution),
        )

    return PrimarySourceResolution(
        artifact=None,
        source_asset=None,
        attachment=None,
        profile="none",
        name="",
        kind="",
        mime_type="",
        resolution="none",
    )


def resolve_media_source(
    db_session: Session,
    session_id: str,
    *,
    primary: PrimarySourceResolution | None = None,
    authorize_input: Callable[[Session, str], None] | None = None,
    include_metadata: bool = True,
) -> PrimarySourceResolution:
    """Resolve the independently attached media target, then a media primary.

    A subtitle primary remains authoritative text when a recording is attached.
    Do not search old uploads: a detached recording must not reappear.
    """
    statement = (
        select(SessionSource, SourceAsset, Artifact)
        .join(SourceAsset, SourceAsset.id == SessionSource.source_asset_id)
        .join(Artifact, Artifact.id == SourceAsset.artifact_id)
        .where(
            SessionSource.session_id == session_id,
            SessionSource.role == "media",
            SessionSource.is_current.is_(True),
            SourceAsset.state == "current",
            Artifact.state != "deleted",
        )
        .order_by(SessionSource.updated_at.desc(), SessionSource.id.desc())
        .limit(1)
    )
    header_statement = statement.with_only_columns(Artifact.id, SourceAsset.id, SessionSource.id)
    if not include_metadata:
        statement = statement.options(*_finite_source_options())
    if authorize_input is not None:
        header = db_session.execute(header_statement).first()
        if header is None:
            row = None
        else:
            artifact_id, asset_id, attachment_id = header
            authorize_input(db_session, artifact_id)
            row = db_session.execute(
                statement.where(
                    Artifact.id == artifact_id,
                    SourceAsset.id == asset_id,
                    SessionSource.id == attachment_id,
                )
            ).first()
    else:
        row = db_session.execute(statement).first()
    if row is None:
        # Reuse a resolution from this transaction, never a cross-request cache.
        if primary is not None:
            if authorize_input is not None and (primary.artifact or primary.source_asset):
                authorize_input(db_session, primary.artifact.id if primary.artifact else "")
            return primary
        if not include_metadata:
            if authorize_input is not None:
                return resolve_primary_source(
                    db_session, session_id, authorize_input=authorize_input, include_metadata=False
                )
            return resolve_primary_source(db_session, session_id, include_metadata=False)
        if authorize_input is not None:
            return resolve_primary_source(db_session, session_id, authorize_input=authorize_input)
        return resolve_primary_source(db_session, session_id)
    attachment, asset, artifact = row
    name = str(
        asset.display_name
        or (
            _artifact_name(artifact)
            if include_metadata
            else _artifact_name(artifact, include_metadata=False)
        )
    )
    kind = str(asset.kind or artifact.kind or "")
    mime = str(asset.mime_type or artifact.mime_type or "")
    return PrimarySourceResolution(
        artifact=artifact,
        source_asset=asset,
        attachment=attachment,
        profile=classify_source(name=name, kind=kind, mime_type=mime),
        name=name,
        kind=kind,
        mime_type=mime,
        resolution="attached_media",
    )
