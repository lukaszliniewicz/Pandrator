"""Private cache namespaces, publication fences and derived-data cleanup."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from pandrator.runtime import DataPaths
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.database import Database, sqlite_url
from pandrator.web.job_identity import job_ownership
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Job, Project, ResearchCacheEntry, SessionRecord, Workspace, utcnow
from pandrator.web.projects.identity import delete_empty_project
from pandrator.web.sessions import SessionService
from pandrator.web.web_research import PersistentResearchCache
from tests.web_test_support import prepare_web_test_data_root

PAYLOAD = {"query": "A private synthetic project query", "limit": 3}


def _use_legacy_schema(database: Database, revision: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(database.path))
    command.downgrade(config, revision)


@dataclass
class CacheCase:
    database: Database
    paths: DataPaths
    workspaces: dict[str, str]
    sessions: dict[str, SessionRecord]

    def cache(self, name: str) -> PersistentResearchCache:
        return PersistentResearchCache(self.database, session_id=self.sessions[name].id)


@pytest.fixture
def cache_case(tmp_path: Path) -> Iterator[CacheCase]:
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
        yield CacheCase(database, paths, workspaces, sessions)
    finally:
        database.dispose()


def _put(cache: PersistentResearchCache, response: dict[str, Any], *, ttl_days: int = 7) -> None:
    cache.put("jina", "search_web", PAYLOAD, response, ttl_days=ttl_days)


def _get(cache: PersistentResearchCache) -> dict[str, Any] | None:
    return cache.get("jina", "search_web", PAYLOAD)


def _rows(case: CacheCase) -> list[tuple[Any, ...]]:
    with case.database.snapshot_session() as session:
        return [
            tuple(row)
            for row in session.execute(
                select(ResearchCacheEntry.__table__).order_by(ResearchCacheEntry.cache_key)
            )
        ]


def test_projects_and_workspaces_have_separate_keys_and_one_project_reuses_results(
    cache_case: CacheCase,
) -> None:
    case = cache_case
    for name in ("alpha", "alpha_second", "beta"):
        cache = case.cache(name)
        assert _get(cache) is None
        _put(cache, {"owner": name})
    for name in ("alpha", "alpha_second", "beta"):
        assert _get(case.cache(name)) == {"owner": name}
    with case.database.immediate_session() as session:
        branch = SessionRecord(
            name="Shared project session",
            storage_key="cache-branch",
            project_id=case.sessions["alpha"].project_id,
        )
        session.add(branch)
        session.flush()
        branch_id = branch.id
    shared = PersistentResearchCache(case.database, session_id=branch_id)
    assert _get(shared) == {"owner": "alpha"}
    workspace_cache = PersistentResearchCache(case.database, workspace_id=case.workspaces["alpha"])
    assert _get(workspace_cache) is None
    _put(workspace_cache, {"owner": "workspace-only"})
    solo = PersistentResearchCache(case.database)
    _put(solo, {"owner": "personal"})
    assert _get(
        PersistentResearchCache(case.database, workspace_id=case.workspaces["personal"])
    ) == {"owner": "personal"}
    with case.database.session() as session:
        assert len(session.scalars(select(ResearchCacheEntry.cache_key)).all()) == 5


def test_legacy_key_is_retained_without_unscoped_fallback_or_overwrite(
    cache_case: CacheCase,
) -> None:
    case = cache_case
    material = json.dumps(
        {"provider": "jina", "operation": "search_web", "payload": PAYLOAD},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    legacy_key = hashlib.sha256(material.encode()).hexdigest()
    with case.database.immediate_session() as session:
        session.add(
            ResearchCacheEntry(
                cache_key=legacy_key,
                workspace_id=case.workspaces["personal"],
                provider="jina",
                operation="search_web",
                request_json=PAYLOAD,
                response_json={"legacy": "private"},
                expires_at=utcnow() + timedelta(days=7),
            )
        )
    solo = PersistentResearchCache(case.database)
    assert _get(solo) is None
    _put(solo, {"new": "scoped"})
    assert _get(solo) == {"new": "scoped"}
    with case.database.session() as session:
        legacy = session.get(ResearchCacheEntry, legacy_key)
        assert legacy is not None and legacy.response_json == {"legacy": "private"}
        assert legacy.workspace_id == case.workspaces["personal"] and legacy.project_id is None
        assert len(session.scalars(select(ResearchCacheEntry.cache_key)).all()) == 2


@pytest.mark.parametrize("change", ("archive", "retarget", "missing_session"))
def test_frozen_context_cannot_read_or_write_after_root_changes(
    cache_case: CacheCase, change: str
) -> None:
    case = cache_case
    if change == "retarget":
        _use_legacy_schema(case.database, "0075_mandatory_ownership")
    cache = case.cache("alpha")
    _put(cache, {"private": "retained"})
    with case.database.immediate_session() as session:
        if change == "archive":
            workspace = session.get(Workspace, case.workspaces["alpha"])
            assert workspace is not None
            workspace.state = "archived"
        else:
            record = session.get(SessionRecord, case.sessions["alpha"].id)
            assert record is not None
            if change == "retarget":
                record.project_id = case.sessions["alpha_second"].project_id
            else:
                session.delete(record)
    before = _rows(case)
    with pytest.raises(ValueError):
        _get(cache)
    with pytest.raises(ValueError):
        _put(cache, {"unexpected": "overwrite"})
    assert _rows(case) == before


def test_inconsistent_key_scope_does_not_disclose_or_overwrite_response(
    cache_case: CacheCase,
) -> None:
    case = cache_case
    _use_legacy_schema(case.database, "0077_media_scope_consistency")
    cache = case.cache("alpha")
    _put(cache, {"private": "alpha"})
    with case.database.immediate_session() as session:
        row = session.scalar(select(ResearchCacheEntry))
        assert row is not None
        row.workspace_id = case.workspaces["beta"]
    before = _rows(case)
    assert _get(cache) is None
    assert _get(case.cache("beta")) is None
    with pytest.raises(ValueError, match="inconsistent ownership"):
        _put(cache, {"unexpected": "overwrite"})
    assert _rows(case) == before


def test_expiry_and_upsert_keep_the_same_owned_entry(cache_case: CacheCase) -> None:
    case = cache_case
    cache = case.cache("alpha")
    _put(cache, {"version": 1}, ttl_days=0)
    with case.database.immediate_session() as session:
        row = session.scalar(select(ResearchCacheEntry))
        assert row is not None
        key = row.cache_key
        expiry = row.expires_at.replace(tzinfo=UTC)
        assert utcnow() + timedelta(hours=23) < expiry < utcnow() + timedelta(hours=25)
        row.expires_at = utcnow() - timedelta(minutes=1)
    assert _get(cache) is None
    _put(cache, {"version": 2})
    assert _get(cache) == {"version": 2}
    with case.database.session() as session:
        row = session.scalar(select(ResearchCacheEntry))
        assert row is not None and row.cache_key == key
        assert row.project_id == case.sessions["alpha"].project_id
        assert row.workspace_id == case.workspaces["alpha"]
        assert len(session.scalars(select(ResearchCacheEntry.cache_key)).all()) == 1


def test_two_concurrent_writers_share_one_scoped_key_without_integrity_failure(
    cache_case: CacheCase,
) -> None:
    case = cache_case
    barrier = threading.Barrier(2)

    def write(label: str) -> None:
        cache = case.cache("alpha")
        barrier.wait(timeout=5)
        _put(cache, {"writer": label})

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(write, label) for label in ("one", "two")]
        for future in futures:
            future.result(timeout=10)
    assert _get(case.cache("alpha")) in ({"writer": "one"}, {"writer": "two"})
    with case.database.session() as session:
        assert len(session.scalars(select(ResearchCacheEntry.cache_key)).all()) == 1


def test_last_session_cleanup_drops_only_its_cache_even_when_job_keeps_root(
    cache_case: CacheCase,
) -> None:
    case = cache_case
    _put(case.cache("alpha"), {"project": "deleted"})
    _put(case.cache("alpha_second"), {"project": "retained"})
    solo = PersistentResearchCache(case.database)
    _put(solo, {"workspace": "retained"})
    target = case.sessions["alpha"]
    job = JobQueue(case.database).enqueue(
        "maintenance.session_purge",
        {"target_session_id": target.id},
        workspace_id=case.workspaces["alpha"],
        project_id=target.project_id,
    )
    with case.database.immediate_session() as session:
        record = session.get(SessionRecord, target.id)
        assert record is not None
        session.delete(record)
        delete_empty_project(session, target.project_id)
    with case.database.session() as session:
        assert session.get(Project, target.project_id) is not None
        assert (
            session.scalar(
                select(ResearchCacheEntry.cache_key).where(
                    ResearchCacheEntry.project_id == target.project_id
                )
            )
            is None
        )
        retained = session.get(Job, job.id)
        assert (
            retained is not None
            and job_ownership(session, retained).project_id == target.project_id
        )
    assert _get(case.cache("alpha_second")) == {"project": "retained"}
    assert _get(solo) == {"workspace": "retained"}
