"""Disposable non-HTTP tests for the account-bound project-link foundation."""

from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlsplit

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, select, text

from pandrator.web.access.contracts import AccessContext, ProjectAction
from pandrator.web.access.project_policy import require_project_access
from pandrator.web.accounts.actor_authorization import require_actor_in_session
from pandrator.web.accounts.auth_contracts import ALL_SCOPES
from pandrator.web.accounts.auth_models import BrowserAuthSession, UserCredential
from pandrator.web.accounts.execution_actor import UserActor
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import (
    BrowserAuthProjectGrant,
    BrowserAuthWorkspaceGrant,
)
from pandrator.web.audit import AuditService
from pandrator.web.auth import AuthService
from pandrator.web.credentials import SecretRedactor
from pandrator.web.database import Database
from pandrator.web.model_base import Base, utcnow
from pandrator.web.models import (
    Artifact,
    Document,
    DocumentRevision,
    SessionRecord,
    SessionStageSelection,
)
from pandrator.web.projects.models import Project
from pandrator.web.public_links.authorization import aware
from pandrator.web.public_links.contracts import LINK_SCOPES, PublicLinkError
from pandrator.web.public_links.models import ProjectLink, ProjectLinkSubmission
from pandrator.web.public_links.service import PublicLinkService, digest


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
def schema_template(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(dir=tmp_path_factory.mktemp("project-link-schema")) as directory:
        path = Path(directory) / "schema.sqlite3"
        database = Database(path)
        Base.metadata.create_all(database.engine)
        database.engine.dispose()
        yield path


@pytest.fixture
def case(schema_template: Path, tmp_path: Path) -> Iterator[Case]:
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
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


def enable(case: Case) -> None:
    case.service.configure(
        case.admin, expected_revision=0, enabled=True, public_origin="https://share.example"
    )


def create(case: Case, **kwargs: Any) -> dict[str, Any]:
    return case.service.create_link(
        case.admin,
        case.project_id,
        label="Reviewer",
        mode=kwargs.pop("mode", "manage_project"),
        **kwargs,
    )


def redeem(case: Case, created: dict[str, Any]):
    secret = parse_qs(urlsplit(created["url"]).fragment)["token"][0]
    return case.service.redeem(created["link"]["id"], secret)


def test_off_default_admin_config_cas_and_no_grants(case: Case) -> None:
    assert case.service.status() == {"enabled": False, "configured": False, "revision": 0}
    assert case.service.settings(case.admin) == {
        "enabled": False,
        "public_origin": None,
        "revision": 0,
    }
    with pytest.raises(PublicLinkError, match="disabled"):
        create(case)
    enable(case)
    with pytest.raises(PublicLinkError) as error:
        enable(case)
    assert error.value.status_code == 409
    with case.database.snapshot_session() as session:
        assert session.scalar(select(ProjectLink.id)) is None
    with case.database.immediate_session() as session:
        session.get(User, case.outsider.actor.user_id).is_instance_admin = False
    with pytest.raises(PermissionError):
        case.service.settings(case.outsider)


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "https://example.com/",
        "https://user@example.com",
        "https://example.com?x=1",
        "https://example.com#x",
        "https://example.com/path",
        "https://example.com:0",
        "https://example.com\\evil",
        " https://example.com",
        "https://%65xample.com",
    ],
)
def test_invalid_origins(case: Case, origin: str) -> None:
    with pytest.raises(PublicLinkError):
        case.service.configure(case.admin, expected_revision=0, enabled=True, public_origin=origin)


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
        "https://share.example",
    ],
)
def test_origins(case: Case, origin: str) -> None:
    assert (
        case.service.configure(case.admin, expected_revision=0, enabled=True, public_origin=origin)[
            "public_origin"
        ]
        == origin
    )


def test_secret_digest_no_leak_idempotency_and_no_admin_bypass(case: Case) -> None:
    enable(case)
    created = create(case, request_key="creation")
    secret = parse_qs(urlsplit(created["url"]).fragment)["token"][0]
    repeated = create(case, request_key="creation")
    assert repeated["already_created"] and repeated["url"] is None
    with pytest.raises(PublicLinkError) as error:
        create(case, request_key="creation", expires_in_days=2)
    assert error.value.status_code == 409
    metadata = json.dumps(case.service.list_links(case.admin, case.project_id))
    assert secret not in metadata and "token_hash" not in metadata and "url" not in metadata
    with case.database.snapshot_session() as session:
        assert session.get(ProjectLink, created["link"]["id"]).token_hash == digest(secret)
    assert secret not in json.dumps(case.service.audit.list())
    with pytest.raises((PermissionError, KeyError)):
        case.service.create_link(case.outsider, case.project_id, label="No", mode="manage_project")


