"""Disposable service-level acceptance for pinned public translation submissions."""

from __future__ import annotations

import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from pandrator.web.access.contracts import AccessContext
from pandrator.web.accounts.auth_contracts import ALL_SCOPES
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.execution_actor import UserActor
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.artifacts import ArtifactService
from pandrator.web.audit import AuditService
from pandrator.web.auth import AuthService
from pandrator.web.credentials import SecretRedactor
from pandrator.web.database import Database
from pandrator.web.models import (
    Artifact,
    Document,
    DocumentRevision,
    Segment,
    SessionRecord,
    SessionStageSelection,
)
from pandrator.web.projects.models import Project, ProjectMembership
from pandrator.web.public_links.contracts import PublicLinkError
from pandrator.web.public_links.models import ProjectLink, ProjectLinkSubmission
from pandrator.web.public_links.reviews import PublicTranslationReviewService
from pandrator.web.public_links.service import PublicLinkService
from pandrator.web.sessions import SessionService
from pandrator.web.subtitle_review import SubtitleReviewService
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workspace_settings import WorkspaceSettingsService
from tests.web_test_support import prepare_web_test_data_root

VERIFY_ROOT = (
    Path(__file__).resolve().parents[1]
    / ".local-notes/public-access-sharing-2026-11-01/verification/submissions"
)


@pytest.fixture
def case():
    VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=VERIFY_ROOT) as directory:
        paths = prepare_web_test_data_root(directory)
        database = Database(paths.database)
        sessions = SessionService(database)
        source = sessions.create("Source", workflow_kind="subtitles")
        target = sessions.create(
            "Review", workflow_kind="subtitles", target_language="pl", project_id=source.project_id
        )
        target_dir = paths.sessions / target.storage_key
        target_dir.mkdir()
        artifacts = ArtifactService(database, paths)
        source_path = target_dir / "translation.srt"
        source_path.write_text(
            "1\n00:00:00,000 --> 00:00:02,000\nPierwsza kwestia.\n\n"
            "2\n00:00:03,000 --> 00:00:05,000\nDruga kwestia.\n",
            encoding="utf-8",
        )
        artifact = artifacts.register(
            source_path, kind="srt", role="translation", session_id=target.id
        )
        WorkflowHandlers(database, paths)._store_srt_document(target.id, artifact, "translation")
        auth = AuthService(database)
        with database.immediate_session() as session:
            owner = session.scalar(select(User).where(User.legacy_subject == "owner"))
            workspace = session.get(Workspace, session.get(Project, target.project_id).workspace_id)
            workspace.kind = "team"
            workspace.personal_owner_user_id = None
            credential = session.get(UserCredential, owner.id)
            if credential is None:
                session.add(UserCredential(user_id=owner.id, login_name="owner", auth_revision=1))
            users = [
                User(display_name=name, is_instance_admin=name == "administrator")
                for name in ("editor", "viewer", "administrator")
            ]
            session.add_all(users)
            session.flush()
            for user in users:
                session.add(
                    UserCredential(user_id=user.id, login_name=user.display_name, auth_revision=1)
                )
            session.add(
                WorkspaceMembership(workspace_id=workspace.id, user_id=users[0].id, role="member")
            )
            session.add(
                ProjectMembership(project_id=target.project_id, user_id=users[1].id, role="viewer")
            )
            owner_id, user_ids, workspace_id = owner.id, [u.id for u in users], workspace.id

        def actor(user_id, *, admin=False):
            scopes = ALL_SCOPES if admin else ALL_SCOPES - {"app.admin"}
            issued = auth.browser_sessions.issue(
                user_id,
                1,
                subject=f"test:{user_id}",
                principal_kind="owner_session",
                scopes=scopes,
            )
            return AccessContext(
                UserActor(
                    user_id,
                    1,
                    f"test:{user_id}",
                    "owner_session",
                    scopes,
                    browser_session_id=issued.record_id,
                )
            )

        owner_context = actor(owner_id, admin=True)
        audit = AuditService(database, SecretRedactor(database))
        links = PublicLinkService(database, auth, audit)
        links.configure(
            owner_context, expected_revision=0, enabled=True, public_origin="https://share.example"
        )
        created = links.create_link(
            owner_context,
            target.project_id,
            label="Polish reviewer",
            mode="review_translation",
            target_artifact_id=artifact.id,
        )
        issued = links.redeem(
            created["link"]["id"], parse_qs(urlsplit(created["url"]).fragment)["token"][0]
        )
        guest = AccessContext(
            links.session_context(created["link"]["id"], issued.raw_browser_session).actor
        )
        review = SubtitleReviewService(database, artifacts, lambda _sid: target_dir)
        service = PublicTranslationReviewService(database, review, audit)
        yield SimpleNamespace(
            database=database,
            paths=paths,
            target=target,
            artifact=artifact,
            review=review,
            service=service,
            links=links,
            guest=guest,
            owner=owner_context,
            editor=actor(user_ids[0]),
            viewer=actor(user_ids[1]),
            administrator=actor(user_ids[2], admin=True),
            link_id=created["link"]["id"],
            workspace_id=workspace_id,
        )
        database.dispose()


