"""Closed HTTP operation policy for explicitly selected project-link credentials."""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .access.artifact_policy import require_artifact_access, require_artifact_content_access
from .access.contracts import AccessContext
from .access.job_policy import require_job_access
from .access.session_policy import require_session_access
from .accounts.actor_authorization import require_actor_in_session
from .database import Database
from .public_links.contracts import LINK_SCOPES, ProjectLinkContext, PublicLinkError
from .sound_project_models import ProjectSoundCue, ProjectSoundPlan


def _uuid(value: object) -> str:
    if type(value) is not str:
        raise PublicLinkError("invalid_link_selector")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise PublicLinkError("invalid_link_selector") from None


def _values(mapping: Any, name: str) -> list[Any]:
    if hasattr(mapping, "getlist"):
        return list(mapping.getlist(name))
    value = mapping.get(name)
    return [] if value is None else [value]


def selector(request: Any) -> str | None:
    header = _values(request.headers, "X-Pandrator-Project-Link")
    query = _values(request.args, "project_link_id")
    if len(header) > 1 or len(query) > 1:
        raise PublicLinkError("invalid_link_selector")
    selected_header = _uuid(header[0]) if header else None
    selected_query = _uuid(query[0]) if query else None
    if selected_header and selected_query and selected_header != selected_query:
        raise PublicLinkError("link_selector_conflict")
    return selected_header or selected_query


def cookie_name(link_id: str) -> str:
    return "pandrator_link_" + UUID(_uuid(link_id)).hex


def csrf_valid(context: ProjectLinkContext, supplied: object) -> bool:
    return (
        type(supplied) is str
        and 0 < len(supplied) <= 256
        and secrets.compare_digest(
            hashlib.sha256(supplied.encode("utf-8")).hexdigest(), context.csrf_hash
        )
    )