@pytest.mark.parametrize("mode", list(LINK_SCOPES))
def test_modes_one_project_ceiling_and_session_binding(case: Case, mode: str) -> None:
    enable(case)
    created = create(
        case,
        mode=mode,
        **({"target_artifact_id": case.translation_id} if mode == "review_translation" else {}),
    )
    issued = redeem(case, created)
    context = case.service.session_context(created["link"]["id"], issued.raw_browser_session)
    assert context.actor.scopes == LINK_SCOPES[mode]
    assert issued.raw_browser_session not in repr(issued) and issued.csrf_token not in repr(issued)
    assert context.csrf_hash == digest(issued.csrf_token)
    with case.database.snapshot_session() as session:
        require_actor_in_session(session, context.actor)
        assert list(
            session.scalars(
                select(BrowserAuthProjectGrant.project_id).where(
                    BrowserAuthProjectGrant.browser_session_id == context.actor.browser_session_id
                )
            )
        ) == [case.project_id]
        assert (
            session.scalar(
                select(BrowserAuthWorkspaceGrant.workspace_id).where(
                    BrowserAuthWorkspaceGrant.browser_session_id == context.actor.browser_session_id
                )
            )
            is None
        )
        browser = session.get(BrowserAuthSession, context.actor.browser_session_id)
        assert aware(browser.expires_at) <= aware(
            session.get(ProjectLink, created["link"]["id"]).expires_at
        )
        assert aware(browser.idle_expires_at) - aware(browser.last_seen_at) <= timedelta(hours=1)
        assert aware(browser.expires_at) - aware(browser.created_at) <= timedelta(hours=24)
        require_project_access(
            session, AccessContext(context.actor), case.project_id, ProjectAction.READ
        )
        with pytest.raises((PermissionError, KeyError)):
            require_project_access(session, AccessContext(context.actor), case.other_project_id)
    other = create(case)
    with pytest.raises(PublicLinkError):
        case.service.session_context(other["link"]["id"], issued.raw_browser_session)
    with pytest.raises(PublicLinkError):
        case.service.session_context(created["link"]["id"], "invalid")
    with pytest.raises(PermissionError):
        case.service.settings(AccessContext(context.actor))
    with pytest.raises(PermissionError):
        create_with_guest = case.service.create_link(
            AccessContext(context.actor), case.project_id, label="No", mode=mode
        )
        assert create_with_guest is None


def test_target_hash_selection_and_project_validation(case: Case) -> None:
    enable(case)
    with pytest.raises(PublicLinkError):
        create(case, mode="review_translation")
    created = create(case, mode="review_translation", target_artifact_id=case.translation_id)
    assert created["link"]["target_revision_number"] == 1
    with case.database.immediate_session() as session:
        artifact = session.get(Artifact, case.translation_id)
        artifact.content_hash = "b" * 64
    # Artifact/file hashes and normalized-revision hashes are independent.
    with pytest.raises(PublicLinkError):
        redeem(case, created)
    with case.database.immediate_session() as session:
        artifact = session.get(Artifact, case.translation_id)
        artifact.content_hash = "a" * 64
        session.get(SessionStageSelection, (artifact.session_id, "translate")).artifact_id = None
    with pytest.raises(PublicLinkError):
        create(case, mode="review_translation", target_artifact_id=case.translation_id)


@pytest.mark.parametrize(
    "change",
    [
        "revoke",
        "disable",
        "expire",
        "downgrade",
        "auth_revision",
        "archive",
        "trash",
        "root",
        "workspace",
    ],
)
def test_captured_actor_denies_after_lifecycle_change(case: Case, change: str) -> None:
    enable(case)
    created = create(case)
    issued = redeem(case, created)
    context = case.service.session_context(created["link"]["id"], issued.raw_browser_session)
    if change == "revoke":
        case.service.revoke_link(case.admin, created["link"]["id"], expected_revision=1)
    elif change == "disable":
        case.service.configure(
            case.admin, expected_revision=1, enabled=False, public_origin="https://share.example"
        )
        case.service.configure(
            case.admin, expected_revision=2, enabled=True, public_origin="https://share.example"
        )
    else:
        with case.database.immediate_session() as session:
            if change == "expire":
                session.get(ProjectLink, created["link"]["id"]).expires_at = utcnow() - timedelta(
                    seconds=1
                )
            elif change == "downgrade":
                session.get(
                    WorkspaceMembership, (case.workspace_id, case.admin.actor.user_id)
                ).role = "member"
            elif change == "auth_revision":
                session.get(UserCredential, case.admin.actor.user_id).auth_revision += 1
            elif change == "archive":
                session.get(Workspace, case.workspace_id).state = "archived"
            elif change == "trash":
                session.get(SessionRecord, case.source_id).trashed_at = utcnow()
            elif change == "root":
                session.get(Project, case.project_id).source_session_id = None
            elif change == "workspace":
                other = Workspace(kind="team", name="Other")
                session.add(other)
                session.flush()
                session.add(
                    WorkspaceMembership(
                        workspace_id=other.id, user_id=case.admin.actor.user_id, role="owner"
                    )
                )
                session.get(Project, case.project_id).workspace_id = other.id
    with case.database.snapshot_session() as session:
        with pytest.raises(PermissionError):
            require_actor_in_session(session, context.actor)
        if change in {"revoke", "disable"}:
            assert (
                session.get(BrowserAuthSession, context.actor.browser_session_id).revoked_at
                is not None
            )
    with pytest.raises(PublicLinkError):
        case.service.session_context(created["link"]["id"], issued.raw_browser_session)


