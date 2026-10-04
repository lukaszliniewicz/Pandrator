"""Local CLI commands retire native checked-in SQLite pool connections."""

from __future__ import annotations

import argparse
import io
import json
import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.pool import Pool

from pandrator.runtime import DataPaths
from pandrator.web import cli
from pandrator.web.database import Database
from pandrator.web.models import ApiToken, Job, JobEvent, SessionRecord
from tests.web_test_support import prepare_web_test_data_root

COMMANDS = {
    "command_auth_init": (["auth", "init"], "AuthService.initialize_owner"),
    "command_auth_token_create": (
        ["auth", "token", "create", "--scope", "app.read"],
        "AuthService.create_api_token",
    ),
    "command_auth_token_list": (["auth", "token", "list"], "AuthService.list_tokens"),
    "command_auth_token_revoke": (
        ["auth", "token", "revoke", "fixture-id"],
        "AuthService.revoke_token",
    ),
    "command_session_list": (["session", "list"], "SessionService.list"),
    "command_session_create": (["session", "create", "Fixture session"], "SessionService.create"),
    "command_session_show": (["session", "show", "fixture-id"], "SessionService.get"),
    "command_job_list": (["job", "list"], "JobQueue.list"),
    "command_job_enqueue": (["job", "enqueue", "noop"], "JobQueue.enqueue"),
    "command_job_show": (["job", "show", "fixture-id"], "JobQueue.get"),
    "command_job_cancel": (["job", "cancel", "fixture-id"], "JobQueue.request_cancel"),
    "command_doctor": (["doctor"], "ArtifactService.reconcile"),
}


@dataclass
class HeldDatabase:
    paths: DataPaths
    database: Database
    connection: sqlite3.Connection
    initial_pool: Pool
    dispose: Mock

    def assert_retired(self) -> None:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            self.connection.execute("SELECT 1")
        self.dispose.assert_called_once_with()
        assert self.database.engine.pool is not self.initial_pool


@contextmanager
def held_database(paths: DataPaths) -> Iterator[HeldDatabase]:
    database = Database(paths.database)
    native_dispose = database.dispose
    with database.engine.connect() as checkout:
        checkout.exec_driver_sql("SELECT 1")
        connection = checkout.connection.driver_connection
        assert isinstance(connection, sqlite3.Connection)
    assert connection.execute("SELECT 1").fetchone() == (1,)
    initial_pool = database.engine.pool
    try:
        with patch.object(database, "dispose", wraps=native_dispose) as dispose:
            yield HeldDatabase(paths, database, connection, initial_pool, dispose)
    finally:
        native_dispose()


@pytest.fixture
def owner(tmp_path: Path) -> Iterator[HeldDatabase]:
    paths = prepare_web_test_data_root(tmp_path)
    with held_database(paths) as retained:
        yield retained


def command_arguments(name: str, root: Path) -> argparse.Namespace:
    args = cli.build_parser().parse_args(["--data-dir", str(root), "--json", *COMMANDS[name][0]])
    if name == "command_auth_init":
        args.password = secrets.token_urlsafe(24)
    return args


@pytest.mark.parametrize("name", list(COMMANDS))
@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt])
def test_service_failure_retires_native_pool(
    owner: HeldDatabase, name: str, failure_type: type[BaseException]
) -> None:
    sentinel = failure_type("service sentinel")
    args = command_arguments(name, owner.paths.root)
    with (
        patch.object(cli, "_database", return_value=(owner.paths, owner.database)),
        patch(f"pandrator.web.cli.{COMMANDS[name][1]}", side_effect=sentinel),
        patch.object(cli, "_emit") as emit,
    ):
        with pytest.raises(failure_type) as caught:
            args.handler(args)
        assert caught.value is sentinel
        emit.assert_not_called()
    owner.assert_retired()


@pytest.mark.parametrize("failure", ["invalid_json", "password_mismatch"])
def test_early_return_retires_native_pool_and_preserves_output(
    owner: HeldDatabase, failure: str
) -> None:
    name = "command_job_enqueue" if failure == "invalid_json" else "command_auth_init"
    args = command_arguments(name, owner.paths.root)
    if failure == "invalid_json":
        args.payload = "{invalid"
    else:
        args.password = None
    error = io.StringIO()
    with (
        patch.dict(os.environ, {}, clear=True),
        patch("getpass.getpass", side_effect=["synthetic-first", "synthetic-second"]),
        patch.object(cli, "_database", return_value=(owner.paths, owner.database)),
        patch(f"pandrator.web.cli.{COMMANDS[name][1]}") as service,
        patch.object(cli, "_emit") as emit,
        redirect_stderr(error),
    ):
        assert args.handler(args) == 2
        service.assert_not_called()
        emit.assert_not_called()
    assert (
        "Invalid --payload JSON:" if failure == "invalid_json" else "Passwords do not match."
    ) in error.getvalue()
    owner.assert_retired()


def test_constructor_failure_retires_native_pool(owner: HeldDatabase) -> None:
    args = command_arguments("command_session_list", owner.paths.root)
    sentinel = RuntimeError("constructor sentinel")
    with (
        patch.object(cli, "_database", return_value=(owner.paths, owner.database)),
        patch.object(cli, "SessionService", side_effect=sentinel),
        patch.object(cli, "_emit") as emit,
    ):
        with pytest.raises(RuntimeError) as caught:
            args.handler(args)
        assert caught.value is sentinel
        emit.assert_not_called()
    owner.assert_retired()