# Reviewed source inventory, 2026-11-01. These are complete registered templates,
# never prefix permissions. New endpoints need an explicit entry and root proof.
_SESSION_RULES: dict[str, frozenset[str]] = {
    "": frozenset({"GET", "PATCH", "DELETE"}),
    "/generation-plan": frozenset({"POST"}),
    "/generation-plan/topology": frozenset({"POST"}),
    "/generation-plan/topology/batch": frozenset({"POST"}),
    "/generation-plan/revisions": frozenset({"GET"}),
    "/generation-plan/revisions/<revision_id>": frozenset({"GET"}),
    "/generation-plan/repair-batches/<batch_id>": frozenset({"GET"}),
    "/generation-plan/repair-batches/<batch_id>/undo": frozenset({"POST"}),
    "/generation-plan/history": frozenset({"GET"}),
    "/generation-plan/status": frozenset({"GET"}),
    "/generation-plan/prepare": frozenset({"POST"}),
    "/generation-plan/select": frozenset({"POST"}),
    "/generation-plan/review": frozenset({"POST"}),
    "/generation-controls": frozenset({"GET", "PUT"}),
    "/generation-admissions": frozenset({"POST"}),
    "/generation-recast": frozenset({"POST"}),
    "/generation-recast/preview": frozenset({"POST"}),
    "/generation-runs": frozenset({"GET", "POST"}),
    "/generation-runs/latest": frozenset({"GET"}),
    "/generation-runs/preview": frozenset({"POST"}),
    "/generation-segments": frozenset({"GET", "PATCH"}),
    "/generation/summary": frozenset({"GET"}),
    "/media-edit": frozenset({"GET", "PUT"}),
    "/media-edit/boundary": frozenset({"PATCH"}),
    "/media-edit/cuts": frozenset({"GET"}),
    "/media-edit/prepare": frozenset({"POST"}),
    "/media-edit/propose": frozenset({"POST"}),
    "/media-edit/render": frozenset({"POST"}),
    "/outcome-plan": frozenset({"GET", "PUT"}),
    "/output-assemblies": frozenset({"POST"}),
    "/output-assemblies/latest": frozenset({"GET"}),
    "/output-mix-preview": frozenset({"POST"}),
    "/outputs/<artifact_id>": frozenset({"DELETE"}),
    "/pdf/apply": frozenset({"POST"}),
    "/settings/<section>": frozenset({"GET", "PUT", "PATCH"}),
    "/settings/resolve": frozenset({"POST"}),
    "/sound-plan": frozenset({"GET"}),
    "/sound-plan/cues": frozenset({"POST"}),
    "/sound-plan/cues/<cue_id>": frozenset({"PATCH", "DELETE"}),
    "/sound-plan/restore": frozenset({"POST"}),
    "/sound-plan/render": frozenset({"POST"}),
    "/sound-plan/resolve": frozenset({"POST"}),
    "/sound-plan/mix": frozenset({"GET"}),
    "/documents": frozenset({"GET"}),
    "/sources": frozenset({"GET", "POST"}),
    "/sources/<attachment_id>": frozenset({"DELETE"}),
    "/sources/adopt-subtitles": frozenset({"POST"}),
    "/sources/align-subtitles": frozenset({"POST"}),
    "/sources/change": frozenset({"POST"}),
    "/sources/change-preview": frozenset({"POST"}),
    "/sources/cleanup": frozenset({"POST"}),
    "/sources/confirm-timing": frozenset({"POST"}),
    "/sources/reuse": frozenset({"POST"}),
    "/sources/status": frozenset({"GET"}),
    "/sources/subtitle-status": frozenset({"GET"}),
    "/sources/<artifact_id>/passages": frozenset({"GET"}),
    "/sources/<artifact_id>/passages/preview": frozenset({"POST"}),
    "/sources/<artifact_id>/passages/rebuild": frozenset({"POST"}),
    "/stages/<stage_key>/artifacts": frozenset({"GET"}),
    "/stages/<stage_key>/artifacts/<artifact_id>": frozenset({"DELETE"}),
    "/stages/<stage_key>/impact": frozenset({"GET"}),
    "/stages/<stage_key>/run": frozenset({"POST"}),
    "/stages/<stage_key>/selection": frozenset({"PUT"}),
    "/stages/<stage_key>/settings-mismatches": frozenset({"GET"}),
    "/subtitles": frozenset({"GET"}),
    "/subtitles/catalog": frozenset({"GET"}),
    "/subtitles/review": frozenset({"GET"}),
    "/subtitles/<stage>/review": frozenset({"POST"}),
    "/subtitles/<stage>/passage-review": frozenset({"POST"}),
    "/subtitles/split-boundaries": frozenset({"POST"}),
    "/subtitle-evidence": frozenset({"GET", "POST"}),
    "/subtitle-evidence/<evidence_id>/resolve": frozenset({"POST"}),
    "/workflow": frozenset({"GET"}),
    "/workflow-inputs": frozenset({"GET", "PUT"}),
    "/workflow-inputs/speech-optimization": frozenset({"POST"}),
    "/workflow-plans": frozenset({"POST"}),
    "/translation-project": frozenset({"GET", "POST"}),
    "/dispatch-runs": frozenset({"GET", "POST"}),
    "/speech-optimization-dispatch-runs": frozenset({"GET", "POST"}),
    "/source-cleaning-dispatch-runs": frozenset({"GET", "POST"}),
    "/media-edit-dispatch-runs": frozenset({"GET", "POST"}),
    "/comparison-exports": frozenset({"POST"}),
    "/performance-plans": frozenset({"GET", "POST"}),
    "/performance-plans/<plan_id>": frozenset({"GET", "PATCH"}),
    "/performance-plans/<plan_id>/adopt": frozenset({"POST"}),
    "/performance-plans/<plan_id>/analyse": frozenset({"POST"}),
    "/performance-plans/<plan_id>/preview": frozenset({"POST"}),
    "/performance-plans/<plan_id>/claim": frozenset({"POST"}),
    "/performance-plans/<plan_id>/batches/<batch_id>/submit": frozenset({"POST"}),
    "/performance-plans/<plan_id>/batches/<batch_id>/renew": frozenset({"POST"}),
    "/performance-plans/<plan_id>/batches/<batch_id>/release": frozenset({"POST"}),
}
_ITEM_RULES: dict[str, tuple[str, frozenset[str]]] = {
    "/artifacts/<artifact_id>/content": ("artifact_content", frozenset({"GET"})),
    "/artifacts/<artifact_id>/context": ("artifact", frozenset({"GET"})),
    "/artifacts/<artifact_id>/pdf": ("artifact", frozenset({"GET"})),
    "/artifacts/<artifact_id>/audio-preview": ("artifact", frozenset({"GET", "POST"})),
    "/artifacts/<artifact_id>/video-preview": ("artifact", frozenset({"GET", "POST"})),
    "/artifacts/<artifact_id>/waveform": ("artifact", frozenset({"GET", "POST"})),
    "/artifacts/<artifact_id>/optimization-review": ("artifact", frozenset({"POST"})),
    "/generation-runs/<run_id>": ("generation_run", frozenset({"GET"})),
    "/generation-runs/<run_id>/coverage": ("generation_run", frozenset({"GET"})),
    "/generation-runs/<run_id>/pause": ("generation_run", frozenset({"POST"})),
    "/generation-runs/<run_id>/resume": ("generation_run", frozenset({"POST"})),
    "/generation-runs/<run_id>/cancel": ("generation_run", frozenset({"POST"})),
    "/generation-runs/<run_id>/audio-version/undo": ("generation_run", frozenset({"POST"})),
    "/generation-runs/<run_id>/audio-version/undo-preview": ("generation_run", frozenset({"POST"})),
    "/generation-segments/<segment_id>": ("generation_segment", frozenset({"PATCH"})),
    "/generation-segments/<segment_id>/takes/<take_id>/select": (
        "generation_segment",
        frozenset({"POST"}),
    ),
    "/generation-operations/<operation_id>": ("generation_operation", frozenset({"GET"})),
    "/jobs/<job_id>": ("job", frozenset({"GET"})),
    "/jobs/<job_id>/logs": ("job", frozenset({"GET"})),
    "/jobs/<job_id>/cancel": ("job", frozenset({"POST"})),
    "/jobs/<job_id>/video-tail-decision": ("job", frozenset({"POST"})),
    "/work/<job_id>": ("job", frozenset({"GET"})),
    "/work/<job_id>/events": ("job", frozenset({"GET"})),
    "/work/<job_id>/cancel": ("job", frozenset({"POST"})),
    "/dispatch-runs/<run_id>": ("dispatch_run", frozenset({"GET"})),
    "/dispatch-runs/<run_id>/preview": ("dispatch_run", frozenset({"GET"})),
    "/dispatch-runs/<run_id>/claim": ("dispatch_run", frozenset({"POST"})),
    "/dispatch-runs/<run_id>/terminate": ("dispatch_run", frozenset({"POST"})),
    "/dispatch-batches/<batch_id>/split-boundaries": ("dispatch_batch", frozenset({"POST"})),
    "/dispatch-batches/<batch_id>/renew": ("dispatch_batch", frozenset({"POST"})),
    "/dispatch-batches/<batch_id>/release": ("dispatch_batch", frozenset({"POST"})),
    "/dispatch-batches/<batch_id>/submit": ("dispatch_batch", frozenset({"POST"})),
    "/media-edit-dispatch-runs/<run_id>": ("media_dispatch_run", frozenset({"GET"})),
    "/media-edit-dispatch-runs/<run_id>/claim": ("media_dispatch_run", frozenset({"POST"})),
    "/media-edit-dispatch-batches/<batch_id>/renew": ("media_dispatch_batch", frozenset({"POST"})),
    "/media-edit-dispatch-batches/<batch_id>/release": (
        "media_dispatch_batch",
        frozenset({"POST"}),
    ),
    "/media-edit-dispatch-batches/<batch_id>/submit": ("media_dispatch_batch", frozenset({"POST"})),
    "/speech-optimization-dispatch-runs/<run_id>": ("speech_dispatch_run", frozenset({"GET"})),
    "/speech-optimization-dispatch-runs/<run_id>/claim": (
        "speech_dispatch_run",
        frozenset({"POST"}),
    ),
    "/speech-optimization-dispatch-batches/<batch_id>/renew": (
        "speech_dispatch_batch",
        frozenset({"POST"}),
    ),
    "/speech-optimization-dispatch-batches/<batch_id>/release": (
        "speech_dispatch_batch",
        frozenset({"POST"}),
    ),
    "/speech-optimization-dispatch-batches/<batch_id>/submit": (
        "speech_dispatch_batch",
        frozenset({"POST"}),
    ),
    "/source-cleaning-dispatch-runs/<run_id>": ("source_dispatch_run", frozenset({"GET"})),
    "/source-cleaning-dispatch-runs/<run_id>/claim": ("source_dispatch_run", frozenset({"POST"})),
    "/source-cleaning-dispatch-batches/<batch_id>/renew": (
        "source_dispatch_batch",
        frozenset({"POST"}),
    ),
    "/source-cleaning-dispatch-batches/<batch_id>/release": (
        "source_dispatch_batch",
        frozenset({"POST"}),
    ),
    "/source-cleaning-dispatch-batches/<batch_id>/submit": (
        "source_dispatch_batch",
        frozenset({"POST"}),
    ),
    "/source-cleaning-dispatch-batches/<batch_id>/inspect": (
        "source_dispatch_batch",
        frozenset({"POST"}),
    ),
    "/translation-projects/<project_id>": ("translation_project", frozenset({"GET", "PATCH"})),
    "/translation-projects/<project_id>/source": ("translation_project", frozenset({"POST"})),
    "/translation-projects/<project_id>/branches": ("translation_project", frozenset({"POST"})),
    "/translation-projects/<project_id>/operations/preview": (
        "translation_project",
        frozenset({"POST"}),
    ),
    "/translation-project-operations/<operation_id>": ("translation_operation", frozenset({"GET"})),
    "/translation-project-operations/<operation_id>/execute": (
        "translation_operation",
        frozenset({"POST"}),
    ),
    "/translation-project-operations/<operation_id>/cancel": (
        "translation_operation",
        frozenset({"POST"}),
    ),
    "/translation-project-operations/<operation_id>/retry-preview": (
        "translation_operation",
        frozenset({"POST"}),
    ),
    "/translation-project-operations/<operation_id>/exports/bundle": (
        "translation_operation",
        frozenset({"POST"}),
    ),
    "/translation-project-operations/<operation_id>/exports/manifest": (
        "translation_operation",
        frozenset({"GET"}),
    ),
    "/uploads/<upload_id>": ("upload", frozenset({"GET", "DELETE"})),
    "/uploads/<upload_id>/chunks/<int:index>": ("upload", frozenset({"PUT"})),
    "/uploads/<upload_id>/complete": ("upload", frozenset({"POST"})),
    "/workflow-plans/<plan_id>": ("workflow_plan", frozenset({"GET"})),
    "/workflow-plans/<plan_id>/execute": ("workflow_plan", frozenset({"POST"})),
    "/document-revisions/<revision_id>/words": ("document_revision", frozenset({"GET"})),
    "/subtitle-evidence/<evidence_id>": ("evidence", frozenset({"GET"})),
}
_COLLECTION_RULES = frozenset(
    {
        "/capabilities",
        "/parameter-definitions",
        "/sessions",
        "/jobs",
        "/events",
        "/events/snapshot",
        "/artifacts",
    }
)
_PREFIX = "/api/v1"
_SESSION_PREFIX = _PREFIX + "/sessions/<session_id>"


