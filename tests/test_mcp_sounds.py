"""Focused contracts for sound MCP routes, schemas, and projections."""

from __future__ import annotations

import asyncio
import inspect
import os
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from pandrator.logic.audio_cpp_generation_models import get_model as get_audio_cpp_generation_model
from pandrator_mcp.clients.application_project_sounds import ApplicationProjectSoundMethods
from pandrator_mcp.clients.application_sounds import ApplicationSoundMethods
from pandrator_mcp.clients.application_voices import ApplicationVoiceMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.results import ToolOutcome
from pandrator_mcp.schemas.audiobook import PreviewSpeechSelectionInput
from pandrator_mcp.schemas.sounds import (
    GenerateSoundInput,
    ImportAudioReferenceInput,
    ProjectSoundsInput,
    SoundCatalogueInput,
    SoundLibraryInput,
)
from pandrator_mcp.schemas.voice_lifecycle import ImportVoiceReferenceInput
from pandrator_mcp.server import _call_with_validated_input
from pandrator_mcp.tools import project_sounds as project_sounds_module
from pandrator_mcp.tools import sound_audio_import, sounds, voice_lifecycle


def _runtime(application=None) -> McpRuntime:
    return cast(McpRuntime, SimpleNamespace(require_application=lambda: application))


class _Client(ApplicationSoundMethods):
    def __init__(self):
        self.json_calls: list[tuple[tuple, dict]] = []
        self.binary_calls: list[tuple[tuple, dict]] = []

    def _request_json(self, *args, **kwargs):
        self.json_calls.append((args, kwargs))
        return {"ok": True}

    def _request_binary_json(self, *args, **kwargs):
        self.binary_calls.append((args, kwargs))
        return {"ok": True}


class _ProjectClient(ApplicationProjectSoundMethods):
    def __init__(self):
        self.json_calls: list[tuple[tuple, dict]] = []
        self.binary_calls: list[tuple[tuple, dict]] = []

    def _request_json(self, *args, **kwargs):
        self.json_calls.append((args, kwargs))
        return {"ok": True}

    def _request_binary_json(self, *args, **kwargs):
        self.binary_calls.append((args, kwargs))
        return {"ok": True}


class _VoiceClient(ApplicationVoiceMethods):
    def __init__(self):
        self.json_calls: list[tuple[tuple, dict]] = []
        self.binary_calls: list[tuple[tuple, dict]] = []

    def _request_json(self, *args, **kwargs):
        self.json_calls.append((args, kwargs))
        return {"ok": True}

    def _request_binary_json(self, *args, **kwargs):
        self.binary_calls.append((args, kwargs))
        return {"ok": True}


def test_sound_client_uses_fixed_routes_defaults_and_escaped_identifiers():
    client = _Client()

    client.sound_catalogue()
    assert client.json_calls[-1] == (("/api/v1/sounds/catalogue",), {})

    client.generate_sound(prompt="Rain on a tin roof", idempotency_key="sound-gen-001")
    path, options = client.json_calls[-1]
    assert path == ("/api/v1/sounds/generate",)
    assert options == {
        "method": "POST",
        "body": {
            "prompt": "Rain on a tin roof",
            "model_id": "stable_audio_3_small_sfx_q8_0",
        },
        "idempotency_key": "sound-gen-001",
        "maximum_body_bytes": 512 * 1024,
    }

    client.list_sounds(query="rain", tag="weather", limit=15, archived=True)
    assert client.json_calls[-1][0] == ("/api/v1/sounds",)
    assert client.json_calls[-1][1]["parameters"] == {
        "query": "rain",
        "limit": 15,
        "archived": "true",
        "tag": "weather",
    }

    client.get_sound("item/a", cursor="cursor-1", limit=20)
    assert client.json_calls[-1] == (
        ("/api/v1/sounds/item%2Fa",),
        {"parameters": {"limit": 20, "cursor": "cursor-1"}},
    )
    client.list_sound_candidates()
    assert client.json_calls[-1] == (
        ("/api/v1/sounds/candidates",),
        {"parameters": {"limit": 20}},
    )
    client.get_sound_candidate("candidate/a")
    assert client.json_calls[-1][0] == ("/api/v1/sounds/candidates/candidate%2Fa",)


def test_sound_client_forwards_bounded_music_controls_only_when_supplied():
    client = _Client()

    client.generate_sound(
        prompt="A slow ambient bed",
        model_id="stable_audio_3_small_music_q8_0",
        duration_seconds=None,
        kind="music",
        backend="vulkan",
        lyrics="stay with me",
        parameters={"num_inference_steps": 24, "guidance_scale": 3.5, "loop": True},
        idempotency_key="sound-music-001",
    )
    path, options = client.json_calls[-1]
    assert path == ("/api/v1/sounds/generate",)
    assert options["body"] == {
        "prompt": "A slow ambient bed",
        "model_id": "stable_audio_3_small_music_q8_0",
        "kind": "music",
        "backend": "vulkan",
        "lyrics": "stay with me",
        "parameters": {"num_inference_steps": 24, "guidance_scale": 3.5, "loop": True},
    }
    assert options["maximum_body_bytes"] == 512 * 1024

    client.generate_sound(
        prompt="Three ambient variations",
        candidate_count=3,
        idempotency_key="sound-batch-client-001",
    )
    assert client.json_calls[-1][1]["body"] == {
        "prompt": "Three ambient variations",
        "model_id": "stable_audio_3_small_sfx_q8_0",
        "candidate_count": 3,
    }

    client.generate_sound(
        prompt="An H3 instrumental phrase",
        model_id="minimax_h3_q4_k_int8_dit",
        kind="music",
        backend="cuda",
        seed=73,
        idempotency_key="sound-music-cuda-001",
    )
    assert client.json_calls[-1][1]["body"] == {
        "prompt": "An H3 instrumental phrase",
        "model_id": "minimax_h3_q4_k_int8_dit",
        "kind": "music",
        "backend": "cuda",
        "seed": 73,
    }

    with pytest.raises(ValueError, match="32 entries"):
        client.generate_sound(
            prompt="sound",
            parameters={f"p{index}": index for index in range(33)},
            idempotency_key="sound-music-002",
        )
    with pytest.raises(ValueError, match="64 characters"):
        client.generate_sound(
            prompt="sound",
            parameters={"k" * 65: True},
            idempotency_key="sound-music-005",
        )
    with pytest.raises(ValueError, match="finite"):
        client.generate_sound(
            prompt="sound",
            parameters={"guidance_scale": float("inf")},
            idempotency_key="sound-music-003",
        )
    with pytest.raises(ValueError, match="parameter values"):
        client.generate_sound(
            prompt="sound",
            parameters={"nested": {"unsafe": True}},
            idempotency_key="sound-music-004",
        )
    for candidate_count in (True, 1.0, None, 0, 9):
        with pytest.raises(ValueError, match="candidate_count"):
            client.generate_sound(
                prompt="sound",
                candidate_count=candidate_count,
                idempotency_key="sound-batch-invalid-001",
            )
    assert len(client.json_calls) == 3


def test_sound_client_omits_duration_for_music_model_default():
    client = _Client()

    client.generate_sound(
        prompt="A quiet instrumental passage",
        model_id="stable_audio_3_small_music_q8_0",
        idempotency_key="sound-music-default-001",
    )

    assert client.json_calls[-1][1]["body"] == {
        "prompt": "A quiet instrumental passage",
        "model_id": "stable_audio_3_small_music_q8_0",
    }


