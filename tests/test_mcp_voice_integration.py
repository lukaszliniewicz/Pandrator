from __future__ import annotations

import asyncio
import io
import json
import tempfile
import threading
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from mcp import Client
from werkzeug.serving import make_server

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.jobs import Worker
from pandrator.web.models import Artifact, SessionRecord
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.clients.manager_gateway import ManagerUnavailableGateway
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.credentials import CredentialResolver
from pandrator_mcp.guide_registry import GuideRegistry
from pandrator_mcp.network_policy import NetworkPolicy, TargetMode
from pandrator_mcp.server import build_server
from pandrator_mcp.settings import McpSettings
from pandrator_mcp.targets import TargetProfile, TargetRegistry
from tests.web_test_support import prepare_web_test_data_root


def _silent_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\0\0" * 160)
    return output.getvalue()


@contextmanager
def _running_runtime() -> Iterator[tuple[McpRuntime, Any, Any]]:
    temporary = tempfile.TemporaryDirectory(prefix="pandrator-mcp-voice-")
    server = None
    thread = None
    app = None
    try:
        paths = prepare_web_test_data_root(temporary.name)
        bootstrap = BootstrapTokenStore()
        bootstrap_token = bootstrap.issue()
        app = create_app(
            data_root=paths.root,
            testing=True,
            background_maintenance=False,
            bootstrap_tokens=bootstrap,
        )
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"

        profile = TargetProfile(
            name="local",
            mode=TargetMode.LOCAL_MANAGED,
            workspace=str(paths.root),
        )
        registry = TargetRegistry(
            [profile],
            network_policy=NetworkPolicy(lambda _host, _port: ("127.0.0.1",)),
            local_discovery=lambda _profile: (origin, "manager-id"),
        )

        def local_bootstrap(target, session) -> str:
            response = session.post(
                f"{target.application.origin}/api/v1/auth/bootstrap",
                json={"token": bootstrap_token},
                timeout=5,
                allow_redirects=False,
            )
            response.raise_for_status()
            return str(response.json()["csrf_token"])

        application = ApplicationClient(
            registry.bind("local"),
            CredentialResolver(()),
            local_bootstrap=local_bootstrap,
            timeout_seconds=5,
        )
        runtime = McpRuntime(
            settings=McpSettings(
                target_name="local",
                configuration_path=paths.root / "mcp-targets.json",
            ),
            guides=GuideRegistry(),
            application=application,
            manager=ManagerUnavailableGateway(),
            profile=profile,
        )
        yield runtime, app, paths
    finally:
        if server is not None:
            server.shutdown()
        if thread is not None:
            thread.join(timeout=5)
        if app is not None:
            app.extensions["pandrator"]["database"].dispose()
        temporary.cleanup()


def _error_code(result: Any) -> str:
    assert result.is_error, result
    text = result.content[0].text
    payload = json.loads(text[text.index("{") :])
    return str(payload["code"])