def _save(case, **kwargs):
    preview = case.service.preview(case.guest, case.link_id)
    return case.service.submit(
        case.guest,
        case.link_id,
        expected_source_hash=preview["target_hash"],
        changes=kwargs.pop(
            "changes",
            [{"segment_id": preview["items"][0]["id"], "text": "Zażółć gęślą jaźń. 漢字"}],
        ),
        reviewer_label=kwargs.pop("reviewer_label", "Unverified name"),
        request_key=kwargs.pop("request_key", str(uuid4())),
        **kwargs,
    )


def _accept(case, saved, **kwargs):
    return case.service.accept(
        kwargs.pop("context", case.owner),
        saved["submission_id"],
        expected_revision=kwargs.pop("expected_revision", 1),
        expected_source_hash=kwargs.pop("expected_source_hash", case.artifact.content_hash),
        expected_document_revision=kwargs.pop("expected_document_revision", 1),
        expected_selected_artifact_id=kwargs.pop("expected_selected_artifact_id", case.artifact.id),
        idempotency_key=kwargs.pop("idempotency_key", str(uuid4())),
        **kwargs,
    )


def _state(case):
    with case.database.snapshot_session() as session:
        counts = [
            session.scalar(select(func.count()).select_from(model))
            for model in (Artifact, DocumentRevision, Segment)
        ]
        selected = session.get(SessionStageSelection, (case.target.id, "translate")).artifact_id
    files = {
        str(path.relative_to(case.paths.root)): path.read_bytes()
        for path in case.paths.sessions.rglob("*")
        if path.is_file()
    }
    return counts, selected, files


def test_sparse_save_has_no_publication_and_safe_paged_projection(case):
    before = _state(case)
    preview = case.service.preview(case.guest, case.link_id, limit=10000)
    assert preview["limit"] == 100 and preview["total"] == 2
    assert set(preview["items"][0]) == {"id", "ordinal", "start_ms", "end_ms", "text"}
    assert case.service.preview(case.guest, case.link_id, offset=2)["items"] == []
    saved = _save(case)
    assert saved["status"] == "pending" and saved["change_count"] == 1
    assert _state(case) == before
    detail = case.service.get_submission(case.owner, saved["submission_id"], limit=1)
    assert detail["changes"][0]["before"] == preview["items"][0]["text"]
    assert detail["changes"][0]["after"] == "Zażółć gęślą jaźń. 漢字"
    assert detail["submission"]["reviewer_label_verified"] is False
    assert (
        case.service.get_submission(case.owner, saved["submission_id"], offset=1)["changes"] == []
    )
    assert len(case.service.list_submissions(case.owner, case.target.project_id)["items"]) == 1
    serialized = json.dumps(detail)
    assert (
        "csrf" not in serialized
        and "browser_session" not in serialized
        and "token_hash" not in serialized
    )
    assert "Unverified name" not in json.dumps(case.links.audit.list())
    with pytest.raises(PublicLinkError):
        case.service.preview(case.guest, case.link_id, offset=-1)


def test_save_idempotency_payload_mismatch_and_pending_cap(case):
    key = str(uuid4())
    saved = _save(case, request_key=key)
    assert _save(case, request_key=key) == saved
    with pytest.raises(PublicLinkError) as error:
        _save(case, request_key=key, reviewer_label="Changed label")
    assert error.value.status_code == 409
    with case.database.immediate_session() as session:
        for _ in range(99):
            session.add(
                ProjectLinkSubmission(
                    link_id=case.link_id,
                    browser_session_id=case.guest.actor.browser_session_id,
                    request_key=str(uuid4()),
                    payload_hash="x",
                    changes_json=[],
                )
            )
    assert _save(case, request_key=key) == saved
    with pytest.raises(PublicLinkError) as error:
        _save(case)
    assert error.value.status_code == 429