def test_sound_client_mutations_are_guarded_and_send_only_domain_fields():
    client = _Client()

    client.save_sound(
        candidate_id="candidate-1",
        name="Door slam",
        tags=["door", "impact"],
        idempotency_key="sound-save-001",
    )
    assert client.json_calls[-1][1] == {
        "method": "POST",
        "body": {
            "candidate_id": "candidate-1",
            "name": "Door slam",
            "tags": ["door", "impact"],
        },
        "idempotency_key": "sound-save-001",
        "maximum_body_bytes": 16 * 1024,
    }

    client.save_sound(
        candidate_id="candidate-2",
        item_id="item-1",
        expected_revision=4,
        idempotency_key="sound-link-001",
    )
    assert client.json_calls[-1][1]["body"] == {
        "candidate_id": "candidate-2",
        "item_id": "item-1",
        "expected_revision": 4,
    }

    client.update_sound(
        "item/1",
        expected_revision=5,
        changes={"archived": False, "preferred_variant_id": "variant-2"},
        idempotency_key="sound-edit-001",
    )
    assert client.json_calls[-1] == (
        ("/api/v1/sounds/item%2F1",),
        {
            "method": "PATCH",
            "body": {
                "expected_revision": 5,
                "archived": False,
                "preferred_variant_id": "variant-2",
            },
            "idempotency_key": "sound-edit-001",
            "maximum_body_bytes": 16 * 1024,
        },
    )

    client.discard_sound_candidate("candidate-3", idempotency_key="sound-drop-001")
    assert client.json_calls[-1] == (
        ("/api/v1/sounds/candidates/candidate-3",),
        {"method": "DELETE", "idempotency_key": "sound-drop-001"},
    )


def test_sound_client_rejects_invalid_routes_and_payload_limits_before_request():
    client = _Client()
    with pytest.raises(ValueError, match="200 characters"):
        client.list_sounds(query="x" * 201)
    with pytest.raises(ValueError, match="1 to 100"):
        client.list_sounds(limit=101)
    with pytest.raises(ValueError, match="32-bit"):
        client.generate_sound(
            prompt="test",
            seed=-1,
            idempotency_key="sound-gen-002",
        )
    with pytest.raises(ValueError, match="cannot change"):
        client.save_sound(
            candidate_id="candidate-1",
            item_id="item-1",
            expected_revision=1,
            name="Forbidden attach rename",
            idempotency_key="sound-link-002",
        )
    with pytest.raises(ValueError, match="unsupported fields"):
        client.update_sound(
            "item-1",
            expected_revision=1,
            changes={"prompt": "must not reach backend"},
            idempotency_key="sound-edit-002",
        )
    assert client.json_calls == []


def test_audio_import_uses_typed_put_routes_raw_bytes_and_percent_encoded_filename(monkeypatch):
    client = _Client()
    client.import_audio_reference(
        target="sound",
        filename="Café SFX.WAV",
        body=b"RIFF-audio",
        idempotency_key="sound-import-001",
    )
    args, kwargs = client.binary_calls[-1]
    assert args == ("/api/v1/sounds/import",)
    assert kwargs == {
        "method": "PUT",
        "body": b"RIFF-audio",
        "content_type": "application/octet-stream",
        "extra_headers": {
            "X-Audio-Filename": "Caf%C3%A9%20SFX.WAV",
            "Idempotency-Key": "sound-import-001",
        },
    }

    client.import_audio_reference(
        target="voice_reference",
        filename="reference.m4a",
        body=b"audio",
        idempotency_key="sound-import-002",
    )
    assert client.binary_calls[-1][0] == ("/api/v1/voices/reference-audio/import",)

    with pytest.raises(ValueError, match="support"):
        client.import_audio_reference(
            target="sound",
            filename="notes.txt",
            body=b"audio",
            idempotency_key="sound-import-003",
        )
    import pandrator_mcp.clients.application_sounds as application_sounds

    monkeypatch.setattr(application_sounds, "MAX_AUDIO_IMPORT_BYTES", 4)
    with pytest.raises(ValueError, match="16 MiB"):
        client.import_audio_reference(
            target="sound",
            filename="long.wav",
            body=b"12345",
            idempotency_key="sound-import-004",
        )


def test_project_sound_client_repeats_segment_filters_and_uses_revision_guards():
    client = _Client()
    client.get_project_sound_plan(
        "session/a", segment_ids=["seg-1", "seg-2"], cursor="cue-1", limit=7
    )
    assert client.json_calls[-1] == (
        ("/api/v1/sessions/session%2Fa/sound-plan",),
        {
            "parameters": [
                ("limit", 7),
                ("segment_id", "seg-1"),
                ("segment_id", "seg-2"),
                ("cursor", "cue-1"),
            ]
        },
    )

    client.add_project_sound_cue(
        "session-1",
        expected_revision=0,
        variant_id="variant-1",
        anchor={"kind": "before_segment", "segment_id": "segment-1"},
        playback={"mode": "overlay", "gain_db": -3.0},
        included=False,
        idempotency_key="cue-add-001",
    )
    assert client.json_calls[-1][1]["body"] == {
        "expected_revision": 0,
        "variant_id": "variant-1",
        "anchor": {"kind": "before_segment", "segment_id": "segment-1"},
        "playback": {"mode": "overlay", "gain_db": -3.0},
        "included": False,
    }
    assert client.json_calls[-1][1]["idempotency_key"] == "cue-add-001"

    client.update_project_sound_cue(
        "session-1",
        "cue-1",
        expected_revision=2,
        changes={"included": False},
        idempotency_key="cue-edit-001",
    )
    assert client.json_calls[-1][1]["body"] == {
        "expected_revision": 2,
        "included": False,
    }

    client.remove_project_sound_cue(
        "session-1",
        "cue-1",
        expected_revision=3,
        idempotency_key="cue-remove-001",
    )
    assert client.json_calls[-1][1] == {
        "method": "DELETE",
        "idempotency_key": "cue-remove-001",
        "if_match_revision": 3,
    }

    client.restore_project_sound_plan(
        "session-1",
        expected_revision=4,
        source_plan_id="plan-1",
        idempotency_key="plan-restore-001",
    )
    assert client.json_calls[-1][0] == ("/api/v1/sessions/session-1/sound-plan/restore",)


def test_project_sound_client_rejects_unbounded_or_unknown_values():
    client = _Client()
    with pytest.raises(ValueError, match="100"):
        client.get_project_sound_plan("session-1", segment_ids=["segment"] * 101)
    with pytest.raises(ValueError, match="unsupported"):
        client.add_project_sound_cue(
            "session-1",
            expected_revision=0,
            variant_id="variant-1",
            anchor={"kind": "source_time", "time_seconds": 1.0, "segment_id": "unused"},
            idempotency_key="cue-add-002",
        )
    with pytest.raises(ValueError, match="finite"):
        client.add_project_sound_cue(
            "session-1",
            expected_revision=0,
            variant_id="variant-1",
            anchor={"kind": "source_time", "time_seconds": float("nan")},
            idempotency_key="cue-add-003",
        )
    with pytest.raises(ValueError, match="insert offset"):
        client.add_project_sound_cue(
            "session-1",
            expected_revision=0,
            variant_id="variant-1",
            anchor={"kind": "before_segment", "segment_id": "segment-1"},
            playback={"mode": "insert", "offset_ms": -1},
            idempotency_key="cue-add-004",
        )
    assert client.json_calls == []