async def _call(
    client: Any, name: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    result = await client.call_tool(name, arguments or {})
    assert not result.is_error, f"{name}: {result.content}"
    assert result.structured_content == json.loads(result.content[0].text)
    envelope = result.structured_content
    assert envelope["schema_version"] == "1"
    return envelope["result"]


def test_mcp_voice_and_session_lifecycle_reaches_native_http_routes():
    provider_catalog = {
        "services": [
            {
                "id": "fixture_tts",
                "name": "Fixture TTS",
                "available": True,
                "models": ["fixture-model"],
                "model_catalog": [{"id": "fixture-model", "voice_mode": "prebuilt"}],
                "voices": ["Fixture Voice"],
            }
        ]
    }

    with _running_runtime() as (runtime, app, paths):

        async def exercise() -> None:
            async with Client(
                build_server(runtime), mode="auto", raise_exceptions=True
            ) as client:
                registered = {tool.name for tool in (await client.list_tools()).tools}
                assert {
                    "pandrator_get_voice_capabilities",
                    "pandrator_create_voice",
                    "pandrator_get_voice_catalog",
                    "pandrator_create_voice_collection",
                    "pandrator_update_voice_collection",
                    "pandrator_list_voice_collections",
                    "pandrator_create_session",
                    "pandrator_get_generation_controls",
                    "pandrator_update_generation_controls",
                    "pandrator_list_sessions",
                    "pandrator_trash_session",
                    "pandrator_restore_session",
                    "pandrator_delete_output",
                    "pandrator_list_artifacts",
                    "pandrator_import_voice_reference",
                    "pandrator_get_voice_samples",
                } <= registered

                capabilities = await _call(
                    client,
                    "pandrator_get_voice_capabilities",
                )
                assert capabilities["markup"]["format"] == "speech_xml"
                model = next(
                    item
                    for item in capabilities["models"]
                    if item["model"] == "fixture-model"
                )
                assert model["modes"]["prebuilt"] is True

                voice_arguments = {
                    "name": "Scrooge",
                    "language": "en",
                    "voice_category": "male",
                    "profile": {
                        "pitch": "low",
                        "languages": [
                            {
                                "language": "en",
                                "accent": "Scottish",
                                "evidence": {
                                    "source": "user",
                                    "status": "described",
                                },
                            }
                        ],
                    },
                    "idempotency_key": "voice-create-scrooge",
                }
                voice = await _call(client, "pandrator_create_voice", voice_arguments)
                retry = await _call(client, "pandrator_create_voice", voice_arguments)
                assert retry["id"] == voice["id"]

                catalog = await _call(
                    client,
                    "pandrator_get_voice_catalog",
                    {"accent": "Scottish", "kind": "managed"},
                )
                assert [item["id"] for item in catalog["items"]] == [voice["id"]]

                collection = await _call(
                    client,
                    "pandrator_create_voice_collection",
                    {
                        "name": "Christmas Carol",
                        "idempotency_key": "collection-create-scrooge",
                    },
                )
                member_reference = {"kind": "managed", "voice_id": voice["id"]}
                updated_collection = await _call(
                    client,
                    "pandrator_update_voice_collection",
                    {
                        "collection_id": collection["id"],
                        "expected_revision": collection["revision"],
                        "add_members": [member_reference],
                        "idempotency_key": "collection-add-scrooge",
                    },
                )
                assert updated_collection["revision"] == collection["revision"] + 1
                assert updated_collection["members"][0]["reference"] == member_reference
                collections = await _call(client, "pandrator_list_voice_collections")
                listed_collection = next(
                    item
                    for item in collections["items"]
                    if item["id"] == collection["id"]
                )
                assert listed_collection["members"][0]["reference"] == member_reference
                collection_catalog = await _call(
                    client,
                    "pandrator_get_voice_catalog",
                    {"collection_id": collection["id"]},
                )
                assert [item["id"] for item in collection_catalog["items"]] == [
                    voice["id"]
                ]

                session = await _call(
                    client,
                    "pandrator_create_session",
                    {
                        "name": "Scrooge audiobook",
                        "workflow_kind": "audiobook",
                        "source_language": "en",
                        "workflow_preset": "custom",
                        "included_stages": ["prepare_text", "generate_audio"],
                        "idempotency_key": "session-create-scrooge",
                    },
                )
                session_id = session["id"]
                controls = await _call(
                    client,
                    "pandrator_get_generation_controls",
                    {"session_id": session_id},
                )
                cast = {
                    "narrator": {"voice_id": voice["id"]},
                    "characters": {"scrooge": {"voice_id": voice["id"]}},
                }
                controls_update = await _call(
                    client,
                    "pandrator_update_generation_controls",
                    {
                        "session_id": session_id,
                        "expected_revision": controls["revision"],
                        "characters": [
                            {
                                "id": "scrooge",
                                "display_name": "Scrooge",
                                "status": "accepted",
                            }
                        ],
                        "cast": cast,
                        "idempotency_key": "controls-update-scrooge",
                    },
                )
                assert controls_update["cast"]["narrator"]["voice_id"] == voice["id"]
                assert (
                    controls_update["cast"]["characters"]["scrooge"]["voice_id"]
                    == voice["id"]
                )
                refreshed_controls = await _call(
                    client,
                    "pandrator_get_generation_controls",
                    {"session_id": session_id},
                )
                assert refreshed_controls == controls_update

                stale_controls = await client.call_tool(
                    "pandrator_update_generation_controls",
                    {
                        "session_id": session_id,
                        "expected_revision": controls["revision"],
                        "cast": cast,
                        "idempotency_key": "controls-stale-scrooge",
                    },
                )
                assert _error_code(stale_controls) == "revision_conflict"

                stale_trash = await client.call_tool(
                    "pandrator_trash_session",
                    {"session_id": session_id, "expected_revision": 0},
                )
                assert _error_code(stale_trash) == "revision_conflict"
                trashed = await _call(
                    client,
                    "pandrator_trash_session",
                    {
                        "session_id": session_id,
                        "expected_revision": session["revision"],
                    },
                )
                default_sessions = await _call(client, "pandrator_list_sessions")
                assert session_id not in {
                    item["id"] for item in default_sessions["items"]
                }
                trashed_sessions = await _call(
                    client,
                    "pandrator_list_sessions",
                    {"include_trashed": True},
                )
                assert session_id in {item["id"] for item in trashed_sessions["items"]}
                restored = await _call(
                    client,
                    "pandrator_restore_session",
                    {
                        "session_id": session_id,
                        "expected_revision": trashed["revision"],
                    },
                )
                assert restored["id"] == session_id
                assert restored["revision"] == trashed["revision"] + 1

                extension = app.extensions["pandrator"]
                with extension["database"].session() as db_session:
                    record = db_session.get(SessionRecord, session_id)
                    assert record is not None
                    session_directory = paths.sessions / record.storage_key
                output_path = session_directory / "exports" / "scrooge.wav"
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(_silent_wav())
                output = extension["artifacts"].register(
                    output_path,
                    kind="export",
                    role="export",
                    session_id=session_id,
                )
                source_path = session_directory / "source.txt"
                source_path.write_text("source", encoding="utf-8")
                source = extension["artifacts"].register(
                    source_path,
                    kind="text",
                    role="source",
                    session_id=session_id,
                )
                before_delete = await _call(
                    client,
                    "pandrator_list_artifacts",
                    {"session_id": session_id},
                )
                assert output.id in {item["id"] for item in before_delete["items"]}
                deleted = await _call(
                    client,
                    "pandrator_delete_output",
                    {"session_id": session_id, "artifact_id": output.id},
                )
                assert deleted["state"] == "deleted"
                assert not output_path.exists()
                after_delete = await _call(
                    client,
                    "pandrator_list_artifacts",
                    {"session_id": session_id},
                )
                assert output.id not in {item["id"] for item in after_delete["items"]}
                assert source.id in {item["id"] for item in after_delete["items"]}
                with extension["database"].session() as db_session:
                    tombstone = db_session.get(Artifact, output.id)
                    assert tombstone is not None
                    assert tombstone.state == "deleted"
                    assert tombstone.metadata_json.get("deleted_at")
                protected_source = await client.call_tool(
                    "pandrator_delete_output",
                    {"session_id": session_id, "artifact_id": source.id},
                )
                assert _error_code(protected_source) == "revision_conflict"
                assert source_path.exists()

                reference_path = paths.artifacts / "scrooge-reference.wav"
                reference_path.write_bytes(_silent_wav())
                reference = extension["artifacts"].register(
                    reference_path,
                    kind="audio",
                    role="voice_reference",
                )
                imported = await _call(
                    client,
                    "pandrator_import_voice_reference",
                    {
                        "voice_id": voice["id"],
                        "artifact_id": reference.id,
                        "transcript": "Unreviewed reference words.",
                        "transcript_reviewed": False,
                        "language": "en",
                        "expected_voice_revision": voice["revision"],
                        "idempotency_key": "voice-import-scrooge",
                    },
                )
                assert imported["status"] in {"queued", "running"}
                worker = Worker(
                    extension["jobs"],
                    "mcp-voice-integration",
                    extension["workflow_handlers"].handlers(),
                )
                assert worker.run_once()
                samples = await _call(
                    client,
                    "pandrator_get_voice_samples",
                    {"voice_id": voice["id"]},
                )
                sample = next(
                    item
                    for item in samples["items"]
                    if item["artifact_id"] != reference.id
                )
                assert sample["available"] is True
                assert sample["file_status"] == "ready"
                assert sample["transcript"] == "Unreviewed reference words."
                assert sample["transcript_reviewed"] is False

        with patch(
            "pandrator.web.tts_providers.TtsCatalogueService.snapshot",
            return_value=(provider_catalog, 1),
        ):
            asyncio.run(exercise())
