"""Per-session interface mode persistence and API/MCP contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.database import sqlite_url, upgrade_database
from pandrator.web.models import SessionRecord
from pandrator.web.schemas import SessionCreate, SessionForkRequest, SessionUpdate
from pandrator_mcp.clients.application_sessions import ApplicationSessionMethods
from pandrator_mcp.schemas.sessions import CreateSessionInput
from pandrator_mcp.tools.sessions import _session_projection, create_session
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def web_application(tmp_path: Path):
    root = tmp_path / "data"
    prepare_web_test_data_root(root)
    bootstrap = BootstrapTokenStore()
    app = create_app(
        data_root=root,
        testing=True,
        bootstrap_tokens=bootstrap,
        background_maintenance=False,
    )
    services = app.extensions["pandrator"]["services"]
    client = app.test_client()
    authenticated = client.post(
        "/api/v1/auth/bootstrap", json={"token": bootstrap.issue()}
    )
    assert authenticated.status_code == 200
    headers = {"X-CSRF-Token": authenticated.get_json()["csrf_token"]}
    try:
        yield client, headers, services
    finally:
        services.close()


def test_old_database_rows_migrate_to_advanced_and_keep_their_ids(tmp_path: Path):
    database_path = tmp_path / "previous-schema.sqlite3"
    migration_dir = Path(__file__).parents[1] / "pandrator" / "web" / "migrations"
    config = Config()
    config.set_main_option("script_location", str(migration_dir))
    config.set_main_option("sqlalchemy.url", sqlite_url(database_path))
    command.upgrade(config, "0053_generation_audio_versions")

    engine = sa.create_engine(sqlite_url(database_path))
    legacy = {
        "id": "existing-session",
        "name": "Existing session",
        "storage_key": "existing-storage",
        "workflow_kind": "audiobook",
        "source_language": "auto",
        "workflow_preset": "custom",
        "included_stages_json": [],
        "status": "idle",
        "revision": 7,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
    }
    with engine.begin() as connection:
        sessions = sa.Table("sessions", sa.MetaData(), autoload_with=connection)
        connection.execute(sessions.insert().values(**legacy))

    # Exercise the production fast-path, not only Alembic directly: a stale
    # SCHEMA_HEAD previously skipped this migration on existing installations.
    upgrade_database(database_path)
    upgrade_database(database_path)
    with engine.begin() as connection:
        assert connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one() == ScriptDirectory.from_config(config).get_current_head()
        sessions = sa.Table("sessions", sa.MetaData(), autoload_with=connection)
        connection.execute(
            sessions.insert().values(
                **{
                    **legacy,
                    "id": "database-default-session",
                    "name": "Database default session",
                    "storage_key": "database-default-storage",
                }
            )
        )
        rows = connection.execute(
            sa.text(
                "SELECT id, name, interface_mode, revision FROM sessions ORDER BY id"
            )
        ).all()
    assert [tuple(row) for row in rows] == [
        ("database-default-session", "Database default session", "advanced", 7),
        ("existing-session", "Existing session", "advanced", 7),
    ]
    mode_column = next(
        column
        for column in sa.inspect(engine).get_columns("sessions")
        if column["name"] == "interface_mode"
    )
    assert mode_column["nullable"] is False
    assert "advanced" in str(mode_column["default"])

    command.downgrade(config, "0053_generation_audio_versions")
    assert "interface_mode" not in {
        column["name"] for column in sa.inspect(engine).get_columns("sessions")
    }
    engine.dispose()


def test_creation_defaults_and_enum_validation(web_application):
    _client, _headers, services = web_application
    with services.database.session() as database_session:
        model_default = SessionRecord(name="Model default")
        database_session.add(model_default)
        database_session.flush()
        assert model_default.interface_mode == "advanced"

    simple = services.sessions.create("Direct default")
    advanced = services.sessions.create("Direct advanced", interface_mode="advanced")
    assert simple.interface_mode == "simple"
    assert advanced.interface_mode == "advanced"

    for value in (None, "compact"):
        with pytest.raises(ValueError, match="Interface mode"):
            services.sessions.create("Invalid mode", interface_mode=value)

    with pytest.raises(ValueError, match="Interface mode"):
        services.sessions.update(
            simple.id,
            simple.revision,
            {"interface_mode": None},
        )
    persisted = services.sessions.get(simple.id)
    assert (persisted.interface_mode, persisted.revision) == ("simple", 1)

    assert SessionCreate(name="Schema default").interface_mode == "simple"
    assert SessionCreate(name="Schema advanced", interface_mode="advanced").interface_mode == "advanced"
    with pytest.raises(ValidationError):
        SessionCreate(name="Null mode", interface_mode=None)
    with pytest.raises(ValidationError):
        SessionCreate(name="Unknown mode", interface_mode="compact")
    assert SessionUpdate().model_fields_set == set()
    mode_schema = SessionUpdate.model_json_schema()["properties"]["interface_mode"]
    assert mode_schema["anyOf"] == [
        {"type": "string", "enum": ["simple", "advanced"]}
    ]
    with pytest.raises(ValidationError):
        SessionUpdate.model_validate({"interface_mode": None})
    with pytest.raises(ValidationError):
        SessionUpdate.model_validate({"interface_mode": "compact"})
    assert SessionForkRequest(checkpoint_artifact_id="checkpoint").interface_mode is None
    assert (
        SessionForkRequest(
            checkpoint_artifact_id="checkpoint",
            interface_mode=None,
        ).interface_mode
        is None
    )


def test_api_payloads_and_revision_safe_mode_update(web_application):
    client, headers, services = web_application
    created = client.post(
        "/api/v1/sessions",
        json={"name": "HTTP default", "source_language": "en"},
        headers=headers,
    )
    assert created.status_code == 201
    default_payload = created.get_json()
    assert default_payload["interface_mode"] == "simple"
    assert client.get(f"/api/v1/sessions/{default_payload['id']}").get_json()[
        "interface_mode"
    ] == "simple"
    listed = client.get("/api/v1/sessions").get_json()["items"]
    assert next(item for item in listed if item["id"] == default_payload["id"])[
        "interface_mode"
    ] == "simple"

    advanced = client.post(
        "/api/v1/sessions",
        json={"name": "HTTP advanced", "interface_mode": "advanced"},
        headers=headers,
    )
    assert advanced.status_code == 201
    assert advanced.get_json()["interface_mode"] == "advanced"

    before = services.sessions.get(default_payload["id"])
    updated = client.patch(
        f"/api/v1/sessions/{default_payload['id']}",
        json={"interface_mode": "advanced"},
        headers={
            **headers,
            "If-Match": created.headers["ETag"],
            "Idempotency-Key": "interface-mode:update:1",
        },
    )
    assert updated.status_code == 200
    updated_payload = updated.get_json()
    assert updated_payload["interface_mode"] == "advanced"
    assert updated_payload["revision"] == before.revision + 1
    replayed = client.patch(
        f"/api/v1/sessions/{default_payload['id']}",
        json={"interface_mode": "advanced"},
        headers={
            **headers,
            "If-Match": created.headers["ETag"],
            "Idempotency-Key": "interface-mode:update:1",
        },
    )
    assert replayed.status_code == 200
    assert replayed.headers["Idempotency-Replayed"] == "true"
    assert replayed.get_json() == updated_payload
    after = services.sessions.get(default_payload["id"])
    for field in (
        "id",
        "name",
        "workflow_kind",
        "source_language",
        "target_language",
        "workflow_preset",
        "included_stages_json",
        "status",
    ):
        assert getattr(after, field) == getattr(before, field)
    assert after.updated_at >= before.updated_at

    stale = client.patch(
        f"/api/v1/sessions/{default_payload['id']}",
        json={"interface_mode": "simple"},
        headers={**headers, "If-Match": created.headers["ETag"]},
    )
    assert stale.status_code == 409
    assert stale.get_json()["error"]["code"] == "revision_conflict"
    current = services.sessions.get(default_payload["id"])
    assert (current.interface_mode, current.revision) == (
        "advanced",
        updated_payload["revision"],
    )


def test_mcp_accepts_forwards_and_projects_interface_mode():
    default_input = CreateSessionInput(
        name="MCP default", idempotency_key="session:create:default"
    )
    assert default_input.interface_mode == "simple"

    class Application:
        arguments: dict[str, object]

        def create_session(self, **arguments):
            self.arguments = arguments
            return {
                "id": "mcp-session",
                "name": str(arguments["name"]),
                "workflow_kind": str(arguments["workflow_kind"]),
                "interface_mode": str(arguments["interface_mode"]),
            }

    application = Application()
    runtime = SimpleNamespace(require_application=lambda: application)
    default_outcome = create_session(runtime, default_input)
    assert application.arguments["interface_mode"] == "simple"
    assert default_outcome.result["interface_mode"] == "simple"
    outcome = create_session(
        runtime,
        CreateSessionInput(
            name="MCP advanced",
            interface_mode="advanced",
            idempotency_key="session:create:advanced",
        ),
    )
    assert application.arguments["interface_mode"] == "advanced"
    assert outcome.result["interface_mode"] == "advanced"
    assert _session_projection({"id": "legacy-session"})["interface_mode"] == "advanced"

    class CaptureRequests(ApplicationSessionMethods):
        request: tuple[tuple[object, ...], dict[str, object]]

        def _request_json(self, *args, **kwargs):
            self.request = args, kwargs
            return {}

        def _request_binary_json(self, *args, **kwargs):
            raise AssertionError("The session client should not use binary requests.")

    client = CaptureRequests()
    client.create_session(
        name="Application client default",
        workflow_kind="audiobook",
        source_language="auto",
        target_language=None,
        workflow_preset="custom",
        included_stages=(),
        idempotency_key="session:create:client-default",
    )
    assert client.request[1]["body"]["interface_mode"] == "simple"
    client.create_session(
        name="Application client",
        workflow_kind="audiobook",
        source_language="auto",
        target_language=None,
        workflow_preset="custom",
        included_stages=(),
        idempotency_key="session:create:client",
        interface_mode="advanced",
    )
    assert client.request[1]["body"]["interface_mode"] == "advanced"