def test_project_sound_job_client_uses_fixed_routes_and_bounded_payloads():
    client = _ProjectClient()
    client.get_project_sound_mix_status("session/a")
    assert client.json_calls[-1] == (("/api/v1/sessions/session%2Fa/sound-plan/mix",), {})

    client.get_project_sound_mix_status("session-1", base_artifact_id="base-1")
    assert client.json_calls[-1] == (
        ("/api/v1/sessions/session-1/sound-plan/mix",),
        {"parameters": {"base_artifact_id": "base-1"}},
    )

    client.render_project_sound_plan(
        "session-1",
        expected_revision=0,
        base_artifact_id="base-1",
        timeline_artifact_id="timeline-1",
        idempotency_key="sound-render-001",
    )
    assert client.json_calls[-1] == (
        ("/api/v1/sessions/session-1/sound-plan/render",),
        {
            "method": "POST",
            "body": {
                "expected_revision": 0,
                "base_artifact_id": "base-1",
                "timeline_artifact_id": "timeline-1",
            },
            "idempotency_key": "sound-render-001",
            "maximum_body_bytes": 16 * 1024,
        },
    )

    client.resolve_project_sound_plan(
        "session-1",
        expected_revision=3,
        cue_ids=["cue-1", "cue-2"],
        idempotency_key="sound-resolve-001",
    )
    assert client.json_calls[-1] == (
        ("/api/v1/sessions/session-1/sound-plan/resolve",),
        {
            "method": "POST",
            "body": {"expected_revision": 3, "cue_ids": ["cue-1", "cue-2"]},
            "idempotency_key": "sound-resolve-001",
            "maximum_body_bytes": 8 * 1024,
        },
    )

    client.resolve_project_sound_plan(
        "session-1", expected_revision=3, idempotency_key="sound-resolve-002"
    )
    assert client.json_calls[-1][1]["body"] == {"expected_revision": 3}

    with pytest.raises(ValueError, match="1-20"):
        client.resolve_project_sound_plan(
            "session-1", expected_revision=3, cue_ids=[], idempotency_key="sound-resolve-003"
        )
    with pytest.raises(ValueError, match="1-20"):
        client.resolve_project_sound_plan(
            "session-1",
            expected_revision=3,
            cue_ids=["cue-1"] * 21,
            idempotency_key="sound-resolve-004",
        )
    assert len(client.json_calls) == 5


def test_project_sound_job_schemas_keep_actions_strict_and_bounded():
    status = ProjectSoundsInput.model_validate(
        {"action": "status", "session_id": "session-1", "base_artifact_id": "base-1"}
    )
    assert status.action == "status"
    ProjectSoundsInput.model_validate(
        {
            "action": "render",
            "session_id": "session-1",
            "expected_revision": 0,
            "base_artifact_id": "base-1",
            "idempotency_key": "sound-render-002",
        }
    )
    ProjectSoundsInput.model_validate(
        {
            "action": "resolve",
            "session_id": "session-1",
            "expected_revision": 1,
            "cue_ids": ["cue-1"],
            "idempotency_key": "sound-resolve-005",
        }
    )
    for fields in (
        {"action": "status", "session_id": "session-1", "idempotency_key": "sound-status-1"},
        {
            "action": "render",
            "session_id": "session-1",
            "expected_revision": 0,
            "base_artifact_id": "base-1",
        },
        {
            "action": "render",
            "session_id": "session-1",
            "expected_revision": 0,
            "base_artifact_id": "base-1",
            "idempotency_key": "sound-render-003",
            "cue_ids": ["unrelated"],
        },
        {
            "action": "resolve",
            "session_id": "session-1",
            "expected_revision": 0,
            "idempotency_key": "sound-resolve-006",
        },
        {
            "action": "resolve",
            "session_id": "session-1",
            "expected_revision": 1,
            "cue_ids": ["cue"] * 21,
            "idempotency_key": "sound-resolve-007",
        },
    ):
        with pytest.raises(ValidationError):
            ProjectSoundsInput.model_validate(fields)


def test_project_sound_job_projections_are_compact_and_only_map_active_work():
    application = Mock()
    application.get_project_sound_mix_status.return_value = {
        "session_id": "session-1",
        "sound_plan_id": "plan-1",
        "sound_plan_revision": 4,
        "mix_id": "mix-1",
        "status": "running",
        "cache_hit": False,
        "request_fingerprint": "a" * 64,
        "remaining_count": 2,
        "job_id": "job-1",
        "private_context": "drop me",
        "artifact_path": "/private/audio.wav",
        "resolution": {"transcript": "not returned"},
    }
    runtime = _runtime(application)
    queued = cast(
        ToolOutcome,
        project_sounds_module.project_sounds(
            runtime, ProjectSoundsInput(action="status", session_id="session-1")
        ),
    )
    assert queued.work is not None
    assert queued.work.id == "job-1"
    assert queued.result["status"] == "running"
    assert "job_status" not in queued.result
    assert queued.result["sound_plan_id"] == "plan-1"
    assert queued.result["sound_plan_revision"] == 4
    assert queued.result["remaining_count"] == 2
    assert "private_context" not in str(queued.result)
    assert "artifact_path" not in str(queued.result)
    assert "resolution" not in str(queued.result)
    application.render_project_sound_plan.assert_not_called()

    application.get_project_sound_mix_status.return_value = {
        "session_id": "session-1",
        "sound_plan_id": "plan-1",
        "sound_plan_revision": 4,
        "job_id": "completed-job-1",
        "status": "ready",
        "request_fingerprint": "c" * 64,
    }
    ready = project_sounds_module.project_sounds(
        runtime, ProjectSoundsInput(action="status", session_id="session-1")
    )
    assert isinstance(ready, dict)
    assert ready["status"] == "ready"
    assert ready["job_id"] == "completed-job-1"

    application.render_project_sound_plan.return_value = {
        "status": "ready",
        "cache_hit": False,
        "job": {"id": "job-2", "status": "completed"},
        "request_fingerprint": "b" * 64,
        "resolution": {"private": "drop"},
    }
    completed = cast(
        ToolOutcome,
        project_sounds_module.project_sounds(
            runtime,
            ProjectSoundsInput(
                action="render",
                session_id="session-1",
                expected_revision=1,
                base_artifact_id="base-1",
                idempotency_key="sound-render-004",
            ),
        ),
    )
    assert completed.work is None
    assert completed.result["status"] == "ready"
    assert completed.result["job_status"] == "completed"
    assert "resolution" not in str(completed.result)

    application.resolve_project_sound_plan.return_value = {
        "status": "queued",
        "cache_hit": True,
        "job_id": "job-cache",
        "job": {"id": "job-cache", "status": "queued"},
    }
    cached = cast(
        ToolOutcome,
        project_sounds_module.project_sounds(
            runtime,
            ProjectSoundsInput(
                action="resolve",
                session_id="session-1",
                expected_revision=1,
                idempotency_key="sound-resolve-008",
            ),
        ),
    )
    assert cached.work is None


