"""Durable, explicitly pinned language projects for corrected subtitle sessions."""

from __future__ import annotations

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
from .multilingual_setup import SECTION, canonical_language, read_setup
from .outcome_plans import derive_legacy_outcome
from .session_forks import SessionForkService
from .settings_policy import RevisionConflict
from .workspace_settings import WorkspaceSettingsService


class TranslationProjectConflict(RevisionConflict):
    """A pinned snapshot or project revision no longer matches the live source."""


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
        "workflow_kind": record.workflow_kind,
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


def get_session_project(
    session: Session, session_id: str, *, paths: DataPaths | None = None
) -> dict[str, Any]:
    if session.get(SessionRecord, session_id) is None:
        raise KeyError(session_id)
    project = session.scalar(
        select(TranslationProject).where(TranslationProject.source_session_id == session_id)
    )
    branch = None
    if project is None:
        branch = session.scalar(
            select(TranslationProjectBranch).where(
                TranslationProjectBranch.session_id == session_id
            )
        )
        project = session.get(TranslationProject, branch.project_id) if branch else None
    setup = None if branch else read_setup(session, session_id)
    checkpoint_id = project.checkpoint_artifact_id if project and not branch else None
    state = "active" if setup is not None and project and not branch else "none"
    blocked_reason = None
    if setup is not None and state == "none":
        state = "awaiting_correction"
        if paths is not None:
            candidates = session.scalars(
                select(Artifact).where(
                    Artifact.session_id == session_id,
                    Artifact.role == "correction",
                    Artifact.state == "current",
                ).order_by(Artifact.created_at.desc(), Artifact.id.desc()).limit(50)
            )
            for candidate in candidates:
                try:
                    _, source_language = _checkpoint(session, paths, session_id, candidate.id)
                    _source_edit(session, session_id)
                except (KeyError, ValueError, RevisionConflict, OSError) as error:
                    blocked_reason = blocked_reason or str(error)
                    continue
                if source_language in setup.target_languages:
                    checkpoint_id = candidate.id
                    blocked_reason = blocked_reason or "A planned target repeats the correction language."
                    continue
                checkpoint_id = candidate.id
                state = "ready"
                blocked_reason = None
                break
            if state != "ready" and blocked_reason is not None:
                state = "blocked"
    result: dict[str, Any] = project_payload(session, project) if project else {"project": None}
    result.update({
        "setup": setup.model_dump(mode="json") if setup else None,
        "setup_state": state,
        "setup_blocked_reason": blocked_reason,
        "correction_checkpoint_artifact_id": checkpoint_id,
    })
    return result