def test_denials_happen_before_canonical_source_read(case):
    with patch.object(
        case.review, "review", side_effect=AssertionError("unauthorized source read")
    ):
        for context, link in ((case.owner, case.link_id), (case.guest, str(uuid4()))):
            with pytest.raises(PermissionError):
                case.service.preview(context, link)
        with case.database.immediate_session() as session:
            session.get(ProjectLink, case.link_id).mode = "manage_project"
        with pytest.raises(PermissionError):
            case.service.preview(case.guest, case.link_id)


@pytest.mark.parametrize("target_change", ["project", "deleted"])
def test_guest_target_lifecycle_and_project_denied(case, target_change):
    with case.database.immediate_session() as session:
        if target_change == "project":
            other_project = Project(workspace_id=case.workspace_id, name="Other project")
            session.add(other_project)
            session.flush()
            other = SessionRecord(project_id=other_project.id, name="Other session")
            session.add(other)
            session.flush()
            session.get(ProjectLink, case.link_id).target_session_id = other.id
        elif target_change == "deleted":
            session.get(Artifact, case.artifact.id).state = "deleted"
    with patch.object(
        case.review, "review", side_effect=AssertionError("unauthorized source read")
    ):
        with pytest.raises((PermissionError, PublicLinkError)):
            case.service.preview(case.guest, case.link_id)


@pytest.mark.parametrize("kind", ["unknown", "duplicate", "noop", "empty", "long", "bytes", "many"])
def test_invalid_sparse_changes(case, kind):
    item = case.service.preview(case.guest, case.link_id)["items"][0]
    changes = [{"segment_id": item["id"], "text": "New"}]
    if kind == "unknown":
        changes[0]["segment_id"] = "unknown"
    elif kind == "duplicate":
        changes *= 2
    elif kind == "noop":
        changes[0]["text"] = item["text"]
    elif kind == "empty":
        changes = []
    elif kind == "long":
        changes[0]["text"] = "x" * 20001
    elif kind == "bytes":
        changes = [{"segment_id": str(i), "text": "漢" * 20000} for i in range(4)]
    elif kind == "many":
        changes *= 5001
    with pytest.raises(PublicLinkError):
        _save(case, changes=changes)


@pytest.mark.parametrize("operator", ["owner", "editor"])
def test_accept_publishes_once_preserves_original_and_selects_translation(case, operator):
    original = case.review.review(case.target.id, [case.artifact.id])["columns"][0][
        "logical_passages"
    ]
    saved = _save(case)
    before = _state(case)
    accepted = _accept(case, saved, context=getattr(case, operator))
    after = _state(case)
    assert accepted["status"] == "accepted" and accepted["revision"] == 2
    assert after[0][0] == before[0][0] + 1 and after[0][1] == before[0][1] + 1
    assert after[1] == accepted["accepted_artifact_id"] != case.artifact.id
    assert (
        case.review.review(case.target.id, [case.artifact.id])["columns"][0]["logical_passages"]
        == original
    )
    published = case.review.review(case.target.id, [after[1]])["columns"][0]["logical_passages"]
    assert [r["id"] for r in published] == [r["id"] for r in original]
    assert [{k: v for k, v in r.items() if k != "text"} for r in published] == [
        {k: v for k, v in r.items() if k != "text"} for r in original
    ]
    assert (
        published[0]["text"] == "Zażółć gęślą jaźń. 漢字"
        and published[1]["text"] == original[1]["text"]
    )
    with patch.object(
        case.review, "prepare_passage_review", side_effect=AssertionError("duplicate preparation")
    ):
        assert _accept(case, saved, context=getattr(case, operator)) == accepted
    assert _state(case) == after
    assert case.service.get_submission(case.owner, saved["submission_id"])["submission"] == accepted
    with pytest.raises(PublicLinkError):
        case.service.reject(
            case.owner, saved["submission_id"], expected_revision=1, idempotency_key=str(uuid4())
        )