def test_voice_reference_import_forwards_only_explicit_crop_fields(monkeypatch):
    client = _VoiceClient()
    base = {
        "artifact_id": "artifact-1",
        "transcript_reviewed": False,
        "expected_voice_revision": 3,
    }
    client.import_voice_reference(
        "voice-1",
        artifact_id="artifact-1",
        transcript=None,
        language=None,
        transcript_reviewed=False,
        expected_voice_revision=3,
        idempotency_key="voice-import-001",
    )
    assert client.json_calls[-1] == (
        ("/api/v1/voices/voice-1/samples/from-artifact",),
        {
            "method": "POST",
            "body": base,
            "idempotency_key": "voice-import-001",
            "maximum_body_bytes": 64 * 1024,
        },
    )

    client.import_voice_reference(
        "voice-1",
        artifact_id="artifact-1",
        transcript="text from this crop only",
        language=None,
        transcript_reviewed=True,
        expected_voice_revision=3,
        idempotency_key="voice-import-002",
        start_seconds=0.0,
        end_seconds=1.5,
    )
    assert client.json_calls[-1][1]["body"] == {
        **base,
        "transcript": "text from this crop only",
        "transcript_reviewed": True,
        "start_seconds": 0.0,
        "end_seconds": 1.5,
    }

    with pytest.raises(ValueError, match="end_seconds"):
        client.import_voice_reference(
            "voice-1",
            artifact_id="artifact-1",
            transcript=None,
            language=None,
            transcript_reviewed=False,
            expected_voice_revision=3,
            idempotency_key="voice-import-003",
            start_seconds=2.0,
            end_seconds=1.5,
        )
    assert len(client.json_calls) == 2

    application = Mock()
    application.import_voice_reference.return_value = {"job_id": "job-voice", "status": "queued"}
    monkeypatch.setattr(voice_lifecycle, "_job_outcome", lambda *args, **kwargs: None)
    no_crop = ImportVoiceReferenceInput(
        voice_id="voice-1",
        artifact_id="artifact-1",
        expected_voice_revision=3,
        idempotency_key="voice-import-004",
    )
    voice_lifecycle.import_voice_reference(_runtime(application), no_crop)
    no_crop_kwargs = application.import_voice_reference.call_args.kwargs
    assert "start_seconds" not in no_crop_kwargs
    assert "end_seconds" not in no_crop_kwargs

    cropped = ImportVoiceReferenceInput(
        voice_id="voice-1",
        artifact_id="artifact-1",
        transcript="only the crop transcript",
        expected_voice_revision=3,
        idempotency_key="voice-import-005",
        start_seconds=0.0,
        end_seconds=2.0,
    )
    voice_lifecycle.import_voice_reference(_runtime(application), cropped)
    crop_kwargs = application.import_voice_reference.call_args.kwargs
    assert crop_kwargs["start_seconds"] == 0.0
    assert crop_kwargs["end_seconds"] == 2.0
    assert crop_kwargs["transcript"] == "only the crop transcript"


def test_voice_import_crop_schema_and_transcribe_route_target_sample_id():
    base = {
        "voice_id": "voice-1",
        "artifact_id": "artifact-1",
        "expected_voice_revision": 2,
        "idempotency_key": "voice-import-006",
    }
    assert ImportVoiceReferenceInput.model_validate(base).start_seconds == 0.0
    with pytest.raises(ValidationError):
        ImportVoiceReferenceInput.model_validate({**base, "end_seconds": 0.0})
    with pytest.raises(ValidationError):
        ImportVoiceReferenceInput.model_validate({**base, "start_seconds": float("nan")})

    client = _VoiceClient()
    client.transcribe_voice_sample(
        "voice-1",
        "derived-sample-1",
        settings={"language": "en"},
        idempotency_key="voice-transcribe-001",
    )
    assert client.json_calls[-1] == (
        ("/api/v1/voices/voice-1/samples/derived-sample-1/transcribe",),
        {
            "method": "POST",
            "body": {"language": "en"},
            "idempotency_key": "voice-transcribe-001",
            "maximum_body_bytes": 32 * 1024,
        },
    )


def test_speech_selection_sound_plan_revision_is_optional_and_bound_to_speech_xml():
    base = {
        "session_id": "session-1",
        "revision_id": "revision-1",
        "segment_id": "segment-1",
        "expected_segment_revision": 1,
        "start": 0,
        "end": 5,
    }
    assert PreviewSpeechSelectionInput.model_validate(base).expected_sound_plan_revision is None
    selection = PreviewSpeechSelectionInput.model_validate(
        {**base, "speech_xml": "<speak>hello</speak>", "expected_sound_plan_revision": 0}
    )
    assert selection.expected_sound_plan_revision == 0
    with pytest.raises(ValidationError, match="only valid with speech_xml"):
        PreviewSpeechSelectionInput.model_validate({**base, "expected_sound_plan_revision": 1})
    with pytest.raises(ValidationError):
        PreviewSpeechSelectionInput.model_validate(
            {**base, "speech_xml": "<speak/>", "expected_sound_plan_revision": -1}
        )


