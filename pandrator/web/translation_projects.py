"""Durable, explicitly pinned language projects for corrected subtitle sessions."""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .artifacts import sha256_file
from .models import (
    Artifact,
    DispatchRun,
    Document,
    DocumentRevision,
    Job,
    MediaEditPlan,
    MediaEditPlanRevision,
    OutcomePlan,
    SessionRecord,
    SessionSetting,
    TranslationProject,
    TranslationProjectBranch,
    utcnow,
)
from .outcome_plans import derive_legacy_outcome
from .session_forks import SessionForkService
from .settings_policy import RevisionConflict

_LANGUAGE = re.compile(r"^[a-z]{2,8}(?:-[a-z0-9]{1,8})*$")


class TranslationProjectConflict(RevisionConflict):
    """A pinned snapshot or project revision no longer matches the live source."""


def canonical_language(value: str) -> str:
    normalized = str(value or "").strip().replace("_", "-").lower()
    if not 2 <= len(normalized) <= 40 or not _LANGUAGE.fullmatch(normalized):
        raise ValueError("Use a language code of 2-40 letters, digits, and hyphens.")
    if normalized == "auto":
        raise ValueError("Choose a specific target language.")
    return normalized


def _source_edit(session: Session, source_session_id: str) -> tuple[str | None, str | None]:
    plan = session.scalar(
        select(MediaEditPlan).where(MediaEditPlan.session_id == source_session_id)
    )
    if plan is not None and not plan.active_revision_id:
        raise TranslationProjectConflict("The source media edit plan has no active revision.")
    revision_id = plan.active_revision_id if plan else None
    revision = session.get(MediaEditPlanRevision, revision_id) if revision_id else None
    if revision_id and (plan is None or revision is None or revision.plan_id != plan.id):
        raise TranslationProjectConflict("The source media edit revision is unavailable.")
    return revision_id, revision.content_hash if revision else None


def _checkpoint(
    session: Session,
    paths: DataPaths,
    source_session_id: str,
    checkpoint_artifact_id: str,
    *,
    expected_hash: str | None = None,
) -> tuple[Artifact, str]:
    checkpoint = session.get(Artifact, checkpoint_artifact_id)
    if checkpoint is None or checkpoint.session_id != source_session_id:
        raise KeyError(checkpoint_artifact_id)
    if checkpoint.role != "correction" or checkpoint.state != "current":
        raise TranslationProjectConflict("The pinned correction is no longer current.")
    if not checkpoint.content_hash or (
        expected_hash is not None and checkpoint.content_hash != expected_hash
    ):
        raise TranslationProjectConflict("The pinned correction hash changed.")
    try:
        source_path = paths.managed_path(checkpoint.relative_path)
    except ValueError as error:
        raise TranslationProjectConflict("The correction path is invalid.") from error
    if not source_path.is_file() or sha256_file(source_path) != checkpoint.content_hash:
        raise TranslationProjectConflict("The correction file is missing or changed.")
    revision_id = str((checkpoint.metadata_json or {}).get("revision_id") or "")
    if revision_id:
        revision = session.get(DocumentRevision, revision_id)
        document = session.get(Document, revision.document_id) if revision else None
        if (
            revision is None
            or document is None
            or document.session_id != source_session_id
            or document.stage != "correction"
            or document.active_revision_id != revision.id
        ):
            raise TranslationProjectConflict("The correction document revision changed.")
    language = str((checkpoint.metadata_json or {}).get("language") or "").strip()
    if not language or language.lower() == "auto":
        source = session.get(SessionRecord, source_session_id)
        language = str(source.source_language or "").strip() if source else ""
    if not language or language.lower() == "auto":
        raise ValueError("Set a known source language before creating a project.")
    return checkpoint, canonical_language(language)