def _denied() -> PublicLinkError:
    return PublicLinkError("link_operation_denied", 403)


def _unsafe_keys(value: Any, *, allow_lease_token: bool = False, allow_impact_token: bool = False) -> bool:
    pending = [value]
    count = 0
    while pending:
        current = pending.pop()
        count += 1
        if count > 10000:
            return True
        if isinstance(current, dict):
            for key, child in current.items():
                if not isinstance(key, str):
                    return True
                lowered = key.lower()
                if lowered in {
                    "path",
                    "base_url",
                    "api_key",
                    "credentials",
                    "endpoint",
                    "credential_id",
                } or lowered.endswith(("_path", "_url", "_token")):
                    if not (current is value and (
                        (allow_lease_token and key == "lease_token")
                        or (allow_impact_token and key == "impact_token")
                    )):
                        return True
                if isinstance(child, (dict, list)):
                    pending.append(child)
        elif isinstance(current, list):
            pending.extend(current)
    return False


def _identifier(value: Any) -> str:
    if type(value) is not str or not value or len(value) > 80 or value != value.strip():
        raise _denied()
    return value


def _prove_session(session: Session, context: ProjectLinkContext, session_id: Any) -> str:
    identifier = _identifier(session_id)
    require_session_access(
        session,
        AccessContext(context.actor),
        identifier,
        expected_project_id=context.link["project_id"],
    )
    if (
        session.scalar(
            select(m.SessionRecord.id).where(
                m.SessionRecord.id == identifier, m.SessionRecord.trashed_at.is_(None)
            )
        )
        is None
    ):
        raise _denied()
    return identifier


