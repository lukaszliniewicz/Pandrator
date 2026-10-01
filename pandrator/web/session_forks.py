"""Independent session branches created from reviewed subtitle checkpoints."""

from __future__ import annotations

import shutil
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.logic.dubbing.srt_utils import parse_srt
from pandrator.runtime import DataPaths

from .artifacts import ArtifactService, sha256_file
from .database import Database
from .logical_passages import stored_passages
from .media_edit import MediaEditService
from .models import (
    Artifact,
    ArtifactEdge,
    Document,
    DocumentRevision,
    MediaEditPlan,
    MediaEditPlanRevision,
    OutcomePlan,
    Segment,
    SegmentLineage,
    SessionRecord,
    SessionSetting,
    SessionSource,
    SourceAsset,
    SubtitleEvidence,
    TimedWord,
    new_id,
)
from .sessions import RevisionConflict, SessionService

CHECKPOINT_STAGES = ("transcription", "correction", "translation")
CHECKPOINT_RANK = {stage: index for index, stage in enumerate(CHECKPOINT_STAGES)}


@dataclass(frozen=True, slots=True)
class SessionForkResult:
    record: SessionRecord
    directory: Path
    checkpoint_artifact_id: str
    copied_stages: tuple[str, ...]
    copied_media_artifact_ids: tuple[str, ...] = ()
    artifact_id_map: dict[str, str] = field(default_factory=dict)


