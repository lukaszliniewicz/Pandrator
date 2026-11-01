"""Disposable-database checks for scoped knowledge ledger access."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from pandrator.web.access.contracts import (
    AccessContext,
    AccessDenied,
)
from pandrator.web.access.session_policy import SessionNotFound
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.execution_actor import (
    HostActor,
    UserActor,
    execution_actor_scope,
)
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.database import Database
from pandrator.web.knowledge import KnowledgeLedgerStore, KnowledgeValidationError
from pandrator.web.knowledge_access import (
    KnowledgeLedgerNotFound,
    KnowledgeLedgerRevisionConflict,
    ensure_ledger,
    read_ledger,
    replace_ledger,
)
from pandrator.web.model_base import Base, new_id, utcnow
from pandrator.web.models import ApiToken, KnowledgeLedger, SessionRecord
from pandrator.web.projects.identity import create_session_project
from pandrator.web.projects.models import ProjectMembership

ALL_SCOPES = frozenset({"app.read", "app.write", "app.run", "app.cancel"})
ADMIN_SCOPES = frozenset({"app.read", "app.admin"})
READ_ONLY_SCOPES = frozenset({"app.read"})


@dataclass
class KnowledgeCase:
    database: Database
    user_ids: dict[str, str]
    contexts: dict[str, AccessContext]
    token_ids: dict[str, str]
    team_workspace_id: str
    team_session_id: str
    personal_session_id: str


def _add_user(
    session: Session,
    key: str,
    *,
    instance_admin: bool = False,
) -> str:
    user_id = new_id()
    session.add(
        User(
            id=user_id,
            display_name=f"Knowledge {key}",
            is_active=True,
            is_instance_admin=instance_admin,
        )
    )
    session.flush()
    session.add(
        UserCredential(
            user_id=user_id,
            login_name=f"knowledge-{key}-{user_id[:8]}",
            password_hash=None,
            auth_revision=1,
        )
    )
    session.flush()
    return user_id


def _add_actor(
    session: Session,
    user_id: str,
    *,
    scopes: frozenset[str] = ALL_SCOPES,
    workspace_ids: tuple[str, ...] = (),
) -> tuple[AccessContext, str]:
    credential = session.get(UserCredential, user_id)
    assert credential is not None
    token_id = new_id()
    subject = f"knowledge-access:{user_id}"
    session.add(
        ApiToken(
            id=token_id,
            label="Knowledge access test token",
            token_hash=hashlib.sha256(token_id.encode("ascii")).hexdigest(),
            token_prefix=token_id[:12],
            subject=subject,
            scopes_json=sorted(scopes),
            principal_kind="api_token",
            user_id=user_id,
            user_auth_revision=credential.auth_revision,
            created_by="knowledge-access-test",
        )
    )
    session.flush()
    session.add_all(
        ApiTokenWorkspaceGrant(token_id=token_id, workspace_id=workspace_id)
        for workspace_id in workspace_ids
    )
    session.flush()
    return (
        AccessContext(
            UserActor(
                user_id=user_id,
                auth_revision=credential.auth_revision,
                subject=subject,
                principal_kind="api_token",
                scopes=scopes,
                token_id=token_id,
            )
        ),
        token_id,
    )


def _add_project_session(session: Session, workspace_id: str, name: str) -> tuple[str, str]:
    record = SessionRecord(id=new_id(), name=name)
    project = create_session_project(session, record, workspace_id=workspace_id)
    return project.id, record.id


@pytest.fixture
def knowledge_case(tmp_path: Path) -> Iterator[KnowledgeCase]:
    database = Database(tmp_path / "knowledge-access.sqlite3")
    Base.metadata.create_all(database.engine)
    users: dict[str, str] = {}
    contexts: dict[str, AccessContext] = {}
    token_ids: dict[str, str] = {}
    team_workspace_id = new_id()
    personal_workspace_id = new_id()
    foreign_workspace_id = new_id()
    with database.immediate_session() as session:
        for key in (
            "owner",
            "member",
            "viewer",
            "foreign_admin",
            "foreign",
            "narrowed",
            "resource_limited",
            "revoked",
            "disabled",
            "solo_owner",
        ):
            users[key] = _add_user(
                session,
                key,
                instance_admin=key == "foreign_admin",
            )

        team = Workspace(
            id=team_workspace_id,
            kind="team",
            name="Knowledge team",
            state="active",
        )
        personal = Workspace(
            id=personal_workspace_id,
            kind="personal",
            name="Solo owner workspace",
            personal_owner_user_id=users["solo_owner"],
            state="active",
        )
        foreign = Workspace(
            id=foreign_workspace_id,
            kind="team",
            name="Foreign administrator workspace",
            state="active",
        )
        session.add_all([team, personal, foreign])
        session.flush()
        session.add_all(
            [
                WorkspaceMembership(
                    workspace_id=team_workspace_id,
                    user_id=users["owner"],
                    role="owner",
                ),
                WorkspaceMembership(
                    workspace_id=team_workspace_id,
                    user_id=users["member"],
                    role="member",
                ),
                WorkspaceMembership(
                    workspace_id=team_workspace_id,
                    user_id=users["narrowed"],
                    role="member",
                ),
                WorkspaceMembership(
                    workspace_id=team_workspace_id,
                    user_id=users["resource_limited"],
                    role="member",
                ),
                WorkspaceMembership(
                    workspace_id=team_workspace_id,
                    user_id=users["revoked"],
                    role="member",
                ),
                WorkspaceMembership(
                    workspace_id=team_workspace_id,
                    user_id=users["disabled"],
                    role="member",
                ),
                WorkspaceMembership(
                    workspace_id=personal_workspace_id,
                    user_id=users["solo_owner"],
                    role="owner",
                ),
                WorkspaceMembership(
                    workspace_id=foreign_workspace_id,
                    user_id=users["foreign_admin"],
                    role="admin",
                ),
            ]
        )
        session.flush()
        team_project_id, team_session_id = _add_project_session(
            session,
            team_workspace_id,
            "Team source session",
        )
        _personal_project_id, personal_session_id = _add_project_session(
            session,
            personal_workspace_id,
            "Solo source session",
        )
        session.add(
            ProjectMembership(
                project_id=team_project_id,
                user_id=users["viewer"],
                role="viewer",
                granted_by_user_id=users["owner"],
            )
        )
        session.flush()

        for key, user_id, scopes, workspace_ids in (
            ("owner", users["owner"], ALL_SCOPES, (team_workspace_id,)),
            ("member", users["member"], ALL_SCOPES, (team_workspace_id,)),
            ("viewer", users["viewer"], ALL_SCOPES, (team_workspace_id,)),
            (
                "foreign_admin",
                users["foreign_admin"],
                ADMIN_SCOPES,
                (foreign_workspace_id,),
            ),
            ("foreign", users["foreign"], ALL_SCOPES, ()),
            ("narrowed", users["narrowed"], READ_ONLY_SCOPES, (team_workspace_id,)),
            ("resource_limited", users["resource_limited"], ALL_SCOPES, ()),
            ("revoked", users["revoked"], ALL_SCOPES, (team_workspace_id,)),
            ("disabled", users["disabled"], ALL_SCOPES, (team_workspace_id,)),
            ("solo_owner", users["solo_owner"], ALL_SCOPES, (personal_workspace_id,)),
        ):
            contexts[key], token_ids[key] = _add_actor(
                session,
                user_id,
                scopes=scopes,
                workspace_ids=workspace_ids,
            )

    case = KnowledgeCase(
        database=database,
        user_ids=users,
        contexts=contexts,
        token_ids=token_ids,
        team_workspace_id=team_workspace_id,
        team_session_id=team_session_id,
        personal_session_id=personal_session_id,
    )
    try:
        yield case
    finally:
        database.dispose()


def _count_ledgers(session: Session, session_id: str) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(KnowledgeLedger)
            .where(KnowledgeLedger.session_id == session_id)
        )
        or 0
    )


def _owner_ledger(case: KnowledgeCase, *, kind: str = "research") -> dict[str, Any]:
    with case.database.immediate_session() as session:
        ledger, _created = ensure_ledger(
            session,
            case.contexts["owner"],
            case.team_session_id,
            kind,
        )
    return ledger


def test_owner_member_and_personal_owner_can_read_and_edit_with_current_credentials(
    knowledge_case: KnowledgeCase,
) -> None:
    case = knowledge_case
    with case.database.immediate_session() as session:
        owner_ledger, created = ensure_ledger(
            session,
            case.contexts["owner"],
            case.team_session_id,
            " Research ",
            source_language=" DE ",
            target_language="EN",
        )
        assert created is True
        assert owner_ledger["kind"] == "research"
        assert owner_ledger["source_language"] == "de"
        assert owner_ledger["target_language"] == "en"

    with case.database.immediate_session() as session:
        same_ledger, created = ensure_ledger(
            session,
            case.contexts["member"],
            case.team_session_id,
            "research",
            source_language="de",
            target_language="en",
        )
        assert created is False
        assert same_ledger["id"] == owner_ledger["id"]
        updated = replace_ledger(
            session,
            case.contexts["member"],
            owner_ledger["id"],
            expected_revision=1,
            payload={
                "evidence": [{"recommendation": "Retain the original name."}],
                "warnings": ["  check   dialect  "],
                "summary": "  Keep  context.  ",
            },
        )
    assert set(updated) == {"id", "payload", "revision", "updated_at"}
    assert updated["payload"] == {
        "evidence": [{"recommendation": "Retain the original name."}],
        "warnings": ["check dialect"],
        "summary": "Keep context.",
    }
    assert updated["revision"] == 2
    assert isinstance(updated["updated_at"], str)

    with case.database.snapshot_session() as session:
        read_back = read_ledger(
            session,
            case.contexts["owner"],
            case.team_session_id,
            "research",
            source_language="de",
            target_language="en",
        )
    assert set(read_back) == {
        "id",
        "session_id",
        "kind",
        "source_language",
        "target_language",
        "payload",
        "revision",
        "updated_at",
    }
    assert read_back["id"] == owner_ledger["id"]
    assert read_back["revision"] == 2
    assert read_back["payload"] == updated["payload"]

    with case.database.immediate_session() as session:
        solo_ledger, created = ensure_ledger(
            session,
            case.contexts["solo_owner"],
            case.personal_session_id,
            "glossary",
        )
        assert created is True
        assert solo_ledger["payload"] == {"entries": [], "conflicts": []}
        solo_update = replace_ledger(
            session,
            case.contexts["solo_owner"],
            solo_ledger["id"],
            expected_revision=1,
            payload={"entries": [{"source": "Hallo", "target": "Hello"}]},
        )
    assert solo_update["revision"] == 2
    assert solo_update["payload"] == {
        "entries": [
            {
                "source": "Hallo",
                "target": "Hello",
                "locked": False,
                "status": "active",
            }
        ],
        "conflicts": [],
    }


def test_missing_read_is_virtual_normalized_and_does_not_flush_or_insert(
    knowledge_case: KnowledgeCase,
) -> None:
    case = knowledge_case
    pending_user_id = new_id()
    with case.database.snapshot_session() as session:
        pending_user = User(id=pending_user_id, display_name="Pending unrelated user")
        session.add(pending_user)
        result = read_ledger(
            session,
            case.contexts["member"],
            case.team_session_id,
            " GlOsSaRy ",
            source_language="",
            target_language=" PT-BR ",
        )
        assert result == {
            "id": None,
            "session_id": case.team_session_id,
            "kind": "glossary",
            "source_language": "auto",
            "target_language": "pt-br",
            "payload": {"entries": [], "conflicts": []},
            "revision": 0,
            "updated_at": None,
        }
        assert pending_user in session.new
        with session.no_autoflush:
            assert _count_ledgers(session, case.team_session_id) == 0
            assert session.scalar(select(User.id).where(User.id == pending_user_id)) is None


def test_viewer_reads_existing_ledger_but_cannot_initialize_or_replace(
    knowledge_case: KnowledgeCase,
) -> None:
    case = knowledge_case
    ledger = _owner_ledger(case)
    with case.database.snapshot_session() as session:
        viewed = read_ledger(
            session,
            case.contexts["viewer"],
            case.team_session_id,
            "research",
        )
    assert viewed["id"] == ledger["id"]
    assert viewed["revision"] == 1

    with case.database.immediate_session() as session:
        with pytest.raises(AccessDenied):
            ensure_ledger(
                session,
                case.contexts["viewer"],
                case.team_session_id,
                "research",
            )
        with pytest.raises(AccessDenied):
            replace_ledger(
                session,
                case.contexts["viewer"],
                ledger["id"],
                expected_revision=1,
                payload=cast(Mapping[str, Any], None),
            )

    with case.database.snapshot_session() as session:
        unchanged = read_ledger(
            session,
            case.contexts["owner"],
            case.team_session_id,
            "research",
        )
    assert unchanged["revision"] == 1
    assert unchanged["payload"] == {"evidence": [], "warnings": [], "summary": ""}


def test_ensure_retry_reuses_one_id_and_canonical_empty_payload(
    knowledge_case: KnowledgeCase,
) -> None:
    case = knowledge_case
    with case.database.immediate_session() as session:
        first, created_first = ensure_ledger(
            session,
            case.contexts["owner"],
            case.team_session_id,
            " glossary ",
            source_language="DE",
            target_language="FR",
        )
        second, created_second = ensure_ledger(
            session,
            case.contexts["member"],
            case.team_session_id,
            "glossary",
            source_language="de",
            target_language="fr",
        )
        assert created_first is True
        assert created_second is False
        assert first["id"] == second["id"]
        assert first["revision"] == second["revision"] == 1
        assert first["payload"] == {"entries": [], "conflicts": []}
        assert isinstance(first["updated_at"], str)
        assert _count_ledgers(session, case.team_session_id) == 1
    with case.database.snapshot_session() as session:
        persisted = read_ledger(
            session,
            case.contexts["member"],
            case.team_session_id,
            "glossary",
            source_language="de",
            target_language="fr",
        )
        assert persisted["updated_at"] == first["updated_at"]
        empty = read_ledger(
            session,
            case.contexts["member"],
            case.team_session_id,
            "research",
            source_language="   ",
        )
        assert empty["source_language"] == "auto" and empty["id"] is None


def test_initialization_and_update_roll_back_with_the_caller_transaction(
    knowledge_case: KnowledgeCase,
) -> None:
    case = knowledge_case
    with pytest.raises(RuntimeError, match="abort caller transaction"):
        with case.database.immediate_session() as session:
            ensure_ledger(
                session,
                case.contexts["owner"],
                case.team_session_id,
                "research",
            )
            raise RuntimeError("abort caller transaction")
    with case.database.snapshot_session() as session:
        assert _count_ledgers(session, case.team_session_id) == 0

    ledger = _owner_ledger(case)
    with pytest.raises(RuntimeError, match="abort caller transaction"):
        with case.database.immediate_session() as session:
            replace_ledger(
                session,
                case.contexts["member"],
                ledger["id"],
                expected_revision=1,
                payload={"evidence": [{"recommendation": "uncommitted"}]},
            )
            raise RuntimeError("abort caller transaction")
    with case.database.snapshot_session() as session:
        unchanged = read_ledger(
            session,
            case.contexts["owner"],
            case.team_session_id,
            "research",
        )
    assert unchanged["revision"] == 1
    assert unchanged["payload"] == {"evidence": [], "warnings": [], "summary": ""}


def test_stale_writers_use_revision_conflict_without_changing_the_winner(
    knowledge_case: KnowledgeCase,
) -> None:
    case = knowledge_case
    ledger = _owner_ledger(case)
    with case.database.immediate_session() as session:
        winning = replace_ledger(
            session,
            case.contexts["member"],
            ledger["id"],
            expected_revision=1,
            payload={"evidence": [{"recommendation": "first writer"}]},
        )
    assert winning["revision"] == 2

    with case.database.immediate_session() as session:
        with pytest.raises(KnowledgeLedgerRevisionConflict) as stale:
            replace_ledger(
                session,
                case.contexts["owner"],
                ledger["id"],
                expected_revision=1,
                payload={"evidence": [{"recommendation": "stale writer"}]},
            )
    assert stale.value.args == ("The knowledge ledger changed in another client.",)
    with case.database.snapshot_session() as session:
        result = read_ledger(
            session,
            case.contexts["owner"],
            case.team_session_id,
            "research",
        )
    assert result["revision"] == 2
    assert result["payload"] == {
        "evidence": [{"recommendation": "first writer"}],
        "warnings": [],
        "summary": "",
    }


def test_foreign_admin_foreign_resource_narrowed_revoked_and_disabled_deny_before_payload(
    knowledge_case: KnowledgeCase,
) -> None:
    case = knowledge_case
    ledger = _owner_ledger(case)
    with case.database.immediate_session() as session:
        session.execute(
            update(ApiToken)
            .where(ApiToken.id == case.token_ids["revoked"])
            .values(revoked_at=utcnow())
        )
        session.execute(
            update(User).where(User.id == case.user_ids["disabled"]).values(is_active=False)
        )

    denied = (
        ("foreign_admin", SessionNotFound),
        ("foreign", SessionNotFound),
        ("resource_limited", SessionNotFound),
        ("narrowed", AccessDenied),
        ("revoked", AccessDenied),
        ("disabled", AccessDenied),
    )
    malformed_payload = cast(Mapping[str, Any], None)
    for actor_name, error_type in denied:
        with case.database.immediate_session() as session:
            with pytest.raises(error_type):
                replace_ledger(
                    session,
                    case.contexts[actor_name],
                    ledger["id"],
                    expected_revision=1,
                    payload=malformed_payload,
                )

    with case.database.snapshot_session() as session:
        current = read_ledger(
            session,
            case.contexts["owner"],
            case.team_session_id,
            "research",
        )
    assert current["revision"] == 1
    assert current["payload"] == {"evidence": [], "warnings": [], "summary": ""}


def test_missing_ledger_and_invalid_scopes_use_fixed_non_echoing_errors(
    knowledge_case: KnowledgeCase,
) -> None:
    case = knowledge_case
    invalid_scopes: tuple[tuple[Any, Any, Any, Any], ...] = (
        (case.team_session_id, "private ledger name", "auto", ""),
        (case.team_session_id, "k" * 33, "auto", ""),
        (case.team_session_id, "research", None, ""),
        (case.team_session_id, "research", "a" * 41, ""),
        (case.team_session_id, "research", "auto", "b" * 41),
        ("s" * 37, "research", "auto", ""),
    )
    with case.database.snapshot_session() as session:
        for session_id, kind, source, target in invalid_scopes:
            with pytest.raises(ValueError) as invalid:
                read_ledger(
                    session,
                    case.contexts["owner"],
                    session_id,
                    kind,
                    source_language=cast(str, source),
                    target_language=target,
                )
            assert str(invalid.value) == "Invalid knowledge ledger scope."
            assert "private ledger name" not in str(invalid.value)

    with case.database.immediate_session() as session:
        with pytest.raises(KnowledgeLedgerNotFound) as missing:
            replace_ledger(
                session,
                case.contexts["owner"],
                new_id(),
                expected_revision=1,
                payload={},
            )
        assert missing.value.args == ("Knowledge ledger not found.",)
        with pytest.raises(ValueError) as invalid_revision:
            replace_ledger(
                session,
                case.contexts["owner"],
                new_id(),
                expected_revision=True,
                payload={},
            )
        assert str(invalid_revision.value) == "Invalid knowledge ledger revision."


def test_host_or_ambient_actor_never_substitutes_for_explicit_user_context(
    knowledge_case: KnowledgeCase,
) -> None:
    case = knowledge_case
    with execution_actor_scope(HostActor("pandrator-cli")):
        with case.database.snapshot_session() as session:
            with pytest.raises(TypeError, match="access context"):
                read_ledger(
                    session,
                    cast(AccessContext, None),
                    case.team_session_id,
                    "research",
                )
            with pytest.raises(TypeError):
                AccessContext(cast(UserActor, HostActor("pandrator-cli")))


def test_existing_payload_validation_semantics_are_reused() -> None:
    with pytest.raises(KnowledgeValidationError):
        KnowledgeLedgerStore._validated_payload(
            "glossary",
            {"entries": [{"source": "  "}, {"target": "  "}]},
        )
