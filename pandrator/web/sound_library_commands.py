"""Transactional sound-item updates and candidate lifecycle commands."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .access.contracts import ProjectAction, WorkspaceNotFound
from .access.sound_library_policy import require_sound_library_workspace
from .accounts.execution_actor import UserActor, current_execution_actor
from .models import Artifact, Job, SoundItem, SoundVariant, utcnow
from .settings_policy import RevisionConflict
from .sound_identity import (
    sound_item_workspace,
    sound_variant_workspace,
)
from .sound_library_queries import candidate_summary, get_sound
from .sound_project_models import ProjectSoundCue
from .sound_schemas import SoundSave, SoundUpdate

_ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "cancel_requested"})


def _job_status(
    session: Session,
    variant: SoundVariant,
    *,
    workspace_id: str,
) -> str | None:
    if variant.job_id is None:
        return None
    return session.scalar(
        select(Job.status).where(
            Job.id == variant.job_id,
            Job.workspace_id == workspace_id,
        )
    )


def _item_payload(session: Session, item_id: str, *, workspace_id: str) -> dict:
    item = get_sound(session, item_id, limit=1, workspace_id=workspace_id)
    item.pop("variants", None)
    return item


def _require_ready_artifact(
    session: Session,
    paths: DataPaths,
    candidate: SoundVariant,
    *,
    workspace_id: str,
) -> Artifact:
    if candidate.state != "ready" or not candidate.artifact_id:
        raise ValueError("Only a ready sound candidate can be saved.")
    artifact = session.scalar(
        select(Artifact).where(
            Artifact.id == candidate.artifact_id,
            Artifact.workspace_id == workspace_id,
        )
    )
    if (
        artifact is None
        or artifact.kind != "audio"
        or artifact.role != "sound.original"
        or artifact.session_id is not None
        or artifact.state != "current"
        or not artifact.content_hash
        or not artifact.size_bytes
    ):
        raise ValueError("The candidate has no valid managed sound artifact.")
    try:
        path = paths.managed_path(artifact.relative_path)
    except (OSError, ValueError) as error:
        raise ValueError("The candidate sound artifact path is invalid.") from error
    if not path.is_file():
        raise ValueError("The candidate sound artifact is missing from managed storage.")
    return artifact


def _require_candidate_workspace(
    session: Session,
    candidate: SoundVariant,
    workspace_id: str,
) -> None:
    if candidate.item_id is not None and session.scalar(
        select(SoundItem.id).where(
            SoundItem.id == candidate.item_id,
            SoundItem.workspace_id == workspace_id,
        )
    ) is None:
        raise ValueError("The candidate references a missing or foreign sound item.")
    if candidate.artifact_id is not None and session.scalar(
        select(Artifact.id).where(
            Artifact.id == candidate.artifact_id,
            Artifact.workspace_id == workspace_id,
        )
    ) is None:
        raise ValueError("The candidate references a missing or foreign sound artifact.")
    sound_variant_workspace(session, candidate, workspace_id=workspace_id)


def _require_item_workspace(
    session: Session,
    item: SoundItem,
    workspace_id: str,
) -> None:
    if item.preferred_variant_id is not None and session.scalar(
        select(SoundVariant.id).where(
            SoundVariant.id == item.preferred_variant_id,
            SoundVariant.item_id == item.id,
            SoundVariant.workspace_id == workspace_id,
        )
    ) is None:
        raise ValueError("The sound item references a missing or foreign preferred variant.")
    sound_item_workspace(session, item, workspace_id=workspace_id)


def _validate_expected_revision(item: SoundItem, expected_revision: int) -> None:
    if item.revision != expected_revision:
        raise RevisionConflict(
            f"The sound item changed from revision {expected_revision} to {item.revision}. Refresh and try again."
        )


def save_in_session(
    session: Session,
    paths: DataPaths,
    payload: dict,
    *,
    workspace_id: str | None = None,
) -> dict:
    workspace = require_sound_library_workspace(
        session, ProjectAction.EDIT, workspace_id=workspace_id
    )
    request = SoundSave.model_validate(payload)
    fields = request.model_dump(exclude_unset=True)
    candidate = session.scalar(
        select(SoundVariant).where(
            SoundVariant.id == request.candidate_id,
            SoundVariant.workspace_id == workspace.id,
        )
    )
    if candidate is None:
        raise WorkspaceNotFound()
    _require_candidate_workspace(session, candidate, workspace.id)

    if candidate.item_id is not None:
        item = session.scalar(
            select(SoundItem).where(
                SoundItem.id == candidate.item_id,
                SoundItem.workspace_id == workspace.id,
            )
        )
        if item is None:
            raise ValueError("The candidate points to a missing sound item.")
        _require_item_workspace(session, item, workspace.id)
        return {
            "item_id": item.id,
            "item": _item_payload(session, item.id, workspace_id=workspace.id),
            "candidate": candidate_summary(
                candidate,
                _job_status(session, candidate, workspace_id=workspace.id),
            ),
            "reused": True,
        }

    _require_ready_artifact(session, paths, candidate, workspace_id=workspace.id)
    if request.item_id is None:
        if request.name is None:
            raise ValueError("name is required when creating a sound item.")
        if request.expected_revision is not None:
            raise ValueError("expected_revision is only valid when item_id is provided.")
        item = SoundItem(
            name=request.name,
            tags_json=list(request.tags or []),
            preferred_variant_id=None,
            workspace_id=workspace.id,
        )
        session.add(item)
        session.flush([item])
    else:
        if "name" in fields or "tags" in fields:
            raise ValueError("Use update_sound to change an existing item's name or tags.")
        item = session.scalar(
            select(SoundItem).where(
                SoundItem.id == request.item_id,
                SoundItem.workspace_id == workspace.id,
            )
        )
        if item is None:
            raise WorkspaceNotFound()
        _require_item_workspace(session, item, workspace.id)
        assert request.expected_revision is not None
        _validate_expected_revision(item, request.expected_revision)

    candidate.item_id = item.id
    session.flush([candidate])
    if item.preferred_variant_id is None:
        item.preferred_variant_id = candidate.id
    if request.item_id is not None:
        item.revision += 1
        item.updated_at = utcnow()

    session.flush()
    return {
        "item_id": item.id,
        "item": _item_payload(session, item.id, workspace_id=workspace.id),
        "candidate": candidate_summary(
            candidate,
            _job_status(session, candidate, workspace_id=workspace.id),
        ),
        "reused": False,
    }


def update_in_session(
    session: Session,
    item_id: str,
    payload: dict,
    *,
    workspace_id: str | None = None,
) -> dict:
    workspace = require_sound_library_workspace(
        session, ProjectAction.EDIT, workspace_id=workspace_id
    )
    request = SoundUpdate.model_validate(payload)
    fields = request.model_dump(exclude_unset=True)
    item = session.scalar(
        select(SoundItem).where(
            SoundItem.id == item_id,
            SoundItem.workspace_id == workspace.id,
        )
    )
    if item is None:
        raise WorkspaceNotFound()
    _require_item_workspace(session, item, workspace.id)
    _validate_expected_revision(item, request.expected_revision)

    preferred_variant = None
    if "preferred_variant_id" in fields:
        assert request.preferred_variant_id is not None
        preferred_variant = session.scalar(
            select(SoundVariant).where(
                SoundVariant.id == request.preferred_variant_id,
                SoundVariant.workspace_id == workspace.id,
            )
        )
        if preferred_variant is None:
            if type(current_execution_actor.get()) is UserActor:
                raise WorkspaceNotFound()
            raise ValueError(
                "preferred_variant_id must name a ready variant that belongs to this item."
            )
        if preferred_variant.item_id != item.id or preferred_variant.state != "ready":
            raise ValueError(
                "preferred_variant_id must name a ready variant that belongs to this item."
            )
        _require_candidate_workspace(session, preferred_variant, workspace.id)

    changed = False
    if "name" in fields and request.name is not None and item.name != request.name:
        item.name = request.name
        changed = True
    if "tags" in fields and request.tags is not None and item.tags_json != request.tags:
        item.tags_json = list(request.tags)
        changed = True
    if "archived" in fields and request.archived is not None and item.archived != request.archived:
        item.archived = request.archived
        changed = True
    if "preferred_variant_id" in fields:
        assert preferred_variant is not None
        if item.preferred_variant_id != preferred_variant.id:
            item.preferred_variant_id = preferred_variant.id
            changed = True
    if changed:
        item.revision += 1
        item.updated_at = utcnow()
    session.flush()
    return _item_payload(session, item.id, workspace_id=workspace.id)


def discard_in_session(
    session: Session,
    candidate_id: str,
    *,
    workspace_id: str | None = None,
) -> dict:
    workspace = require_sound_library_workspace(
        session, ProjectAction.EDIT, workspace_id=workspace_id
    )
    candidate = session.scalar(
        select(SoundVariant).where(
            SoundVariant.id == candidate_id,
            SoundVariant.workspace_id == workspace.id,
        )
    )
    if candidate is None:
        raise WorkspaceNotFound()
    _require_candidate_workspace(session, candidate, workspace.id)
    if candidate.item_id is not None:
        raise ValueError("Saved candidates stay in the library; archive the sound item instead.")
    referenced = session.scalar(
        select(ProjectSoundCue.id).where(ProjectSoundCue.variant_id == candidate.id).limit(1)
    )
    if referenced is not None:
        raise ValueError(
            "This candidate is pinned by a project sound cue. Remove or replace that cue first, or keep the candidate."
        )
    job = None
    if candidate.job_id is not None:
        job = session.scalar(
            select(Job).where(
                Job.id == candidate.job_id,
                Job.workspace_id == workspace.id,
            )
        )
        if job is None:
            raise ValueError("The candidate references a missing or foreign sound job.")
    if job is not None and job.status in _ACTIVE_JOB_STATUSES:
        raise ValueError("A candidate cannot be discarded while its generation job is active.")
    if candidate.state != "discarded":
        candidate.state = "discarded"
        session.flush()
    return candidate_summary(candidate, job.status if job else None)