class SessionForkService:
    """Clone coherent subtitle, media and evidence snapshots into an independent session."""

    def __init__(
        self,
        database: Database,
        paths: DataPaths,
        artifacts: ArtifactService,
    ) -> None:
        self.database = database
        self.paths = paths
        self.artifacts = artifacts

    @staticmethod
    def _unique_name(session: Session, requested: str, source: SessionRecord, stage: str) -> str:
        base = str(requested or "").strip() or f"{source.name} — {stage.title()} fork"
        if len(base) > 255:
            raise ValueError("A fork name cannot be longer than 255 characters.")
        active_names = {
            str(name or "").strip().casefold()
            for name in session.scalars(
                select(SessionRecord.name).where(SessionRecord.trashed_at.is_(None))
            ).all()
        }
        if base.casefold() not in active_names:
            return base
        for number in range(2, 10_000):
            suffix = f" ({number})"
            candidate = f"{base[: 255 - len(suffix)].rstrip()}{suffix}"
            if candidate.casefold() not in active_names:
                return candidate
        raise ValueError("Could not create a unique fork name.")

    @staticmethod
    def _coherent_checkpoints(
        session: Session,
        source_session_id: str,
        checkpoint: Artifact,
    ) -> list[Artifact]:
        artifacts = {
            artifact.id: artifact
            for artifact in session.scalars(
                select(Artifact).where(Artifact.session_id == source_session_id)
            ).all()
        }
        parent_ids: dict[str, list[str]] = {}
        if artifacts:
            for parent_id, child_id in session.execute(
                select(
                    ArtifactEdge.parent_artifact_id,
                    ArtifactEdge.child_artifact_id,
                ).where(ArtifactEdge.child_artifact_id.in_(tuple(artifacts)))
            ):
                parent_ids.setdefault(child_id, []).append(parent_id)

        maximum_rank = CHECKPOINT_RANK[checkpoint.role]
        selected: dict[str, Artifact] = {}
        pending: deque[str] = deque([checkpoint.id])
        visited: set[str] = set()
        while pending:
            artifact_id = pending.popleft()
            if artifact_id in visited:
                continue
            visited.add(artifact_id)
            artifact = artifacts.get(artifact_id)
            if artifact is not None:
                rank = CHECKPOINT_RANK.get(artifact.role)
                if rank is not None and rank <= maximum_rank:
                    selected.setdefault(artifact.role, artifact)
            pending.extend(parent_ids.get(artifact_id, ()))

        if checkpoint.role not in selected:
            raise ValueError("The selected checkpoint is not part of this session's text lineage.")
        return [selected[stage] for stage in CHECKPOINT_STAGES if stage in selected]

    @staticmethod
    def _revision_records(
        session: Session,
        source_session_id: str,
        artifact: Artifact,
        source_path: Path,
    ) -> tuple[
        str | None,
        bool,
        str,
        str | None,
        list[dict[str, Any]],
        list[TimedWord],
    ]:
        metadata = dict(artifact.metadata_json or {})
        revision_id = str(metadata.get("revision_id") or "")
        if revision_id:
            revision = session.get(DocumentRevision, revision_id)
            document = session.get(Document, revision.document_id) if revision else None
            if (
                revision is not None
                and document is not None
                and document.session_id == source_session_id
                and document.stage == artifact.role
            ):
                segments = list(
                    session.scalars(
                        select(Segment)
                        .where(Segment.revision_id == revision.id)
                        .order_by(Segment.ordinal)
                    ).all()
                )
                words = list(
                    session.scalars(
                        select(TimedWord)
                        .where(TimedWord.revision_id == revision.id)
                        .order_by(TimedWord.ordinal)
                    ).all()
                )
                return (
                    document.language,
                    revision.reviewed,
                    revision.content_hash,
                    revision.settings_hash,
                    [
                        {
                            "source_id": item.id,
                            "ordinal": item.ordinal,
                            "node_kind": item.node_kind,
                            "start_ms": item.start_ms,
                            "end_ms": item.end_ms,
                            "text": item.text,
                            "speaker": item.speaker,
                            "metadata_json": deepcopy(item.metadata_json or {}),
                        }
                        for item in segments
                    ],
                    words,
                )

        if source_path.suffix.lower() != ".srt":
            raise ValueError(
                f"The {artifact.role} checkpoint has no recoverable subtitle document."
            )
        parsed = parse_srt(source_path.read_text(encoding="utf-8-sig"))
        return (
            str(metadata.get("language") or "") or None,
            bool(metadata.get("reviewed")),
            str(artifact.content_hash or "forked-checkpoint"),
            artifact.settings_hash,
            [
                {
                    "source_id": None,
                    "ordinal": index,
                    "node_kind": "subtitle_cue",
                    "start_ms": item.start_ms,
                    "end_ms": item.end_ms,
                    "text": item.text,
                    "speaker": item.speaker or None,
                    "metadata_json": {},
                }
                for index, item in enumerate(parsed)
            ],
            [],
        )

    @staticmethod
    def _remap_translation_source_setting(
        setting: SessionSetting | None,
        old_to_new_artifact_ids: dict[str, str],
    ) -> None:
        """Keep a copied translation source inside the forked checkpoint path.

        ``translation.source_artifact_id`` is the only persisted settings field
        that participates in subtitle checkpoint lineage. Other settings may
        contain values that look like IDs but do not belong to the checkpoint
        clone, so they must remain untouched. If the configured source was not
        cloned, removing the override lets the fork select a local prerequisite
        instead of retaining a foreign-session artifact ID.
        """
        if setting is None or not isinstance(setting.value_json, dict):
            return
        source_id = str(setting.value_json.get("source_artifact_id") or "")
        if not source_id:
            return
        value = dict(setting.value_json)
        remapped_source_id = old_to_new_artifact_ids.get(source_id)
        if remapped_source_id:
            value["source_artifact_id"] = remapped_source_id
        else:
            value.pop("source_artifact_id", None)
        setting.value_json = value

    def fork_in_session(
        self,
        session: Session,
        source_session_id: str,
        checkpoint_artifact_id: str,
        *,
        name: str = "",
        carry_media_assets: bool = True,
        target_language: str | None = None,
        expected_revision: int | None = None,
    ) -> SessionForkResult:
        source = session.get(SessionRecord, source_session_id)
        checkpoint = session.get(Artifact, checkpoint_artifact_id)
        if source is None or source.trashed_at is not None:
            raise KeyError(source_session_id)
        if checkpoint is None or checkpoint.session_id != source_session_id:
            raise KeyError(checkpoint_artifact_id)
        if checkpoint.role not in {"correction", "translation"}:
            raise ValueError("A session can be forked only after correction or translation.")
        if checkpoint.state == "deleted":
            raise ValueError("The selected checkpoint is no longer available.")
        if expected_revision is not None and source.revision != expected_revision:
            raise RevisionConflict(
                f"Session revision conflict: expected {expected_revision}, current is {source.revision}."
            )

        plan = None
        active_edit = None
        render_sources: list[Artifact] = []
        if carry_media_assets:
            plan = session.scalar(
                select(MediaEditPlan).where(MediaEditPlan.session_id == source_session_id)
            )
            if plan is not None:
                active_edit = (
                    session.get(MediaEditPlanRevision, plan.active_revision_id)
                    if plan.active_revision_id
                    else None
                )
                if active_edit is None or active_edit.plan_id != plan.id:
                    raise ValueError("The active media-edit revision is unavailable.")
                if checkpoint.state != "current":
                    raise ValueError("A media-edit fork requires a current text checkpoint.")
                for role in ("media_edit_media", "media_edit_subtitles"):
                    rendered = MediaEditService._lookup_current_artifact(
                        session, source_session_id, role
                    )
                    if rendered is None:
                        raise ValueError(
                            f"The current {role} render is unavailable; render the edited timeline first."
                        )
                    metadata = rendered.metadata_json or {}
                    if (
                        metadata.get("plan_id") != plan.id
                        or metadata.get("media_edit_revision_id") != active_edit.id
                        or metadata.get("content_hash") != active_edit.content_hash
                    ):
                        raise ValueError(
                            f"The current {role} render does not match the active media-edit revision."
                        )
                    render_sources.append(rendered)
                timing_render = MediaEditService._lookup_current_artifact(
                    session, source_session_id, "media_edit_word_timestamps"
                )
                if timing_render is None:
                    raise ValueError(
                        "The current media-edit timing render is unavailable; render the edited timeline first."
                    )
                metadata = timing_render.metadata_json or {}
                if (
                    metadata.get("plan_id") != plan.id
                    or metadata.get("media_edit_revision_id") != active_edit.id
                    or metadata.get("content_hash") != active_edit.content_hash
                ):
                    raise ValueError(
                        "The current media-edit timing render does not match the active revision."
                    )
                render_sources.append(timing_render)

        checkpoints = self._coherent_checkpoints(
            session,
            source_session_id,
            checkpoint,
        )
        if active_edit is not None:
            edited_subtitle_id = next(
                item.id for item in render_sources if item.role == "media_edit_subtitles"
            )
            frontier = [checkpoint.id]
            ancestors: set[str] = set()
            while frontier:
                child_id = frontier.pop()
                if child_id in ancestors:
                    continue
                ancestors.add(child_id)
                frontier.extend(
                    session.scalars(
                        select(ArtifactEdge.parent_artifact_id).where(
                            ArtifactEdge.child_artifact_id == child_id
                        )
                    ).all()
                )
                item = session.get(Artifact, child_id)
                source_id = (item.metadata_json or {}).get("source_artifact_id") if item else None
                if source_id:
                    frontier.append(str(source_id))
            if edited_subtitle_id not in ancestors:
                raise ValueError(
                    "The current text checkpoint is not derived from the active edited timeline."
                )
        checkpoint_sources = [
            (artifact, self.paths.managed_path(artifact.relative_path)) for artifact in checkpoints
        ]
        for artifact, path in checkpoint_sources:
            if not path.is_file():
                raise FileNotFoundError(f"The {artifact.role} checkpoint file is missing: {path}")
            if artifact.content_hash and sha256_file(path) != artifact.content_hash:
                raise ValueError(f"The {artifact.role} checkpoint content hash changed.")

        record_id = new_id()
        storage_key = new_id()
        destination_dir = self.paths.sessions / storage_key
        try:
            destination_dir.mkdir(parents=True, exist_ok=False)
            record = SessionService(self.database).create(
                self._unique_name(session, name, source, checkpoint.role),
                workflow_kind=source.workflow_kind,
                source_language=source.source_language,
                target_language=(
                    target_language if target_language is not None else source.target_language
                ),
                workflow_preset=source.workflow_preset,
                included_stages=list(source.included_stages_json or []),
                record_id=record_id,
                storage_key=storage_key,
                db_session=session,
                seed_voice_mode=False,
            )

            copied_translation_setting: SessionSetting | None = None
            for setting in session.scalars(
                select(SessionSetting).where(SessionSetting.session_id == source_session_id)
            ).all():
                copied_setting = SessionSetting(
                    session_id=record.id,
                    section=setting.section,
                    value_json=deepcopy(setting.value_json or {}),
                )
                session.add(copied_setting)
                if copied_setting.section == "translation":
                    copied_translation_setting = copied_setting
                    if target_language is not None:
                        value = dict(copied_setting.value_json)
                        value["target_language"] = target_language
                        copied_setting.value_json = value
            outcome = session.get(OutcomePlan, source_session_id)
            if outcome is not None:
                session.add(
                    OutcomePlan(
                        session_id=record.id,
                        value_json=deepcopy(outcome.value_json or {}),
                    )
                )

            source_attachments = list(
                session.scalars(
                    select(SessionSource).where(
                        SessionSource.session_id == source_session_id,
                        SessionSource.is_current.is_(True),
                    )
                ).all()
            )
            for attachment in source_attachments:
                attached_asset = session.get(SourceAsset, attachment.source_asset_id)
                if attached_asset is None or attached_asset.state != "current":
                    raise ValueError("A current source attachment is unavailable for this fork.")
                session.add(
                    SessionSource(
                        session_id=record.id,
                        source_asset_id=attachment.source_asset_id,
                        role=attachment.role,
                    )
                )
            session.flush()

            # Source assets are intentionally shared library objects. Their
            # managed artifact remains the first immutable parent in the fork.
            source_parent_id = None
            if source_attachments:
                primary = next(
                    (item for item in source_attachments if item.role == "primary"),
                    source_attachments[0],
                )
                source_asset = session.get(SourceAsset, primary.source_asset_id)
                source_parent_id = source_asset.artifact_id if source_asset else None

            old_to_new_segments: dict[str, str] = {}
            old_to_new_revisions: dict[str, str] = {}
            old_to_new_artifact_ids: dict[str, str] = {}
            previous_artifact_id = source_parent_id
            copied_artifacts: dict[str, Artifact] = {}
            for artifact, source_path in checkpoint_sources:
                (
                    language,
                    reviewed,
                    revision_content_hash,
                    revision_settings_hash,
                    segments,
                    words,
                ) = self._revision_records(
                    session,
                    source_session_id,
                    artifact,
                    source_path,
                )
                document = Document(
                    session_id=record.id,
                    stage=artifact.role,
                    language=language,
                )
                session.add(document)
                session.flush()
                revision = DocumentRevision(
                    document_id=document.id,
                    revision_number=1,
                    content_hash=revision_content_hash,
                    reviewed=reviewed,
                    settings_hash=revision_settings_hash,
                )
                session.add(revision)
                session.flush()
                old_revision_id = str((artifact.metadata_json or {}).get("revision_id") or "")
                if old_revision_id:
                    old_to_new_revisions[old_revision_id] = revision.id
                new_segments: list[Segment] = []
                for item in segments:
                    segment = Segment(
                        revision_id=revision.id,
                        ordinal=int(item["ordinal"]),
                        node_kind=str(item["node_kind"]),
                        start_ms=item["start_ms"],
                        end_ms=item["end_ms"],
                        text=str(item["text"]),
                        speaker=item["speaker"],
                        metadata_json=deepcopy(item["metadata_json"]),
                    )
                    session.add(segment)
                    new_segments.append(segment)
                session.flush()
                for item, segment in zip(segments, new_segments, strict=True):
                    if item["source_id"]:
                        old_to_new_segments[str(item["source_id"])] = segment.id
                for word in words:
                    session.add(
                        TimedWord(
                            revision_id=revision.id,
                            segment_id=(
                                old_to_new_segments.get(str(word.segment_id))
                                if word.segment_id
                                else None
                            ),
                            ordinal=word.ordinal,
                            text=word.text,
                            start_ms=word.start_ms,
                            end_ms=word.end_ms,
                            speaker=word.speaker,
                            confidence=word.confidence,
                            metadata_json=deepcopy(word.metadata_json or {}),
                        )
                    )
                document.active_revision_id = revision.id

                destination = (
                    destination_dir
                    / "checkpoints"
                    / f"{artifact.role}-{new_id()}{source_path.suffix.lower()}"
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination)
                metadata = deepcopy(artifact.metadata_json or {})
                metadata.update(
                    {
                        "document_id": document.id,
                        "revision_id": revision.id,
                        "stage": artifact.role,
                        "language": language,
                        "forked_from_session_id": source_session_id,
                        "forked_from_artifact_id": artifact.id,
                    }
                )
                if previous_artifact_id:
                    metadata["source_artifact_id"] = previous_artifact_id
                passage_rows = stored_passages(artifact)
                if passage_rows is not None:
                    packet = deepcopy((artifact.metadata_json or {})["logical_passages"])
                    packet["display_revision_id"] = revision.id
                    packet["copied_from_artifact_id"] = artifact.id
                    packet.pop("speech_source_revision_id", None)
                    # Historical source IDs remain traceability only. The fork
                    # creates its own executable passage revision when needed.
                    metadata["logical_passages"] = packet
                else:
                    metadata.pop("logical_passages", None)
                cloned = self.artifacts.register_in_session(
                    session,
                    destination,
                    kind=artifact.kind,
                    role=artifact.role,
                    session_id=record.id,
                    parent_ids=([previous_artifact_id] if previous_artifact_id else []),
                    metadata=metadata,
                )
                cloned.settings_hash = artifact.settings_hash
                copied_artifacts[artifact.role] = cloned
                old_to_new_artifact_ids[artifact.id] = cloned.id
                previous_artifact_id = cloned.id

            shared_ids = {
                asset.artifact_id
                for attachment in source_attachments
                if (
                    (asset := session.get(SourceAsset, attachment.source_asset_id))
                    and asset.state == "current"
                )
            }
            copied_extra: dict[str, Artifact] = {}
            cloning: set[str] = set()
            forbidden_roles = {
                "assembled_audio",
                "audiobook_audio",
                "dubbing_audio",
                "export",
                "export_media",
                "generated_audio",
                "tts_audio",
            }

            def copy_revision(source_revision_id: str, *, auxiliary: bool) -> str:
                if source_revision_id in old_to_new_revisions:
                    return old_to_new_revisions[source_revision_id]
                original = session.get(DocumentRevision, source_revision_id)
                original_document = (
                    session.get(Document, original.document_id) if original else None
                )
                if (
                    original is None
                    or original_document is None
                    or original_document.session_id != source_session_id
                ):
                    raise ValueError(
                        f"Referenced subtitle revision is unavailable: {source_revision_id}"
                    )
                document = Document(
                    session_id=record.id,
                    stage=original_document.stage,
                    language=original_document.language,
                    created_at=original_document.created_at,
                )
                session.add(document)
                session.flush()
                revision = DocumentRevision(
                    document_id=document.id,
                    revision_number=original.revision_number,
                    content_hash=original.content_hash,
                    reviewed=original.reviewed,
                    settings_hash=original.settings_hash,
                    created_at=original.created_at,
                )
                session.add(revision)
                session.flush()
                old_to_new_revisions[original.id] = revision.id
                for segment in session.scalars(
                    select(Segment)
                    .where(Segment.revision_id == original.id)
                    .order_by(Segment.ordinal)
                ).all():
                    clone = Segment(
                        revision_id=revision.id,
                        ordinal=segment.ordinal,
                        node_kind=segment.node_kind,
                        start_ms=segment.start_ms,
                        end_ms=segment.end_ms,
                        text=segment.text,
                        speaker=segment.speaker,
                        metadata_json=deepcopy(segment.metadata_json or {}),
                    )
                    session.add(clone)
                    session.flush()
                    old_to_new_segments[segment.id] = clone.id
                for word in session.scalars(
                    select(TimedWord)
                    .where(TimedWord.revision_id == original.id)
                    .order_by(TimedWord.ordinal)
                ).all():
                    session.add(
                        TimedWord(
                            revision_id=revision.id,
                            segment_id=old_to_new_segments.get(word.segment_id)
                            if word.segment_id
                            else None,
                            ordinal=word.ordinal,
                            text=word.text,
                            start_ms=word.start_ms,
                            end_ms=word.end_ms,
                            speaker=word.speaker,
                            confidence=word.confidence,
                            metadata_json=deepcopy(word.metadata_json or {}),
                        )
                    )
                if not auxiliary:
                    document.active_revision_id = revision.id
                return revision.id

            def copy_artifact(source_artifact_id: str) -> str:
                if source_artifact_id in old_to_new_artifact_ids:
                    return old_to_new_artifact_ids[source_artifact_id]
                original = session.get(Artifact, source_artifact_id)
                if original is None or original.state == "deleted":
                    raise ValueError(
                        f"Referenced fork artifact is unavailable: {source_artifact_id}"
                    )
                if source_artifact_id in shared_ids:
                    shared_path = self.paths.managed_path(original.relative_path)
                    if not shared_path.is_file():
                        raise FileNotFoundError(
                            f"Referenced source media file is missing: {shared_path}"
                        )
                    if original.content_hash and sha256_file(shared_path) != original.content_hash:
                        raise ValueError(f"Referenced source media hash changed: {original.id}")
                    old_to_new_artifact_ids[source_artifact_id] = source_artifact_id
                    return source_artifact_id
                if original.session_id != source_session_id:
                    raise ValueError(
                        f"Referenced artifact belongs to another session: {source_artifact_id}"
                    )
                if original.role in forbidden_roles:
                    raise ValueError(
                        f"A generated output cannot be used as a fork dependency: {original.role}"
                    )
                if source_artifact_id in cloning:
                    raise ValueError("Artifact provenance contains a cycle.")
                cloning.add(source_artifact_id)
                parents = list(
                    session.scalars(
                        select(ArtifactEdge.parent_artifact_id).where(
                            ArtifactEdge.child_artifact_id == original.id
                        )
                    ).all()
                )
                copied_parents = [copy_artifact(parent_id) for parent_id in parents]
                original_path = self.paths.managed_path(original.relative_path)
                if not original_path.is_file():
                    raise FileNotFoundError(
                        f"Referenced fork artifact file is missing: {original_path}"
                    )
                if original.content_hash and sha256_file(original_path) != original.content_hash:
                    raise ValueError(f"Referenced fork artifact hash changed: {original.id}")
                destination = (
                    destination_dir / "dependencies" / f"{new_id()}{original_path.suffix.lower()}"
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original_path, destination)
                metadata = deepcopy(original.metadata_json or {})
                old_revision_id = str(metadata.get("revision_id") or "")
                if old_revision_id:
                    linked_revision = session.get(DocumentRevision, old_revision_id)
                    linked_document = (
                        session.get(Document, linked_revision.document_id)
                        if linked_revision
                        else None
                    )
                    if (
                        linked_document is not None
                        and linked_document.session_id == source_session_id
                    ):
                        metadata["revision_id"] = copy_revision(old_revision_id, auxiliary=True)
                        copied_document_revision = session.get(
                            DocumentRevision, metadata["revision_id"]
                        )
                        if copied_document_revision is None:
                            raise ValueError("The copied subtitle revision is unavailable.")
                        metadata["document_id"] = copied_document_revision.document_id
                metadata["forked_from_session_id"] = source_session_id
                metadata["forked_from_artifact_id"] = original.id
                prepared = self.artifacts.prepare_registration(destination)
                cloned = Artifact(
                    session_id=record.id,
                    kind=original.kind,
                    role=original.role,
                    relative_path=prepared.relative_path,
                    mime_type=original.mime_type,
                    size_bytes=prepared.size_bytes,
                    content_hash=prepared.content_hash,
                    settings_hash=original.settings_hash,
                    state=original.state,
                    metadata_json=metadata,
                )
                session.add(cloned)
                session.flush()
                for parent_id in copied_parents:
                    session.add(
                        ArtifactEdge(
                            parent_artifact_id=parent_id,
                            child_artifact_id=cloned.id,
                        )
                    )
                old_to_new_artifact_ids[original.id] = cloned.id
                copied_extra[original.id] = cloned
                cloning.remove(source_artifact_id)
                return cloned.id

            # Copy the active edit and its render ancestry, plus exact ancestors
            # of each chosen text checkpoint. No edge points to an old render.
            if active_edit is not None:
                for original_id in (
                    active_edit.source_media_artifact_id,
                    active_edit.editorial_transcript_artifact_id,
                    active_edit.timing_artifact_id,
                ):
                    if original_id:
                        copy_artifact(original_id)
                for rendered in render_sources:
                    copy_artifact(rendered.id)
                for artifact in checkpoints:
                    original_parent_ids = set(
                        session.scalars(
                            select(ArtifactEdge.parent_artifact_id).where(
                                ArtifactEdge.child_artifact_id == artifact.id
                            )
                        ).all()
                    )
                    declared_source_id = str(
                        (artifact.metadata_json or {}).get("source_artifact_id") or ""
                    )
                    if declared_source_id:
                        original_parent_ids.add(declared_source_id)
                    for original_parent_id in original_parent_ids:
                        copied_parent_id = copy_artifact(original_parent_id)
                        copied_child_id = old_to_new_artifact_ids[artifact.id]
                        if session.get(ArtifactEdge, (copied_parent_id, copied_child_id)) is None:
                            session.add(
                                ArtifactEdge(
                                    parent_artifact_id=copied_parent_id,
                                    child_artifact_id=copied_child_id,
                                )
                            )
                copied_plan = MediaEditPlan(session_id=record.id)
                session.add(copied_plan)
                session.flush()
                copied_revision = MediaEditPlanRevision(
                    plan_id=copied_plan.id,
                    revision_number=active_edit.revision_number,
                    source_media_artifact_id=old_to_new_artifact_ids[
                        active_edit.source_media_artifact_id
                    ],
                    editorial_transcript_artifact_id=old_to_new_artifact_ids[
                        active_edit.editorial_transcript_artifact_id
                    ],
                    timing_artifact_id=(
                        old_to_new_artifact_ids[active_edit.timing_artifact_id]
                        if active_edit.timing_artifact_id
                        else None
                    ),
                    duration_ms=active_edit.duration_ms,
                    instructions=active_edit.instructions,
                    keep_ranges_json=deepcopy(active_edit.keep_ranges_json or []),
                    cues_json=deepcopy(active_edit.cues_json or []),
                    evidence_json=deepcopy(active_edit.evidence_json or {}),
                    operation_json=deepcopy(active_edit.operation_json or {}),
                    reviewed=active_edit.reviewed,
                    content_hash="pending",
                    created_at=active_edit.created_at,
                )
                copied_revision.content_hash = MediaEditService._content_hash(
                    MediaEditService._revision_snapshot(copied_revision)
                )
                session.add(copied_revision)
                session.flush()
                copied_plan.active_revision_id = copied_revision.id
                for rendered in render_sources:
                    clone = copied_extra[rendered.id]
                    metadata = dict(clone.metadata_json or {})
                    metadata.update(
                        plan_id=copied_plan.id,
                        media_edit_revision_id=copied_revision.id,
                        content_hash=copied_revision.content_hash,
                    )
                    if rendered.role != "media_edit_subtitles":
                        metadata["revision_id"] = copied_revision.id
                    clone.metadata_json = metadata

            referenced_evidence_ids: set[str] = set()
            for old_revision_id in list(old_to_new_revisions):
                for segment in session.scalars(
                    select(Segment).where(Segment.revision_id == old_revision_id)
                ).all():
                    referenced_evidence_ids.update(
                        str(value)
                        for value in (segment.metadata_json or {}).get("evidence_ids") or []
                    )
            for original_id in list(old_to_new_artifact_ids):
                original = session.get(Artifact, original_id)
                if original is None:
                    continue
                passage_packet = (original.metadata_json or {}).get("logical_passages")
                if isinstance(passage_packet, dict):
                    for row in passage_packet.get("items") or []:
                        if isinstance(row, dict):
                            referenced_evidence_ids.update(
                                str(value) for value in row.get("evidence_ids") or []
                            )

            evidence_id_map: dict[str, str] = {}
            for evidence_id in sorted(referenced_evidence_ids):
                evidence = session.get(SubtitleEvidence, evidence_id)
                if evidence is None or evidence.session_id != source_session_id:
                    raise ValueError(f"Referenced subtitle evidence is unavailable: {evidence_id}")
                if evidence.status in {"queued", "running"}:
                    raise ValueError("Pending subtitle evidence cannot be copied into a fork.")
                source_artifact_id = copy_artifact(evidence.source_artifact_id)
                source_revision_id = copy_revision(evidence.source_revision_id, auxiliary=True)
                source_segment_id = (
                    old_to_new_segments.get(evidence.source_segment_id)
                    if evidence.source_segment_id
                    else None
                )
                if evidence.source_segment_id and source_segment_id is None:
                    raise ValueError(
                        f"Referenced evidence segment is unavailable: {evidence.source_segment_id}"
                    )
                source_media_id = (
                    copy_artifact(evidence.source_media_artifact_id)
                    if evidence.source_media_artifact_id
                    else None
                )
                clip_id = (
                    copy_artifact(evidence.clip_artifact_id) if evidence.clip_artifact_id else None
                )
                candidates = deepcopy(evidence.candidates_json or [])
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        continue
                    transcript_id = str(candidate.get("transcript_artifact_id") or "")
                    if transcript_id:
                        candidate["transcript_artifact_id"] = copy_artifact(transcript_id)
                cloned_evidence = SubtitleEvidence(
                    session_id=record.id,
                    source_artifact_id=source_artifact_id,
                    source_media_artifact_id=source_media_id,
                    source_revision_id=source_revision_id,
                    source_segment_id=source_segment_id,
                    cue_id=evidence.cue_id,
                    start_ms=evidence.start_ms,
                    end_ms=evidence.end_ms,
                    clip_start_ms=evidence.clip_start_ms,
                    clip_end_ms=evidence.clip_end_ms,
                    reason=evidence.reason,
                    routes_json=deepcopy(evidence.routes_json or []),
                    audio_model_ids_json=deepcopy(evidence.audio_model_ids_json or []),
                    status=evidence.status,
                    job_id=None,
                    clip_artifact_id=clip_id,
                    candidates_json=candidates,
                    resolution_json={
                        **deepcopy(evidence.resolution_json or {}),
                        "forked_from_evidence_id": evidence.id,
                    },
                    error_message=evidence.error_message,
                    created_at=evidence.created_at,
                    updated_at=evidence.updated_at,
                )
                session.add(cloned_evidence)
                session.flush()
                evidence_id_map[evidence.id] = cloned_evidence.id

            def remap_evidence_ids(value: dict[str, Any]) -> None:
                if "evidence_ids" in value:
                    value["evidence_ids"] = [
                        evidence_id_map[str(item)] for item in value["evidence_ids"]
                    ]

            for new_revision_id in old_to_new_revisions.values():
                for segment in session.scalars(
                    select(Segment).where(Segment.revision_id == new_revision_id)
                ).all():
                    metadata = deepcopy(segment.metadata_json or {})
                    remap_evidence_ids(metadata)
                    for key in ("source_cue_ids", "uncertain_source_cue_ids", "source_segment_ids"):
                        if key in metadata:
                            metadata[key] = [
                                old_to_new_segments.get(str(item), item) for item in metadata[key]
                            ]
                    segment.metadata_json = metadata

            for old_id, new_id_value in old_to_new_artifact_ids.items():
                if old_id == new_id_value:
                    continue
                cloned = session.get(Artifact, new_id_value)
                if cloned is None:
                    raise ValueError("The copied fork artifact is unavailable.")
                metadata = deepcopy(cloned.metadata_json or {})
                for key in (
                    "source_artifact_id",
                    "source_media_artifact_id",
                    "subtitle_artifact_id",
                    "editorial_transcript_artifact_id",
                    "timing_artifact_id",
                ):
                    if metadata.get(key) in old_to_new_artifact_ids:
                        metadata[key] = old_to_new_artifact_ids[metadata[key]]
                for key in ("source_revision_id", "display_revision_id"):
                    if metadata.get(key) in old_to_new_revisions:
                        metadata[key] = old_to_new_revisions[metadata[key]]
                remap_evidence_ids(metadata)
                if metadata.get("evidence_id") in evidence_id_map:
                    metadata["evidence_id"] = evidence_id_map[metadata["evidence_id"]]
                if metadata.get("reused_from_transcript_artifact_id") in old_to_new_artifact_ids:
                    metadata["reused_from_transcript_artifact_id"] = old_to_new_artifact_ids[
                        metadata["reused_from_transcript_artifact_id"]
                    ]
                if metadata.get("reused_from_evidence_id") in evidence_id_map:
                    metadata["reused_from_evidence_id"] = evidence_id_map[
                        metadata["reused_from_evidence_id"]
                    ]
                packet = metadata.get("logical_passages")
                if isinstance(packet, dict):
                    packet = deepcopy(packet)
                    for key in ("display_revision_id", "source_revision_id"):
                        if packet.get(key) in old_to_new_revisions:
                            packet[key] = old_to_new_revisions[packet[key]]
                    if packet.get("source_artifact_id") in old_to_new_artifact_ids:
                        packet["source_artifact_id"] = old_to_new_artifact_ids[
                            packet["source_artifact_id"]
                        ]
                    packet.pop("speech_source_revision_id", None)
                    for row in packet.get("items") or []:
                        if isinstance(row, dict):
                            remap_evidence_ids(row)
                            for key in ("source_cue_ids", "uncertain_source_cue_ids"):
                                if isinstance(row.get(key), list):
                                    row[key] = [
                                        old_to_new_segments.get(str(item), item)
                                        for item in row[key]
                                    ]
                    metadata["logical_passages"] = packet
                cloned.metadata_json = metadata

            self._remap_translation_source_setting(
                copied_translation_setting,
                old_to_new_artifact_ids,
            )

            if old_to_new_segments:
                old_ids = tuple(old_to_new_segments)
                for lineage in session.scalars(
                    select(SegmentLineage).where(
                        SegmentLineage.parent_segment_id.in_(old_ids),
                        SegmentLineage.child_segment_id.in_(old_ids),
                    )
                ).all():
                    session.add(
                        SegmentLineage(
                            parent_segment_id=old_to_new_segments[lineage.parent_segment_id],
                            child_segment_id=old_to_new_segments[lineage.child_segment_id],
                            relation=lineage.relation,
                            sequence=lineage.sequence,
                        )
                    )
            session.flush()
            return SessionForkResult(
                record=record,
                directory=destination_dir,
                checkpoint_artifact_id=copied_artifacts[checkpoint.role].id,
                copied_stages=tuple(copied_artifacts),
                copied_media_artifact_ids=tuple(
                    old_to_new_artifact_ids[item.id] for item in render_sources
                ),
                artifact_id_map=dict(old_to_new_artifact_ids),
            )
        except Exception:
            session.rollback()
            shutil.rmtree(destination_dir, ignore_errors=True)
            raise
