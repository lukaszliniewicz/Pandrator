"""Resolve the exact media artifact associated with a subtitle artifact."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Artifact, ArtifactEdge, SessionSource, SourceAsset
from .source_resolution import classify_source, resolve_media_source


def _media_identity(artifact: Artifact) -> tuple[str, str] | None:
    """Return the immutable edit identity carried by a rendered artifact."""

    metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
    revision_id = str(
        metadata.get("media_edit_revision_id")
        or metadata.get("revision_id")
        or ""
    ).strip()
    content_hash = str(metadata.get("content_hash") or "").strip()
    if not revision_id or not content_hash:
        return None
    return revision_id, content_hash


def _is_media_artifact(artifact: Artifact) -> bool:
    if artifact.role == "media_edit_media":
        return True
    metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
    name = str(metadata.get("original_filename") or artifact.relative_path or "")
    return classify_source(
        name=name,
        kind=str(artifact.kind or ""),
        mime_type=str(artifact.mime_type or ""),
    ) in {"audio", "video"}


def artifact_accessible_in_session(
    session: Session,
    session_id: str,
    artifact: Artifact,
) -> bool:
    if artifact.state == "deleted":
        return False
    if artifact.session_id == session_id:
        return True
    return (
        session.scalar(
            select(SessionSource.id)
            .join(SourceAsset, SourceAsset.id == SessionSource.source_asset_id)
            .where(
                SessionSource.session_id == session_id,
                SessionSource.is_current.is_(True),
                SourceAsset.artifact_id == artifact.id,
            )
            .limit(1)
        )
        is not None
    )


def _lineage_artifacts(
    session: Session,
    session_id: str,
    source: Artifact,
) -> tuple[list[Artifact], list[Artifact]]:
    """Walk accessible subtitle provenance and collect explicit media."""

    artifacts_by_id = {source.id: source}
    pending = [source.id]
    explicit_media: dict[str, Artifact] = {}
    while pending:
        child_id = pending.pop()
        child = artifacts_by_id[child_id]
        metadata = child.metadata_json if isinstance(child.metadata_json, dict) else {}
        related_ids = set(
            session.scalars(
                select(ArtifactEdge.parent_artifact_id).where(
                    ArtifactEdge.child_artifact_id == child.id
                )
            ).all()
        )
        # Correction and translation artifacts persist this pointer even
        # when an older database did not retain the corresponding edge.
        metadata_source_id = str(metadata.get("source_artifact_id") or "").strip()
        if metadata_source_id:
            related_ids.add(metadata_source_id)
        metadata_media_id = str(
            metadata.get("source_media_artifact_id") or ""
        ).strip()
        if metadata_media_id:
            related_ids.add(metadata_media_id)

        for related_id in related_ids:
            if related_id in artifacts_by_id:
                continue
            related = session.get(Artifact, related_id)
            if related is None or not artifact_accessible_in_session(
                session, session_id, related
            ):
                raise ValueError(
                    "Subtitle provenance is unavailable: every declared "
                    "parent must be owned by or currently attached to this "
                    "session and remain nondeleted."
                )
            artifacts_by_id[related.id] = related
            pending.append(related.id)
            if _is_media_artifact(related):
                explicit_media[related.id] = related

    ancestors = list(artifacts_by_id.values())
    cut_ancestors = [
        artifact
        for artifact in ancestors
        if artifact.role == "media_edit_subtitles"
    ]
    return cut_ancestors, list(explicit_media.values())


def resolve_subtitle_media(
    session: Session,
    session_id: str,
    source: Artifact,
) -> Artifact:
    """Resolve media without allowing a cut subtitle to drift to another edit."""

    cut_ancestors, explicit_media = _lineage_artifacts(session, session_id, source)
    if cut_ancestors:
        identities = {
            identity
            for artifact in cut_ancestors
            if (identity := _media_identity(artifact)) is not None
        }
        if any(_media_identity(artifact) is None for artifact in cut_ancestors):
            raise ValueError(
                "A cut-derived subtitle requires a matching rendered edit; "
                "its immutable edit identity is missing."
            )
        if len(identities) != 1:
            raise ValueError(
                "A cut-derived subtitle requires a matching rendered edit; "
                "its immutable edit identity is ambiguous."
            )
        identity = next(iter(identities))
        candidates = list(
            session.scalars(
                select(Artifact)
                .where(
                    Artifact.session_id == session_id,
                    Artifact.role == "media_edit_media",
                    Artifact.state != "deleted",
                )
                .order_by(Artifact.created_at.desc(), Artifact.id.desc())
            ).all()
        )
        matching = next(
            (
                item
                for item in candidates
                if _media_identity(item) == identity
            ),
            None,
        )
        if matching is None:
            raise ValueError(
                "A cut-derived subtitle requires a matching rendered edit; "
                "no available media matches its immutable identity."
            )
        return matching

    if explicit_media:
        return max(
            explicit_media,
            key=lambda item: (item.created_at, item.id),
        )

    primary = resolve_media_source(session, session_id)
    media = primary.artifact
    if media is None or not primary.has_audio:
        raise ValueError("Attach a managed audio or video recording to this subtitle source.")
    return media