def test_sound_schemas_validate_action_fields_and_revision_ranges():
    with pytest.raises(ValidationError):
        GenerateSoundInput(prompt="click", duration_seconds=601.0, idempotency_key="sound-gen-005")
    with pytest.raises(ValidationError):
        GenerateSoundInput(prompt="click", seed=2**32, idempotency_key="sound-gen-006")
    for candidate_count in (True, 1.0, None, 0, 9):
        with pytest.raises(ValidationError):
            GenerateSoundInput(
                prompt="click",
                candidate_count=candidate_count,
                idempotency_key="sound-batch-schema-001",
            )
    assert GenerateSoundInput(
        prompt="click", candidate_count=3, idempotency_key="sound-batch-schema-002"
    ).candidate_count == 3
    assert GenerateSoundInput(
        prompt="click",
        model_id="minimax_h3_q4_k_int8_dit",
        kind="music",
        backend="cuda",
        seed=73,
        idempotency_key="sound-gen-cuda-001",
    ).backend == "cuda"
    with pytest.raises(ValidationError):
        GenerateSoundInput(
            prompt="click",
            parameters={"k" * 65: True},
            idempotency_key="sound-gen-009",
        )

    with pytest.raises(ValidationError, match="cannot change"):
        SoundLibraryInput.model_validate(
            {
                "action": "save",
                "candidate_id": "candidate-1",
                "item_id": "item-1",
                "expected_revision": 1,
                "name": "New name",
                "idempotency_key": "sound-save-002",
            }
        )
    with pytest.raises(ValidationError, match="requires"):
        SoundLibraryInput.model_validate(
            {
                "action": "save",
                "candidate_id": "candidate-1",
                "idempotency_key": "sound-save-003",
            }
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        SoundLibraryInput.model_validate({"action": "list", "prompt": "unexpected"})
    with pytest.raises(ValidationError):
        SoundLibraryInput.model_validate(
            {
                "action": "update",
                "item_id": "item-1",
                "expected_revision": 1,
                "changes": {"prompt": "unknown"},
                "idempotency_key": "sound-edit-003",
            }
        )

    with pytest.raises(ValidationError, match="expected_revision >= 1"):
        ProjectSoundsInput.model_validate(
            {
                "action": "update",
                "session_id": "session-1",
                "logical_cue_id": "cue-1",
                "expected_revision": 0,
                "included": False,
                "idempotency_key": "cue-edit-002",
            }
        )
    with pytest.raises(ValidationError):
        ProjectSoundsInput.model_validate(
            {
                "action": "add",
                "session_id": "session-1",
                "expected_revision": 0,
                "variant_id": "variant-1",
                "anchor": {"kind": "source_time", "time_seconds": float("inf")},
                "idempotency_key": "cue-add-005",
            }
        )
    with pytest.raises(ValidationError):
        ProjectSoundsInput.model_validate(
            {
                "action": "add",
                "session_id": "session-1",
                "expected_revision": 0,
                "variant_id": "variant-1",
                "anchor": {
                    "kind": "before_segment",
                    "segment_id": "segment-1",
                    "quote": "unused",
                },
                "idempotency_key": "cue-add-006",
            }
        )
    with pytest.raises(ValidationError):
        ProjectSoundsInput.model_validate(
            {"action": "get", "session_id": "session-1", "segment_ids": ["segment-1"] * 101}
        )

    validated = ImportAudioReferenceInput(
        root_name="approved",
        relative_path="audio/ref.wav",
        target="voice_reference",
        idempotency_key="reference-import-01",
    )
    assert validated.target == "voice_reference"
    with pytest.raises(ValidationError):
        ImportAudioReferenceInput(
            root_name="approved",
            relative_path="audio/ref.wav",
            target="https://example.com/audio/ref.wav",
            idempotency_key="reference-import-02",
        )

    music = GenerateSoundInput(
        prompt="music",
        model_id="stable_audio_3_small_music_q8_0",
        idempotency_key="sound-gen-music-01",
    )
    assert music.duration_seconds is None
    assert "duration_seconds" not in music.model_fields_set
    assert music.kind is None
    assert music.backend is None


def test_sound_projections_keep_default_reads_compact_and_detail_bounded():
    candidate = {
        "id": "candidate-1",
        "status": "ready",
        "artifact_id": "artifact-1",
        "job_id": "job-1",
        "item_id": None,
        "duration_seconds": 4.0,
        "sample_rate": 44100,
        "channels": 2,
        "provenance": {
            "provider": "audio_cpp",
            "prompt": "a bell rings",
            "threads": 4,
            "num_inference_steps": 8,
            "guidance_scale": 1.0,
            "runtime_identity": {"runtime_version": "0.9.1", "path": "/private/runtime"},
        },
        "raw_metadata": {"secret": "drop"},
    }
    default = sounds._safe_candidate(candidate)
    assert "provenance" not in default
    assert "raw_metadata" not in default
    detail = sounds._safe_candidate(candidate, include_detail=True)
    assert detail["provenance"] == {
        "provider": "audio_cpp", "prompt": "a bell rings", "threads": 4,
        "num_inference_steps": 8, "guidance_scale": 1.0,
        "runtime_identity": {"runtime_version": "0.9.1"},
    }
    assert "/private/runtime" not in str(detail)
    assert sounds._safe_provenance({
        "threads": "private", "num_inference_steps": True, "guidance_scale": float("nan"),
    }) is None

    item = {
        "id": "item-1",
        "name": "Bell",
        "tags": ["metal"],
        "revision": 3,
        "archived": False,
        "preferred_variant": candidate,
        "variants": {"items": [candidate], "next_cursor": "opaque-next"},
        "source_path": "/must/not/escape",
    }
    assert "provenance" not in str(sounds._safe_sound_item(item))
    assert "source_path" not in sounds._safe_sound_item(item)
    assert (
        "prompt"
        in sounds._safe_sound_item(item, include_detail=True)["preferred_variant"]["provenance"]
    )
    assert "provenance" not in str(sounds._safe_sound_detail(item, include_detail=False))
    sound_detail = sounds._safe_sound_detail(item, include_detail=True)
    assert sound_detail["variants"]["items"][0]["provenance"] == detail["provenance"]
    assert sound_detail["variants"]["next_cursor"] == "opaque-next"
    assert "/private" not in str(sound_detail)

    cue = {
        "logical_id": "cue-1",
        "variant_id": "variant-1",
        "anchor": {
            "kind": "before_segment",
            "segment_id": "segment-1",
            "origin_session_id": "secret",
            "resolution": {
                "status": "needs_placement",
                "reason": "no_safe_quiet_gap",
                "transcript": "not returned",
                "artifact_path": "/private",
            },
        },
        "playback": {"mode": "overlay", "offset_ms": 0, "raw": "drop"},
        "included": True,
        "placement_status": "ready",
        "sound": {
            "name": "Bell",
            "artifact_id": "artifact-1",
            "artifact_path": "/private",
            "status": "ready",
        },
        "artifact_path": "/private",
    }
    safe_cue = sounds._safe_project_cue(cue)
    assert "origin_session_id" not in str(safe_cue)
    assert "artifact_path" not in str(safe_cue)
    assert "raw" not in safe_cue["playback"]
    assert safe_cue["placement"] == {
        "status": "needs_placement", "reason": "no_safe_quiet_gap"
    }
    assert "transcript" not in str(safe_cue)


def test_sound_catalogue_and_provenance_project_bounded_model_metadata():
    catalogue = sounds._safe_catalogue(
        {
            "provider": "audio_cpp",
            "operation": "gen",
            "models": [
                {
                    "id": "stable_audio_3_small_music_q8_0",
                    "label": "Stable Audio Music",
                    "family": "stable_audio",
                    "cached": True,
                    "kinds": ["music", "sound", "unknown"],
                    "description": "Generate a short music bed.",
                    "source_url": "https://huggingface.co/example/stable-audio",
                    "backends": ["cpu", "vulkan", "cuda"],
                    "duration": {
                        "minimum": 1,
                        "maximum": 600,
                        "default": 30,
                        "description": "Instrumental requests may last up to ten minutes.",
                    },
                    "supports_lyrics": True,
                    "requires_lyrics": False,
                    "parameters": {
                        "num_inference_steps": {
                            "type": "integer",
                            "default": 8,
                            "minimum": 1,
                            "maximum": 64,
                            "label": "Inference steps",
                            "description": "Sampling steps.",
                            "native_path": "/private/model.gguf",
                        },
                        "positive_control": {
                            "type": "number",
                            "default": 1.25,
                            "exclusive_minimum": 0,
                            "maximum": float("inf"),
                            "label": "Positive control",
                        },
                        "unknown_option": {"type": "object", "label": "Drop me"},
                    },
                    "local_path": "/private/model.gguf",
                }
            ],
            "unavailable_models": [
                {
                    "id": "music-without-sidecars",
                    "available": False,
                    "reason": "Pinned sidecar is missing.",
                }
            ],
            "runtime": {
                "available": True,
                "version": "0.9.1",
                "backends": ["cpu", "vulkan", "cuda", "/private/runtime"],
                "path": "/private/runtime",
            },
            "notes": "CUDA is available only for supported model packages.",
            "backends": ["cpu", "vulkan", "cuda", "/private/runtime"],
            "defaults": {"model_id": "stable_audio_3_small_sfx_q8_0", "backend": "cuda"},
            "credentials": {"token": "never expose"},
        }
    )
    model = catalogue["models"][0]
    assert model["kinds"] == ["music", "sound"]
    assert model["backends"] == ["cpu", "vulkan", "cuda"]
    assert model["duration"] == {
        "minimum": 1,
        "maximum": 600,
        "default": 30,
        "description": "Instrumental requests may last up to ten minutes.",
    }
    assert model["parameters"]["num_inference_steps"] == {
        "type": "integer",
        "default": 8,
        "minimum": 1,
        "maximum": 64,
        "label": "Inference steps",
        "description": "Sampling steps.",
    }
    assert model["parameters"]["positive_control"] == {
        "type": "number",
        "default": 1.25,
        "exclusive_minimum": 0,
        "label": "Positive control",
    }
    assert "unknown_option" not in model["parameters"]
    assert "local_path" not in model
    assert "credentials" not in catalogue
    assert catalogue["backends"] == ["cpu", "vulkan", "cuda"]
    assert catalogue["runtime"]["backends"] == ["cpu", "vulkan", "cuda"]
    assert catalogue["defaults"]["backend"] == "cuda"
    assert catalogue["notes"] == "CUDA is available only for supported model packages."
    assert "/private" not in str(catalogue)
    assert catalogue["unavailable_models"][0]["available"] is False

    detail = sounds._safe_provenance(
        {
            "kind": "music",
            "backend": "cuda",
            "model_id": "/private/model.gguf",
            "lyrics": "stay with me",
            "parameters": {
                "steps": 24,
                "path": "/private/model.gguf",
                "api_key": "must-not-escape",
                "tags": "jazz/swing",
                "negative_prompt": "No vocals / bright drums",
                "profile": "balanced",
                "sampler": "weights.gguf",
                "cot": "melody",
            },
            "runtime_identity": {
                "runtime_version": "0.9.1",
                "model_revision": "/private/revision",
                "runtime_path": "/private/runtime",
                "repository": "example/stable-audio",
                "asset_manifest": [
                    {
                        "filename": "model/weights.gguf",
                        "sha256": "a" * 64,
                        "revision": "pinned-r1",
                        "bytes": 1234,
                        "path": "/private/model/weights.gguf",
                    },
                    {"filename": "../escape.gguf", "sha256": "b" * 64},
                    {"filename": "C:/private/weights.gguf", "sha256": "c" * 64},
                ],
            },
        }
    )
    assert detail is not None
    assert detail["kind"] == "music"
    assert detail["backend"] == "cuda"
    assert "model_id" not in detail
    assert detail["lyrics"] == "stay with me"
    assert detail["parameters"] == {
        "steps": 24,
        "tags": "jazz/swing",
        "negative_prompt": "No vocals / bright drums",
        "profile": "balanced",
        "cot": "melody",
    }
    identity = detail["runtime_identity"]
    assert "model_revision" not in identity
    assert identity["asset_manifest"] == [
        {
            "filename": "model/weights.gguf",
            "sha256": "a" * 64,
            "revision": "pinned-r1",
            "bytes": 1234,
        }
    ]
    assert "/private" not in str(detail)

    generation, _outcome = sounds._safe_generation_result(
        {
            "candidate_id": "candidate-cuda",
            "resolved_settings": {
                "model_id": "minimax_h3_q4_k_int8_dit",
                "backend": "cuda",
                "runtime_path": "/private/runtime",
            },
        }
    )
    assert generation["resolved_settings"]["backend"] == "cuda"
    assert "/private" not in str(generation)


def test_real_music3_q4_model_assets_are_safely_projected_in_candidate_provenance():
    model = get_audio_cpp_generation_model("minimax_music3_q4_0")
    runtime_root = "/private/runtime/audio_cpp"
    model_assets = [
        {
            "path": asset.path,
            "size_bytes": asset.size_bytes,
            "sha256": asset.sha256,
        }
        for asset in model.files
    ]
    model_assets.append(
        {
            "path": f"{runtime_root}/models/private.gguf",
            "size_bytes": 1,
            "sha256": "f" * 64,
        }
    )
    candidate = {
        "id": "candidate-music3-q4",
        "status": "ready",
        "provenance": {
            "kind": "music",
            "model_id": model.id,
            "runtime_identity": {
                "provider": "audio_cpp",
                "operation": "generate",
                "model_id": model.id,
                "model_revision": model.revision,
                "runtime_path": runtime_root,
                "model_assets": model_assets,
            },
        },
    }

    projected = sounds._safe_candidate(candidate, include_detail=True)
    manifest = projected["provenance"]["runtime_identity"]["asset_manifest"]

    assert len(model.files) == len(manifest) == 13
    assert manifest == [
        {
            "filename": asset.path,
            "sha256": asset.sha256,
            "revision": model.revision,
            "bytes": asset.size_bytes,
        }
        for asset in model.files
    ]
    assert "model_assets" not in projected["provenance"]["runtime_identity"]
    assert runtime_root not in str(projected)
    assert "private.gguf" not in str(projected)
    assert all(not asset["filename"].startswith("/") for asset in manifest)


def test_sound_catalogue_model_and_kind_selectors_filter_safe_projection():
    with pytest.raises(ValidationError):
        SoundCatalogueInput(model_id="m" * 256)
    with pytest.raises(ValidationError):
        SoundCatalogueInput(kind="voice")

    application = Mock()
    application.sound_catalogue.return_value = {
        "provider": "audio_cpp",
        "models": [
            {
                "id": "ace_step_turbo_q8_0",
                "label": "ACE-Step Turbo",
                "family": "ace_step",
                "kinds": ["music"],
                "backends": ["cpu"],
                "parameters": {
                    "bpm": {"type": "integer", "label": "BPM", "default": 96},
                    "sampler_mode": {
                        "type": "string",
                        "label": "Sampler",
                        "default": "heun",
                        "options": ["euler", "heun"],
                    },
                },
                "model_revision": "pinned-r1",
                "model_sha256": "a" * 64,
                "local_path": "/private/ace-step",
            },
            {
                "id": "stable_audio_3_small_sfx_q8_0",
                "label": "Stable Audio SFX",
                "family": "stable_audio",
                "kinds": ["sound"],
                "backends": ["cpu"],
            },
        ],
        "unavailable_models": [
            {"id": "music-without-sidecars", "available": False, "reason": "Missing sidecar."}
        ],
    }

    full = sounds.sound_catalogue(_runtime(application), SoundCatalogueInput())
    assert [item["id"] for item in full["models"]] == [
        "ace_step_turbo_q8_0",
        "stable_audio_3_small_sfx_q8_0",
    ]
    selected = sounds.sound_catalogue(
        _runtime(application),
        SoundCatalogueInput(model_id="ace_step_turbo_q8_0", kind="music"),
    )
    assert [item["id"] for item in selected["models"]] == ["ace_step_turbo_q8_0"]
    ace = selected["models"][0]
    assert ace["parameters"]["bpm"]["default"] == 96
    assert ace["parameters"]["sampler_mode"]["options"] == ["euler", "heun"]
    assert ace["model_revision"] == "pinned-r1"
    assert ace["model_sha256"] == "a" * 64
    assert "local_path" not in ace

    sounds_only = sounds.sound_catalogue(
        _runtime(application), SoundCatalogueInput(kind="sound")
    )
    assert [item["id"] for item in sounds_only["models"]] == [
        "stable_audio_3_small_sfx_q8_0"
    ]

    unavailable = sounds.sound_catalogue(
        _runtime(application),
        SoundCatalogueInput(model_id="music-without-sidecars", kind="sound"),
    )
    assert unavailable["models"] == []
    assert unavailable["unavailable_models"] == [
        {
            "id": "music-without-sidecars",
            "available": False,
            "reason": "Missing sidecar.",
        }
    ]
    unknown = sounds.sound_catalogue(
        _runtime(application),
        SoundCatalogueInput(model_id="not-in-catalogue", kind="music"),
    )
    assert unknown["models"] == []
    assert unknown["unavailable_models"] == []
    assert application.sound_catalogue.call_count == 5


def test_audio_import_reads_only_approved_contained_regular_files(monkeypatch, tmp_path):
    approved = tmp_path / "approved"
    approved.mkdir()
    audio = approved / "ref.wav"
    audio.write_bytes(b"RIFF-audio")
    monkeypatch.setattr(
        sound_audio_import, "_named_source_root", lambda _runtime, _name: ("approved", approved)
    )

    runtime = _runtime()
    data, filename = sound_audio_import._read_approved_audio(
        runtime, root_name="approved", relative_path="ref.wav"
    )
    assert data == b"RIFF-audio"
    assert filename == "ref.wav"

    for path in ("../outside.wav", str(audio), "ref.txt"):
        with pytest.raises((PandratorMcpError, ValueError)):
            sound_audio_import._read_approved_audio(
                runtime, root_name="approved", relative_path=path
            )

    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"outside")
    link = approved / "linked.wav"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    with pytest.raises(PandratorMcpError):
        sound_audio_import._read_approved_audio(
            runtime, root_name="approved", relative_path="linked.wav"
        )