def _prove_artifact(
    session: Session,
    context: ProjectLinkContext,
    artifact_id: Any,
    expected_session_id: str | None = None,
) -> None:
    artifact = require_artifact_access(
        session, AccessContext(context.actor), _identifier(artifact_id)
    )
    if artifact.project_id != context.link["project_id"] or (
        expected_session_id is not None and artifact.session_id != expected_session_id
    ):
        raise _denied()
    if artifact.session_id is not None:
        _prove_session(session, context, artifact.session_id)


def _scalar_session(session: Session, statement: Any, context: ProjectLinkContext) -> str:
    identifier = session.scalar(statement)
    return _prove_session(session, context, identifier)


def _prove_item(
    session: Session, context: ProjectLinkContext, kind: str, arguments: dict[str, Any]
) -> str | None:
    if kind == "artifact_content":
        require_artifact_content_access(
            session, AccessContext(context.actor), _identifier(arguments["artifact_id"])
        )
        return None
    if kind == "artifact":
        _prove_artifact(session, context, arguments["artifact_id"])
        return None
    if kind == "upload":
        # Prove retained headers before any private body, stream or filesystem access.
        row = session.execute(
            select(
                m.UploadSessionRecord.id,
                m.UploadSessionRecord.session_id,
                m.UploadSessionRecord.project_id,
                m.UploadSessionRecord.workspace_id,
            ).where(m.UploadSessionRecord.id == _identifier(arguments["upload_id"]))
        ).one_or_none()
        if (
            row is None
            or not row.session_id
            or not row.project_id
            or not row.workspace_id
            or row.project_id != context.link["project_id"]
        ):
            raise _denied()
        expected_workspace = session.scalar(
            select(m.Project.workspace_id).where(m.Project.id == context.link["project_id"])
        )
        if row.workspace_id != expected_workspace:
            raise _denied()
        return _prove_session(session, context, row.session_id)
    if kind in {"job", "generation_operation"}:
        identifier = arguments["job_id"] if kind == "job" else arguments["operation_id"]
        job = require_job_access(session, AccessContext(context.actor), _identifier(identifier))
        if job.project_id != context.link["project_id"]:
            raise _denied()
        if kind == "generation_operation" and job.kind not in {
            "generation.admit",
            "generation.run",
        }:
            raise _denied()
        if job.session_id is not None:
            return _prove_session(session, context, job.session_id)
        return None
    if kind in {"translation_project", "translation_operation"}:
        statement = select(m.TranslationProject.source_session_id).where(
            m.TranslationProject.project_root_id == context.link["project_id"]
        )
        if kind == "translation_project":
            statement = statement.where(
                m.TranslationProject.id == _identifier(arguments["project_id"])
            )
        else:
            statement = statement.join(
                m.TranslationProjectOperation,
                m.TranslationProjectOperation.project_id == m.TranslationProject.id,
            ).where(
                m.TranslationProjectOperation.id == _identifier(arguments["operation_id"]),
            )
        return _scalar_session(session, statement, context)
    if kind == "generation_segment":
        return _scalar_session(
            session,
            select(m.GenerationPlan.session_id)
            .join(m.GenerationPlanRevision, m.GenerationPlanRevision.plan_id == m.GenerationPlan.id)
            .join(
                m.GenerationSegment,
                m.GenerationSegment.plan_revision_id == m.GenerationPlanRevision.id,
            )
            .where(m.GenerationSegment.id == _identifier(arguments["segment_id"])),
            context,
        )
    models = {
        "generation_run": m.GenerationRun,
        "dispatch_run": m.DispatchRun,
        "media_dispatch_run": m.MediaEditDispatchRun,
        "speech_dispatch_run": m.SpeechOptimizationDispatchRun,
        "source_dispatch_run": m.SourceCleaningDispatchRun,
        "workflow_plan": m.WorkflowExecutionPlan,
        "evidence": m.SubtitleEvidence,
    }
    if kind in models:
        model = models[kind]
        key = (
            "evidence_id"
            if kind == "evidence"
            else "plan_id"
            if kind == "workflow_plan"
            else "run_id"
        )
        return _scalar_session(
            session,
            select(model.session_id).where(model.id == _identifier(arguments[key])),
            context,
        )
    batch_models = {
        "dispatch_batch": (m.DispatchBatch, m.DispatchRun),
        "media_dispatch_batch": (m.MediaEditDispatchBatch, m.MediaEditDispatchRun),
        "speech_dispatch_batch": (
            m.SpeechOptimizationDispatchBatch,
            m.SpeechOptimizationDispatchRun,
        ),
        "source_dispatch_batch": (m.SourceCleaningDispatchBatch, m.SourceCleaningDispatchRun),
    }
    if kind in batch_models:
        batch, run = batch_models[kind]
        return _scalar_session(
            session,
            select(run.session_id)
            .join(batch, batch.dispatch_run_id == run.id)
            .where(batch.id == _identifier(arguments["batch_id"])),
            context,
        )
    if kind == "document_revision":
        return _scalar_session(
            session,
            select(m.Document.session_id)
            .join(m.DocumentRevision, m.DocumentRevision.document_id == m.Document.id)
            .where(m.DocumentRevision.id == _identifier(arguments["revision_id"])),
            context,
        )
    raise _denied()