def create_project_in_session(
    db: Session,
    source_session_id: str,
    checkpoint_artifact_id: str,
    name: str,
    expected_revision: int,
    *,
    paths: DataPaths,
    create_planned_branches: bool = False,
    session_forks: SessionForkService | None = None,
    created_directories: list[Path] | None = None,
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
    setup = read_setup(db, source_session_id) if create_planned_branches else None
    if create_planned_branches and setup is None:
        raise ValueError("Save multilingual setup before creating planned branches.")
    if setup is not None and language in setup.target_languages:
        raise ValueError("A planned target repeats the correction language.")
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
    if setup is not None:
        if session_forks is None or created_directories is None:
            raise ValueError("Planned branch creation requires a fork service and cleanup list.")
        create_branches_in_session(
            db, project.id, project.revision,
            [
                {
                    "target_language": language,
                    "carry_source_subtitle_settings": setup.carry_source_subtitle_settings,
                }
                for language in setup.target_languages
            ],
            session_forks=session_forks, paths=paths,
            created_directories=created_directories,
        )
    return get_session_project(db, source_session_id, paths=paths)


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
    setup = read_setup(db, source.id)
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
    normalized: list[tuple[str, str, bool]] = []
    requested: set[str] = set()
    for target in targets:
        language = canonical_language(target["target_language"])
        if language == project.source_language or language in existing or language in requested:
            raise TranslationProjectConflict("A target repeats the source or an existing language.")
        requested.add(language)
        title = str(target.get("name") or "").strip() or f"{source.name} — {language}"
        if len(title) > 255:
            raise ValueError("A branch name cannot exceed 255 characters.")
        carry_source_subtitle_settings = target.get(
            "carry_source_subtitle_settings", False
        )
        if type(carry_source_subtitle_settings) is not bool:
            raise ValueError("carry_source_subtitle_settings must be a boolean.")
        normalized.append((language, title, carry_source_subtitle_settings))

    source_subtitle_settings = None
    if any(carry for _, _, carry in normalized):
        # get_in_session uses the caller's SQLAlchemy session and does not open
        # another transaction, so carried settings share the branch write.
        source_subtitle_settings = WorkspaceSettingsService(
            session_forks.database
        ).get_in_session(db, source.id, "subtitles")["effective"]
    # The caller owns BEGIN IMMEDIATE and directory cleanup if any later fork or
    # commit fails. Each fork also removes its own partial directory on failure.
    for language, title, carry_source_subtitle_settings in normalized:
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
        inherited_setup = db.get(SessionSetting, (record.id, SECTION))
        if inherited_setup is not None:
            db.delete(inherited_setup)
        for copied_setting in db.scalars(
            select(SessionSetting).where(SessionSetting.session_id == record.id)
        ).all():
            if copied_setting.section == "subtitles":
                continue
            child_value = copied_setting.value_json
            if not isinstance(child_value, dict):
                continue
            child_value = dict(child_value)
            for key in (
                "subtitle_language_defaults",
                "subtitle_max_chars_per_line",
                "subtitle_max_cps",
            ):
                child_value.pop(key, None)
            copied_setting.value_json = child_value
        subtitle_setting = db.get(SessionSetting, (record.id, "subtitles"))
        if subtitle_setting is None:
            subtitle_setting = SessionSetting(
                session_id=record.id, section="subtitles", value_json={}
            )
            db.add(subtitle_setting)
        if carry_source_subtitle_settings:
            subtitle_value = deepcopy(source_subtitle_settings or {})
            for key in (
                "subtitle_language_defaults",
                "subtitle_max_chars_per_line",
                "subtitle_max_cps",
            ):
                subtitle_value.pop(key, None)
            subtitle_setting.value_json = subtitle_value
        else:
            subtitle_value = dict(subtitle_setting.value_json or {})
            subtitle_value["language_defaults"] = True
            for key in (
                "subtitle_language_defaults",
                "max_chars_per_line",
                "max_cps",
                "subtitle_max_chars_per_line",
                "subtitle_max_cps",
            ):
                subtitle_value.pop(key, None)
            subtitle_setting.value_json = subtitle_value
        record.workflow_kind = (
            "voiceover" if setup.generate_voiceover else "subtitles"
        ) if setup is not None else (
            "voiceover" if source.workflow_kind in {"voiceover", "media_edit"} else "subtitles"
        )
        record.target_language = language
        stages = [] if setup is not None else [
            stage for stage in (record.included_stages_json or [])
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
        branch_output: dict[str, Any] | None = None
        for section in ("tts", "output"):
            child_setting = db.get(SessionSetting, (record.id, section))
            if child_setting is None:
                child_setting = SessionSetting(session_id=record.id, section=section, value_json={})
                db.add(child_setting)
            child_value = dict(child_setting.value_json or {})
            child_value.pop("target_language", None)
            child_value["language"] = language
            if section == "output" and setup is not None:
                child_value["subtitle_selection"] = (
                    "dual" if setup.keep_source_subtitles else "translation"
                )
                if setup.generate_voiceover:
                    context = WorkspaceSettingsService._output_context(db, record)
                    child_value["export_mode"] = "media"
                    child_value["audio_mode"] = "dubbing_only"
                    if source.workflow_kind == "subtitles" or child_value.get("subtitle_mode") not in {
                        "none", "soft", "burned",
                    }:
                        child_value["subtitle_mode"] = (
                            "soft" if context["has_source_video"] else "none"
                        )
                else:
                    child_value.update({
                        "export_mode": "subtitles",
                        "audio_mode": "preserve",
                        "subtitle_mode": "none",
                    })
            child_setting.value_json = child_value
            if section == "output":
                branch_output = child_value
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
        export["subtitles"] = (
            "dual" if setup and setup.keep_source_subtitles else "translation"
        )
        export["audio"] = "generated" if record.workflow_kind == "voiceover" else "preserve"
        export["target_language"] = language
        if setup is not None and branch_output is not None:
            export["mode"] = branch_output["export_mode"]
            export["subtitle_mode"] = branch_output["subtitle_mode"]
            export["audio_mode"] = branch_output["audio_mode"]
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