def test_audio_import_rejects_oversized_or_changed_sources(monkeypatch, tmp_path):
    approved = tmp_path / "approved"
    approved.mkdir()
    audio = approved / "ref.wav"
    audio.write_bytes(b"123456789")
    monkeypatch.setattr(
        sound_audio_import, "_named_source_root", lambda _runtime, _name: ("approved", approved)
    )
    monkeypatch.setattr(sound_audio_import, "MAX_AUDIO_IMPORT_BYTES", 8)
    with pytest.raises(PandratorMcpError, match="16 MiB"):
        sound_audio_import._read_approved_audio(
            _runtime(), root_name="approved", relative_path="ref.wav"
        )

    monkeypatch.setattr(sound_audio_import, "MAX_AUDIO_IMPORT_BYTES", 16 * 1024 * 1024)
    audio.write_bytes(b"12345678")
    real_fstat = os.fstat
    calls = 0

    def mutate_between_stats(descriptor):
        nonlocal calls
        result = real_fstat(descriptor)
        calls += 1
        if calls == 1:
            with audio.open("ab") as handle:
                handle.write(b"changed")
        return result

    monkeypatch.setattr(sound_audio_import.os, "fstat", mutate_between_stats)
    with pytest.raises(PandratorMcpError, match="changed while it was being read"):
        sound_audio_import._read_approved_audio(
            _runtime(), root_name="approved", relative_path="ref.wav"
        )