def test_ordinary_permissions_and_retained_review_after_revoke_and_feature_off(case):
    saved = _save(case)
    for context in (case.viewer, case.administrator, case.guest):
        with pytest.raises((PermissionError, KeyError)):
            _accept(case, saved, context=context)
        with pytest.raises((PermissionError, KeyError)):
            case.service.reject(
                context, saved["submission_id"], expected_revision=1, idempotency_key=str(uuid4())
            )
    for context in (case.administrator, case.guest):
        with pytest.raises((PermissionError, KeyError)):
            case.service.get_submission(context, saved["submission_id"])
    case.links.revoke_link(case.owner, case.link_id, expected_revision=1)
    case.links.configure(
        case.owner, expected_revision=1, enabled=False, public_origin="https://share.example"
    )
    with pytest.raises(PermissionError):
        case.service.preview(case.guest, case.link_id)
    assert case.service.get_submission(case.owner, saved["submission_id"])["changes"]
    assert _accept(case, saved)["status"] == "accepted"


@pytest.mark.parametrize("change", ["hash", "head", "selection", "composition", "payload_revision"])
def test_accept_stale_source_conflicts_leave_pending_and_files_unchanged(case, change):
    saved = _save(case)
    with case.database.immediate_session() as session:
        if change == "hash":
            session.get(Artifact, case.artifact.id).content_hash = "b" * 64
        elif change == "head":
            document = session.scalar(
                select(Document).where(
                    Document.session_id == case.target.id, Document.stage == "translation"
                )
            )
            document.active_revision_id = None
        elif change == "selection":
            session.get(SessionStageSelection, (case.target.id, "translate")).artifact_id = None
    if change == "composition":
        settings = WorkspaceSettingsService(case.database)
        current = settings.get(case.target.id, "subtitles")
        settings.patch(
            case.target.id,
            "subtitles",
            current["revision"],
            {"max_chars_per_line": 37},
        )
    before = _state(case)
    with pytest.raises((PublicLinkError, RuntimeError)):
        _accept(
            case,
            saved,
            **({"expected_document_revision": 2} if change == "payload_revision" else {}),
        )
    assert _state(case) == before
    with case.database.snapshot_session() as session:
        row = session.get(ProjectLinkSubmission, saved["submission_id"])
        assert row.status == "pending" and row.revision == 1


def test_reject_is_disposition_only_and_idempotent(case):
    saved = _save(case)
    before = _state(case)
    key = str(uuid4())
    result = case.service.reject(
        case.editor, saved["submission_id"], expected_revision=1, idempotency_key=key
    )
    assert result["status"] == "rejected" and result["revision"] == 2
    assert (
        case.service.reject(
            case.editor, saved["submission_id"], expected_revision=1, idempotency_key=key
        )
        == result
    )
    assert _state(case) == before
    with pytest.raises(PublicLinkError):
        _accept(case, saved)


def test_publication_rollback_and_postcommit_failure_cleanup(case):
    saved = _save(case)
    before = _state(case)

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit transaction failed")

    with patch.object(case.service, "_audit", side_effect=fail_audit):
        with pytest.raises(RuntimeError, match="audit transaction failed"):
            _accept(case, saved)
    assert _state(case) == before
    real_writer = case.database.immediate_session

    @contextmanager
    def committed_failure(**kwargs):
        with real_writer(**kwargs) as session:
            yield session
        raise RuntimeError("after commit")

    with patch.object(case.database, "immediate_session", committed_failure):
        with pytest.raises(RuntimeError, match="after commit"):
            _accept(case, saved)
    with case.database.snapshot_session() as session:
        row = session.get(ProjectLinkSubmission, saved["submission_id"])
        artifact = session.get(Artifact, row.accepted_artifact_id)
        assert row.status == "accepted"
        assert (case.paths.root / artifact.relative_path).is_file()
    assert _accept(case, saved)["accepted_artifact_id"] == artifact.id


def test_guest_can_submit_against_retained_historical_base_but_accept_conflicts(case):
    saved = _save(case)
    _accept(case, saved)
    preview = case.service.preview(case.guest, case.link_id)
    assert preview["target_artifact_id"] == case.artifact.id
    assert preview["items"][0]["text"] == "Pierwsza kwestia."
    retained = _save(case)
    before = _state(case)
    assert case.service.get_submission(case.owner, retained["submission_id"])["changes"]
    with pytest.raises(PublicLinkError):
        _accept(case, retained)
    assert _state(case) == before