def _secondary(
    session: Session,
    context: ProjectLinkContext,
    rule: str,
    arguments: dict[str, Any],
    primary_session: str | None,
) -> None:
    """Check nested identifiers against the proved primary parent, never just existence."""
    if "session_id" in arguments:
        if "artifact_id" in arguments:
            _prove_artifact(session, context, arguments["artifact_id"], primary_session)
        if "revision_id" in arguments:
            actual = session.scalar(
                select(m.GenerationPlan.session_id)
                .join(
                    m.GenerationPlanRevision,
                    m.GenerationPlanRevision.plan_id == m.GenerationPlan.id,
                )
                .where(m.GenerationPlanRevision.id == _identifier(arguments["revision_id"]))
            )
            if actual != primary_session:
                raise _denied()
        if "attachment_id" in arguments:
            actual = session.scalar(
                select(m.SessionSource.session_id).where(
                    m.SessionSource.id == _identifier(arguments["attachment_id"])
                )
            )
            if actual != primary_session:
                raise _denied()
        if "evidence_id" in arguments:
            actual = session.scalar(
                select(m.SubtitleEvidence.session_id).where(
                    m.SubtitleEvidence.id == _identifier(arguments["evidence_id"])
                )
            )
            if actual != primary_session:
                raise _denied()
        if "cue_id" in arguments:
            actual = session.scalar(
                select(ProjectSoundPlan.session_id)
                .join(ProjectSoundCue, ProjectSoundCue.plan_id == ProjectSoundPlan.id)
                .where(
                    ProjectSoundCue.logical_id == _identifier(arguments["cue_id"]),
                    ProjectSoundPlan.session_id == primary_session,
                )
                .limit(1)
            )
            if actual != primary_session:
                raise _denied()
        if "plan_id" in arguments:
            actual = session.scalar(
                select(m.PerformancePlan.session_id).where(
                    m.PerformancePlan.id == _identifier(arguments["plan_id"])
                )
            )
            if actual != primary_session:
                raise _denied()
        if "batch_id" in arguments:
            if "/performance-plans/" in rule:
                actual = session.scalar(
                    select(m.PerformanceBatch.performance_plan_id).where(
                        m.PerformanceBatch.id == _identifier(arguments["batch_id"])
                    )
                )
                if actual != arguments.get("plan_id"):
                    raise _denied()
            elif "/generation-plan/repair-batches/" in rule:
                # Repair batch IDs are their source_generation_run_id in immutable revision metadata.
                actual = session.scalar(
                    select(m.GenerationRun.session_id).where(
                        m.GenerationRun.id == _identifier(arguments["batch_id"])
                    )
                )
                if actual != primary_session:
                    raise _denied()
            else:
                raise _denied()
    if "take_id" in arguments:
        take = session.get(m.AudioTake, _identifier(arguments["take_id"]), populate_existing=True)
        if take is None or take.generation_segment_id != arguments.get("segment_id"):
            raise _denied()
        if take.artifact_id:
            _prove_artifact(session, context, take.artifact_id, primary_session)
        if take.generation_run_id:
            actual = session.scalar(
                select(m.GenerationRun.session_id).where(
                    m.GenerationRun.id == take.generation_run_id
                )
            )
            if actual != primary_session:
                raise _denied()


