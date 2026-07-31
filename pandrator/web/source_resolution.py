"""Single source of truth for a session's current primary source."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import literal, select, union_all
from sqlalchemy.orm import Session

from .models import Artifact, SessionSource, SourceAsset


VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".mpeg", ".mpg"}
)
AUDIO_EXTENSIONS = frozenset(
    {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"}
)
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


def _artifact_name(artifact: Artifact) -> str:
    return str(
        (artifact.metadata_json or {}).get("original_filename")
        or Path(artifact.relative_path).name
    )


def resolve_primary_source(
    db_session: Session,
    session_id: str,
) -> PrimarySourceResolution:
    """Resolve the attached primary source, with a bounded legacy fallback.

    Modern uploads always have a ``SourceAsset``. If such an asset is no longer
    attached, that is an intentional detach and must not be undone by looking at
    the old ``Artifact(role="upload")`` row. The fallback is therefore limited
    to pre-source-library uploads which have never been promoted to an asset.
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
    legacy_candidates = (
        select(
            Artifact.id.label("artifact_id"),
            literal(None).label("source_asset_id"),
            literal(None).label("attachment_id"),
            literal("legacy").label("resolution"),
            literal(1).label("priority"),
            literal(None).label("source_updated_at"),
            Artifact.created_at.label("artifact_created_at"),
        )
        .where(
            Artifact.session_id == session_id,
            Artifact.role == "upload",
            Artifact.state == "current",
            ~select(SourceAsset.id)
            .where(SourceAsset.artifact_id == Artifact.id)
            .exists(),
        )
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
    row = db_session.execute(
        select(Artifact, SourceAsset, SessionSource, selected.c.resolution)
        .select_from(selected)
        .outerjoin(Artifact, Artifact.id == selected.c.artifact_id)
        .outerjoin(SourceAsset, SourceAsset.id == selected.c.source_asset_id)
        .outerjoin(SessionSource, SessionSource.id == selected.c.attachment_id)
    ).first()
    if row:
        artifact, asset, attachment, resolution = row
        name = str(
            (asset.display_name if asset else "")
            or (_artifact_name(artifact) if artifact else "")
        )
        kind = str((asset.kind if asset else "") or (artifact.kind if artifact else ""))
        mime_type = str(
            (asset.mime_type if asset else "")
            or (artifact.mime_type if artifact else "")
            or ""
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
