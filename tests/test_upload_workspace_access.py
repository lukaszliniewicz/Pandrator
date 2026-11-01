"""Disposable upload authority checks at admission, writer and committed cleanup."""

from __future__ import annotations

import errno
import io
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select

from pandrator.runtime import DataPaths
from pandrator.web import upload_publication, uploads
from pandrator.web.access.contracts import AccessDenied, ProjectAction
from pandrator.web.access.upload_policy import (
    require_upload_lookup_workspace,
    require_upload_maintenance_access,
)
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.accounts.execution_actor import HostActor, UserActor, execution_actor_scope
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import AuthService
from pandrator.web.database import Database
from pandrator.web.job_execution import current_job_lease
from pandrator.web.jobs import JobQueue
from pandrator.web.model_base import new_id, utcnow
from pandrator.web.models import ApiToken, Artifact, SourceAsset, UploadSessionRecord
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.sessions import SessionService
from pandrator.web.source_library import SourceLibraryService
from pandrator.web.uploads import ChunkUploadService
from tests.web_test_support import prepare_web_test_data_root

SCOPES = frozenset({"app.read", "app.write"})


@dataclass
class UploadCase:
    database: Database
    paths: DataPaths
    service: ChunkUploadService
    users: dict[str, str]
    personal: dict[str, str]
    team: str
    team_session: str
    team_project: str
    private_session: str
    private_project: str

    def actor(
        self, name: str, *, scopes: frozenset[str] = SCOPES,
        project_only: bool = False,
    ) -> UserActor:
        if name == "viewer":
            resources = CredentialResources(project_ids=frozenset({self.private_project}))
        elif project_only:
            resources = CredentialResources(project_ids=frozenset({self.team_project}))
        else:
            workspaces = {self.personal[name]}
            if name in {"owner", "member", "peer"}:
                workspaces.add(self.team)
            resources = CredentialResources(workspace_ids=frozenset(workspaces))
        token, _ = AuthService(self.database).create_api_token(
            "Disposable upload credential", user_id=self.users[name], scopes=scopes,
            resource_grants=resources,
        )
        assert token.user_id is not None and token.user_auth_revision is not None
        assert token.subject is not None and token.principal_kind == "api_token"
        return UserActor(
            user_id=token.user_id, auth_revision=token.user_auth_revision,
            subject=token.subject, principal_kind=token.principal_kind,
            scopes=scopes, token_id=token.id,
        )

    def lose_access(self, actor: UserActor, reason: str) -> None:
        with self.database.immediate_session() as unit:
            if reason == "membership":
                unit.delete(unit.get_one(WorkspaceMembership, (self.team, actor.user_id)))
            elif reason == "revoked":
                unit.get_one(ApiToken, actor.token_id).revoked_at = utcnow()
            elif reason == "disabled":
                unit.get_one(User, actor.user_id).is_active = False
            else:
                unit.get_one(UserCredential, actor.user_id).auth_revision += 1

    def publish(self, staged: Path, **kwargs: Any) -> dict[str, Any]:
        return upload_publication.publish_multipart_upload(
            staged, filename="synthetic.txt", original_filename="synthetic.txt",
            session_id=kwargs.pop("session_id", None), purpose=kwargs.pop("purpose", "source"),
            database=self.database, paths=self.paths, artifacts=self.service.artifacts,
            sources=self.service.sources, **kwargs,
        )


