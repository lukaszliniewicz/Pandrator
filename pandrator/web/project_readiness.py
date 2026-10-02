"""Read-only projections of pinned projects and independent language sessions."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from pandrator.logic.language_capabilities import language_decision
from pandrator.logic.tts_language_preflight import (
    resolve_tts_language_support,
    validate_tts_language,
)

from .models import (
    Artifact,
    ArtifactEdge,
    DispatchRun,
    Document,
    DocumentRevision,
    GenerationPlanRevision,
    GenerationRun,
    Job,
    MediaEditPlan,
    MediaEditPlanRevision,
    SessionRecord,
)
from .multilingual_setup import canonical_language
from .output_settings_snapshot import build_output_settings_snapshot
from .settings_policy import stable_hash
from .speech_plan_workspace import speech_plan_status
from .translation_projects import _checkpoint, _source_edit
from .voice_setup import get_voice_setup

_ACTIVE_JOBS = frozenset({"queued", "running", "retrying"})
_ACTIVE_DISPATCH = frozenset({"ready", "running", "finalizing"})
_TTS_FIELDS = (
    "service",
    "model",
    "voice",
    "language",
    "casting_enabled",
    "voice_mode_version",
    "silero_model",
    "elevenlabs_model",
)


def _service(services: Any, name: str) -> Any:
    return services[name] if isinstance(services, Mapping) else getattr(services, name)


def _identity(artifact: Artifact | None, revision: DocumentRevision | None = None) -> dict:
    return {
        "artifact_id": artifact.id if artifact else None,
        "revision_id": revision.id
        if revision
        else (artifact.metadata_json or {}).get("revision_id")
        if artifact
        else None,
        "content_hash": artifact.content_hash if artifact else None,
        "created_at": artifact.created_at.isoformat() if artifact else None,
        "revision_created_at": revision.created_at.isoformat() if revision else None,
        "state": artifact.state if artifact else None,
    }


def _settings_identity(snapshot: dict) -> dict:
    return {
        "revision": snapshot["revision"],
        "global_revision": snapshot["global_revision"],
        "settings_hash": stable_hash(snapshot["effective"]),
    }


def _settings_payload(snapshots: dict, resolved: dict, origin: dict | None = None) -> dict:
    result = {}
    for section in ("subtitles", "tts"):
        snapshot = snapshots[section]
        current = _settings_identity(snapshot)
        initial = (origin or {}).get("initial_branch_settings", {}).get(section)
        mode = (
            "unknown_historical"
            if not initial
            else "custom_override"
            if current != initial
            else (
                (origin or {}).get("subtitles_mode") if section == "subtitles" else "copied_source"
            )
        )
        fields = _TTS_FIELDS if section == "tts" else tuple(snapshot["builtin"])
        effective = resolved.get(section, snapshot["effective"])
        result[section] = {
            **current,
            "origin": mode,
            "effective": {key: effective[key] for key in fields if key in effective},
            "provenance": {
                layer: {key: snapshot[layer][key] for key in fields if key in snapshot[layer]}
                for layer in ("builtin", "global", "session_context", "override")
            },
        }
        if section == "subtitles":
            for key in (
                "subtitle_profiles",
                "subtitle_settings_provenance",
                "subtitle_profile_scope",
            ):
                result[section][key] = deepcopy(snapshot.get(key))
    result["creation_origin"] = deepcopy(origin)
    return result


def _state(status: str, **fields: Any) -> dict:
    return {"status": status, "reasons": [], **fields}


def _stage_state(stage: dict | None) -> dict:
    stage = stage or {}
    status = stage.get("status", "missing")
    mapped = {"failed": "blocked", "unavailable": "blocked"}.get(status, status)
    if mapped not in {"missing", "ready", "running", "completed", "blocked", "stale"}:
        mapped = "missing"
    reason = stage.get("stale_reason") or stage.get("detail")
    return _state(
        mapped,
        reasons=[reason] if reason and mapped in {"blocked", "stale"} else [],
        active_job_id=stage.get("job_id"),
        progress=stage.get("progress"),
        progress_detail=stage.get("detail"),
        artifact_id=stage.get("selected_artifact_id"),
    )


def _job_fields(job: Job | None) -> dict:
    return {
        "job_id": job.id if job else None,
        "active_job_id": job.id if job and job.status in _ACTIVE_JOBS else None,
        "job_status": job.status if job else None,
        "progress": job.progress if job else None,
        "progress_detail": job.progress_detail if job else None,
        "failure": {"code": job.error_code, "message": job.error_message}
        if job and (job.error_code or job.error_message)
        else None,
    }


def _source_status(db, services, project: dict, artifacts: list[Artifact], revisions: dict) -> dict:
    source_id = project["source_session_id"]
    pinned = next((a for a in artifacts if a.id == project["checkpoint_artifact_id"]), None)
    current = next(
        (
            a
            for a in artifacts
            if a.session_id == source_id and a.role == "correction" and a.state == "current"
        ),
        None,
    )
    pinned_revision = (
        revisions.get((pinned.metadata_json or {}).get("revision_id")) if pinned else None
    )
    current_revision = (
        revisions.get((current.metadata_json or {}).get("revision_id")) if current else None
    )
    document = db.scalar(
        select(Document).where(Document.session_id == source_id, Document.stage == "correction")
    )
    plan = db.scalar(select(MediaEditPlan).where(MediaEditPlan.session_id == source_id))
    edit = (
        db.get(MediaEditPlanRevision, plan.active_revision_id)
        if plan and plan.active_revision_id
        else None
    )
    reasons = []
    try:
        _, language = _checkpoint(
            db,
            _service(services, "paths"),
            source_id,
            project["checkpoint_artifact_id"],
            expected_hash=project["source_content_hash"],
        )
        if language != project["source_language"]:
            reasons.append("The source correction language changed.")
    except (KeyError, ValueError, OSError) as error:
        reasons.append(str(error))
    if current is None or current.id != project["checkpoint_artifact_id"]:
        reasons.append("The pinned correction is no longer current.")
    if pinned_revision and document and document.active_revision_id != pinned_revision.id:
        reasons.append("The correction document revision changed.")
    try:
        live_edit = _source_edit(db, source_id)
        if live_edit != (
            project["source_media_edit_revision_id"],
            project["source_media_edit_content_hash"],
        ):
            reasons.append("The source media edit plan changed since project creation.")
    except ValueError as error:
        reasons.append(str(error))
    source = db.get(SessionRecord, source_id)
    if source is None or source.trashed_at:
        reasons.append("The source session is unavailable.")
    return {
        "pinned_checkpoint": {
            **_identity(pinned, pinned_revision),
            "content_hash": project["source_content_hash"],
        },
        "current_correction": _identity(current, current_revision),
        "current_correction_revision_id": document.active_revision_id if document else None,
        "pinned_media_edit": {
            "revision_id": project["source_media_edit_revision_id"],
            "content_hash": project["source_media_edit_content_hash"],
        },
        "current_media_edit": {
            "revision_id": edit.id if edit else None,
            "content_hash": edit.content_hash if edit else None,
            "created_at": edit.created_at.isoformat() if edit else None,
        },
        "source_changed": bool(reasons),
        "reasons": list(dict.fromkeys(reasons)),
    }


def _export_state(
    services,
    session_id: str,
    stage: dict,
    artifacts: list[Artifact],
    jobs: list[Job],
    parents: dict[str, set[str]],
    *,
    subtitles: bool,
    generation_run_id: str | None = None,
) -> dict:
    selected = [
        a
        for a in artifacts
        if a.state == "current"
        and (
            a.role.startswith("export_subtitle_")
            if subtitles
            else a.role == "export" or a.role.startswith("export_")
        )
    ]
    result = _stage_state(stage)
    result["artifact_ids"] = [a.id for a in selected]
    # Resolving captures the same inputs as enqueue, without starting a job.
    settings = (
        {
            "export_mode": "subtitles",
            "subtitle_mode": "translation",
            "subtitle_format": "srt",
            "subtitle_selection": "translation",
        }
        if subtitles
        else {"generation_run_id": generation_run_id}
        if generation_run_id
        else None
    )
    result["generation_run_id"] = None if subtitles else generation_run_id
    try:
        captured = _service(services, "workflows").resolve_stage(session_id, "export", settings)
        expected = build_output_settings_snapshot(
            captured.payload.get("settings"), captured.payload.get("resolved_settings_snapshot")
        )
        matching = [
            a
            for a in selected
            if (a.metadata_json or {}).get("output_settings", {}).get("settings_hash")
            == expected["settings_hash"]
            and captured.source_artifact_id in parents.get(a.id, set())
        ]
        result["matching_artifact_ids"] = [a.id for a in matching]
        result["artifact_settings_unverified"] = any(
            not (a.metadata_json or {}).get("output_settings") for a in selected
        )
        result["source_artifact_id"] = captured.source_artifact_id
        result.update(
            status="completed" if matching else "stale" if selected else "ready",
            reasons=["Existing exports use different inputs or output settings. Export again to create the current selection."] if selected and not matching else [],
        )
    except (ValueError, KeyError) as error:
        result.update(status="stale" if selected else "blocked", reasons=[str(error)])
    relevant_jobs = [
        j
        for j in jobs
        if j.kind == "export.create"
        and (
            not subtitles
            or (
                (j.payload_json or {})
                .get("resolved_settings_snapshot", {})
                .get("output", {})
                .get("export_mode")
                == "subtitles"
                or (j.payload_json or {}).get("settings", {}).get("export_mode") == "subtitles"
            )
        )
    ]
    job = next(
        (j for j in relevant_jobs if j.status in _ACTIVE_JOBS),
        relevant_jobs[0] if relevant_jobs else None,
    )
    result.update(_job_fields(job))
    if job and job.status in _ACTIVE_JOBS:
        result.update(status="running", reasons=[])
    elif job and job.status in {"failed", "interrupted"} and result["status"] != "completed":
        result.update(
            status="blocked",
            reasons=[job.error_message] if job.error_message else result["reasons"],
        )
    return result


def enrich_project_payload(services: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Add current readiness without mutating the payload or any stored records."""
    result = deepcopy(payload)
    project = result.get("project")
    if not project:
        return result
    branches = project.get("branches", [])
    if len(branches) > 100:
        raise ValueError("This project exceeds the supported branch limit.")
    ids = [project["source_session_id"], *(b["session_id"] for b in branches)]
    database = _service(services, "database")
    settings_service = _service(services, "workspace_settings")
    with database.session() as db:
        artifacts = list(
            db.scalars(
                select(Artifact)
                .where(Artifact.session_id.in_(ids))
                .order_by(Artifact.created_at.desc(), Artifact.id.desc())
            )
        )
        parents: dict[str, set[str]] = {}
        for edge in db.scalars(
            select(ArtifactEdge).where(
                ArtifactEdge.child_artifact_id.in_(
                    [a.id for a in artifacts if a.role == "export" or a.role.startswith("export_")]
                )
            )
        ):
            parents.setdefault(edge.child_artifact_id, set()).add(edge.parent_artifact_id)
        revision_ids = [(a.metadata_json or {}).get("revision_id") for a in artifacts]
        revisions = {
            r.id: r
            for r in db.scalars(
                select(DocumentRevision).where(
                    DocumentRevision.id.in_([r for r in revision_ids if r])
                )
            )
        }
        documents = {
            d.id: d for d in db.scalars(select(Document).where(Document.session_id.in_(ids)))
        }
        jobs = list(
            db.scalars(
                select(Job)
                .where(Job.session_id.in_(ids))
                .order_by(Job.created_at.desc(), Job.id.desc())
            )
        )
        runs = list(
            db.scalars(
                select(GenerationRun)
                .where(GenerationRun.session_id.in_(ids))
                .order_by(GenerationRun.sequence_number.desc())
            )
        )
        dispatches = list(
            db.scalars(
                select(DispatchRun)
                .where(DispatchRun.session_id.in_(ids), DispatchRun.kind == "translation")
                .order_by(DispatchRun.created_at.desc(), DispatchRun.id.desc())
            )
        )
        project["source_status"] = _source_status(db, services, project, artifacts, revisions)
        source_snapshots = {
            section: settings_service.get_in_session(db, project["source_session_id"], section)
            for section in ("subtitles", "tts")
        }
        source_resolved, _ = settings_service.resolve_in_session(
            db, project["source_session_id"], ["tts", "subtitles"]
        )
        project["source_settings"] = _settings_payload(source_snapshots, source_resolved)
        for branch in branches:
            sid = branch["session_id"]
            record = db.get(SessionRecord, sid)
            if record is None or record.trashed_at is not None:
                branch["readiness"] = {
                    key: _state(
                        "not_requested"
                        if branch["workflow_kind"] == "subtitles"
                        and key in {"speech_plan", "voice", "generation"}
                        else "blocked",
                        session_unavailable=True,
                    )
                    for key in ("translation", "review", "speech_plan", "voice", "generation")
                }
                branch["readiness"]["exports"] = {
                    key: _state("blocked", session_unavailable=True, artifact_ids=[])
                    for key in ("configured", "subtitles")
                }
                continue
            branch_artifacts = [a for a in artifacts if a.session_id == sid]
            snapshots = {
                section: settings_service.get_in_session(db, sid, section)
                for section in ("subtitles", "tts", "translation")
            }
            resolved, _ = settings_service.resolve_in_session(db, sid, ["tts", "subtitles"])
            checkpoint = next(
                (a for a in branch_artifacts if a.id == branch["source_checkpoint_artifact_id"]),
                None,
            )
            origin = (
                (checkpoint.metadata_json or {}).get("translation_project_settings_origin")
                if checkpoint
                else None
            )
            if not isinstance(origin, dict):
                origin = None
            branch["settings"] = _settings_payload(snapshots, resolved, origin)
            try:
                support = resolve_tts_language_support(resolved["tts"], _service_config_cache={})
            except ValueError:
                support = None
            branch["settings"]["tts"]["provider_id"] = (
                support.get("provider_id") if support else None
            )
            branch["settings"]["tts"]["model_id"] = (
                support.get("model_id") if support else resolved["tts"].get("model")
            )
            target = str(snapshots["translation"]["effective"].get("target_language") or "")
            matches = canonical_language(target) == canonical_language(branch["target_language"])
            branch["target_language_matches_settings"] = matches
            branch["effective_target_language"] = target
            workflow = _service(services, "workflows").snapshot(sid)
            stages = {s["key"]: s for s in workflow["stages"]}
            translation = _stage_state(stages.get("translate"))
            translated = next(
                (a for a in branch_artifacts if a.id == translation.get("artifact_id")), None
            )
            if translated is None:
                translated = next(
                    (
                        a
                        for a in branch_artifacts
                        if a.role == "translation" and a.state == "current"
                    ),
                    None,
                )
            translation["artifact_id"] = translated.id if translated else None
            translation_job = next(
                (j for j in jobs if j.session_id == sid and j.kind == "dubbing.translate"), None
            )
            dispatch = next((r for r in dispatches if r.session_id == sid), None)
            translation.update(_job_fields(translation_job))
            translation["dispatch_run_id"] = dispatch.id if dispatch else None
            translation["active_dispatch_run_id"] = (
                dispatch.id if dispatch and dispatch.status in _ACTIVE_DISPATCH else None
            )
            translation["dispatch_status"] = dispatch.status if dispatch else None
            if dispatch and dispatch.status in _ACTIVE_DISPATCH:
                translation.update(
                    status="running",
                    progress=dispatch.completed_batch_count / dispatch.batch_count
                    if dispatch.batch_count
                    else 0,
                    failure={"code": dispatch.error_code, "message": dispatch.error_message}
                    if dispatch.error_code or dispatch.error_message
                    else None,
                )
            elif dispatch and dispatch.status == "failed":
                translation.update(
                    status="blocked",
                    reasons=[dispatch.error_message] if dispatch.error_message else [],
                    failure={"code": dispatch.error_code, "message": dispatch.error_message},
                )
            revision = (
                revisions.get((translated.metadata_json or {}).get("revision_id"))
                if translated
                else None
            )
            document = documents.get(revision.document_id) if revision else None
            current_review = bool(
                translated
                and revision
                and document
                and document.active_revision_id == revision.id
                and document.session_id == sid
                and document.stage == "translation"
                and revision.content_hash == translated.content_hash
            )
            reviewed = bool(current_review and revision and revision.reviewed)
            review = _state(
                "missing"
                if not translated
                else "stale"
                if translated.state != "current" or revision and not current_review
                else "completed"
                if reviewed
                else "needs_review",
                artifact_id=translated.id if translated else None,
                revision_id=revision.id if revision else None,
                content_hash=revision.content_hash
                if revision
                else translated.content_hash
                if translated
                else None,
                reviewed=reviewed,
            )
            wants_voice = branch["workflow_kind"] != "subtitles"
            speech = _state("not_requested")
            voice = _state("not_requested")
            generation = _state("not_requested")
            run = None
            if wants_voice:
                speech_services = (
                    SimpleNamespace(
                        database=database,
                        workflows=_service(services, "workflows"),
                        workspace_settings=settings_service,
                    )
                    if isinstance(services, Mapping)
                    else services
                )
                plan = speech_plan_status(speech_services, sid, summary=True)
                selected = next(
                    (p for p in plan["items"] if p["id"] == plan["selected_revision_id"]), None
                )
                selected_revision = (
                    db.get(GenerationPlanRevision, plan["selected_revision_id"])
                    if plan["selected_revision_id"]
                    else None
                )
                plan_source = selected_revision.settings_json if selected_revision else {}
                current_input = next(
                    (
                        a
                        for a in branch_artifacts
                        if a.id == (plan.get("current_input") or {}).get("artifact_id")
                    ),
                    None,
                )
                stale_plan = bool(
                    selected
                    and (
                        not selected.get("compatible", True)
                        or plan_source.get("_source_artifact_id")
                        and (
                            current_input is None
                            or plan_source["_source_artifact_id"] != current_input.id
                            or plan_source.get("_source_content_hash", current_input.content_hash)
                            != current_input.content_hash
                        )
                    )
                )
                speech = _state(
                    "missing"
                    if not selected
                    else "stale"
                    if stale_plan
                    else "blocked"
                    if plan["warning"]
                    else "completed"
                    if selected["reviewed"]
                    else "needs_review",
                    revision_id=plan["selected_revision_id"],
                    content_signature=plan["content_signature"],
                    reviewed=bool(selected and selected["reviewed"]),
                    reviewed_at=selected.get("reviewed_at") if selected else None,
                    reasons=[plan["warning"]] if plan["warning"] else [],
                    can_generate=plan["can_generate"],
                )
                voice_setup = get_voice_setup(services, db, sid)
                voice = _state(
                    "ready"
                    if resolved["tts"].get("voice") or voice_setup["casting_enabled"]
                    else "missing",
                    mode=voice_setup["mode"],
                    configuration_revision=voice_setup["configuration_revision"],
                    validation_scope="configuration",
                )
                try:
                    language = validate_tts_language(resolved["tts"], _service_config_cache={})
                    voice["language_support"] = language
                    voice["coverage_unverified"] = language["decision"] == "unverified"
                except ValueError as error:
                    voice.update(status="blocked", reasons=[str(error)], coverage_unverified=False)
                    if support:
                        voice["language_support"] = {
                            "language": resolved["tts"].get("language"),
                            "decision": language_decision(support, resolved["tts"].get("language")),
                            "language_support": support,
                        }
                generation = _stage_state(stages.get("generate_audio"))
                run = next(
                    (
                        r
                        for r in runs
                        if r.session_id == sid and r.job_id == generation.get("active_job_id")
                    ),
                    None,
                ) or next((r for r in runs if r.session_id == sid), None)
                job = next(
                    (j for j in jobs if j.id == generation.get("active_job_id")), None
                ) or next((j for j in jobs if run and j.id == run.job_id), None)
                generation.update(
                    _job_fields(job),
                    generation_run_id=run.id if run else None,
                    plan_revision_id=run.plan_revision_id if run else plan["selected_revision_id"],
                    run_status=run.status if run else None,
                )
                if voice["status"] == "blocked" and generation["status"] != "running":
                    generation.update(status="blocked", reasons=voice["reasons"])
                elif plan["warning"] and generation["status"] not in {"running", "completed"}:
                    generation.update(
                        status="stale" if speech["status"] == "stale" else "blocked",
                        reasons=[plan["warning"]],
                    )
                elif (
                    not plan["can_generate"]
                    and plan["generation_blocked_reason"]
                    and generation["status"] not in {"running", "completed"}
                ):
                    generation.update(status="blocked", reasons=[plan["generation_blocked_reason"]])
            exports = {
                "configured": _export_state(
                    services,
                    sid,
                    stages.get("export", {}),
                    branch_artifacts,
                    [j for j in jobs if j.session_id == sid],
                    parents,
                    subtitles=False,
                    generation_run_id=run.id if run else None,
                ),
                "subtitles": _export_state(
                    services,
                    sid,
                    stages.get("export", {}),
                    branch_artifacts,
                    [j for j in jobs if j.session_id == sid],
                    parents,
                    subtitles=True,
                ),
            }
            if not matches:
                # This is an identity mismatch, not a rewrite of the pinned branch.
                for operation in (translation, generation, *exports.values()):
                    if operation["status"] not in {"running", "not_requested"}:
                        operation.update(
                            status="blocked",
                            reasons=operation["reasons"] + ["target_language_settings_mismatch"],
                        )
            branch["readiness"] = {
                "translation": translation,
                "review": review,
                "speech_plan": speech,
                "voice": voice,
                "generation": generation,
                "exports": exports,
            }
    return _service(services, "redactor").redact_value(result)
