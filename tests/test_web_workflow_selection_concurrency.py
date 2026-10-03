"""Native concurrent selection requests must consume a revision only once."""

import json
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import Connection, event, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import FromClause
from werkzeug.wrappers import Response

from pandrator.runtime import DataPaths
from pandrator.web import api_routes
from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.database import Database
from pandrator.web.models import Artifact, ArtifactEdge, Job, SessionStageSelection
from tests.web_test_support import prepare_web_test_data_root


def _json_object(response: Response) -> dict[str, object]:
    payload = response.get_json()
    assert isinstance(payload, dict), payload
    return payload


@dataclass
class SelectionCase:
    app: Flask
    database: Database
    clients: tuple[FlaskClient, FlaskClient]
    headers: tuple[dict[str, str], dict[str, str]]
    session_id: str
    artifact_ids: tuple[str, str, str]
    artifact_paths: tuple[Path, Path, Path]
    revision: int


@pytest.fixture
def selection_case(tmp_path: Path) -> Iterator[SelectionCase]:
    prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=bootstrap)
    extension = app.extensions["pandrator"]
    database = extension["database"]
    artifacts = extension["artifacts"]
    paths = extension["paths"]
    assert isinstance(database, Database)
    assert isinstance(artifacts, ArtifactService)
    assert isinstance(paths, DataPaths)
    try:
        clients = (app.test_client(), app.test_client())
        authenticated_headers: list[dict[str, str]] = []
        for client in clients:
            response = client.post(
                "/api/v1/auth/bootstrap", json={"token": bootstrap.issue()}
            )
            assert response.status_code == 200, _json_object(response)
            csrf = _json_object(response)["csrf_token"]
            assert isinstance(csrf, str)
            authenticated_headers.append({"X-CSRF-Token": csrf})
        response = clients[0].post(
            "/api/v1/sessions",
            json={"name": "Selection race", "workflow_kind": "voiceover"},
            headers=authenticated_headers[0],
        )
        assert response.status_code == 201, _json_object(response)
        session_id = _json_object(response)["id"]
        assert isinstance(session_id, str)
        artifact_ids: list[str] = []
        artifact_paths: list[Path] = []
        for name in ("A", "B", "C"):
            path = paths.sessions / f"selection-{name}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"Correction {name}", encoding="utf-8")
            artifact = artifacts.register(
                path, kind="source", role="correction", session_id=session_id
            )
            artifact_ids.append(artifact.id)
            artifact_paths.append(path)
        history_response = clients[0].get(
            f"/api/v1/sessions/{session_id}/stages/correct/artifacts"
        )
        assert history_response.status_code == 200, _json_object(history_response)
        history = _json_object(history_response)
        revision = history["revision"]
        assert isinstance(revision, int)
        assert history["selected_artifact_id"] == artifact_ids[2]
        yield SelectionCase(
            app=app,
            database=database,
            clients=clients,
            headers=(authenticated_headers[0], authenticated_headers[1]),
            session_id=session_id,
            artifact_ids=(artifact_ids[0], artifact_ids[1], artifact_ids[2]),
            artifact_paths=(artifact_paths[0], artifact_paths[1], artifact_paths[2]),
            revision=revision,
        )
    finally:
        database.dispose()


def _rows(database: Database, table: FromClause) -> list[dict[str, object]]:
    with database.session() as db_session:
        statement = select(table).order_by(*table.primary_key)
        return [dict(row) for row in db_session.execute(statement).mappings()]


