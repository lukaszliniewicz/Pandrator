"""Cross-cutting authorization, idempotency, MCP, CLI, and migration acceptance."""

from __future__ import annotations

import asyncio
import io
import sqlite3
import wave
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from alembic import command
from alembic.config import Config

from pandrator.web import cli as web_cli
from pandrator.web.api import create_app
from pandrator.web.database import SCHEMA_HEAD, sqlite_url
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.clients.application_sounds import ApplicationSoundMethods
from pandrator_mcp.context import build_runtime
from pandrator_mcp.server import build_server
from pandrator_mcp.settings import McpSettings


@pytest.fixture(scope="session")
def case(tmp_path_factory):
    root = tmp_path_factory.mktemp("sound-http") / "data"
    app = create_app(data_root=root, testing=True, background_maintenance=False)
    services = app.extensions["pandrator"]
    scopes = {
        "read": ("app.read",),
        "write": ("app.read", "app.write"),
        "run": ("app.read", "app.run"),
        "full": ("app.read", "app.write", "app.run"),
    }
    tokens = {
        name: services["auth"].create_api_token(f"sound-http-{name}", scopes=value)[1]
        for name, value in scopes.items()
    }

    def headers(scope, key=None):
        result = {"Authorization": f"Bearer {tokens[scope]}"}
        if key:
            result["Idempotency-Key"] = key
        return result

    yield {"client": app.test_client(), "services": services, "headers": headers}
    services["database"].dispose()


def _wav(sample: bytes = b"\x00\x00") -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(sample * 800)
    return output.getvalue()


def _raw_import(case, data: bytes, filename: str, key: str):
    headers = {
        **case["headers"]("full", key),
        "Content-Type": "application/octet-stream",
        "X-Audio-Filename": quote(filename, safe=""),
    }
    return case["client"].put("/api/v1/sounds/import", data=data, headers=headers)


def test_auth_scopes_cover_sound_reads_and_every_mutation_route(case):
    client = case["client"]
    anonymous = client.get("/api/v1/sounds")
    assert anonymous.status_code == 401
    assert client.post("/api/v1/sounds/generate", json={"prompt": "rain"}).status_code == 401
    assert client.get("/api/v1/sounds", headers=case["headers"]("read")).status_code == 200

    write_only_for_run = client.post(
        "/api/v1/sounds/generate",
        json={"prompt": "rain", "duration_seconds": 1.0},
        headers=case["headers"]("write", "sound-scope-run-001"),
    )
    assert write_only_for_run.status_code == 403
    assert write_only_for_run.get_json()["error"]["code"] == "scope_denied"
    missing_key = client.post(
        "/api/v1/sounds/generate",
        json={"prompt": "rain", "duration_seconds": 1.0},
        headers=case["headers"]("run"),
    )
    assert missing_key.status_code == 400
    assert missing_key.get_json()["error"]["code"] == "idempotency_key_required"

    read = case["headers"]("read")
    denied = (
        ("POST", "/api/v1/sounds/generate", {"json": {"prompt": "rain"}}),
        ("POST", "/api/v1/sounds/save", {"json": {}}),
        ("PATCH", "/api/v1/sounds/item", {"json": {}}),
        ("DELETE", "/api/v1/sounds/candidates/candidate", {}),
        ("PUT", "/api/v1/sounds/import", {"data": b"audio"}),
        ("POST", "/api/v1/sounds/import", {}),
        ("PUT", "/api/v1/voices/reference-audio/import", {"data": b"audio"}),
        ("POST", "/api/v1/sessions/session/sound-plan/cues", {"json": {}}),
        ("PATCH", "/api/v1/sessions/session/sound-plan/cues/cue", {"json": {}}),
        ("DELETE", "/api/v1/sessions/session/sound-plan/cues/cue", {}),
        ("POST", "/api/v1/sessions/session/sound-plan/restore", {"json": {}}),
    )
    for method, path, options in denied:
        response = client.open(path, method=method, headers=read, **options)
        assert response.status_code == 403, (method, path, response.get_json())
        assert response.get_json()["error"]["code"] == "scope_denied"