def _branch_payload(session: Session, branch: TranslationProjectBranch) -> dict[str, Any]:
    record = session.get(SessionRecord, branch.session_id)
    if record is None:
        raise TranslationProjectConflict("A language session is missing.")
    current = session.scalar(
        select(Artifact)
        .where(
            Artifact.session_id == branch.session_id,
            Artifact.role == "translation",
            Artifact.state == "current",
        )
        .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        .limit(1)
    )
    historical = session.scalar(
        select(Artifact.id)
        .where(Artifact.session_id == branch.session_id, Artifact.role == "translation")
        .limit(1)
    )
    active_run = session.scalar(
        select(DispatchRun)
        .where(
            DispatchRun.session_id == branch.session_id,
            DispatchRun.kind == "translation",
            DispatchRun.status.in_(("ready", "running", "finalizing")),
        )
        .order_by(DispatchRun.created_at.desc(), DispatchRun.id.desc())
        .limit(1)
    )
    active_job = session.scalar(
        select(Job.id)
        .where(
            Job.session_id == branch.session_id,
            Job.kind == "dubbing.translate",
            Job.status.in_(("queued", "running", "retrying")),
        )
        .limit(1)
    )
    translation_status = (
        "running"
        if active_run or active_job
        else "completed"
        if current
        else "stale"
        if historical
        else "ready"
    )
    return {
        "id": branch.id,
        "session_id": branch.session_id,
        "name": record.name,
        "target_language": branch.target_language,
        "source_checkpoint_artifact_id": branch.source_checkpoint_artifact_id,
        "source_content_hash": branch.source_content_hash,
        "status": record.status,
        "trashed_at": record.trashed_at.isoformat() if record.trashed_at else None,
        "translation_artifact_id": current.id if current else None,
        "translation_status": translation_status,
        "active_translation_run_id": active_run.id if active_run else None,
    }


def project_payload(session: Session, project: TranslationProject) -> dict[str, Any]:
    source = session.get(SessionRecord, project.source_session_id)
    branches = list(
        session.scalars(
            select(TranslationProjectBranch)
            .where(TranslationProjectBranch.project_id == project.id)
            .order_by(TranslationProjectBranch.created_at, TranslationProjectBranch.id)
            .limit(101)
        ).all()
    )
    if len(branches) > 100:
        raise TranslationProjectConflict("This project exceeds the supported branch limit.")
    return {
        "project": {
            "id": project.id,
            "name": project.name,
            "source_session_id": project.source_session_id,
            "source_session_name": source.name if source else None,
            "source_language": project.source_language,
            "checkpoint_artifact_id": project.checkpoint_artifact_id,
            "source_content_hash": project.source_content_hash,
            "source_media_edit_revision_id": project.source_media_edit_revision_id,
            "source_media_edit_content_hash": project.source_media_edit_content_hash,
            "revision": project.revision,
            "created_at": project.created_at.isoformat(),
            "branches": [_branch_payload(session, branch) for branch in branches],
        }
    }


def get_project(session: Session, project_id: str) -> dict[str, Any]:
    project = session.get(TranslationProject, project_id)
    if project is None:
        raise KeyError(project_id)
    return project_payload(session, project)


def get_session_project(session: Session, session_id: str) -> dict[str, Any]:
    if session.get(SessionRecord, session_id) is None:
        raise KeyError(session_id)
    project = session.scalar(
        select(TranslationProject).where(TranslationProject.source_session_id == session_id)
    )
    if project is None:
        branch = session.scalar(
            select(TranslationProjectBranch).where(
                TranslationProjectBranch.session_id == session_id
            )
        )
        project = session.get(TranslationProject, branch.project_id) if branch else None
    return project_payload(session, project) if project else {"project": None}


def create_project_in_session(
    db: Session,
    source_session_id: str,
    checkpoint_artifact_id: str,
    name: str,
    expected_revision: int,
    *,
    paths: DataPaths,
) -> dict[str, Any]:
    source = db.get(SessionRecord, source_session_id)
    if source is None or source.trashed_at is not None:
        raise KeyError(source_session_id)
    if source.revision != expected_revision:
        raise RevisionConflict("The source session changed in another client.")
    if db.scalar(
        select(TranslationProject.id).where(
            TranslationProject.source_session_id == source_session_id
        )
    ):
        raise TranslationProjectConflict("This source already has a translation project.")
    if db.scalar(
        select(TranslationProjectBranch.id).where(
            TranslationProjectBranch.session_id == source_session_id
        )
    ):
        raise TranslationProjectConflict("A language branch cannot become a project source.")
    checkpoint, language = _checkpoint(db, paths, source_session_id, checkpoint_artifact_id)
    title = str(name or "").strip() or f"{source.name} translations"
    if len(title) > 255:
        raise ValueError("A project name cannot exceed 255 characters.")
    edit_id, edit_hash = _source_edit(db, source_session_id)
    project = TranslationProject(
        name=title,
        source_session_id=source_session_id,
        checkpoint_artifact_id=checkpoint.id,
        source_content_hash=checkpoint.content_hash,
        source_language=language,
        source_media_edit_revision_id=edit_id,
        source_media_edit_content_hash=edit_hash,
    )
    db.add(project)
    db.flush()
    return project_payload(db, project)


