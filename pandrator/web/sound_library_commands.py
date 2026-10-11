"""Transactional sound-item updates and candidate lifecycle commands."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .models import Artifact, Job, SoundItem, SoundVariant, utcnow
from .settings_policy import RevisionConflict
from .sound_library_queries import candidate_summary, get_sound
from .sound_project_models import ProjectSoundCue
from .sound_schemas import SoundSave, SoundUpdate

_ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "cancel_requested"})


def _job_status(session: Session, variant: SoundVariant) -> str | None:
    if variant.job_id is None:
        return None
    job = session.get(Job, variant.job_id)
    return job.status if job is not None else None


def _item_payload(session: Session, item_id: str) -> dict:
    item = get_sound(session, item_id, limit=1)
    item.pop("variants", None)
    return item


def _require_ready_artifact(
    session: Session,
    paths: DataPaths,
    candidate: SoundVariant,
) -> Artifact:
    if candidate.state != "ready" or not candidate.artifact_id:
        raise ValueError("Only a ready sound candidate can be saved.")
    artifact = session.get(Artifact, candidate.artifact_id)
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


def _validate_expected_revision(item: SoundItem, expected_revision: int) -> None:
    if item.revision != expected_revision:
        raise RevisionConflict(
            f"The sound item changed from revision {expected_revision} to {item.revision}. Refresh and try again."
        )


def save_in_session(
    session: Session,
    paths: DataPaths,
    payload: dict,
) -> dict:
    request = SoundSave.model_validate(payload)
    fields = request.model_dump(exclude_unset=True)
    candidate = session.get(SoundVariant, request.candidate_id)
    if candidate is None:
        raise KeyError(request.candidate_id)

    if candidate.item_id is not None:
        item = session.get(SoundItem, candidate.item_id)
        if item is None:
            raise ValueError("The candidate points to a missing sound item.")
        return {
            "item_id": item.id,
            "item": _item_payload(session, item.id),
            "candidate": candidate_summary(candidate, _job_status(session, candidate)),
            "reused": True,
        }

    _require_ready_artifact(session, paths, candidate)
    if request.item_id is None:
        if request.name is None:
            raise ValueError("name is required when creating a sound item.")
        if request.expected_revision is not None:
            raise ValueError("expected_revision is only valid when item_id is provided.")
        item = SoundItem(
            name=request.name,
            tags_json=list(request.tags or []),
            preferred_variant_id=candidate.id,
        )
        session.add(item)
        session.flush()
        candidate.item_id = item.id
    else:
        if "name" in fields or "tags" in fields:
            raise ValueError("Use update_sound to change an existing item's name or tags.")
        item = session.get(SoundItem, request.item_id)
        if item is None:
            raise KeyError(request.item_id)
        assert request.expected_revision is not None
        _validate_expected_revision(item, request.expected_revision)
        candidate.item_id = item.id
        if item.preferred_variant_id is None:
            item.preferred_variant_id = candidate.id
        item.revision += 1
        item.updated_at = utcnow()

    session.flush()
    return {
        "item_id": item.id,
        "item": _item_payload(session, item.id),
        "candidate": candidate_summary(candidate, _job_status(session, candidate)),
        "reused": False,
    }


def update_in_session(
    session: Session,
    item_id: str,
    payload: dict,
) -> dict:
    request = SoundUpdate.model_validate(payload)
    fields = request.model_dump(exclude_unset=True)
    item = session.get(SoundItem, item_id)
    if item is None:
        raise KeyError(item_id)
    _validate_expected_revision(item, request.expected_revision)

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
        assert request.preferred_variant_id is not None
        variant = session.get(SoundVariant, request.preferred_variant_id)
        if (
            variant is None
            or variant.item_id != item.id
            or variant.state != "ready"
        ):
            raise ValueError("preferred_variant_id must name a ready variant that belongs to this item.")
        if item.preferred_variant_id != variant.id:
            item.preferred_variant_id = variant.id
            changed = True
    if changed:
        item.revision += 1
        item.updated_at = utcnow()
    session.flush()
    return _item_payload(session, item.id)


def discard_in_session(session: Session, candidate_id: str) -> dict:
    candidate = session.get(SoundVariant, candidate_id)
    if candidate is None:
        raise KeyError(candidate_id)
    if candidate.item_id is not None:
        raise ValueError("Saved candidates stay in the library; archive the sound item instead.")
    referenced = session.scalar(
        select(ProjectSoundCue.id)
        .where(ProjectSoundCue.variant_id == candidate.id)
        .limit(1)
    )
    if referenced is not None:
        raise ValueError(
            "This candidate is pinned by a project sound cue. Remove or replace that cue first, or keep the candidate."
        )
    job = session.get(Job, candidate.job_id) if candidate.job_id else None
    if job is not None and job.status in _ACTIVE_JOB_STATUSES:
        raise ValueError("A candidate cannot be discarded while its generation job is active.")
    if candidate.state != "discarded":
        candidate.state = "discarded"
        session.flush()
    return candidate_summary(candidate, job.status if job else None)
