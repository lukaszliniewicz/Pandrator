import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

from pydantic import ValidationError

from pandrator_mcp.context import McpRuntime
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.network_policy import TargetMode
from pandrator_mcp.schemas import (
    BrowseLocalSourcesInput,
    ConfigureTtsInput,
    CreateTextSourceInput,
    DownloadArtifactInput,
    ImportLocalSourceInput,
    ListGenerationRunsInput,
    PlanExportVariantInput,
    TtsCatalogInput,
)
from pandrator_mcp.targets import LocalSourceRoot, TargetProfile
from pandrator_mcp.tools.e2e import (
    _open_contained_file,
    _safe_tts_service,
    browse_local_sources,
    configure_tts,
    create_text_source,
    download_artifact,
    import_local_source,
    list_generation_runs,
    plan_export_variant,
    tts_catalog,
)


def test_audio_cpp_model_selectability_preserves_unknown_health_and_advertisement():
    for availability, expected, reason in (
        (None, None, "health_unknown"),
        (False, False, "service_unavailable"),
        (True, True, "available"),
    ):
        service = {
            "id": "audio_cpp", "adapter": "audio_cpp", "available": availability,
            "models": ["listed-model"],
            "model_catalog": [{"id": "listed-model"}, {"id": "other-model"}],
        }
        rows = _safe_tts_service(service, detail="summary")["model_states"]
        assert rows[0] == {
            "id": "listed-model", "listed": True, "selectable": expected,
            "selectability_reason": reason, "loaded": None,
        }
        assert rows[1] == {
            "id": "other-model", "listed": False, "selectable": False,
            "selectability_reason": "not_advertised", "loaded": None,
        }


class _Application:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.uploaded = b""

    def list_sources(self):
        return {"items": []}

    def initialize_upload(self, **kwargs):
        self.calls.append(("initialize_upload", kwargs))
        return {
            "id": "upload-1",
            "state": "open",
            "chunk_size": 1024 * 1024,
            "chunk_count": 1,
            "received": [],
        }

    def upload_chunk(self, upload_id, index, body, *, sha256):
        self.calls.append(
            (
                "upload_chunk",
                {
                    "upload_id": upload_id,
                    "index": index,
                    "sha256": sha256,
                },
            )
        )
        self.uploaded += body
        return {"index": index}

    def complete_upload(self, upload_id):
        self.calls.append(("complete_upload", {"upload_id": upload_id}))
        return {
            "artifact_id": "artifact-1",
            "source_asset_id": "source-1",
        }

    def attach_existing_source(self, session_id, **kwargs):
        self.calls.append(("attach", {"session_id": session_id, **kwargs}))
        return {"id": "attachment-1", "session_revision": 2}

    def tts_catalog(self, *, refresh):
        self.calls.append(("tts_catalog", {"refresh": refresh}))
        return {
            "default_service": "service-a",
            "revision": 7,
            "services": getattr(self, "services", [
                {
                    "id": "service-a",
                    "name": "Service A",
                    "available": True,
                    "models": ["model-a"],
                    "default_model": "model-a",
                    "voices": ["native-a"],
                    "default_voice": "native-a",
                    "generation_prompt_models": ["model-a"],
                    "base_url": "https://must-not-leak.example",
                    "credential_reference": "must-not-leak",
                }
            ]),
        }

    def list_voices(self):
        return {
            "items": [
                {
                    "id": "managed-1",
                    "name": "Narrator",
                    "language": "en",
                    "revision": 3,
                    "metadata_json": {
                        "providers": {
                            "service-a": {
                                "status": "ready",
                                "voice_id": "provider-voice-9",
                                "credential": "must-not-leak",
                            }
                        }
                    },
                }
            ]
        }

    def get_session_settings(self, session_id, section):
        self.calls.append(
            ("get_session_settings", {"session_id": session_id, "section": section})
        )
        return {"revision": 4, "override": getattr(self, "settings_override", {"speed": 0.95})}

    def update_session_settings(self, session_id, **kwargs):
        self.calls.append(
            ("update_session_settings", {"session_id": session_id, **kwargs})
        )
        return {"revision": 5}

    def list_generation_runs(self, session_id, **kwargs):
        return {
            "items": [
                {
                    "id": "run-1",
                    "status": "completed",
                    "take_count": 4,
                    "settings_snapshot": {"tts": {"provider_configs": [{"id": "unused"}]}},
                }
            ]
        }

    def create_workflow_plan(self, session_id, **kwargs):
        self.calls.append(
            ("create_workflow_plan", {"session_id": session_id, **kwargs})
        )
        return {"id": "plan-1", **kwargs}

    def artifact_context(self, artifact_id):
        return {
            "artifact": {
                "id": artifact_id,
                "state": "current",
                "size_bytes": 4,
                "content_hash": "a" * 64,
                "relative_path": "artifacts/final.mp4",
                "metadata_json": {},
            }
        }

    def download_artifact(self, artifact_id, destination, **kwargs):
        self.calls.append(
            (
                "download_artifact",
                {"artifact_id": artifact_id, "destination": destination, **kwargs},
            )
        )
        destination.write_bytes(b"data")
        return {
            "path": str(destination),
            "size_bytes": 4,
            "sha256": "a" * 64,
            "resumed": False,
            "reused": False,
        }