@pytest.fixture
def upload_case(tmp_path: Path) -> Iterator[UploadCase]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    artifacts = ArtifactService(database, paths)
    sources = SourceLibraryService(database, artifacts)
    users: dict[str, str] = {}
    personal: dict[str, str] = {}
    with database.immediate_session() as unit:
        owner = initial_personal_workspace(unit)
        assert owner.personal_owner_user_id is not None
        users["owner"] = owner.personal_owner_user_id
        personal["owner"] = owner.id
        for name in ("member", "peer", "viewer", "foreign", "foreign_admin"):
            user = User(id=new_id(), display_name=name, is_active=True,
                        is_instance_admin=name == "foreign_admin")
            unit.add(user)
            unit.flush()
            users[name] = user.id
            unit.add(UserCredential(user_id=user.id, login_name=name, auth_revision=1))
            workspace = Workspace(kind="personal", name=name, personal_owner_user_id=user.id)
            unit.add(workspace)
            unit.flush()
            personal[name] = workspace.id
            unit.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="owner"))
        team = Workspace(kind="team", name="Upload team")
        unit.add(team)
        unit.flush()
        team_id = team.id
        for name in ("owner", "member", "peer"):
            unit.add(WorkspaceMembership(
                workspace_id=team_id, user_id=users[name],
                role="owner" if name == "owner" else "member",
            ))
    sessions = SessionService(database)
    team_root = sessions.create("Upload project", workspace_id=team_id)
    private = sessions.create("Private viewed project")
    with database.immediate_session() as unit:
        unit.add(ProjectMembership(
            project_id=private.project_id, user_id=users["viewer"], role="viewer",
        ))
    try:
        yield UploadCase(
            database, paths, ChunkUploadService(database, paths, artifacts, sources),
            users, personal, team_id, team_root.id, team_root.project_id,
            private.id, private.project_id,
        )
    finally:
        database.dispose()


def _initialize(case: UploadCase, **kwargs: Any) -> str:
    return case.service.initialize(filename="private.txt", size_bytes=3, **kwargs)["id"]


def _directory(case: UploadCase, identifier: str) -> Path:
    return case.paths.temporary / "uploads" / identifier


def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("Denied upload reached files, locks or request stream")