def test_save_patch_discard_replay_and_revision_conflict(case):
    client, headers = case["client"], case["headers"]
    imported = _raw_import(case, _wav(), "save-check.wav", "sound-save-import-001")
    assert imported.status_code == 201
    candidate_id = imported.get_json()["id"]
    save_body = {"candidate_id": candidate_id, "name": "Saved tone", "tags": ["tone"]}
    first = client.post(
        "/api/v1/sounds/save", json=save_body, headers=headers("full", "sound-save-replay-001")
    )
    replay = client.post(
        "/api/v1/sounds/save", json=save_body, headers=headers("full", "sound-save-replay-001")
    )
    assert first.status_code == replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.get_json() == first.get_json()
    item_id = first.get_json()["item_id"]

    patch_body = {"expected_revision": 1, "archived": True}
    edited = client.patch(
        f"/api/v1/sounds/{item_id}",
        json=patch_body,
        headers=headers("full", "sound-patch-replay-001"),
    )
    patch_replay = client.patch(
        f"/api/v1/sounds/{item_id}",
        json=patch_body,
        headers=headers("full", "sound-patch-replay-001"),
    )
    assert edited.status_code == patch_replay.status_code == 200
    assert patch_replay.headers["Idempotency-Replayed"] == "true"
    assert patch_replay.get_json() == edited.get_json()
    stale = client.patch(
        f"/api/v1/sounds/{item_id}",
        json=patch_body,
        headers=headers("full", "sound-patch-stale-001"),
    )
    assert stale.status_code == 409
    assert stale.get_json()["error"]["code"] == "revision_conflict"

    discard_source = _raw_import(case, _wav(b"\x02\x00"), "discard.wav", "sound-discard-import-001")
    assert discard_source.status_code == 201
    path = f"/api/v1/sounds/candidates/{discard_source.get_json()['id']}"
    dropped = client.delete(path, headers=headers("full", "sound-discard-replay-001"))
    dropped_replay = client.delete(path, headers=headers("full", "sound-discard-replay-001"))
    assert dropped.status_code == dropped_replay.status_code == 200
    assert dropped_replay.headers["Idempotency-Replayed"] == "true"
    assert dropped_replay.get_json() == dropped.get_json()
    assert dropped.get_json()["status"] == "discarded"


def test_mcp_and_cli_composition_register_sound_contracts(case, tmp_path, monkeypatch):
    assert issubclass(ApplicationClient, ApplicationSoundMethods)
    services = case["services"]
    assert services["sound_mixes"] is services["services"].sound_mixes
    assert services["sound_resolution"] is services["services"].sound_resolution

    async def list_sound_tools():
        runtime = build_runtime(
            McpSettings(
                target_name="unconfigured", configuration_path=tmp_path / "absent-targets.json"
            )
        )
        async with Client(build_server(runtime), mode="auto", raise_exceptions=True) as client:
            names = [tool.name for tool in (await client.list_tools()).tools]
            sound_names = [
                name
                for name in names
                if (name.startswith("pandrator_") and "sound" in name)
                or name == "pandrator_import_audio_reference"
            ]
            assert len(names) == len(set(names))
            assert set(sound_names) == {
                "pandrator_sound_catalogue",
                "pandrator_generate_sound",
                "pandrator_sound_library",
                "pandrator_import_audio_reference",
                "pandrator_project_sounds",
            }

    from mcp import Client

    asyncio.run(list_sound_tools())

    registries = []

    class CaptureWorker:
        def __init__(self, _queue, _worker_id, registry):
            registries.append(registry)

        def stop(self):
            return None

        def run_once(self):
            return False

        def run_forever(self, *_args, **_kwargs):
            raise AssertionError("the test worker must remain idle")

    @contextmanager
    def no_signal_handlers(_stop):
        yield False

    monkeypatch.setattr(web_cli, "Worker", CaptureWorker)
    monkeypatch.setattr(web_cli, "worker_termination", no_signal_handlers)
    assert (
        web_cli.command_worker(
            SimpleNamespace(
                data_dir=case["services"]["paths"].root,
                command="worker",
                worker_id="sound-registry-test",
                once=True,
                poll_interval=0.1,
            )
        )
        == 0
    )
    registration = registries[0].registrations(domain="sounds")
    assert [item.kind for item in registration] == [
        "sound.generate",
        "sound.mix",
        "sound.resolve",
    ]
    assert [item.payload_contract.required_fields for item in registration] == [
        (
            "candidate_id",
            "request",
            "runtime_identity",
        ),
        ("session_id", "base_artifact_id", "request_fingerprint"),
        ("session_id", "pins", "pin_hash"),
    ]


def test_fresh_and_0054_to_head_migrations_pass_sqlite_foreign_key_check(case, tmp_path):
    services = case["services"]
    database_path = tmp_path / "sound-migrations.sqlite"
    with (
        sqlite3.connect(services["paths"].database) as source,
        sqlite3.connect(database_path) as target,
    ):
        source.backup(target)
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).parents[1] / "pandrator" / "web" / "migrations")
    )
    config.set_main_option("sqlalchemy.url", sqlite_url(database_path))
    with sqlite3.connect(services["paths"].database) as fresh:
        assert (
            fresh.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == SCHEMA_HEAD
        )
        assert fresh.execute("PRAGMA foreign_key_check").fetchall() == []

    command.downgrade(config, "0054_session_interface_mode")
    with sqlite3.connect(database_path) as legacy:
        assert (
            legacy.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0054_session_interface_mode"
        )
        assert not {
            "sound_items",
            "sound_variants",
            "project_sound_plans",
            "project_sound_cues",
        }.intersection(
            row[0] for row in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")
        )
    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as upgraded:
        assert (
            upgraded.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == SCHEMA_HEAD
        )
        assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {
            row[0] for row in upgraded.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "sound_items",
            "sound_variants",
            "project_sound_plans",
            "project_sound_cues",
        } <= tables