def _single(mapping: Any, key: str) -> Any:
    values = _values(mapping, key)
    if len(values) != 1:
        raise _denied()
    return values[0]


def request_allowed(request: Any, link_context: ProjectLinkContext, db: Database) -> bool:
    """Intersect a fixed operation boundary with fresh existing resource policies."""
    if (
        type(link_context) is not ProjectLinkContext
        or link_context.actor.principal_kind != "project_link"
    ):
        raise _denied()
    if selector(request) != link_context.link.get("id"):
        raise _denied()
    registered = getattr(request, "url_rule", None)
    rule = getattr(registered, "rule", None)
    if type(rule) is not str or not rule.startswith(_PREFIX + "/"):
        raise _denied()
    method = request.method.upper()
    if method == "HEAD":
        method = "GET"
    arguments = dict(request.view_args or {})
    template_arguments = set(re.findall(r"<(?:(?:[^:<>]+):)?([^<>]+)>", rule))
    if set(arguments) != template_arguments:
        raise _denied()
    mode = link_context.link.get("mode")
    if type(mode) is not str or link_context.actor.scopes != LINK_SCOPES.get(mode):
        raise _denied()
    own_link = arguments.get("link_id") == link_context.link.get("id")
    common = (rule == _PREFIX + "/auth/status" and method == "GET") or (
        rule == _PREFIX + "/public-links/<link_id>/logout" and method == "POST" and own_link
    )
    review = (
        mode == "review_translation"
        and own_link
        and (
            (rule == _PREFIX + "/public-links/<link_id>/review" and method == "GET")
            or (rule == _PREFIX + "/public-links/<link_id>/submissions" and method == "POST")
        )
    )
    suffix = rule[len(_PREFIX) :]
    session_methods = (
        _SESSION_RULES.get(rule[len(_SESSION_PREFIX) :])
        if rule.startswith(_SESSION_PREFIX)
        else None
    )
    item = _ITEM_RULES.get(suffix)
    collection = suffix in _COLLECTION_RULES and method == "GET"
    catalogue = (suffix in {"/services/tts", "/services/stt"} and method == "GET") or (
        rule == _PREFIX + "/public-links/<link_id>/services/<kind>"
        and method == "GET"
        and own_link
        and arguments.get("kind") in {"tts", "stt"}
    )
    upload = suffix in {"/uploads", "/uploads/init"} and method == "POST"
    if not common and not review:
        if mode != "manage_project" or not (
            (session_methods is not None and method in session_methods)
            or (item is not None and method in item[1])
            or collection
            or catalogue
            or upload
        ):
            raise _denied()
    body = request.get_json(silent=True)
    form = getattr(request, "form", {})
    try:
        with db.snapshot_session() as session:
            require_actor_in_session(session, link_context.actor, required_scopes={"app.read"})
            primary_session = None
            if (
                session_methods is not None
                and method in session_methods
                and not common
                and not review
            ):
                primary_session = _prove_session(session, link_context, arguments["session_id"])
            elif item is not None and method in item[1] and not common and not review:
                primary_session = _prove_item(session, link_context, item[0], arguments)
            elif catalogue:
                primary_session = _prove_session(
                    session, link_context, _single(request.args, "session_id")
                )
            elif upload:
                json_session = body.get("session_id") if isinstance(body, dict) else None
                form_sessions = _values(form, "session_id")
                if len(form_sessions) > 1 or (
                    json_session is not None and form_sessions and json_session != form_sessions[0]
                ):
                    raise _denied()
                selected_session = (
                    json_session
                    if json_session is not None
                    else form_sessions[0]
                    if form_sessions
                    else None
                )
                primary_session = _prove_session(session, link_context, selected_session)
                query_sessions = _values(request.args, "session_id")
                if query_sessions and query_sessions != [primary_session]:
                    raise _denied()
            _secondary(session, link_context, rule, arguments, primary_session)
            lease_route = (
                item is not None
                and item[0]
                in {
                    "dispatch_batch",
                    "media_dispatch_batch",
                    "speech_dispatch_batch",
                    "source_dispatch_batch",
                }
            ) or (
                session_methods is not None
                and "/performance-plans/<plan_id>/batches/<batch_id>/" in rule
            )
            if (
                _unsafe_keys(body, allow_lease_token=lease_route, allow_impact_token=(
                    rule == _SESSION_PREFIX + "/sources/change" and method == "POST" and primary_session is not None
                ))
                or _unsafe_keys(dict(form))
                or _unsafe_keys(dict(request.args))
            ):
                raise _denied()
    except (PermissionError, KeyError, TypeError, ValueError):
        raise _denied() from None
    return True