def test_generation_work_reference_only_represents_fresh_jobs():
    application = Mock()
    application.generate_sound.return_value = {
        "candidate_id": "candidate-1",
        "job_id": "job-1",
        "status": "queued",
        "cache_hit": False,
        "resolved_settings": {
            "model_id": "stable_audio_3_small_sfx_q8_0",
            "prompt": "private prompt",
            "runtime_identity": {"path": "/private"},
            "duration_seconds": 4.0,
        },
    }
    runtime = _runtime(application)
    result = sounds.generate_sound(
        runtime,
        GenerateSoundInput(prompt="private prompt", idempotency_key="sound-gen-007"),
    )
    assert result.work is not None
    assert result.work.id == "job-1"
    assert "candidate_count" not in result.result
    assert "candidates" not in result.result
    assert "private prompt" not in str(result.result)
    assert "/private" not in str(result.result)

    application.generate_sound.return_value = {
        "candidate_id": "candidate-2",
        "job_id": None,
        "status": "ready",
        "cache_hit": True,
        "resolved_settings": {},
    }
    cached = sounds.generate_sound(
        runtime,
        GenerateSoundInput(prompt="cached", idempotency_key="sound-gen-008"),
    )
    assert cached.work is None

    application.generate_sound.return_value = {
        "candidate_id": "candidate-music",
        "job_id": None,
        "status": "ready",
        "cache_hit": True,
        "resolved_settings": {"model_id": "stable_audio_3_small_music_q8_0"},
    }
    sounds.generate_sound(
        runtime,
        GenerateSoundInput(
            prompt="music",
            model_id="stable_audio_3_small_music_q8_0",
            idempotency_key="sound-gen-music-02",
        ),
    )
    assert application.generate_sound.call_args.kwargs["duration_seconds"] is None


def test_batch_generation_projection_includes_pending_cache_hits_and_filters_children():
    raw = {
        "candidate_id": "candidate-cached",
        "job_id": "job-cached",
        "status": "queued",
        "cache_hit": False,
        "candidate_count": 4,
        "candidates": [
            {
                "candidate_id": "candidate-cached",
                "job_id": "job-cached",
                "item_id": None,
                "status": "queued",
                "cache_hit": True,
                "seed": 101,
                "prompt": "private prompt must be omitted",
                "runtime_identity": {"runtime_path": "/private/runtime"},
            },
            {
                "candidate_id": "/private/injected-candidate",
                "job_id": "job-injected",
                "item_id": None,
                "status": "queued",
                "cache_hit": False,
                "seed": 102,
            },
            {
                "candidate_id": "candidate-new",
                "job_id": "job-new",
                "item_id": None,
                "status": "queued",
                "cache_hit": False,
                "seed": 103,
                "lyrics": "private lyrics must be omitted",
            },
            {
                "candidate_id": "candidate-same-job",
                "job_id": "job-cached",
                "item_id": None,
                "status": "running",
                "cache_hit": True,
                "seed": 104,
                "native_path": "/private/model.gguf",
            },
        ],
        "resolved_settings": {
            "model_id": "stable_audio_3_small_sfx_q8_0",
            "prompt": "private prompt must not escape",
            "runtime_identity": {"runtime_path": "/private/runtime"},
        },
        "runtime_path": "/private/runtime",
    }
    result, outcome = sounds._safe_generation_result(raw)

    assert result["candidate_count"] == len(result["candidates"]) == 3
    assert result["cache_hit"] is False
    assert result["candidate_id"] == "candidate-cached"
    assert result["job_id"] == "job-cached"
    expected_child_keys = {
        "candidate_id", "job_id", "item_id", "status", "cache_hit", "seed"
    }
    assert all(set(candidate) == expected_child_keys for candidate in result["candidates"])
    assert outcome is not None and outcome.work is not None
    assert outcome.work.id == "job-cached"
    assert [action.arguments for action in outcome.next_actions] == [
        {"work_id": "job-cached", "work_type": "job"},
        {"work_id": "job-new", "work_type": "job"},
    ]
    assert "private" not in str(result)
    assert "prompt" not in str(result)
    assert "lyrics" not in str(result)


def test_ready_batch_generation_projection_has_no_work():
    result, outcome = sounds._safe_generation_result(
        {
            "candidate_id": "candidate-ready-1",
            "job_id": "job-ready-1",
            "status": "ready",
            "cache_hit": True,
            "candidate_count": 2,
            "candidates": [
                {
                    "candidate_id": "candidate-ready-1",
                    "job_id": "job-ready-1",
                    "item_id": "item-1",
                    "status": "ready",
                    "cache_hit": True,
                    "seed": 21,
                },
                {
                    "candidate_id": "candidate-ready-2",
                    "job_id": "job-ready-2",
                    "item_id": "item-2",
                    "status": "ready",
                    "cache_hit": True,
                    "seed": 22,
                },
            ],
            "resolved_settings": {},
        }
    )
    assert outcome is None
    assert result["candidate_count"] == len(result["candidates"]) == 2
    assert result["cache_hit"] is True


