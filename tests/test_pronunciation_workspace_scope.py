"""Workspace dictionaries keep exact-session overrides and private CRUD scope."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, update

from pandrator.runtime import DataPaths
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.database import Database, sqlite_url
from pandrator.web.models import Project, PronunciationEntry, SessionRecord, Workspace, utcnow
from pandrator.web.projects.identity import create_session_project
from pandrator.web.pronunciation_identity import pronunciation_ownership
from pandrator.web.pronunciations import PronunciationLibrary
from pandrator.web.session_purge import SessionPurgeService
from pandrator.web.sessions import SessionService
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class DictionaryCase:
    database: Database
    paths: DataPaths
    library: PronunciationLibrary
    workspaces: dict[str, str]
    sessions: dict[str, SessionRecord]

    def create(self, name: str, *, scoped: bool = False, **values: Any) -> dict[str, Any]:
        workspace = "beta" if name == "beta" else "alpha"
        return self.library.create(
            {
                "scope": "session" if scoped else "global",
                "session_id": self.sessions[name].id if scoped else None,
                "source_form": "Mira",
                "phonetic": "mee-rah",
                "language": "en",
                **values,
            },
            workspace_id=self.workspaces[workspace],
        )

    def resolve(self, name: str) -> list[dict[str, Any]]:
        return self.library.resolve(
            "Mira arrived.", session_id=self.sessions[name].id, language="en"
        )


@pytest.fixture
def dictionary_case(tmp_path: Path) -> Iterator[DictionaryCase]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    workspaces: dict[str, str] = {}
    with database.immediate_session() as session:
        workspaces["personal"] = initial_personal_workspace(session).id
        for name in ("alpha", "beta"):
            workspace = Workspace(kind="team", name=name)
            session.add(workspace)
            session.flush()
            workspaces[name] = workspace.id
    service = SessionService(database)
    sessions = {
        name: service.create(name, workspace_id=workspaces[owner])
        for name, owner in (("alpha", "alpha"), ("alpha_second", "alpha"), ("beta", "beta"))
    }
    try:
        yield DictionaryCase(database, paths, PronunciationLibrary(database), workspaces, sessions)
    finally:
        database.dispose()


def test_global_keys_and_sql_limits_are_scoped_before_projection(
    dictionary_case: DictionaryCase,
) -> None:
    case = dictionary_case
    own = case.create("alpha")
    foreign = case.create("beta", phonetic="my-rah")
    solo = case.library.create({"source_form": "Solo", "phonetic": "soh-loh", "language": "en"})
    with case.database.immediate_session() as session:
        session.execute(
            update(PronunciationEntry)
            .where(PronunciationEntry.id == foreign["id"])
            .values(updated_at=utcnow() + timedelta(days=1))
        )
    assert [
        row["id"] for row in case.library.list(workspace_id=case.workspaces["alpha"], limit=1)
    ] == [own["id"]]
    assert [row["id"] for row in case.library.list()] == [solo["id"]]
    assert [row["id"] for row in case.resolve("beta")] == [foreign["id"]]
    assert [row["id"] for row in case.resolve("alpha_second")] == [own["id"]]
    assert [
        row["id"]
        for row in case.library.resolve(
            "Mira", session_id="", language="en", workspace_id=case.workspaces["alpha"]
        )
    ] == [own["id"]]
    with pytest.raises(ValueError, match="already exists"):
        case.create("alpha_second")


@pytest.mark.parametrize("action", ("update", "delete"))
def test_foreign_ids_are_missing_before_revision_checks(
    dictionary_case: DictionaryCase, action: str
) -> None:
    case = dictionary_case
    foreign = case.create("beta")
    for identity in (foreign["id"], "missing-entry"):
        with pytest.raises(KeyError):
            if action == "update":
                case.library.update(
                    identity, 999, {"phonetic": "mih-rah"}, workspace_id=case.workspaces["alpha"]
                )
            else:
                case.library.delete(identity, 999, workspace_id=case.workspaces["alpha"])
    assert (
        case.library.list(workspace_id=case.workspaces["beta"])[0]["revision"]
        == foreign["revision"]
    )
    assert not case.library.list(
        session_id=case.sessions["beta"].id, workspace_id=case.workspaces["alpha"]
    )
    assert not case.library.list(
        session_id="missing-session", workspace_id=case.workspaces["alpha"]
    )


def test_worker_resolution_preserves_exact_session_override_and_review(
    dictionary_case: DictionaryCase,
) -> None:
    case = dictionary_case
    shared = case.create("alpha")
    case.create("beta", phonetic="my-rah")
    with case.database.immediate_session() as session:
        branch = SessionRecord(name="Branch", storage_key="dictionary-branch")
        create_session_project(session, branch, project_id=case.sessions["alpha"].project_id)
        session.expunge(branch)
        case.sessions["branch"] = branch
    proposal = case.library.propose(
        session_id=case.sessions["alpha"].id, source_form="Mira", phonetic="mih-rah", language="en"
    )
    assert proposal["status"] == "proposed"
    assert [row["id"] for row in case.resolve("alpha")] == [shared["id"]]
    case.library.update(
        proposal["id"],
        proposal["revision"],
        {"status": "reviewed"},
        workspace_id=case.workspaces["alpha"],
    )
    assert [row["id"] for row in case.resolve("alpha")] == [proposal["id"]]
    assert [row["id"] for row in case.resolve("branch")] == [shared["id"]]
    assert [row["id"] for row in case.resolve("alpha_second")] == [shared["id"]]
    with case.database.snapshot_session() as session:
        entry = session.get_one(PronunciationEntry, proposal["id"])
        assert (entry.workspace_id, entry.project_id) == (
            case.workspaces["alpha"],
            case.sessions["alpha"].project_id,
        )


def test_manual_scope_changes_remain_in_the_selected_workspace(
    dictionary_case: DictionaryCase,
) -> None:
    case = dictionary_case
    original = case.create("alpha")
    first = case.library.update(
        original["id"],
        original["revision"],
        {"scope": "session", "session_id": case.sessions["alpha"].id},
        workspace_id=case.workspaces["alpha"],
    )
    second = case.library.update(
        first["id"],
        first["revision"],
        {"session_id": case.sessions["alpha_second"].id},
        workspace_id=case.workspaces["alpha"],
    )
    with pytest.raises(ValueError):
        case.library.update(
            second["id"],
            second["revision"],
            {"session_id": case.sessions["beta"].id},
            workspace_id=case.workspaces["alpha"],
        )
    with case.database.snapshot_session() as session:
        entry = session.get_one(PronunciationEntry, original["id"])
        assert (
            entry.revision == second["revision"]
            and entry.project_id == case.sessions["alpha_second"].project_id
        )
    final = case.library.update(
        second["id"], second["revision"], {"scope": "global"}, workspace_id=case.workspaces["alpha"]
    )
    with case.database.snapshot_session() as session:
        entry = session.get_one(PronunciationEntry, original["id"])
        assert entry.project_id is None and entry.session_id is None
        assert (
            entry.workspace_id == case.workspaces["alpha"] and entry.revision == final["revision"]
        )


def test_scope_hints_in_values_and_metadata_never_grant_ownership(
    dictionary_case: DictionaryCase,
) -> None:
    case = dictionary_case
    result = case.create(
        "alpha",
        workspace_id=case.workspaces["beta"],
        project_id=case.sessions["beta"].project_id,
        metadata={"workspace_id": case.workspaces["beta"]},
    )
    with case.database.snapshot_session() as session:
        entry = session.get_one(PronunciationEntry, result["id"])
        assert entry.workspace_id == case.workspaces["alpha"] and entry.project_id is None
        assert entry.metadata_json == {"workspace_id": case.workspaces["beta"]}
    with pytest.raises(ValueError):
        case.library.create(
            {
                "scope": "session",
                "session_id": case.sessions["alpha"].id,
                "source_form": "Other",
                "phonetic": "ah-thur",
            }
        )


@pytest.mark.parametrize("kind", ("global", "proposal"))
def test_duplicate_writers_are_serialized(dictionary_case: DictionaryCase, kind: str) -> None:
    case = dictionary_case
    barrier = threading.Barrier(2)

    def write() -> str:
        barrier.wait(timeout=10)
        if kind == "proposal":
            return case.library.propose(
                session_id=" " + case.sessions["alpha"].id + " ",
                source_form="Mira",
                phonetic="mee-rah",
                language="en",
            )["id"]
        try:
            return case.create("alpha")["id"]
        except ValueError as error:
            assert "already exists" in str(error)
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    if kind == "global":
        assert results.count("duplicate") == 1
    else:
        assert results[0] == results[1]
    with case.database.snapshot_session() as session:
        assert len(session.scalars(select(PronunciationEntry)).all()) == 1


def test_archive_and_missing_context_do_not_fall_back_to_another_dictionary(
    dictionary_case: DictionaryCase,
) -> None:
    case = dictionary_case
    own = case.create("alpha")
    case.library.create({"source_form": "Mira", "phonetic": "my-rah", "language": "en"})
    with pytest.raises(KeyError):
        case.library.resolve("Mira", session_id="missing-session", language="en")
    with pytest.raises(ValueError):
        case.library.resolve(
            "Mira",
            session_id=case.sessions["beta"].id,
            language="en",
            workspace_id=case.workspaces["alpha"],
        )
    with case.database.immediate_session() as session:
        session.execute(
            update(Workspace)
            .where(Workspace.id == case.workspaces["alpha"])
            .values(state="archived")
        )
    with pytest.raises(ValueError):
        case.resolve("alpha")
    with pytest.raises(ValueError):
        case.library.list(workspace_id=case.workspaces["alpha"])
    with pytest.raises(ValueError):
        case.library.update(
            own["id"],
            own["revision"],
            {"phonetic": "mih-rah"},
            workspace_id=case.workspaces["alpha"],
        )


def test_current_sql_rejects_a_retargeted_session_after_orm_preload(
    dictionary_case: DictionaryCase,
) -> None:
    case = dictionary_case
    with case.database.immediate_session() as session:
        branch = SessionRecord(name="Branch", storage_key="retargeted-dictionary-branch")
        create_session_project(session, branch, project_id=case.sessions["alpha"].project_id)
        session.expunge(branch)
    result = case.library.create(
        {"scope": "session", "session_id": branch.id, "source_form": "Mira", "phonetic": "mee-rah"},
        workspace_id=case.workspaces["alpha"],
    )
    # A session can only be retargeted on the genuine pre-immutability schema.
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(case.database.path))
    command.downgrade(config, "0075_mandatory_ownership")
    with case.database.immediate_session() as session:
        cached = session.get_one(SessionRecord, branch.id)
        entry = session.get_one(PronunciationEntry, result["id"])
        session.execute(
            update(SessionRecord)
            .where(SessionRecord.id == branch.id)
            .values(project_id=case.sessions["alpha_second"].project_id)
            .execution_options(synchronize_session=False)
        )
        assert cached.project_id == case.sessions["alpha"].project_id
        with pytest.raises(ValueError, match="disagrees"):
            pronunciation_ownership(session, entry)
        session.rollback()


def test_corrupt_stored_owner_is_not_projected_or_repaired(dictionary_case: DictionaryCase) -> None:
    case = dictionary_case
    result = case.create("alpha", scoped=True)
    # Keep the legacy corrupt-row defense without bypassing the new scope guards.
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(case.database.path))
    command.downgrade(config, "0079_execution_scope_consistency")
    with case.database.immediate_session() as session:
        session.execute(
            update(PronunciationEntry)
            .where(PronunciationEntry.id == result["id"])
            .values(session_id=case.sessions["beta"].id)
        )
    with pytest.raises(ValueError):
        case.library.list(workspace_id=case.workspaces["alpha"])
    with case.database.snapshot_session() as session:
        entry = session.get_one(PronunciationEntry, result["id"])
        assert entry.project_id == case.sessions["alpha"].project_id
        assert entry.session_id == case.sessions["beta"].id


def test_actual_purge_removes_only_session_entries_and_keeps_workspace_defaults(
    dictionary_case: DictionaryCase,
) -> None:
    case = dictionary_case
    shared = case.create("alpha")
    owned = case.create("alpha", scoped=True)
    other_session = case.create("alpha_second", scoped=True)
    foreign = case.create("beta")
    record = case.sessions["alpha"]
    (case.paths.sessions / record.storage_key).mkdir()
    SessionService(case.database).trash(record.id, record.revision)
    with pytest.raises(ValueError, match="trashed"):
        case.library.propose(
            session_id=record.id, source_form="Other", phonetic="ah-thur", language="en"
        )
    with pytest.raises(ValueError, match="trashed"):
        case.library.delete(owned["id"], owned["revision"], workspace_id=case.workspaces["alpha"])
    assert case.resolve("alpha")[0]["id"] == owned["id"]
    purge = SessionPurgeService(case.database, case.paths)
    preview = purge.preview(record.id)
    assert preview["can_purge"], preview
    assert purge.purge(record.id, preview["revision"], preview["impact_token"]) == {
        "state": "complete"
    }
    with case.database.snapshot_session() as session:
        ids = set(session.scalars(select(PronunciationEntry.id)))
        assert ids == {shared["id"], other_session["id"], foreign["id"]}
        assert session.get(Project, record.project_id) is None