@pytest.mark.parametrize("race", ["selection", "operator_membership"])
def test_accept_rechecks_selection_and_editor_authority_after_preparation(case, race):
    saved = _save(case)
    before = _state(case)
    prepare = case.review.prepare_passage_review

    def racing_prepare(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        with case.database.immediate_session() as session:
            if race == "selection":
                session.get(SessionStageSelection, (case.target.id, "translate")).artifact_id = None
            else:
                session.delete(
                    session.get(WorkspaceMembership, (case.workspace_id, case.editor.actor.user_id))
                )
        return prepared

    with patch.object(case.review, "prepare_passage_review", side_effect=racing_prepare):
        with pytest.raises((PublicLinkError, PermissionError, KeyError)):
            _accept(case, saved, context=case.editor)
    after = _state(case)
    assert after[0] == before[0] and after[2] == before[2]
    with case.database.snapshot_session() as session:
        assert session.get(ProjectLinkSubmission, saved["submission_id"]).status == "pending"


def test_submit_rechecks_guest_revocation_after_canonical_read(case):
    preview = case.service.preview(case.guest, case.link_id)
    column = case.service._column

    def revoked_column(*args, **kwargs):
        result = column(*args, **kwargs)
        case.links.revoke_link(case.owner, case.link_id, expected_revision=1)
        return result

    with patch.object(case.service, "_column", side_effect=revoked_column):
        with pytest.raises(PermissionError):
            case.service.submit(
                case.guest,
                case.link_id,
                expected_source_hash=preview["target_hash"],
                changes=[{"segment_id": preview["items"][0]["id"], "text": "New text"}],
                request_key=str(uuid4()),
            )
    with case.database.snapshot_session() as session:
        assert session.scalar(select(func.count(ProjectLinkSubmission.id))) == 0


@pytest.mark.parametrize("change", ["deleted", "hash"])
def test_retained_detail_exposes_saved_text_without_claiming_an_unavailable_diff(case, change):
    saved = _save(case)
    with case.database.immediate_session() as session:
        artifact = session.get(Artifact, case.artifact.id)
        if change == "deleted":
            artifact.state = "deleted"
        else:
            artifact.content_hash = "b" * 64
    detail = case.service.get_submission(case.owner, saved["submission_id"])
    assert detail["source_unavailable"] is True
    item = detail["changes"][0]
    assert item["after"] == "Zażółć gęślą jaźń. 漢字" and item["segment_id"]
    assert all(item[key] is None for key in ("before", "ordinal", "start_ms", "end_ms"))
    assert case.service.list_submissions(case.owner, case.target.project_id)["items"]
    with pytest.raises((PermissionError, PublicLinkError)):
        case.service.preview(case.guest, case.link_id)
    with pytest.raises(PublicLinkError):
        _accept(case, saved)


def test_retained_submission_survives_missing_target_language_session(case):
    saved = _save(case)
    with case.database.immediate_session() as session:
        session.delete(session.get(SessionRecord, case.target.id))
    detail = case.service.get_submission(case.owner, saved["submission_id"])
    assert detail["source_unavailable"] is True
    assert detail["changes"][0]["after"] == "Zażółć gęślą jaźń. 漢字"
    assert detail["changes"][0]["before"] is None
    assert case.service.list_submissions(case.owner, case.target.project_id)["items"]
    with pytest.raises(PublicLinkError):
        _accept(case, saved)
    assert (
        case.service.reject(
            case.owner, saved["submission_id"], expected_revision=1, idempotency_key=str(uuid4())
        )["status"]
        == "rejected"
    )


def test_legacy_implicit_current_selection_can_be_explicitly_accepted(case):
    saved = _save(case)
    with case.database.immediate_session() as session:
        session.delete(session.get(SessionStageSelection, (case.target.id, "translate")))
    detail = case.service.get_submission(case.owner, saved["submission_id"])
    assert detail["selected_artifact_id"] == case.artifact.id
    accepted = _accept(case, saved)
    with case.database.snapshot_session() as session:
        assert (
            session.get(SessionStageSelection, (case.target.id, "translate")).artifact_id
            == accepted["accepted_artifact_id"]
        )