def test_mkdir_failure_retires_pool_without_undoing_committed_session(owner: HeldDatabase) -> None:
    args = command_arguments("command_session_create", owner.paths.root)
    sentinel = OSError("mkdir sentinel")
    native_mkdir = Path.mkdir

    def mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.parent == owner.paths.sessions:
            raise sentinel
        native_mkdir(path, *args, **kwargs)

    with (
        patch.object(cli, "_database", return_value=(owner.paths, owner.database)),
        patch.object(Path, "mkdir", new=mkdir),
        patch.object(cli, "_emit") as emit,
    ):
        with pytest.raises(OSError) as caught:
            args.handler(args)
        assert caught.value is sentinel
        emit.assert_not_called()
    owner.assert_retired()
    with owner.database.session() as session:
        record = session.scalar(select(SessionRecord))
        assert record is not None and record.name == args.name
        assert not (owner.paths.sessions / record.storage_key).exists()


def test_acquisition_failure_does_not_guess_database_cleanup(owner: HeldDatabase) -> None:
    args = command_arguments("command_job_list", owner.paths.root)
    sentinel = RuntimeError("acquisition sentinel")
    with (
        patch.object(cli, "_database", side_effect=sentinel),
        patch.object(cli, "_emit") as emit,
    ):
        with pytest.raises(RuntimeError) as caught:
            args.handler(args)
        assert caught.value is sentinel
        emit.assert_not_called()
    owner.dispose.assert_not_called()
    assert owner.connection.execute("SELECT 1").fetchone() == (1,)
    assert owner.database.engine.pool is owner.initial_pool


def test_disposal_failure_is_visible_without_output_or_retry(owner: HeldDatabase) -> None:
    args = command_arguments("command_job_list", owner.paths.root)
    sentinel = RuntimeError("dispose sentinel")
    with (
        patch.object(cli, "_database", return_value=(owner.paths, owner.database)),
        patch.object(owner.database, "dispose", side_effect=sentinel) as failing_dispose,
        patch.object(cli, "_emit") as emit,
    ):
        with pytest.raises(RuntimeError) as caught:
            args.handler(args)
        assert caught.value is sentinel
        failing_dispose.assert_called_once_with()
        emit.assert_not_called()
    assert owner.connection.execute("SELECT 1").fetchone() == (1,)


def test_output_failure_occurs_after_native_pool_retirement(owner: HeldDatabase) -> None:
    args = command_arguments("command_job_list", owner.paths.root)
    sentinel = RuntimeError("output sentinel")

    def fail_output(*_args: Any, **_kwargs: Any) -> None:
        owner.assert_retired()
        raise sentinel

    with (
        patch.object(cli, "_database", return_value=(owner.paths, owner.database)),
        patch.object(cli, "_emit", side_effect=fail_output),
    ):
        with pytest.raises(RuntimeError) as caught:
            args.handler(args)
        assert caught.value is sentinel
    owner.assert_retired()


def test_native_twelve_command_round_trip_disposes_before_every_output(tmp_path: Path) -> None:
    paths = prepare_web_test_data_root(tmp_path)
    observed: list[str] = []

    def invoke(name: str, **values: Any) -> Any:
        args = command_arguments(name, paths.root)
        for key, value in values.items():
            setattr(args, key, value)
        output = io.StringIO()
        native_emit = cli._emit
        with held_database(paths) as retained:

            def emit(value: Any, json_output: bool = False) -> None:
                retained.assert_retired()
                native_emit(value, json_output)

            with (
                patch.object(cli, "_database", return_value=(paths, retained.database)),
                patch.object(cli, "_emit", side_effect=emit),
                redirect_stdout(output),
            ):
                assert args.handler(args) == 0
            retained.assert_retired()
        observed.append(name)
        return json.loads(output.getvalue())

    assert invoke("command_auth_init") == {"initialized": True}
    created_token = invoke("command_auth_token_create")
    token_id = created_token["id"]
    assert created_token["scopes"] == ["app.read"]
    assert isinstance(created_token["token"], str)
    tokens = invoke("command_auth_token_list")
    assert [token["id"] for token in tokens] == [token_id]
    assert invoke("command_auth_token_revoke", token_id=token_id) == {"revoked": token_id}
    session_record = invoke("command_session_create")
    session_id = session_record["id"]
    assert [record["id"] for record in invoke("command_session_list")] == [session_id]
    assert invoke("command_session_show", session_id=session_id)["id"] == session_id
    job = invoke("command_job_enqueue", session_id=session_id)
    job_id = job["id"]
    assert [record["id"] for record in invoke("command_job_list")] == [job_id]
    assert invoke("command_job_show", job_id=job_id)["status"] == "queued"
    assert invoke("command_job_cancel", job_id=job_id)["status"] == "canceled"
    assert invoke("command_doctor") == {
        "ok": True,
        "data_root": str(paths.root),
        "artifact_issues": [],
    }
    assert len(observed) == 12 and set(observed) == set(COMMANDS)
    database = Database(paths.database)
    try:
        with database.session() as session:
            token = session.get(ApiToken, token_id)
            record = session.get(SessionRecord, session_id)
            job = session.get(Job, job_id)
            assert token is not None and token.revoked_at is not None
            assert record is not None and (paths.sessions / record.storage_key).is_dir()
            assert job is not None and job.status == "canceled"
            assert list(
                session.scalars(
                    select(JobEvent.event_type)
                    .where(JobEvent.job_id == job_id)
                    .order_by(JobEvent.id)
                )
            ) == ["job.queued", "job.canceled"]
    finally:
        database.dispose()