def create_branches_in_session(
    db: Session,
    project_id: str,
    expected_revision: int,
    targets: list[dict[str, Any]],
    *,
    session_forks: SessionForkService,
    paths: DataPaths,
    created_directories: list[Path],
) -> dict[str, Any]:
    project = db.get(TranslationProject, project_id)
    if project is None:
        raise KeyError(project_id)
    if project.revision != expected_revision:
        raise RevisionConflict("The translation project changed in another client.")
    source = db.get(SessionRecord, project.source_session_id)
    if source is None or source.trashed_at is not None:
        raise TranslationProjectConflict("The source session is unavailable.")
    _pinned_checkpoint, live_language = _checkpoint(
        db,
        paths,
        source.id,
        project.checkpoint_artifact_id,
        expected_hash=project.source_content_hash,
    )
    if live_language != project.source_language:
        raise TranslationProjectConflict("The source correction language changed.")
    if _source_edit(db, source.id) != (
        project.source_media_edit_revision_id,
        project.source_media_edit_content_hash,
    ):
        raise TranslationProjectConflict(
            "The source media edit plan changed since project creation."
        )
    if not 1 <= len(targets) <= 20:
        raise ValueError("Create between 1 and 20 language branches per request.")
    existing = set(
        db.scalars(
            select(TranslationProjectBranch.target_language).where(
                TranslationProjectBranch.project_id == project.id
            )
        ).all()
    )
    normalized: list[tuple[str, str]] = []
    requested: set[str] = set()
    for target in targets:
        language = canonical_language(target["target_language"])
        if language == project.source_language or language in existing or language in requested:
            raise TranslationProjectConflict("A target repeats the source or an existing language.")
        requested.add(language)
        title = str(target.get("name") or "").strip() or f"{source.name} — {language}"
        if len(title) > 255:
            raise ValueError("A branch name cannot exceed 255 characters.")
        normalized.append((language, title))
    # The caller owns BEGIN IMMEDIATE and directory cleanup if any later fork or
    # commit fails. Each fork also removes its own partial directory on failure.
    for language, title in normalized:
        fork = session_forks.fork_in_session(
            db,
            source.id,
            project.checkpoint_artifact_id,
            name=title,
            carry_media_assets=True,
            target_language=language,
            expected_revision=source.revision,
        )
        created_directories.append(fork.directory)
        record = fork.record
        record.workflow_kind = (
            "voiceover" if source.workflow_kind in {"voiceover", "media_edit"} else "subtitles"
        )
        record.target_language = language
        stages = [
            stage
            for stage in (record.included_stages_json or [])
            if stage not in {"transcribe", "correct", "edit_media"}
        ]
        required = (
            ["translate", "generate_audio", "export"]
            if record.workflow_kind == "voiceover"
            else ["translate", "export"]
        )
        record.included_stages_json = list(dict.fromkeys([*stages, *required]))
        setting = db.get(SessionSetting, (record.id, "translation"))
        if setting is None:
            setting = SessionSetting(session_id=record.id, section="translation", value_json={})
            db.add(setting)
        setting.value_json = {
            **dict(setting.value_json or {}),
            "enabled": True,
            "source_artifact_id": fork.checkpoint_artifact_id,
            "source_language": project.source_language,
            "target_language": language,
        }
        outcome = db.get(OutcomePlan, record.id)
        if outcome is None:
            outcome = OutcomePlan(session_id=record.id, value_json=derive_legacy_outcome(record))
            db.add(outcome)
        value = deepcopy(outcome.value_json or {})
        value["workflow_kind"] = record.workflow_kind
        transformations = dict(value.get("transformations") or {})
        transformations.update(
            {
                "transcribe": False,
                "correct": False,
                "media_edit": False,
                "translate": True,
                "generate_audio": record.workflow_kind == "voiceover",
            }
        )
        value["transformations"] = transformations
        inputs = dict(value.get("inputs") or {})
        inputs["translation"] = "correction"
        inputs["generation"] = "translation"
        value["inputs"] = inputs
        deliverables = dict(value.get("deliverables") or {})
        deliverables.update(
            {
                "audiobook": False,
                "subtitles": True,
                "voiceover": record.workflow_kind == "voiceover",
                "edited_media": False,
            }
        )
        value["deliverables"] = deliverables
        export = dict(value.get("export") or {})
        export["subtitles"] = "translation"
        value["export"] = export
        outcome.value_json = value
        cloned = db.get(Artifact, fork.checkpoint_artifact_id)
        if cloned is None or cloned.content_hash != project.source_content_hash:
            raise TranslationProjectConflict(
                "The fork did not preserve the pinned correction hash."
            )
        db.add(
            TranslationProjectBranch(
                project_id=project.id,
                session_id=record.id,
                target_language=language,
                source_checkpoint_artifact_id=cloned.id,
                source_content_hash=cloned.content_hash,
            )
        )
        db.flush()
    project.revision += 1
    project.updated_at = utcnow()
    db.flush()
    return project_payload(db, project)