def test_batch_projection_with_cached_ready_first_candidate_tracks_later_pending_job():
    result, outcome = sounds._safe_generation_result(
        {
            "candidate_id": "candidate-cached-ready",
            "job_id": "job-cached-ready",
            "status": "ready",
            "cache_hit": False,
            "candidate_count": 2,
            "candidates": [
                {
                    "candidate_id": "candidate-cached-ready",
                    "job_id": "job-cached-ready",
                    "item_id": "item-cached",
                    "status": "ready",
                    "cache_hit": True,
                    "seed": 31,
                },
                {
                    "candidate_id": "candidate-new-pending",
                    "job_id": "job-new-pending",
                    "item_id": None,
                    "status": "queued",
                    "cache_hit": False,
                    "seed": 32,
                },
            ],
            "resolved_settings": {},
        }
    )
    assert result["cache_hit"] is False
    assert result["candidate_id"] == "candidate-cached-ready"
    assert outcome is not None and outcome.work is not None
    assert outcome.work.id == "job-new-pending"
    assert [action.arguments for action in outcome.next_actions] == [
        {"work_id": "job-new-pending", "work_type": "job"}
    ]


def test_registration_exposes_five_fixed_tools_with_typed_signatures():
    recorded = {}

    class Server:
        def tool(self, *, name, title, annotations):
            def decorate(function):
                recorded[name] = (function, title, annotations)
                return function

            return decorate

    read_only = object()
    write_action = object()
    sounds.register_sound_tools(
        Server(), _runtime(), lambda *args: {}, read_only=read_only, write_action=write_action
    )
    assert set(recorded) == {
        "pandrator_sound_catalogue",
        "pandrator_generate_sound",
        "pandrator_sound_library",
        "pandrator_import_audio_reference",
        "pandrator_project_sounds",
    }
    assert recorded["pandrator_sound_catalogue"][2] is read_only
    assert all(
        recorded[name][2] is write_action
        for name in recorded
        if name != "pandrator_sound_catalogue"
    )
    assert list(inspect.signature(recorded["pandrator_generate_sound"][0]).parameters) == [
        "prompt",
        "model_id",
        "candidate_count",
        "duration_seconds",
        "kind",
        "backend",
        "lyrics",
        "parameters",
        "seed",
        "threads",
        "idempotency_key",
    ]
    assert list(inspect.signature(recorded["pandrator_sound_catalogue"][0]).parameters) == [
        "model_id",
        "kind",
    ]


def test_installed_mcp_server_preserves_sound_action_field_presence():
    application = Mock()
    application.get_sound_candidate.return_value = {"id": "candidate-1", "status": "ready"}
    application.get_sound.return_value = {"id": "item-1", "variants": {"items": []}}
    application.get_project_sound_plan.return_value = {"session_id": "session-1", "cues": {"items": []}}
    application.get_project_sound_mix_status.return_value = {"session_id": "session-1"}
    application.update_project_sound_cue.return_value = {"session_id": "session-1", "revision": 2}
    application.generate_sound.return_value = {
        "candidate_id": "candidate-2", "job_id": None, "status": "ready",
        "cache_hit": True, "resolved_settings": {},
    }
    server = MCPServer("sound-presence-test")
    annotations = ToolAnnotations(read_only_hint=True)
    sounds.register_sound_tools(
        server, _runtime(application), _call_with_validated_input,
        read_only=annotations, write_action=annotations,
    )

    async def exercise():
        async with Client(server, mode="auto", raise_exceptions=False) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert set(tools) == {
                "pandrator_sound_catalogue", "pandrator_generate_sound",
                "pandrator_sound_library", "pandrator_import_audio_reference",
                "pandrator_project_sounds",
            }
            assert "arguments" not in tools["pandrator_sound_library"].input_schema["properties"]
            catalogue = await client.call_tool("pandrator_sound_catalogue", {})
            generated = await client.call_tool("pandrator_generate_sound", {
                "prompt": "short bell", "idempotency_key": "sound-generate-1",
            })
            generated_cuda = await client.call_tool("pandrator_generate_sound", {
                "prompt": "H3 instrumental phrase",
                "model_id": "minimax_h3_q4_k_int8_dit",
                "kind": "music",
                "backend": "cuda",
                "seed": 73,
                "candidate_count": 3,
                "idempotency_key": "sound-generate-cuda-1",
            })
            candidate = await client.call_tool(
                "pandrator_sound_library", {"action": "candidate", "candidate_id": "candidate-1"}
            )
            sound = await client.call_tool(
                "pandrator_sound_library", {"action": "get", "item_id": "item-1"}
            )
            plan = await client.call_tool(
                "pandrator_project_sounds", {"action": "get", "session_id": "session-1"}
            )
            status = await client.call_tool(
                "pandrator_project_sounds", {"action": "status", "session_id": "session-1"}
            )
            updated = await client.call_tool("pandrator_project_sounds", {
                "action": "update", "session_id": "session-1", "logical_cue_id": "cue-1",
                "expected_revision": 1, "included": False,
                "idempotency_key": "sound-update-1",
            })
            invalid = await client.call_tool("pandrator_sound_library", {
                "action": "candidate", "candidate_id": "candidate-1", "query": "wrong-action",
            })
            assert all(
                not result.is_error
                for result in (
                    catalogue,
                    generated,
                    generated_cuda,
                    candidate,
                    sound,
                    plan,
                    status,
                    updated,
                )
            )
            assert invalid.is_error

    asyncio.run(exercise())
    application.get_sound_candidate.assert_called_once_with("candidate-1")
    assert application.generate_sound.call_count == 2
    assert application.generate_sound.call_args_list[0].kwargs == {
        "prompt": "short bell",
        "model_id": "stable_audio_3_small_sfx_q8_0",
        "candidate_count": 1,
        "duration_seconds": 4.0,
        "kind": None,
        "backend": None,
        "lyrics": None,
        "parameters": None,
        "seed": None,
        "threads": None,
        "idempotency_key": "sound-generate-1",
    }
    assert application.generate_sound.call_args_list[1].kwargs == {
        "prompt": "H3 instrumental phrase",
        "model_id": "minimax_h3_q4_k_int8_dit",
        "candidate_count": 3,
        "duration_seconds": None,
        "kind": "music",
        "backend": "cuda",
        "lyrics": None,
        "parameters": None,
        "seed": 73,
        "threads": None,
        "idempotency_key": "sound-generate-cuda-1",
    }
    application.sound_catalogue.assert_called_once_with()
    application.get_sound.assert_called_once_with("item-1", cursor=None, limit=20)
    application.get_project_sound_plan.assert_called_once_with(
        "session-1", segment_ids=None, cursor=None, limit=40
    )
    application.get_project_sound_mix_status.assert_called_once_with(
        "session-1", base_artifact_id=None
    )
    application.update_project_sound_cue.assert_called_once_with(
        "session-1", "cue-1", expected_revision=1, changes={"included": False},
        idempotency_key="sound-update-1",
    )