def _runtime(root: Path, output: Path | None = None):
    application = _Application()
    profile = TargetProfile(
        name="local",
        mode=TargetMode.LOCAL_MANAGED,
        workspace=str(root),
        local_source_roots=(LocalSourceRoot(name="downloads", path=str(root)),),
        local_output_root=str(output) if output else None,
    )
    return (
        SimpleNamespace(
            profile=profile,
            require_application=lambda: application,
        ),
        application,
    )


def _audio_cpp_service(
    models: list[str],
    *,
    available: bool = True,
    default_model: str | None = None,
    **metadata,
) -> dict:
    return {
        "id": "audio_cpp",
        "adapter": "audio_cpp",
        "available": available,
        "models": models,
        "default_model": default_model or (models[0] if models else ""),
        "voices": [],
        "default_voice": "",
        **metadata,
    }


def _tts_catalog_payload(*services: dict) -> dict:
    return {
        "default_service": "audio_cpp",
        "revision": 7,
        "services": list(services),
    }


def _configure_audio_cpp(
    runtime,
    *,
    model=None,
    voice=None,
    key="audio-cpp-test",
    service_id="audio_cpp",
):
    return configure_tts(
        runtime,
        ConfigureTtsInput(
            session_id="session-1",
            service_id=service_id,
            model=model,
            voice=voice,
            expected_revision=4,
            idempotency_key=key,
        ),
    )