@contextmanager
def _sql(case: UploadCase) -> Iterator[list[str]]:
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(case.database.engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(case.database.engine, "before_cursor_execute", capture)


def _no_body(statements: list[str]) -> None:
    for column in ("filename", "received_json", "result_json", "temporary_relative_path"):
        assert not any(f"upload_sessions.{column}" in sql for sql in statements)


def test_actual_personal_team_and_project_only_creation(upload_case: UploadCase) -> None:
    case = upload_case
    member = case.actor("member")
    with execution_actor_scope(member):
        personal = _initialize(case)
        team = _initialize(case, workspace_id=case.team)
    with execution_actor_scope(case.actor("member", project_only=True)):
        project = _initialize(case, session_id=case.team_session)
        with pytest.raises(KeyError):
            _initialize(case, workspace_id=case.team)
        with pytest.raises(ValueError):
            _initialize(case, session_id=case.team_session, workspace_id=case.personal["member"])
    with case.database.session() as unit:
        assert unit.get_one(UploadSessionRecord, personal).workspace_id == case.personal["member"]
        assert unit.get_one(UploadSessionRecord, team).workspace_id == case.team
        assert unit.get_one(UploadSessionRecord, project).project_id == case.team_project


@pytest.mark.parametrize("credential", ["viewer", "readonly"])
def test_read_status_cannot_write_or_replay_before_effects(
    upload_case: UploadCase, monkeypatch: pytest.MonkeyPatch, credential: str,
) -> None:
    case = upload_case
    identifier = _initialize(case, session_id=case.private_session if credential == "viewer" else case.team_session)
    actor = case.actor("viewer") if credential == "viewer" else case.actor("member", scopes=frozenset({"app.read"}))
    monkeypatch.setattr(uploads, "upload_activity", _forbidden)
    monkeypatch.setattr(uploads, "managed_upload_directory", _forbidden)
    stream = io.BytesIO(b"abc")
    with execution_actor_scope(actor):
        assert case.service.status(identifier)["state"] == "open"
        for operation in (
            lambda: case.service.write_chunk(identifier, 0, stream),
            lambda: case.service.complete(identifier),
            lambda: case.service.cancel(identifier),
        ):
            with pytest.raises((AccessDenied, KeyError)):
                operation()
    assert stream.tell() == 0


@pytest.mark.parametrize("name", ["foreign", "foreign_admin"])
def test_foreign_accounts_denied_before_upload_body(
    upload_case: UploadCase, name: str,
) -> None:
    case = upload_case
    identifier = _initialize(case, workspace_id=case.team)
    with _sql(case) as statements, execution_actor_scope(case.actor(name)):
        for operation in (case.service.status, case.service.complete, case.service.cancel):
            with pytest.raises(KeyError):
                operation(identifier)
    _no_body(statements)


def test_same_workspace_member_management_and_selector_intersection(upload_case: UploadCase) -> None:
    case = upload_case
    with execution_actor_scope(case.actor("member")):
        identifier = _initialize(case, session_id=case.team_session)
    with execution_actor_scope(case.actor("peer")):
        assert case.service.status(identifier)["id"] == identifier
        with pytest.raises(KeyError):
            case.service.status(identifier, workspace_id=case.personal["peer"])
        case.service.write_chunk(identifier, 0, io.BytesIO(b"abc"))
        result = case.service.complete(identifier)
        assert case.service.complete(identifier) == result
        canceled = _initialize(case, workspace_id=case.team)
        case.service.cancel(canceled)
    with case.database.session() as unit:
        artifact = unit.get_one(Artifact, result["artifact_id"])
        assert artifact.workspace_id == case.team and artifact.project_id == case.team_project
        assert case.paths.managed_path(artifact.relative_path).read_bytes() == b"abc"
        assert unit.get_one(UploadSessionRecord, canceled).state == "canceled"
    assert not _directory(case, identifier).exists()


@pytest.mark.parametrize("reason", ["membership", "revoked"])
def test_chunk_writer_reproof_preserves_previous_chunk(
    upload_case: UploadCase, reason: str,
) -> None:
    case = upload_case
    actor = case.actor("member")
    changed = False

    class LosingStream(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            nonlocal changed
            if not changed:
                changed = True
                case.lose_access(actor, reason)
            return super().read(size)

    with execution_actor_scope(actor):
        identifier = _initialize(case, workspace_id=case.team)
        case.service.write_chunk(identifier, 0, io.BytesIO(b"old"))
        with pytest.raises((AccessDenied, KeyError)):
            case.service.write_chunk(identifier, 0, LosingStream(b"new"))
    directory = _directory(case, identifier)
    assert [p.name for p in directory.iterdir()] == ["00000000.part"]
    assert (directory / "00000000.part").read_bytes() == b"old"
    with case.database.session() as unit:
        assert unit.get_one(UploadSessionRecord, identifier).state == "open"


@pytest.mark.parametrize("reason", ["membership", "revoked"])
def test_completion_writer_loss_preserves_chunks_and_has_no_publication(
    upload_case: UploadCase, monkeypatch: pytest.MonkeyPatch, reason: str,
) -> None:
    case = upload_case
    actor = case.actor("member")
    original = case.service.artifacts.prepare_registration

    def lose_after_preparation(*args: Any, **kwargs: Any):
        prepared = original(*args, **kwargs)
        case.lose_access(actor, reason)
        return prepared

    with execution_actor_scope(actor):
        identifier = _initialize(case, workspace_id=case.team)
        case.service.write_chunk(identifier, 0, io.BytesIO(b"abc"))
        monkeypatch.setattr(case.service.artifacts, "prepare_registration", lose_after_preparation)
        with pytest.raises((AccessDenied, KeyError)):
            case.service.complete(identifier)
    assert (_directory(case, identifier) / "00000000.part").read_bytes() == b"abc"
    assert [p.name for p in _directory(case, identifier).iterdir()] == ["00000000.part"]
    assert not list(case.paths.uploads.iterdir())
    with case.database.session() as unit:
        assert unit.scalar(select(Artifact.id)) is None
        assert unit.scalar(select(SourceAsset.id)) is None


def test_committed_cleanup_survives_role_loss_but_new_replay_denies(
    upload_case: UploadCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = upload_case
    actor = case.actor("member")
    original_writer = case.database.immediate_session
    registration = case.service.artifacts.register_in_session
    publication = False

    def registered(*args: Any, **kwargs: Any):
        nonlocal publication
        artifact = registration(*args, **kwargs)
        publication = True
        return artifact

    @contextmanager
    def lose_after_commit():
        nonlocal publication
        with original_writer() as unit:
            yield unit
        if publication:
            publication = False
            case.lose_access(actor, "membership")

    with execution_actor_scope(actor):
        identifier = _initialize(case, workspace_id=case.team)
        case.service.write_chunk(identifier, 0, io.BytesIO(b"abc"))
        monkeypatch.setattr(case.service.artifacts, "register_in_session", registered)
        monkeypatch.setattr(case.database, "immediate_session", lose_after_commit)
        result = case.service.complete(identifier)
        with pytest.raises(KeyError):
            case.service.complete(identifier)
    assert not _directory(case, identifier).exists()
    with case.database.session() as unit:
        record = unit.get_one(UploadSessionRecord, identifier)
        artifact = unit.get_one(Artifact, result["artifact_id"])
        assert record.state == "completed" and record.result_json == result
        assert case.paths.managed_path(artifact.relative_path).read_bytes() == b"abc"


def test_completed_receipt_requires_current_write_scope(
    upload_case: UploadCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = upload_case
    identifier = _initialize(case, workspace_id=case.team)
    case.service.write_chunk(identifier, 0, io.BytesIO(b"abc"), workspace_id=case.team)
    result = case.service.complete(identifier, workspace_id=case.team)
    monkeypatch.setattr(uploads, "upload_activity", _forbidden)
    with execution_actor_scope(case.actor("member", scopes=frozenset({"app.read"}))):
        assert case.service.status(identifier)["result"] == result
        with pytest.raises(AccessDenied):
            case.service.complete(identifier)


def test_bulk_maintenance_and_none_compatibility(upload_case: UploadCase) -> None:
    case = upload_case
    identifier = _initialize(case)
    with _sql(case) as statements, execution_actor_scope(case.actor("owner")):
        with pytest.raises(AccessDenied):
            case.service.cleanup_expired()
    assert not any("upload_sessions" in statement for statement in statements)
    with case.database.session() as unit, _sql(case) as statements:
        assert require_upload_lookup_workspace(unit, identifier) is None
        assert require_upload_lookup_workspace(unit, identifier, workspace_id=case.team) == case.team
        require_upload_maintenance_access(unit)
    assert statements == []
    with execution_actor_scope(HostActor("pandrator-cli")):
        assert case.service.status(identifier)["id"] == identifier
        assert case.service.cleanup_expired() == 0
    with case.database.session() as unit:
        with pytest.raises(TypeError):
            require_upload_lookup_workspace(unit, identifier, ProjectAction.RUN)


@pytest.mark.parametrize("name", ["foreign", "foreign_admin", "viewer"])
def test_multipart_denied_before_hash_or_link(
    upload_case: UploadCase, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    case = upload_case
    staged = case.paths.temporary / "staged.txt"
    staged.write_bytes(b"abc")
    monkeypatch.setattr(upload_publication, "sha256_file", _forbidden)
    monkeypatch.setattr(upload_publication.os, "link", _forbidden)
    with execution_actor_scope(case.actor(name)), pytest.raises((AccessDenied, KeyError)):
        case.publish(staged, session_id=case.private_session)
    assert staged.read_bytes() == b"abc"
    assert not list(case.paths.uploads.iterdir())


@pytest.mark.parametrize("purpose,bound", [("source", False), ("source", True), ("cover", True)])
def test_multipart_publication_uses_actual_personal_or_project_only_scope(
    upload_case: UploadCase, purpose: str, bound: bool,
) -> None:
    case = upload_case
    staged = case.paths.temporary / "staged.txt"
    staged.write_bytes(b"abc")
    actor = case.actor("member", project_only=bound)
    with execution_actor_scope(actor):
        result = case.publish(
            staged, session_id=case.team_session if bound else None, purpose=purpose,
        )
    with case.database.session() as unit:
        artifact = unit.get_one(Artifact, result["artifact_id"])
        assert artifact.workspace_id == (case.team if bound else case.personal["member"])
        assert artifact.project_id == (case.team_project if bound else None)
        assert artifact.role == ("cover" if purpose == "cover" else "upload")
        assert case.paths.managed_path(artifact.relative_path).read_bytes() == b"abc"
        if purpose == "source":
            assert unit.get_one(SourceAsset, result["source_asset_id"]).workspace_id == artifact.workspace_id
        else:
            assert result["source_asset_id"] is None
    assert staged.read_bytes() == b"abc"


@pytest.mark.parametrize("purpose,reason", [("source", "membership"), ("cover", "revoked")])
def test_multipart_copy_writer_loss_cleans_only_attempt(
    upload_case: UploadCase, monkeypatch: pytest.MonkeyPatch, purpose: str, reason: str,
) -> None:
    case = upload_case
    actor = case.actor("member")
    staged = case.paths.temporary / "staged.txt"
    staged.write_bytes(b"abc")
    retained = case.paths.uploads / "retained.txt"
    retained.write_bytes(b"retained")
    original_copy = upload_publication.copyfileobj

    def unsupported_link(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(errno.ENOSYS, "Synthetic filesystem")

    def lose_after_copy(source: Any, destination: Any) -> None:
        original_copy(source, destination)
        case.lose_access(actor, reason)

    monkeypatch.setattr(upload_publication.os, "link", unsupported_link)
    monkeypatch.setattr(upload_publication, "copyfileobj", lose_after_copy)
    with execution_actor_scope(actor), pytest.raises((AccessDenied, KeyError)):
        case.publish(staged, workspace_id=case.team, purpose=purpose)
    assert staged.read_bytes() == b"abc" and retained.read_bytes() == b"retained"
    assert list(case.paths.uploads.iterdir()) == [retained]
    with case.database.session() as unit:
        assert unit.scalar(select(Artifact.id)) is None
        assert unit.scalar(select(SourceAsset.id)) is None


@pytest.mark.parametrize("reason", ["disabled", "reset"])
def test_user_lifetime_reproof_precedes_private_status(
    upload_case: UploadCase, reason: str,
) -> None:
    case = upload_case
    actor = case.actor("member")
    with execution_actor_scope(actor):
        identifier = _initialize(case, workspace_id=case.team)
        case.lose_access(actor, reason)
        with _sql(case) as statements, pytest.raises(AccessDenied):
            case.service.status(identifier)
    _no_body(statements)


def test_proved_lease_accepts_admitted_expiry_but_rejects_revocation(
    upload_case: UploadCase,
) -> None:
    case = upload_case
    actor = case.actor("member")
    queue = JobQueue(case.database)
    with execution_actor_scope(actor):
        queued = queue.enqueue(
            "noop", {}, workspace_id=case.team,
            project_id=case.team_project, session_id=case.team_session,
        )
    claimed = queue.claim("upload-authority-worker", lease_seconds=300)
    assert claimed is not None and claimed.id == queued.id
    with case.database.immediate_session() as unit:
        unit.get_one(ApiToken, actor.token_id).expires_at = utcnow() - timedelta(seconds=1)
    with execution_actor_scope(actor):
        with pytest.raises(AccessDenied):
            _initialize(case, session_id=case.team_session)
        lease = current_job_lease.set({
            "job_id": claimed.id, "owner_id": "upload-authority-worker",
            "lease_generation": claimed.lease_generation,
        })
        try:
            identifier = _initialize(case, session_id=case.team_session)
            case.service.write_chunk(identifier, 0, io.BytesIO(b"abc"))
            assert case.service.status(identifier)["received"] == [0]
            case.lose_access(actor, "revoked")
            with pytest.raises(AccessDenied):
                case.service.complete(identifier)
        finally:
            current_job_lease.reset(lease)
    assert (_directory(case, identifier) / "00000000.part").read_bytes() == b"abc"