def test_normal_user_has_no_link_queries(case: Case) -> None:
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(case.database.engine, "before_cursor_execute", capture)
    try:
        with case.database.snapshot_session() as session:
            assert require_actor_in_session(session, case.admin.actor) is not None
    finally:
        event.remove(case.database.engine, "before_cursor_execute", capture)
    assert not any(
        "project_link" in statement or "public.links" in statement for statement in statements
    )


def migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "pandrator/web/migrations/versions/0087_public_project_links.py"
    )
    spec = importlib.util.spec_from_file_location("migration0087", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def migration_state(database: Database):
    with database.engine.connect() as connection:
        schema = tuple(
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                )
            ).all()
        )
        tables = [
            str(row[0])
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ).all()
        ]
        rows = {
            table: tuple(
                sorted(
                    (tuple(row) for row in connection.exec_driver_sql(f'SELECT * FROM "{table}"')),
                    key=repr,
                )
            )
            for table in tables
        }
    return schema, rows


def test_migration_reconciles_bootstrap_and_refuses_live_downgrade(case: Case) -> None:
    module = migration()
    with (
        case.database.engine.begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        module.upgrade()
    enable(case)
    create(case)
    with pytest.raises(RuntimeError, match="live project links"):
        with (
            case.database.engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            module.downgrade()
    case.service.configure(case.admin, expected_revision=1, enabled=False, public_origin=None)
    with (
        case.database.engine.begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        module.downgrade()
        module.upgrade()
        assert connection.execute(text("SELECT COUNT(*) FROM project_links")).scalar_one() == 0
        assert connection.execute(text("PRAGMA foreign_key_check")).first() is None


@pytest.mark.parametrize("status", ["pending", "accepted", "rejected"])
@pytest.mark.parametrize("link_state", ["expired", "revoked", "expired_and_revoked"])
def test_migration_refuses_retained_submissions_for_inactive_links(
    case: Case, status: str, link_state: str
) -> None:
    module = migration()
    with case.database.engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": module.revision},
        )

    enable(case)
    created = create(case)
    now = utcnow()
    with case.database.immediate_session() as session:
        link = session.get(ProjectLink, created["link"]["id"])
        assert link is not None
        if link_state in {"expired", "expired_and_revoked"}:
            link.expires_at = now - timedelta(seconds=1)
        if link_state in {"revoked", "expired_and_revoked"}:
            link.revoked_at = now
        session.add(
            ProjectLinkSubmission(
                link_id=link.id,
                changes_json=[],
                request_key="retained-submission",
                payload_hash="b" * 64,
                revision=1,
                status=status,
            )
        )

    before = migration_state(case.database)
    with pytest.raises(RuntimeError, match=r"Cannot discard retained translation submissions\."):
        with (
            case.database.engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            module.downgrade()
    assert migration_state(case.database) == before


def test_unbound_credential_and_tampered_grants_fail_closed(case: Case) -> None:
    enable(case)
    created = create(case)
    issued = redeem(case, created)
    context = case.service.session_context(created["link"]["id"], issued.raw_browser_session)
    with case.database.immediate_session() as session:
        session.add(
            BrowserAuthWorkspaceGrant(
                browser_session_id=context.actor.browser_session_id, workspace_id=case.workspace_id
            )
        )
    with case.database.snapshot_session() as session:
        with pytest.raises(PermissionError):
            require_actor_in_session(session, context.actor)
    from pandrator.web.accounts.credential_resources import CredentialResources

    unbound = case.service.auth.browser_sessions.issue(
        case.admin.actor.user_id,
        1,
        subject=f"project-link:{created['link']['id']}",
        principal_kind="project_link",
        scopes=LINK_SCOPES["manage_project"],
        resource_grants=CredentialResources(project_ids=frozenset({case.project_id})),
    )
    with pytest.raises(PublicLinkError):
        case.service.session_context(created["link"]["id"], unbound.raw_token)


def test_redemption_race_revokes_unbound_issued_session(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable(case)
    created = create(case)
    original = case.service.auth.browser_sessions.issue
    issued_id = []

    def race(*args: Any, **kwargs: Any):
        issued = original(*args, **kwargs)
        issued_id.append(issued.record_id)
        case.service.revoke_link(case.admin, created["link"]["id"], expected_revision=1)
        return issued

    monkeypatch.setattr(case.service.auth.browser_sessions, "issue", race)
    with pytest.raises(PublicLinkError):
        redeem(case, created)
    with case.database.snapshot_session() as session:
        assert session.get(BrowserAuthSession, issued_id[0]).revoked_at is not None


def test_disable_rolls_back_links_sessions_and_settings_if_audit_fails(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable(case)
    created = create(case)
    issued = redeem(case, created)

    def failure(*_args: Any, **_kwargs: Any):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(case.service.audit, "record_command_in_session", failure)
    with pytest.raises(RuntimeError, match="synthetic"):
        case.service.configure(case.admin, expected_revision=1, enabled=False, public_origin=None)
    assert case.service.status() == {"enabled": True, "configured": True, "revision": 1}
    assert case.service.session_context(created["link"]["id"], issued.raw_browser_session).actor


@pytest.mark.parametrize("days", [0, -1, 31, True])
def test_invalid_expiry(case: Case, days: int) -> None:
    enable(case)
    with pytest.raises(PublicLinkError):
        create(case, expires_in_days=days)


def test_stale_revoke_and_scoped_manager_create_ceiling(case: Case) -> None:
    enable(case)
    created = create(case)
    with pytest.raises(PublicLinkError) as error:
        case.service.revoke_link(case.admin, created["link"]["id"], expected_revision=2)
    assert error.value.status_code == 409
    from dataclasses import replace

    limited = AccessContext(replace(case.admin.actor, scopes=frozenset({"app.read", "app.write"})))
    with pytest.raises(PermissionError):
        case.service.create_link(
            limited, case.project_id, label="No run grant", mode="manage_project"
        )
    review = case.service.create_link(
        limited,
        case.project_id,
        label="Review",
        mode="review_translation",
        target_artifact_id=case.translation_id,
    )
    assert review["link"]["target_composition_hash"]


def test_real_subtitle_publication_pins_file_hash_and_immutable_revision(case: Case) -> None:
    from pandrator.runtime import DataPaths
    from pandrator.web.accounts.execution_actor import execution_actor_scope
    from pandrator.web.artifacts import ArtifactService
    from pandrator.web.subtitle_review import SubtitleReviewService

    paths = DataPaths(case.database.path.parent)
    with case.database.immediate_session() as session:
        language = SessionRecord(
            project_id=case.project_id,
            name="Real translation",
            workflow_kind="subtitles",
            target_language="pl",
        )
        session.add(language)
        session.flush()
        language_id, storage_key = language.id, language.storage_key
    session_dir = paths.sessions / storage_key
    session_dir.mkdir(parents=True)
    subtitles = SubtitleReviewService(
        case.database, ArtifactService(case.database, paths), lambda _id: session_dir
    )
    with execution_actor_scope(case.admin.actor):
        result = subtitles.save_review(
            language_id,
            "translation",
            0,
            [{"start_ms": 0, "end_ms": 1500, "text": "Published subtitle", "speaker": None}],
        )
    with case.database.snapshot_session() as session:
        artifact = session.get(Artifact, result["artifact_id"])
        revision = session.get(DocumentRevision, artifact.metadata_json["revision_id"])
        assert artifact.content_hash != revision.content_hash
        assert artifact.content_hash == digest(
            (paths.root / artifact.relative_path).read_text(encoding="utf-8")
        )
        artifact_hash = artifact.content_hash
    enable(case)
    created = create(case, mode="review_translation", target_artifact_id=result["artifact_id"])
    assert created["link"]["target_hash"] == artifact_hash
    issued = redeem(case, created)
    assert case.service.session_context(created["link"]["id"], issued.raw_browser_session)
    # Owner publication makes the old review stale, while its pinned source remains readable.
    with execution_actor_scope(case.admin.actor):
        subtitles.save_review(
            language_id,
            "translation",
            1,
            [{"start_ms": 0, "end_ms": 1500, "text": "Owner changed subtitle", "speaker": None}],
            source_artifact_id=result["artifact_id"],
            expected_source_hash=artifact_hash,
        )
    assert case.service.session_context(created["link"]["id"], issued.raw_browser_session)
    with pytest.raises(PublicLinkError):
        create(case, mode="review_translation", target_artifact_id=result["artifact_id"])