class McpEndToEndToolTests(unittest.TestCase):
    def test_configure_tts_registered_schema_and_mcp_transport(self):
        from mcp import Client

        from pandrator_mcp.server import build_server

        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                runtime, application = _runtime(Path(directory))
                application.services = [{
                    "id": "gemini", "adapter": "gemini", "available": True,
                    "models": ["gemini-tts"], "default_model": "gemini-tts",
                    "voices": ["Aoede"], "default_voice": "Aoede",
                    "expressive_capabilities": {"gemini-tts": {
                        "instructions": "prompt", "semantic_context": "prompt",
                        "event_tags": {"laugh": "laugh"},
                    }},
                }]
                async with Client(build_server(runtime), mode="auto", raise_exceptions=True) as client:
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    schema = tools["pandrator_configure_tts"].input_schema
                    for field in (
                        "tts_context_mode", "performance_context_before",
                        "performance_context_after", "performance_context_max_chars",
                        "performance_allow_vocalizations", "elevenlabs_voice_settings",
                    ):
                        self.assertIn(field, schema["properties"])
                    result = await client.call_tool("pandrator_configure_tts", {
                        "session_id": "session-1", "service_id": "gemini",
                        "expected_revision": 4, "idempotency_key": "mcp-gemini-1",
                        "tts_context_mode": "both", "performance_context_before": 4,
                        "performance_context_after": 3,
                        "performance_context_max_chars": 6000,
                        "performance_allow_vocalizations": True,
                        "style_instructions": "Clear narration",
                    })
                    self.assertFalse(result.is_error)
                    value = application.calls[-1][1]["value"]
                    self.assertEqual("both", value["tts_context_mode"])
                    self.assertEqual("gemini-tts", value["xtts_model"])
                    self.assertEqual("Aoede", value["speaker"])
                    updates = len([name for name, _ in application.calls if name == "update_session_settings"])
                    bad = await client.call_tool("pandrator_configure_tts", {
                        "session_id": "session-1", "service_id": "gemini",
                        "expected_revision": 4, "idempotency_key": "mcp-gemini-2",
                        "performance_context_before": 21,
                    })
                    self.assertTrue(bad.is_error)
                    self.assertEqual(updates, len([name for name, _ in application.calls if name == "update_session_settings"]))

        asyncio.run(exercise())

    def test_configure_audio_cpp_refreshes_stale_models_once_and_normalizes_service_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, application = _runtime(Path(directory))
            stale = _audio_cpp_service(
                ["qwen3_tts_0_6b_base_q8_0"],
                model_voice_modes={"qwen3_tts_0_6b_base_q8_0": "cloning"},
            )
            refreshed = _audio_cpp_service(
                ["kokoro_82m_q8_0"],
                adapter="audio-cpp",
                voice_catalogues={"kokoro_82m_q8_0": ["af_heart"]},
                model_voice_modes={"kokoro_82m_q8_0": "prebuilt"},
                default_voice="af_heart",
            )
            application.tts_catalog = mock.Mock(
                side_effect=[_tts_catalog_payload(stale), _tts_catalog_payload(refreshed)]
            )

            outcome = _configure_audio_cpp(
                runtime,
                model="KOKORO_82M_Q8_0",
                voice="AF_HEART",
                service_id="AUDIO-CPP",
            )

            self.assertEqual(
                [mock.call(refresh=False), mock.call(refresh=True)],
                application.tts_catalog.call_args_list,
            )
            updates = [
                kwargs
                for name, kwargs in application.calls
                if name == "update_session_settings"
            ]
            self.assertEqual(1, len(updates))
            self.assertEqual("kokoro_82m_q8_0", updates[0]["value"]["model"])
            self.assertEqual("af_heart", updates[0]["value"]["voice"])
            self.assertEqual("kokoro_82m_q8_0", outcome.result["selection"]["model"])

    def test_configure_audio_cpp_rejects_models_still_missing_after_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, application = _runtime(Path(directory))
            stale = _audio_cpp_service(["qwen3_tts_0_6b_base_q8_0"])
            refreshed = _audio_cpp_service(["kokoro_82m_q8_0"])
            application.tts_catalog = mock.Mock(
                side_effect=[_tts_catalog_payload(stale), _tts_catalog_payload(refreshed)]
            )

            with self.assertRaises(PandratorMcpError) as captured:
                _configure_audio_cpp(runtime, model="not-installed")

            self.assertEqual("validation_error", captured.exception.code)
            self.assertEqual(
                [mock.call(refresh=False), mock.call(refresh=True)],
                application.tts_catalog.call_args_list,
            )
            self.assertFalse(
                any(name == "update_session_settings" for name, _ in application.calls)
            )

    def test_configure_audio_cpp_known_case_variant_and_default_skip_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, application = _runtime(Path(directory))
            service = _audio_cpp_service(
                ["kokoro_82m_q8_0"],
                model_voice_modes={"kokoro_82m_q8_0": "prebuilt"},
                default_voice="af_heart",
            )
            application.tts_catalog = mock.Mock(
                side_effect=[_tts_catalog_payload(service), _tts_catalog_payload(service)]
            )

            explicit = _configure_audio_cpp(runtime, model="KOKORO_82M_Q8_0")
            self.assertEqual("kokoro_82m_q8_0", explicit.result["selection"]["model"])
            self.assertEqual([mock.call(refresh=False)], application.tts_catalog.call_args_list)

            application.calls.clear()
            application.tts_catalog.reset_mock()
            default = _configure_audio_cpp(runtime)
            self.assertEqual("kokoro_82m_q8_0", default.result["selection"]["model"])
            self.assertEqual([mock.call(refresh=False)], application.tts_catalog.call_args_list)
            self.assertEqual(
                1,
                len([name for name, _ in application.calls if name == "update_session_settings"]),
            )

            default_only = _audio_cpp_service(
                [],
                default_model="kokoro_82m_q8_0",
                default_voice="af_heart",
            )
            application.calls.clear()
            application.tts_catalog = mock.Mock(
                side_effect=[_tts_catalog_payload(default_only)]
            )
            explicit_default = _configure_audio_cpp(
                runtime,
                model="KOKORO_82M_Q8_0",
            )
            self.assertEqual("KOKORO_82M_Q8_0", explicit_default.result["selection"]["model"])
            self.assertEqual([mock.call(refresh=False)], application.tts_catalog.call_args_list)

    def test_configure_tts_does_not_refresh_missing_models_for_other_families(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, application = _runtime(Path(directory))
            service = {
                "id": "service-a",
                "adapter": "gemini",
                "available": True,
                "models": ["model-a"],
                "default_model": "model-a",
            }
            application.tts_catalog = mock.Mock(
                return_value=_tts_catalog_payload(service)
            )

            with self.assertRaises(PandratorMcpError) as captured:
                configure_tts(
                    runtime,
                    ConfigureTtsInput(
                        session_id="session-1",
                        service_id="service-a",
                        model="missing-model",
                        expected_revision=4,
                        idempotency_key="non-audio-cpp",
                    ),
                )

            self.assertEqual("validation_error", captured.exception.code)
            self.assertEqual([mock.call(refresh=False)], application.tts_catalog.call_args_list)
            self.assertFalse(
                any(name == "update_session_settings" for name, _ in application.calls)
            )

    def test_configure_audio_cpp_refresh_uses_new_managed_voice_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, application = _runtime(Path(directory))
            stale = _audio_cpp_service(
                ["qwen3_tts_0_6b_base_q8_0"],
                model_voice_modes={"qwen3_tts_0_6b_base_q8_0": "cloning"},
            )
            refreshed = _audio_cpp_service(
                ["qwen3_tts_1_7b_base_q8_0"],
                model_voice_modes={"qwen3_tts_1_7b_base_q8_0": "cloning"},
            )
            application.tts_catalog = mock.Mock(
                side_effect=[_tts_catalog_payload(stale), _tts_catalog_payload(refreshed)]
            )
            application.list_voices = mock.Mock(
                side_effect=[
                    {"items": []},
                    {
                        "items": [
                            {
                                "id": "managed-qwen",
                                "name": "Narrator",
                                "metadata_json": {
                                    "providers": {
                                        "audio_cpp": {
                                            "status": "ready",
                                            "voice_id": "fresh-reference",
                                        }
                                    }
                                },
                            }
                        ]
                    },
                ]
            )

            outcome = _configure_audio_cpp(
                runtime,
                model="qwen3_tts_1_7b_base_q8_0",
                voice="Narrator",
            )

            self.assertEqual("fresh-reference", outcome.result["selection"]["voice"])
            self.assertEqual(
                [mock.call(refresh=False), mock.call(refresh=True)],
                application.tts_catalog.call_args_list,
            )
            self.assertEqual(2, application.list_voices.call_count)
            self.assertEqual(
                1,
                len([name for name, _ in application.calls if name == "update_session_settings"]),
            )

    def test_configure_audio_cpp_refresh_keeps_missing_and_offline_service_errors(self):
        cases = (
            ((), "not_found"),
            (
                (
                    _audio_cpp_service(
                        ["kokoro_82m_q8_0"],
                        available=False,
                        availability_reason="audio.cpp is offline",
                    ),
                ),
                "source_unavailable",
            ),
        )
        for refreshed_services, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with tempfile.TemporaryDirectory() as directory:
                    runtime, application = _runtime(Path(directory))
                    stale = _audio_cpp_service(["qwen3_tts_0_6b_base_q8_0"])
                    application.tts_catalog = mock.Mock(
                        side_effect=[
                            _tts_catalog_payload(stale),
                            _tts_catalog_payload(*refreshed_services),
                        ]
                    )

                    with self.assertRaises(PandratorMcpError) as captured:
                        _configure_audio_cpp(runtime, model="kokoro_82m_q8_0")

                    self.assertEqual(expected_code, captured.exception.code)
                    self.assertEqual(
                        [mock.call(refresh=False), mock.call(refresh=True)],
                        application.tts_catalog.call_args_list,
                    )
                    self.assertFalse(
                        any(
                            name == "update_session_settings"
                            for name, _ in application.calls
                        )
                    )

    def test_tts_capability_guards_and_atomic_context_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, application = _runtime(Path(directory))
            application.services = [{
                "id": "gemini", "adapter": "gemini", "available": True,
                "models": ["gemini-2.5-flash-preview-tts"],
                "default_model": "gemini-2.5-flash-preview-tts",
                "voices": ["Aoede"], "default_voice": "Aoede",
                "expressive_capabilities": {
                    "gemini-2.5-flash-preview-tts": {
                        "instructions": "prompt", "semantic_context": "prompt",
                        "event_tags": {"laugh": "laugh"},
                    }
                },
            }]
            application.settings_override = {
                "speed": 0.95, "service": "audio_cpp", "tts_service": "audio_cpp",
                "model": "old", "xtts_model": "old", "voice": "old", "speaker": "old",
            }
            outcome = configure_tts(runtime, ConfigureTtsInput(
                session_id="session-1", service_id="gemini",
                model="gemini-2.5-flash-preview-tts", voice="Aoede",
                style_instructions="Warm narrator", tts_context_mode="both",
                performance_context_before=4, performance_context_after=3,
                performance_context_max_chars=6000,
                performance_allow_vocalizations=True,
                expected_revision=4, idempotency_key="gemini-combined-1",
            ))
            updates = [call for call in application.calls if call[0] == "update_session_settings"]
            self.assertEqual(1, len(updates))
            update = updates[0][1]
            self.assertEqual(4, update["expected_revision"])
            self.assertEqual("gemini-combined-1", update["idempotency_key"])
            self.assertEqual("gemini", update["value"]["service"])
            self.assertEqual("gemini", update["value"]["tts_service"])
            self.assertEqual("gemini-2.5-flash-preview-tts", update["value"]["xtts_model"])
            self.assertEqual("Aoede", update["value"]["speaker"])
            self.assertEqual("both", update["value"]["tts_context_mode"])
            self.assertTrue(update["value"]["performance_allow_vocalizations"])
            self.assertEqual(0.95, update["value"]["speed"])
            self.assertEqual(5, outcome.result["settings_revision"])
            self.assertEqual(6000, outcome.result["selection"]["context"]["max_chars"])

            application.calls.clear()
            application.services[0]["expressive_capabilities"]["gemini-2.5-flash-preview-tts"] = {
                "instructions": "none", "semantic_context": "none", "event_tags": {},
            }
            for changes in (
                {"tts_context_mode": "before"},
                {"style_instructions": "unsupported"},
                {"performance_allow_vocalizations": True},
            ):
                with self.assertRaises(PandratorMcpError) as captured:
                    configure_tts(runtime, ConfigureTtsInput(
                        session_id="session-1", service_id="gemini",
                        expected_revision=4, idempotency_key="gemini-reject-1",
                        **changes,
                    ))
                self.assertEqual("validation_error", captured.exception.code)
            self.assertFalse(any(name == "update_session_settings" for name, _ in application.calls))

            clear = configure_tts(runtime, ConfigureTtsInput(
                session_id="session-1", service_id="gemini", style_instructions="",
                tts_context_mode="off", performance_allow_vocalizations=False,
                expected_revision=4, idempotency_key="gemini-clear-1",
            ))
            self.assertEqual("", application.calls[-1][1]["value"]["generation_prompt"])
            self.assertTrue(clear.result["selection"]["style_instructions_applied"])

    def test_tts_provider_aliases_and_native_elevenlabs_voice_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, application = _runtime(Path(directory))
            application.services = [{
                "id": "elevenlabs", "adapter": "elevenlabs_native", "available": True,
                "models": ["eleven_v3", "eleven_multilingual_v2"],
                "default_model": "eleven_v3", "voices": ["new-voice"], "default_voice": "new-voice",
                "expressive_capabilities": {
                    "eleven_v3": {"instructions": "inline", "semantic_context": "none", "event_tags": {"laugh": "[laughs]"}},
                    "eleven_multilingual_v2": {"instructions": "none", "semantic_context": "field", "event_tags": {}},
                },
            }]
            application.settings_override = {
                "elevenlabs_model": "old", "elevenlabs_voice_id": "old-voice",
                "voice": "old-voice", "speaker": "old-voice", "keep": 42,
            }
            native_settings = {
                "stability": 0.3,
                "similarity_boost": 0.8,
                "style": 0.2,
                "speed": 3.5,
                "use_speaker_boost": True,
            }
            outcome = configure_tts(runtime, ConfigureTtsInput(
                session_id="session-1", service_id="elevenlabs", model="eleven_v3",
                elevenlabs_voice_settings=native_settings,
                expected_revision=4, idempotency_key="eleven-v3-1",
            ))
            value = application.calls[-1][1]["value"]
            self.assertNotIn("elevenlabs_model", value)
            self.assertNotIn("elevenlabs_voice_id", value)
            self.assertEqual("new-voice", value["voice"])
            self.assertEqual("new-voice", value["speaker"])
            self.assertEqual("eleven_v3", value["xtts_model"])
            self.assertEqual(native_settings, value["elevenlabs_voice_settings"])
            self.assertEqual(42, value["keep"])
            self.assertEqual(native_settings, outcome.result["selection"]["elevenlabs_voice_settings"])
            application.services[0]["voices"] = []
            application.services[0]["default_voice"] = ""
            configure_tts(runtime, ConfigureTtsInput(
                session_id="session-1", service_id="elevenlabs", model="eleven_v3",
                expected_revision=4, idempotency_key="eleven-no-voice-1",
            ))
            self.assertEqual("", application.calls[-1][1]["value"]["voice"])
            self.assertEqual("", application.calls[-1][1]["value"]["speaker"])
            application.calls.clear()
            for bad in (
                {"stability": float("nan")}, {"speed": True},
                {"speed": 0.24}, {"speed": 4.01},
                {"style": 1.01}, {"unknown": 1},
                {"use_speaker_boost": None},
            ):
                with self.assertRaises(ValidationError):
                    ConfigureTtsInput(
                        session_id="session-1", service_id="elevenlabs",
                        elevenlabs_voice_settings=bad,
                        expected_revision=4, idempotency_key="eleven-invalid",
                    )
            self.assertFalse(any(name == "update_session_settings" for name, _ in application.calls))
            configure_tts(runtime, ConfigureTtsInput(
                session_id="session-1", service_id="elevenlabs",
                model="eleven_multilingual_v2", elevenlabs_voice_settings={},
                expected_revision=4, idempotency_key="eleven-clear-1",
            ))
            self.assertEqual({}, application.calls[-1][1]["value"]["elevenlabs_voice_settings"])
            application.services = [{"id": "other", "adapter": "openai_compatible", "available": True, "models": ["m"], "default_model": "m"}]
            application.calls.clear()
            with self.assertRaises(PandratorMcpError):
                configure_tts(runtime, ConfigureTtsInput(
                    session_id="session-1", service_id="other",
                    elevenlabs_voice_settings={}, expected_revision=4,
                    idempotency_key="non-eleven-1",
                ))
            self.assertFalse(any(name == "update_session_settings" for name, _ in application.calls))
    def test_browse_and_import_use_only_approved_relative_regular_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "lesson.srt"
            source.write_bytes(b"one\ntwo\n")
            outside = root.parent / f"{root.name}-outside.txt"
            outside.write_bytes(b"outside")
            link = root / "escape.txt"
            link.symlink_to(outside)
            self.addCleanup(outside.unlink, missing_ok=True)
            runtime, application = _runtime(root)

            listed = browse_local_sources(
                runtime,
                BrowseLocalSourcesInput(root="downloads", sort="name_asc"),
            )
            self.assertEqual(
                ["lesson.srt"], [item["relative_path"] for item in listed["items"]]
            )
            self.assertNotIn(str(root), str(listed))

            imported = import_local_source(
                runtime,
                ImportLocalSourceInput(
                    session_id="session-1",
                    root="downloads",
                    relative_path="lesson.srt",
                    expected_session_revision=1,
                    idempotency_key="import:lesson:1",
                ),
            )
            self.assertEqual(source.read_bytes(), application.uploaded)
            self.assertEqual("source-1", imported.result["source_asset_id"])
            self.assertEqual("attach", application.calls[-1][0])

            with self.assertRaises(PandratorMcpError) as captured:
                import_local_source(
                    runtime,
                    ImportLocalSourceInput(
                        session_id="session-1",
                        root="downloads",
                        relative_path="escape.txt",
                        expected_session_revision=2,
                        idempotency_key="import:escape:1",
                    ),
                )
            self.assertEqual("network_policy_denied", captured.exception.code)

    def test_local_import_has_a_windows_safe_open_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(strict=True)
            source = root / "windows-test.txt"
            source.write_text("hello", encoding="utf-8")

            with mock.patch("pandrator_mcp.tools.e2e.os.name", "nt"):
                descriptor = _open_contained_file(root, ("windows-test.txt",))
            try:
                self.assertEqual(b"hello", os.read(descriptor, 5))
            finally:
                os.close(descriptor)

    def test_create_text_source_uploads_utf8_and_attaches_without_echoing_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, application = _runtime(root)
            text = "Hello from the MCP. Zażółć gęślą jaźń."

            created = create_text_source(
                runtime,
                CreateTextSourceInput(
                    session_id="session-1",
                    text=text,
                    filename="hello.txt",
                    expected_session_revision=1,
                    idempotency_key="text-source:hello:1",
                ),
            )

            self.assertEqual(text.encode("utf-8"), application.uploaded)
            self.assertEqual("source-1", created.result["source_asset_id"])
            self.assertEqual("hello.txt", created.result["filename"])
            self.assertNotIn(text, str(created.result))
            self.assertEqual("attach", application.calls[-1][0])

    def test_catalog_configuration_export_and_delivery_are_typed_and_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "outputs"
            runtime, application = _runtime(root, output)

            catalog = tts_catalog(runtime, TtsCatalogInput(refresh=True))
            self.assertEqual("service-a", catalog["services"][0]["id"])
            self.assertNotIn("base_url", catalog["services"][0])
            self.assertNotIn("credential", str(catalog))
            self.assertEqual(1, catalog["services"][0]["voice_count"])
            self.assertNotIn("voices", catalog["services"][0])
            self.assertEqual(1, len(catalog["managed_voices"]))
            filtered = tts_catalog(
                runtime,
                TtsCatalogInput(model="MODEL-A", available_only=True, detail="full"),
            )
            self.assertEqual(["native-a"], filtered["services"][0]["voices"])

            vertex_services = [{
                    "id": "google_vertex",
                    "name": "Vertex AI",
                    "adapter": "vertex",
                    "available": True,
                    "models": ["gemini-2.5-pro-preview-tts"],
                    "voices": ["Kore"],
                    "default_model": "gemini-2.5-pro-preview-tts",
                    "default_voice": "Kore",
                }]

            async def verify_registered_catalogue():
                from mcp import Client
                from mcp.types import TextContent

                from pandrator_mcp.server import build_server

                async with Client(
                    build_server(cast(McpRuntime, runtime)), mode="auto", raise_exceptions=True
                ) as client:
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    schema = tools["pandrator_get_tts_catalog"].input_schema
                    self.assertTrue(
                        schema["properties"]["include_managed_voices"]["default"]
                    )
                    with mock.patch.object(
                        application, "list_voices", wraps=application.list_voices
                    ) as list_voices:
                        structured = await client.call_tool(
                            "pandrator_get_tts_catalog",
                            {
                                "service_id": "google_vertex",
                                "model": "gemini-2.5-pro-preview-tts",
                                "include_managed_voices": False,
                                "response_mode": "structured",
                            },
                        )
                        list_voices.assert_not_called()

                result = structured.structured_content["result"]
                self.assertEqual([], result["managed_voices"])
                self.assertEqual(["google_vertex"], [item["id"] for item in result["services"]])
                self.assertEqual(
                    ["gemini-2.5-pro-preview-tts"], result["services"][0]["models"]
                )
                self.assertLess(len(json.dumps(result).encode("utf-8")), 4 * 1024)
                content = structured.content[0]
                self.assertIsInstance(content, TextContent)
                assert isinstance(content, TextContent)
                marker = content.text
                self.assertEqual("structuredContent", json.loads(marker)["data"])
                self.assertLess(len(marker), 500)
                self.assertNotIn("gemini-2.5-pro-preview-tts", marker)

            with mock.patch.object(
                application, "services", vertex_services, create=True
            ):
                asyncio.run(verify_registered_catalogue())

            configured = configure_tts(
                runtime,
                ConfigureTtsInput(
                    session_id="session-1",
                    service_id="SERVICE-A",
                    model="MODEL-A",
                    voice="Narrator",
                    language="de",
                    style_instructions="Warm, clear course narration.",
                    expected_revision=4,
                    idempotency_key="tts:configure:1",
                ),
            )
            self.assertEqual(
                "provider-voice-9", configured.result["selection"]["voice"]
            )
            update = next(
                call
                for call in application.calls
                if call[0] == "update_session_settings"
            )
            self.assertEqual(0.95, update[1]["value"]["speed"])
            self.assertEqual(
                "Warm, clear course narration.",
                update[1]["value"]["generation_prompt"],
            )

            runs = list_generation_runs(
                runtime,
                ListGenerationRunsInput(session_id="session-1"),
            )
            self.assertNotIn("settings_snapshot", runs["items"][0])
            plan = plan_export_variant(
                runtime,
                PlanExportVariantInput(
                    session_id="session-1",
                    generation_run_id="run-1",
                    subtitle_mode="burned",
                    subtitle_selection="translation",
                ),
            )
            plan = plan.result
            self.assertEqual("export", plan["target_stage"])
            self.assertFalse(plan["continuation"])
            self.assertEqual("run-1", plan["overrides"]["output"]["generation_run_id"])

            downloaded = download_artifact(
                runtime,
                DownloadArtifactInput(
                    artifact_id="artifact-1",
                    filename="../course-final.mp4",
                ),
            )
            self.assertEqual(
                (output / "course-final.mp4").resolve(), Path(downloaded["path"])
            )
            self.assertEqual(b"data", Path(downloaded["path"]).read_bytes())

    def test_download_artifact_defaults_to_workspace_exports_for_local_managed(self):
        with tempfile.TemporaryDirectory() as root_dir:
            workspace = Path(root_dir) / "workspace"
            workspace.mkdir()
            application = _Application()
            profile = TargetProfile(
                name="managed-local",
                mode=TargetMode.LOCAL_MANAGED,
                workspace=str(workspace),
                local_output_root=None,
            )
            runtime = SimpleNamespace(
                profile=profile,
                require_application=lambda: application,
            )
            downloaded = download_artifact(
                runtime,
                DownloadArtifactInput(
                    artifact_id="artifact-1",
                    filename="output.mp4",
                ),
            )
            expected_path = workspace / "exports" / "output.mp4"
            self.assertEqual(expected_path.resolve(), Path(downloaded["path"]))
            self.assertEqual(b"data", expected_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
