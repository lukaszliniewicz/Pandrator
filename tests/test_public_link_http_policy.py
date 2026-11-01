"""Disposable non-HTTP tests for the account-bound project-link foundation."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import select

from pandrator.web.access.contracts import AccessContext
from pandrator.web.accounts.auth_contracts import ALL_SCOPES
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.execution_actor import UserActor
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.audit import AuditService
from pandrator.web.auth import AuthService
from pandrator.web.credentials import SecretRedactor
from pandrator.web.database import Database
from pandrator.web.model_base import Base, new_id
from pandrator.web.models import (
    Artifact,
    Document,
    DocumentRevision,
    SessionRecord,
    SessionStageSelection,
)
from pandrator.web.projects.models import Project
from pandrator.web.public_link_http import request_allowed
from pandrator.web.public_links.contracts import PublicLinkError
from pandrator.web.public_links.service import PublicLinkService, digest

VERIFY_ROOT = (
    Path(__file__).resolve().parents[1]
    / ".local-notes/public-access-sharing-2026-11-01/verification/http-policy"
)


@dataclass
class Case:
    database: Database
    service: PublicLinkService
    admin: AccessContext
    outsider: AccessContext
    project_id: str
    other_project_id: str
    workspace_id: str
    source_id: str
    translation_id: str


@pytest.fixture(scope="session")
def schema_template() -> Iterator[Path]:
    VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=VERIFY_ROOT) as directory:
        path = Path(directory) / "schema.sqlite3"
        database = Database(path)
        Base.metadata.create_all(database.engine)
        database.engine.dispose()
        yield path


@pytest.fixture
def case(schema_template: Path) -> Iterator[Case]:
    VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=VERIFY_ROOT) as directory:
        path = Path(directory) / "test.sqlite3"
        shutil.copyfile(schema_template, path)
        db = Database(path)
        with db.immediate_session() as session:
            owner = User(display_name="Owner", is_instance_admin=True)
            outsider = User(display_name="Other admin", is_instance_admin=True)
            session.add_all([owner, outsider])
            session.flush()
            session.add_all(
                [
                    UserCredential(user_id=owner.id, login_name="owner", auth_revision=1),
                    UserCredential(user_id=outsider.id, login_name="other", auth_revision=1),
                ]
            )
            workspace = Workspace(kind="team", name="Team")
            session.add(workspace)
            session.flush()
            session.add(
                WorkspaceMembership(workspace_id=workspace.id, user_id=owner.id, role="owner")
            )
            projects = [Project(workspace_id=workspace.id, name=name) for name in ("Main", "Other")]
            session.add_all(projects)
            session.flush()
            sources = [SessionRecord(project_id=project.id, name="Root") for project in projects]
            session.add_all(sources)
            session.flush()
            for project, source in zip(projects, sources, strict=True):
                project.source_session_id = source.id
            language = SessionRecord(project_id=projects[0].id, name="Polish", target_language="pl")
            session.add(language)
            session.flush()
            document = Document(session_id=language.id, stage="translation", language="pl")
            session.add(document)
            session.flush()
            revision = DocumentRevision(
                document_id=document.id, revision_number=1, content_hash="c" * 64
            )
            session.add(revision)
            session.flush()
            document.active_revision_id = revision.id
            artifact = Artifact(
                workspace_id=workspace.id,
                project_id=projects[0].id,
                session_id=language.id,
                kind="subtitles",
                role="translation",
                relative_path="synthetic.srt",
                content_hash="a" * 64,
                metadata_json={
                    "document_id": document.id,
                    "revision_id": revision.id,
                    "revision_number": 1,
                },
            )
            session.add(artifact)
            session.flush()
            session.add(
                SessionStageSelection(
                    session_id=language.id, stage_key="translate", artifact_id=artifact.id
                )
            )
            ids = (
                owner.id,
                outsider.id,
                projects[0].id,
                projects[1].id,
                workspace.id,
                sources[0].id,
                artifact.id,
            )
        auth = AuthService(db)
        contexts = []
        for user in ids[:2]:
            issued = auth.browser_sessions.issue(
                user, 1, subject=f"test:{user}", principal_kind="owner_session", scopes=ALL_SCOPES
            )
            contexts.append(
                AccessContext(
                    UserActor(
                        user,
                        1,
                        f"test:{user}",
                        "owner_session",
                        ALL_SCOPES,
                        browser_session_id=issued.record_id,
                    )
                )
            )
        service = PublicLinkService(db, auth, AuditService(db, SecretRedactor(db)))
        try:
            yield Case(db, service, contexts[0], contexts[1], *ids[2:])
        finally:
            db.engine.dispose()


@pytest.fixture
def manager(case: Case):
    from tests.test_public_link_foundation import create, enable, redeem

    enable(case)
    created = create(case)
    issued = redeem(case, created)
    return case.service.session_context(created["link"]["id"], issued.raw_browser_session)


def request_double(
    context, rule, method="GET", *, arguments=None, body=None, query=None, form=None
):
    from types import SimpleNamespace

    from werkzeug.datastructures import Headers, MultiDict

    return SimpleNamespace(
        headers=Headers({"X-Pandrator-Project-Link": context.link["id"]}),
        args=MultiDict(query or {}),
        form=MultiDict(form or {}),
        url_rule=SimpleNamespace(rule=rule),
        method=method,
        view_args=arguments or {},
        get_json=lambda silent=True: body,
    )


def allowed(case, manager, rule, method="GET", **kwargs):

    return request_allowed(request_double(manager, rule, method, **kwargs), manager, case.database)


def denied(case, manager, rule, method="GET", **kwargs):

    with pytest.raises(PublicLinkError) as error:
        request_allowed(request_double(manager, rule, method, **kwargs), manager, case.database)
    assert error.value.status_code == 403


def test_selectors_cookie_names_and_csrf(case: Case, manager) -> None:
    from werkzeug.datastructures import Headers, MultiDict

    from pandrator.web.public_link_http import cookie_name, csrf_valid, selector

    request = request_double(manager, "/api/v1/auth/status")
    assert selector(request) == manager.link["id"]
    assert cookie_name(manager.link["id"]) == "pandrator_link_" + manager.link["id"].replace(
        "-", ""
    )
    request.headers = Headers()
    assert selector(request) is None
    request.args = MultiDict({"project_link_id": manager.link["id"]})
    assert selector(request) == manager.link["id"]
    request.headers = Headers({"X-Pandrator-Project-Link": new_id()})
    with pytest.raises(PublicLinkError, match="conflict"):
        selector(request)
    request.headers = Headers({"X-Pandrator-Project-Link": "invalid"})
    with pytest.raises(PublicLinkError):
        selector(request)
    request.headers = Headers()
    request.args = MultiDict(
        [("project_link_id", manager.link["id"]), ("project_link_id", manager.link["id"])]
    )
    with pytest.raises(PublicLinkError):
        selector(request)
    from dataclasses import replace

    context = replace(manager, csrf_hash=digest("synthetic-csrf"))
    assert csrf_valid(context, "synthetic-csrf")
    assert not csrf_valid(context, "wrong") and not csrf_valid(context, None)


def test_project_read_edit_run_and_export_routes(case: Case, manager) -> None:
    routes = [
        ("", "GET"),
        ("", "PATCH"),
        ("", "DELETE"),
        ("/subtitles/catalog", "GET"),
        ("/subtitles/review", "GET"),
        ("/subtitles/<stage>/review", "POST"),
        ("/generation-plan", "POST"),
        ("/generation-controls", "PUT"),
        ("/generation-runs", "POST"),
        ("/generation-admissions", "POST"),
        ("/stages/<stage_key>/run", "POST"),
        ("/output-assemblies", "POST"),
        ("/workflow-plans", "POST"),
        ("/media-edit/render", "POST"),
        ("/comparison-exports", "POST"),
        ("/sound-plan/mix", "GET"),
    ]
    for suffix, method in routes:
        arguments = {"session_id": case.source_id}
        if "<stage>" in suffix:
            arguments["stage"] = "translation"
        if "<stage_key>" in suffix:
            arguments["stage_key"] = "export"
        assert allowed(
            case, manager, "/api/v1/sessions/<session_id>" + suffix, method, arguments=arguments
        )
    for suffix in ["capabilities", "sessions", "jobs", "events", "events/snapshot", "artifacts"]:
        assert allowed(case, manager, "/api/v1/" + suffix)
    assert allowed(
        case,
        manager,
        "/api/v1/artifacts/<artifact_id>/context",
        arguments={"artifact_id": case.translation_id},
    )
    assert allowed(
        case,
        manager,
        "/api/v1/artifacts/<artifact_id>/content",
        method="HEAD",
        arguments={"artifact_id": case.translation_id},
    )


def test_reviewer_closed_operation_set(case: Case) -> None:
    from tests.test_public_link_foundation import create, enable, redeem

    enable(case)
    created = create(case, mode="review_translation", target_artifact_id=case.translation_id)
    issued = redeem(case, created)
    context = case.service.session_context(created["link"]["id"], issued.raw_browser_session)
    for suffix, method in [("review", "GET"), ("submissions", "POST"), ("logout", "POST")]:
        assert allowed(
            case,
            context,
            "/api/v1/public-links/<link_id>/" + suffix,
            method,
            arguments={"link_id": created["link"]["id"]},
        )
        denied(
            case,
            context,
            "/api/v1/public-links/<link_id>/" + suffix,
            method,
            arguments={"link_id": new_id()},
        )
    assert allowed(case, context, "/api/v1/auth/status")
    for rule, arguments in [
        ("/api/v1/sessions/<session_id>", {"session_id": case.source_id}),
        ("/api/v1/artifacts/<artifact_id>/content", {"artifact_id": case.translation_id}),
        ("/api/v1/services/tts", {}),
        ("/api/v1/events", {}),
        ("/api/v1/sessions", {}),
    ]:
        denied(case, context, rule, arguments=arguments)


def test_global_and_future_operations_fail_closed(case: Case, manager) -> None:
    for rule, method, arguments in [
        ("/api/v1/admin/public-sharing", "GET", {}),
        ("/api/v1/auth/tokens", "POST", {}),
        ("/api/v1/settings", "GET", {}),
        ("/api/v1/credentials", "GET", {}),
        ("/api/v1/workspaces", "GET", {}),
        ("/api/v1/sources", "GET", {}),
        ("/api/v1/services/tts/refresh", "POST", {}),
        ("/api/v1/services/tts/discover", "POST", {}),
        ("/api/v1/projects", "POST", {}),
        ("/api/v1/sessions", "POST", {}),
        ("/api/v1/jobs", "POST", {}),
        ("/api/v1/sessions/<session_id>/forks", "POST", {"session_id": case.source_id}),
        ("/api/v1/sessions/<session_id>/bundle", "POST", {"session_id": case.source_id}),
        ("/api/v1/sessions/<session_id>/purge", "POST", {"session_id": case.source_id}),
        ("/api/v1/sessions/<session_id>/reindex", "POST", {"session_id": case.source_id}),
        ("/api/v1/sessions/<session_id>/project-folders", "PUT", {"session_id": case.source_id}),
        ("/api/v1/sessions/<session_id>/sources/url", "POST", {"session_id": case.source_id}),
        (
            "/api/v1/sessions/<session_id>/sources/start-new-session",
            "POST",
            {"session_id": case.source_id},
        ),
        (
            "/api/v1/sessions/<session_id>/generation-plan/future",
            "POST",
            {"session_id": case.source_id},
        ),
        ("/api/v1/artifacts/<artifact_id>/future", "GET", {"artifact_id": case.translation_id}),
        ("/api/v1/public-links/<link_id>/review", "GET", {"link_id": manager.link["id"]}),
    ]:
        denied(case, manager, rule, method, arguments=arguments)


def test_foreign_session_artifact_job_and_secondary_ids(case: Case, manager) -> None:
    from pandrator.web.models import Job

    with case.database.immediate_session() as session:
        foreign = session.scalar(
            select(SessionRecord).where(SessionRecord.project_id == case.other_project_id)
        )
        foreign_session = foreign.id
        artifact = Artifact(
            workspace_id=case.workspace_id,
            project_id=case.other_project_id,
            session_id=foreign.id,
            kind="srt",
            role="translation",
            relative_path="foreign.srt",
        )
        good_job = Job(
            kind="export.test",
            workspace_id=case.workspace_id,
            project_id=case.project_id,
            session_id=case.source_id,
        )
        bad_job = Job(
            kind="export.test",
            workspace_id=case.workspace_id,
            project_id=case.other_project_id,
            session_id=foreign.id,
        )
        session.add_all([artifact, good_job, bad_job])
        session.flush()
        artifact_id, good_job_id, bad_job_id = artifact.id, good_job.id, bad_job.id
    denied(
        case, manager, "/api/v1/sessions/<session_id>", arguments={"session_id": foreign_session}
    )
    for suffix in ["content", "context", "pdf"]:
        denied(
            case,
            manager,
            "/api/v1/artifacts/<artifact_id>/" + suffix,
            arguments={"artifact_id": artifact_id},
        )
    for suffix in ["", "/logs", "/cancel"]:
        method = "POST" if suffix == "/cancel" else "GET"
        assert allowed(
            case,
            manager,
            "/api/v1/jobs/<job_id>" + suffix,
            method,
            arguments={"job_id": good_job_id},
        )
        denied(
            case,
            manager,
            "/api/v1/jobs/<job_id>" + suffix,
            method,
            arguments={"job_id": bad_job_id},
        )
    denied(
        case,
        manager,
        "/api/v1/sessions/<session_id>/outputs/<artifact_id>",
        "DELETE",
        arguments={"session_id": case.source_id, "artifact_id": artifact_id},
    )


def test_unsafe_binding_keys_and_safe_text_values(case: Case, manager) -> None:
    rule = "/api/v1/sessions/<session_id>/settings/<section>"
    arguments = {"session_id": case.source_id, "section": "tts"}
    for key in [
        "path",
        "file_path",
        "base_url",
        "source_url",
        "api_key",
        "access_token",
        "credentials",
        "endpoint",
        "credential_id",
        "PATH",
    ]:
        denied(case, manager, rule, "PATCH", arguments=arguments, body={"nested": [{key: None}]})
    assert allowed(
        case,
        manager,
        "/api/v1/sessions/<session_id>/subtitles/<stage>/review",
        "POST",
        arguments={"session_id": case.source_id, "stage": "translation"},
        body={
            "segments": [
                {"text": "Keep /tmp/example and https://example.invalid literally as text."}
            ]
        },
    )
    denied(case, manager, rule, "PATCH", arguments=arguments, body={"lease_token": "not-exempt"})


def test_service_catalogues_and_uploads_require_explicit_project_session(
    case: Case, manager
) -> None:
    for service in ["tts", "stt"]:
        assert allowed(
            case, manager, "/api/v1/services/" + service, query={"session_id": case.source_id}
        )
        denied(case, manager, "/api/v1/services/" + service)
        with case.database.snapshot_session() as session:
            foreign = session.scalar(
                select(SessionRecord.id).where(SessionRecord.project_id == case.other_project_id)
            )
        denied(case, manager, "/api/v1/services/" + service, query={"session_id": foreign})
    assert allowed(
        case,
        manager,
        "/api/v1/uploads",
        "POST",
        form={"session_id": case.source_id, "purpose": "source"},
    )
    assert allowed(case, manager, "/api/v1/uploads", "POST", body={"session_id": case.source_id})
    denied(case, manager, "/api/v1/uploads", "POST")
    denied(case, manager, "/api/v1/uploads", "POST", query={"session_id": case.source_id})
    denied(
        case,
        manager,
        "/api/v1/uploads",
        "POST",
        form={"session_id": case.source_id, "path": "/tmp/example"},
    )


def test_revoked_context_and_missing_selector_cannot_fall_back(case: Case, manager) -> None:
    from werkzeug.datastructures import Headers

    request = request_double(
        manager, "/api/v1/sessions/<session_id>", arguments={"session_id": case.source_id}
    )
    request.headers = Headers()
    with pytest.raises(PublicLinkError):
        request_allowed(request, manager, case.database)
    case.service.revoke_link(case.admin, manager.link["id"], expected_revision=1)
    denied(case, manager, "/api/v1/sessions/<session_id>", arguments={"session_id": case.source_id})


def test_registered_future_route_and_changed_method_stay_denied(case: Case, manager) -> None:
    from flask import Flask, request

    app = Flask(__name__)
    app.add_url_rule(
        "/api/v1/sessions/<session_id>/generation-plan/future",
        "future",
        lambda session_id: "future",
        methods=["POST"],
    )
    path = f"/api/v1/sessions/{case.source_id}/generation-plan/future"
    with app.test_request_context(
        path, method="POST", headers={"X-Pandrator-Project-Link": manager.link["id"]}
    ):
        with pytest.raises(PublicLinkError):
            request_allowed(request, manager, case.database)
    denied(
        case,
        manager,
        "/api/v1/sessions/<session_id>/documents",
        "POST",
        arguments={"session_id": case.source_id},
    )


def test_published_attached_dependency_allows_bytes_without_metadata(case: Case, manager) -> None:
    from pandrator.web.models import SessionSource, SourceAsset

    with case.database.immediate_session() as session:
        foreign = session.scalar(
            select(SessionRecord).where(SessionRecord.project_id == case.other_project_id)
        )
        artifact = Artifact(
            workspace_id=case.workspace_id,
            project_id=case.other_project_id,
            session_id=foreign.id,
            kind="srt",
            relative_path="attached-dependency.srt",
        )
        session.add(artifact)
        session.flush()
        source = SourceAsset(
            workspace_id=case.workspace_id,
            artifact_id=artifact.id,
            display_name="Attached",
            kind="srt",
        )
        session.add(source)
        session.flush()
        attachment = SessionSource(
            session_id=case.source_id, source_asset_id=source.id, is_current=True
        )
        session.add(attachment)
        artifact_id = artifact.id
    assert allowed(
        case,
        manager,
        "/api/v1/artifacts/<artifact_id>/content",
        arguments={"artifact_id": artifact_id},
    )
    denied(
        case,
        manager,
        "/api/v1/artifacts/<artifact_id>/context",
        arguments={"artifact_id": artifact_id},
    )
    with case.database.immediate_session() as session:
        session.scalar(
            select(SessionSource).where(SessionSource.source_asset_id == source.id)
        ).is_current = False
    denied(
        case,
        manager,
        "/api/v1/artifacts/<artifact_id>/content",
        arguments={"artifact_id": artifact_id},
    )


def test_generation_and_batch_parent_proofs_and_narrow_lease_exception(case: Case, manager) -> None:
    from pandrator.web import models as m

    items = []
    with case.database.immediate_session() as session:
        foreign = session.scalar(
            select(SessionRecord).where(SessionRecord.project_id == case.other_project_id)
        )
        for source in [case.source_id, foreign.id]:
            plan = m.GenerationPlan(session_id=source)
            session.add(plan)
            session.flush()
            revision = m.GenerationPlanRevision(
                plan_id=plan.id, revision_number=1, content_hash="revision"
            )
            session.add(revision)
            session.flush()
            segment = m.GenerationSegment(plan_revision_id=revision.id, ordinal=0, text="Synthetic")
            run = m.GenerationRun(session_id=source, plan_revision_id=revision.id)
            performance = m.PerformancePlan(
                session_id=source, plan_revision_id=revision.id, base_signature="signature"
            )
            session.add_all([segment, run, performance])
            session.flush()
            take = m.AudioTake(generation_segment_id=segment.id, generation_run_id=run.id)
            batch = m.PerformanceBatch(performance_plan_id=performance.id, ordinal=0)
            session.add_all([take, batch])
            session.flush()
            items.append((revision.id, segment.id, run.id, take.id, performance.id, batch.id))
    good, bad = items
    assert allowed(case, manager, "/api/v1/generation-runs/<run_id>", arguments={"run_id": good[2]})
    denied(case, manager, "/api/v1/generation-runs/<run_id>", arguments={"run_id": bad[2]})
    denied(
        case, manager, "/api/v1/generation-runs/<run_id>", "DELETE", arguments={"run_id": good[2]}
    )
    assert allowed(
        case,
        manager,
        "/api/v1/generation-segments/<segment_id>",
        "PATCH",
        arguments={"segment_id": good[1]},
    )
    denied(
        case,
        manager,
        "/api/v1/generation-segments/<segment_id>",
        "PATCH",
        arguments={"segment_id": bad[1]},
    )
    take_rule = "/api/v1/generation-segments/<segment_id>/takes/<take_id>/select"
    assert allowed(
        case, manager, take_rule, "POST", arguments={"segment_id": good[1], "take_id": good[3]}
    )
    denied(case, manager, take_rule, "POST", arguments={"segment_id": good[1], "take_id": bad[3]})
    revision_rule = "/api/v1/sessions/<session_id>/generation-plan/revisions/<revision_id>"
    denied(
        case,
        manager,
        revision_rule,
        arguments={"session_id": case.source_id, "revision_id": bad[0]},
    )
    batch_rule = (
        "/api/v1/sessions/<session_id>/performance-plans/<plan_id>/batches/<batch_id>/submit"
    )
    arguments = {"session_id": case.source_id, "plan_id": good[4], "batch_id": good[5]}
    assert allowed(
        case, manager, batch_rule, "POST", arguments=arguments, body={"lease_token": "synthetic"}
    )
    denied(
        case,
        manager,
        batch_rule,
        "POST",
        arguments=arguments | {"batch_id": bad[5]},
        body={"lease_token": "synthetic"},
    )
    denied(
        case,
        manager,
        batch_rule,
        "POST",
        arguments=arguments,
        body={"lease_token": "synthetic", "items": [{"lease_token": "nested"}]},
    )
    denied(
        case,
        manager,
        batch_rule,
        "POST",
        arguments=arguments,
        body={"lease_token": "synthetic", "api_key": None},
    )


def test_static_allowlist_matches_current_backend_registration_templates() -> None:
    import ast

    from pandrator.web.public_link_http import _ITEM_RULES, _SESSION_PREFIX, _SESSION_RULES

    def literal(node, names):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return names.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = literal(node.left, names), literal(node.right, names)
            return left + right if left is not None and right is not None else None
        if isinstance(node, ast.JoinedStr):
            parts = [
                literal(part.value, names)
                if isinstance(part, ast.FormattedValue)
                else literal(part, names)
                for part in node.values
            ]
            return "".join(parts) if all(part is not None for part in parts) else None
        return None

    registered = set()
    root = Path(__file__).resolve().parents[1] / "pandrator/web"
    for path in root.glob("*routes.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                value = literal(node.value, names)
                if value is not None:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            names[target.id] = value
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if (
                        isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr in {"get", "post", "patch", "put", "delete"}
                        and decorator.args
                    ):
                        value = literal(decorator.args[0], names)
                        if value:
                            registered.add((value, decorator.func.attr.upper()))
    expected = {
        ("/api/v1" + rule, method)
        for rule, (_kind, methods) in _ITEM_RULES.items()
        for method in methods
    }
    expected |= {
        (_SESSION_PREFIX + suffix, method)
        for suffix, methods in _SESSION_RULES.items()
        for method in methods
    }
    assert expected <= registered, sorted(expected - registered)


def _language_aggregate(case, actor, source_id):
    from pandrator.runtime import DataPaths
    from pandrator.web.accounts.execution_actor import execution_actor_scope
    from pandrator.web.artifacts import ArtifactService
    from pandrator.web.session_forks import SessionForkService
    from pandrator.web.subtitle_review import SubtitleReviewService
    from pandrator.web.translation_projects import create_project_in_session

    paths = DataPaths(case.database.path.parent)
    with case.database.immediate_session() as session:
        source = session.get(SessionRecord, source_id)
        source.workflow_kind = "subtitles"
        source.source_language = "en"
        source.included_stages_json = ["transcribe", "translate", "export"]
        directory = paths.sessions / source.storage_key
    directory.mkdir(parents=True, exist_ok=True)
    artifacts = ArtifactService(case.database, paths)
    forks = SessionForkService(case.database, paths, artifacts)
    subtitles = SubtitleReviewService(case.database, artifacts, lambda _id: directory)
    with execution_actor_scope(actor):
        published = subtitles.save_review(
            source_id,
            "transcription",
            0,
            [{"start_ms": 0, "end_ms": 1800, "text": "Synthetic language source", "speaker": None}],
        )
        with case.database.immediate_session() as session:
            source = session.get(SessionRecord, source_id)
            created = create_project_in_session(
                session,
                source_id,
                published["artifact_id"],
                "Languages",
                source.revision,
                paths=paths,
                session_forks=forks,
                created_directories=[],
            )
    return created["project"], paths, forks


def test_guest_language_branches_reuse_existing_project_and_workspace(case: Case, manager) -> None:
    from sqlalchemy import func

    from pandrator.web.access.contracts import ProjectAction
    from pandrator.web.access.session_policy import require_session_access
    from pandrator.web.accounts.execution_actor import execution_actor_scope
    from pandrator.web.translation_projects import create_branches_in_session

    with case.database.snapshot_session() as session:
        before_projects = session.scalar(select(func.count()).select_from(Project))
        before_workspaces = session.scalar(select(func.count()).select_from(Workspace))
    assert allowed(
        case,
        manager,
        "/api/v1/sessions/<session_id>/translation-project",
        "POST",
        arguments={"session_id": case.source_id},
    )
    project, paths, forks = _language_aggregate(case, manager.actor, case.source_id)
    arguments = {"project_id": project["id"]}
    assert allowed(
        case,
        manager,
        "/api/v1/translation-projects/<project_id>/branches",
        "POST",
        arguments=arguments,
        body={"expected_revision": project["revision"], "targets": [{"target_language": "de"}]},
    )
    with execution_actor_scope(manager.actor), case.database.immediate_session() as session:
        require_session_access(
            session, AccessContext(manager.actor), case.source_id, ProjectAction.EDIT
        )
        result = create_branches_in_session(
            session,
            project["id"],
            project["revision"],
            [{"target_language": "de"}, {"target_language": "pl"}],
            session_forks=forks,
            paths=paths,
            created_directories=[],
        )
    branches = result["project"]["branches"]
    assert len(branches) == 2
    with case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(Project)) == before_projects
        assert session.scalar(select(func.count()).select_from(Workspace)) == before_workspaces
        for branch in branches:
            record = session.get(SessionRecord, branch["session_id"])
            assert record.project_id == case.project_id
            assert session.get(Project, record.project_id).workspace_id == case.workspace_id
    for branch in branches:
        assert allowed(
            case,
            manager,
            "/api/v1/sessions/<session_id>",
            arguments={"session_id": branch["session_id"]},
        )
    denied(case, manager, "/api/v1/sessions", "POST")
    denied(
        case,
        manager,
        "/api/v1/sessions/<session_id>/forks",
        "POST",
        arguments={"session_id": case.source_id},
    )


def test_language_aggregate_and_operation_routes_prove_root(case: Case, manager) -> None:
    from datetime import timedelta

    from pandrator.web.model_base import utcnow
    from pandrator.web.models import TranslationProjectOperation

    own, _paths, _forks = _language_aggregate(case, manager.actor, case.source_id)
    with case.database.snapshot_session() as session:
        foreign_source = session.scalar(
            select(SessionRecord.id).where(SessionRecord.project_id == case.other_project_id)
        )
    foreign, _paths, _forks = _language_aggregate(case, case.admin.actor, foreign_source)
    with case.database.immediate_session() as session:
        operations = [
            TranslationProjectOperation(
                project_id=project_id,
                principal_subject=subject,
                target_instance_id="synthetic-instance",
                action="export",
                preview_digest="a" * 64,
                expires_at=utcnow() + timedelta(minutes=30),
            )
            for project_id, subject in [
                (own["id"], manager.actor.subject),
                (foreign["id"], manager.actor.subject),
                (own["id"], case.admin.actor.subject),
            ]
        ]
        session.add_all(operations)
        session.flush()
        own_operation, foreign_operation, owner_operation = [
            operation.id for operation in operations
        ]
    for suffix, method in [
        ("", "GET"),
        ("", "PATCH"),
        ("/source", "POST"),
        ("/branches", "POST"),
        ("/operations/preview", "POST"),
    ]:
        rule = "/api/v1/translation-projects/<project_id>" + suffix
        assert allowed(case, manager, rule, method, arguments={"project_id": own["id"]})
        denied(case, manager, rule, method, arguments={"project_id": foreign["id"]})
    for suffix, method in [
        ("", "GET"),
        ("/execute", "POST"),
        ("/cancel", "POST"),
        ("/retry-preview", "POST"),
        ("/exports/bundle", "POST"),
        ("/exports/manifest", "GET"),
    ]:
        rule = "/api/v1/translation-project-operations/<operation_id>" + suffix
        assert allowed(case, manager, rule, method, arguments={"operation_id": own_operation})
        for denied_id in [foreign_operation]:
            denied(case, manager, rule, method, arguments={"operation_id": denied_id})
    # Finished project exports intentionally remain accessible across subjects.
    # Receipt subject and instance checks stay in the existing operation handlers.
    for suffix, method in [("/exports/bundle", "POST"), ("/exports/manifest", "GET")]:
        assert allowed(
            case,
            manager,
            "/api/v1/translation-project-operations/<operation_id>" + suffix,
            method,
            arguments={"operation_id": owner_operation},
        )
    denied(
        case,
        manager,
        "/api/v1/translation-projects/<project_id>/future",
        "POST",
        arguments={"project_id": own["id"]},
    )
    denied(
        case,
        manager,
        "/api/v1/translation-project-operations/<operation_id>/future",
        "POST",
        arguments={"operation_id": own_operation},
    )
    denied(
        case,
        manager,
        "/api/v1/translation-projects/<project_id>/source",
        "POST",
        arguments={"project_id": own["id"]},
        body={"nested": {"path": "/tmp/unsafe"}},
    )


def test_reviewer_cannot_use_language_aggregate_admission(case: Case) -> None:
    from tests.test_public_link_foundation import create, enable, redeem

    enable(case)
    created = create(case, mode="review_translation", target_artifact_id=case.translation_id)
    issued = redeem(case, created)
    review = case.service.session_context(created["link"]["id"], issued.raw_browser_session)
    denied(
        case,
        review,
        "/api/v1/sessions/<session_id>/translation-project",
        "POST",
        arguments={"session_id": case.source_id},
    )
    denied(
        case,
        review,
        "/api/v1/translation-projects/<project_id>",
        arguments={"project_id": new_id()},
    )
    denied(
        case,
        review,
        "/api/v1/translation-projects/<project_id>/branches",
        "POST",
        arguments={"project_id": new_id()},
    )
    denied(
        case,
        review,
        "/api/v1/translation-project-operations/<operation_id>",
        arguments={"operation_id": new_id()},
    )


def test_language_handlers_reject_foreign_source_bindings(case: Case, manager) -> None:
    from pydantic import ValidationError

    from pandrator.web.accounts.execution_actor import execution_actor_scope
    from pandrator.web.translation_project_routes import (
        TranslationBranchesCreateRequest,
        TranslationProjectCreateRequest,
        TranslationProjectSourceUpdateRequest,
    )
    from pandrator.web.translation_projects import update_project_source_in_session

    own, paths, _forks = _language_aggregate(case, manager.actor, case.source_id)
    with case.database.snapshot_session() as session:
        foreign_source = session.scalar(
            select(SessionRecord.id).where(SessionRecord.project_id == case.other_project_id)
        )
    foreign, _paths, _forks = _language_aggregate(case, case.admin.actor, foreign_source)
    for model, body in [
        (
            TranslationProjectCreateRequest,
            {"checkpoint_artifact_id": own["checkpoint_artifact_id"], "expected_revision": 1},
        ),
        (
            TranslationProjectSourceUpdateRequest,
            {
                "checkpoint_artifact_id": own["checkpoint_artifact_id"],
                "expected_revision": 1,
                "expected_source_revision": 1,
            },
        ),
        (
            TranslationBranchesCreateRequest,
            {"expected_revision": 1, "targets": [{"target_language": "de"}]},
        ),
    ]:
        for field, value in [
            ("source_session_id", foreign_source),
            ("source_artifact_id", foreign["checkpoint_artifact_id"]),
        ]:
            with pytest.raises(ValidationError) as error:
                model.model_validate(body | {field: value})
            assert any(
                item["type"] == "extra_forbidden" and item["loc"] == (field,)
                for item in error.value.errors()
            )
    # The actual accepted checkpoint field is checked against the aggregate's
    # stored source session by the existing source-update publisher.
    with execution_actor_scope(manager.actor), case.database.immediate_session() as session:
        source_revision = session.get(SessionRecord, case.source_id).revision
        with pytest.raises(KeyError):
            update_project_source_in_session(
                session,
                own["id"],
                foreign["checkpoint_artifact_id"],
                own["revision"],
                source_revision,
                paths=paths,
            )


def test_project_chunked_upload_gate_accepts_bound_protocol_and_peer_records(
    case: Case, manager
) -> None:
    from datetime import timedelta

    from sqlalchemy import event

    from pandrator.web.model_base import utcnow
    from pandrator.web.models import UploadSessionRecord

    with case.database.immediate_session() as session:
        upload = UploadSessionRecord(
            session_id=case.source_id,
            project_id=case.project_id,
            workspace_id=case.workspace_id,
            filename="synthetic.bin",
            size_bytes=10,
            chunk_size=1024 * 1024,
            chunk_count=1,
            temporary_relative_path="tmp/unused",
            expires_at=utcnow() + timedelta(hours=1),
        )
        session.add(upload)
        session.flush()
        upload_id = upload.id
    assert allowed(
        case,
        manager,
        "/api/v1/uploads/init",
        "POST",
        body={
            "session_id": case.source_id,
            "filename": "synthetic.bin",
            "size_bytes": 10,
            "chunk_size": 1024 * 1024,
            "sha256": "a" * 64,
        },
    )
    statements = []

    def capture(_connection, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(case.database.engine, "before_cursor_execute", capture)
    try:
        for rule, method, arguments in [
            ("/api/v1/uploads/<upload_id>", "GET", {"upload_id": upload_id}),
            ("/api/v1/uploads/<upload_id>", "DELETE", {"upload_id": upload_id}),
            (
                "/api/v1/uploads/<upload_id>/chunks/<int:index>",
                "PUT",
                {"upload_id": upload_id, "index": 0},
            ),
            ("/api/v1/uploads/<upload_id>/complete", "POST", {"upload_id": upload_id}),
        ]:
            assert allowed(case, manager, rule, method, arguments=arguments)
    finally:
        event.remove(case.database.engine, "before_cursor_execute", capture)
    upload_reads = [statement for statement in statements if "FROM upload_sessions" in statement]
    assert upload_reads and all(
        "filename" not in statement
        and "received_json" not in statement
        and "temporary_relative_path" not in statement
        and "result_json" not in statement
        for statement in upload_reads
    )


def test_chunked_upload_gate_denies_unbound_foreign_missing_and_tampered_roots(
    case: Case, manager
) -> None:
    from datetime import timedelta

    from pandrator.web.model_base import utcnow
    from pandrator.web.models import UploadSessionRecord

    with case.database.immediate_session() as session:
        foreign = session.scalar(
            select(SessionRecord.id).where(SessionRecord.project_id == case.other_project_id)
        )
        other_workspace = Workspace(kind="team", name="Other upload namespace")
        session.add(other_workspace)
        session.flush()
        records = []
        for source_id, project_id, workspace_id in [
            (None, None, case.workspace_id),
            (None, case.project_id, case.workspace_id),
            (foreign, case.other_project_id, case.workspace_id),
            (foreign, case.project_id, case.workspace_id),
            (case.source_id, case.other_project_id, case.workspace_id),
            (case.source_id, case.project_id, other_workspace.id),
        ]:
            records.append(
                UploadSessionRecord(
                    session_id=source_id,
                    project_id=project_id,
                    workspace_id=workspace_id,
                    filename="synthetic.bin",
                    size_bytes=10,
                    chunk_size=1024 * 1024,
                    chunk_count=1,
                    temporary_relative_path="tmp/unused",
                    expires_at=utcnow() + timedelta(hours=1),
                )
            )
        session.add_all(records)
        session.flush()
        ids = [record.id for record in records] + [new_id()]
    for upload_id in ids:
        for rule, method, arguments in [
            ("/api/v1/uploads/<upload_id>", "GET", {"upload_id": upload_id}),
            ("/api/v1/uploads/<upload_id>", "DELETE", {"upload_id": upload_id}),
            (
                "/api/v1/uploads/<upload_id>/chunks/<int:index>",
                "PUT",
                {"upload_id": upload_id, "index": 0},
            ),
            ("/api/v1/uploads/<upload_id>/complete", "POST", {"upload_id": upload_id}),
        ]:
            denied(case, manager, rule, method, arguments=arguments)
    for body in [
        {"workspace_id": case.workspace_id, "filename": "synthetic.bin", "size_bytes": 10},
        {"session_id": foreign, "filename": "synthetic.bin", "size_bytes": 10},
        {"session_id": case.source_id, "path": "/tmp/client-path"},
    ]:
        denied(case, manager, "/api/v1/uploads/init", "POST", body=body)
    denied(
        case, manager, "/api/v1/uploads/<upload_id>/future", "POST", arguments={"upload_id": ids[0]}
    )


def test_reviewer_denied_chunked_protocol_and_parameter_definitions(case: Case) -> None:
    from tests.test_public_link_foundation import create, enable, redeem

    enable(case)
    created = create(case, mode="review_translation", target_artifact_id=case.translation_id)
    issued = redeem(case, created)
    review = case.service.session_context(created["link"]["id"], issued.raw_browser_session)
    for rule, method, arguments in [
        ("/api/v1/uploads/init", "POST", {}),
        ("/api/v1/uploads/<upload_id>", "GET", {"upload_id": new_id()}),
        ("/api/v1/uploads/<upload_id>", "DELETE", {"upload_id": new_id()}),
        (
            "/api/v1/uploads/<upload_id>/chunks/<int:index>",
            "PUT",
            {"upload_id": new_id(), "index": 0},
        ),
        ("/api/v1/uploads/<upload_id>/complete", "POST", {"upload_id": new_id()}),
        ("/api/v1/parameter-definitions", "GET", {}),
    ]:
        denied(case, review, rule, method, arguments=arguments, body={"session_id": case.source_id})


def test_manager_parameter_definitions_is_read_only(case: Case, manager) -> None:
    assert allowed(
        case,
        manager,
        "/api/v1/parameter-definitions",
        "GET",
        query={"section": "output", "limit": "50"},
    )
    assert allowed(case, manager, "/api/v1/parameter-definitions", "HEAD")
    denied(case, manager, "/api/v1/parameter-definitions", "POST")