def _race(
    case: SelectionCase, monkeypatch: pytest.MonkeyPatch, *, clear_second: bool
) -> None:
    before_artifacts = _rows(case.database, Artifact.__table__)
    before_edges = _rows(case.database, ArtifactEdge.__table__)
    before_selections = _rows(case.database, SessionStageSelection.__table__)
    assert _rows(case.database, Job.__table__) == []
    first_read = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    lock = threading.Lock()
    guards: dict[str, list[object]] = {"first": [], "second": []}
    immediate_attempts: list[str] = []
    outcomes: dict[str, dict[str, object]] = {}
    errors: list[str] = []
    first_name = "selection-race-first"
    second_name = "selection-race-second"
    original_history = api_routes.stage_history

    def scheduled_history(
        db_session: Session, session_id: str, stage_key: str
    ) -> dict[str, object]:
        history = original_history(db_session, session_id, stage_key)
        thread_name = threading.current_thread().name
        if thread_name in (first_name, second_name):
            label = "first" if thread_name == first_name else "second"
            with lock:
                guards[label].append(history["revision"])
            if label == "first":
                first_read.set()
                if not release_first.wait(timeout=15):
                    raise TimeoutError("First selection guard was not released")
            else:
                second_entered.set()
        return history

    def observe_immediate(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.strip().upper() != "BEGIN IMMEDIATE":
            return
        frame: FrameType | None = sys._getframe()
        while frame is not None:
            if frame.f_code.co_name == "workflow_stage_selection":
                thread_name = threading.current_thread().name
                with lock:
                    immediate_attempts.append(thread_name)
                if thread_name == second_name:
                    second_entered.set()
                return
            frame = frame.f_back

    def request_selection(index: int, label: str, artifact_id: str | None) -> None:
        try:
            response = case.clients[index].put(
                f"/api/v1/sessions/{case.session_id}/stages/correct/selection",
                json={"artifact_id": artifact_id},
                headers={
                    **case.headers[index],
                    "If-Match": f'"{case.revision}"',
                },
            )
            with lock:
                outcomes[label] = {
                    "status": response.status_code,
                    "body": _json_object(response),
                }
        except Exception as error:
            with lock:
                errors.append(f"{label}: {type(error).__name__}: {error}")

    first = threading.Thread(
        name=first_name,
        target=request_selection,
        args=(0, "first", case.artifact_ids[0]),
    )
    second = threading.Thread(
        name=second_name,
        target=request_selection,
        args=(1, "second", None if clear_second else case.artifact_ids[1]),
    )
    started: list[threading.Thread] = []
    with monkeypatch.context() as patch:
        patch.setattr(api_routes, "stage_history", scheduled_history)
        event.listen(case.database.engine, "before_cursor_execute", observe_immediate)
        try:
            first.start()
            started.append(first)
            assert first_read.wait(timeout=15), "First guard read did not complete"
            second.start()
            started.append(second)
            assert second_entered.wait(timeout=15), "Second guard/transaction did not enter"
            release_first.set()
        finally:
            release_first.set()
            for thread in started:
                thread.join(timeout=40)
            event.remove(case.database.engine, "before_cursor_execute", observe_immediate)
            assert not any(thread.is_alive() for thread in started), "Live request thread"
    assert errors == [], errors
    assert set(outcomes) == {"first", "second"}, outcomes
    after_artifacts = _rows(case.database, Artifact.__table__)
    after_edges = _rows(case.database, ArtifactEdge.__table__)
    after_selections = _rows(case.database, SessionStageSelection.__table__)
    after_jobs = _rows(case.database, Job.__table__)
    selected = next(row for row in after_selections if row["stage_key"] == "correct")
    observation = {
        "case": "choose-clear" if clear_second else "choose-choose",
        "original_revision": case.revision,
        "artifact_ids": case.artifact_ids,
        "guard_revisions": guards,
        "immediate_attempts": immediate_attempts,
        "responses": outcomes,
        "before_selections": before_selections,
        "after_selections": after_selections,
        "artifacts_unchanged": before_artifacts == after_artifacts,
        "edges_unchanged": before_edges == after_edges,
        "jobs": after_jobs,
    }
    print("SELECTION_RACE_OBSERVATION " + json.dumps(observation, default=str, sort_keys=True))
    assert after_artifacts == before_artifacts
    assert after_edges == before_edges
    assert all(path.is_file() for path in case.artifact_paths)
    assert after_jobs == []
    assert outcomes["first"]["status"] == 200, observation
    assert outcomes["second"]["status"] == 409, observation
    second_body = outcomes["second"]["body"]
    assert isinstance(second_body, dict)
    error = second_body["error"]
    assert isinstance(error, dict)
    assert error["code"] == "revision_conflict", observation
    assert selected["artifact_id"] == case.artifact_ids[0], observation
    assert selected["revision"] == case.revision + 1, observation


def test_concurrent_choices_consume_selection_revision_once(
    selection_case: SelectionCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    _race(selection_case, monkeypatch, clear_second=False)


def test_concurrent_clear_cannot_remove_accepted_selection(
    selection_case: SelectionCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    _race(selection_case, monkeypatch, clear_second=True)
