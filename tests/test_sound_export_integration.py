"""Sound mix handoff into the audiobook subtitle/video export pipeline."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from pandrator.web import (
    export_video,
    sound_workflow_outputs,
    soundtrack_export,
    workflow_book_export,
    workflow_export,
)


def test_audiobook_video_export_passes_mix_to_book_caption_and_codec_stage(
    tmp_path, monkeypatch
):
    dry_audio = SimpleNamespace(id="dry-audio", role="assembled_audio")
    mixed_audio = SimpleNamespace(id="mixed-audio", role="sound_mix.audio")
    record = SimpleNamespace(
        workflow_kind="audiobook", name="Test book", storage_key="test-book"
    )
    inputs = SimpleNamespace(
        session_id="session-id",
        settings={"include_sounds": True, "export_mode": "video_book"},
        record=record,
        output_settings_snapshot={},
    )
    context = SimpleNamespace(_session_dir=lambda _session_id: tmp_path)
    events = []

    monkeypatch.setattr(
        workflow_export, "resolve_export_inputs", lambda _context, _payload: inputs
    )
    monkeypatch.setattr(
        workflow_export,
        "select_generated_audio",
        lambda _inputs, *, legacy_role: dry_audio,
    )

    def render_mix(_context, audio, **_kwargs):
        events.append(("mix", audio.id))
        return mixed_audio

    def export_book(_context, received_inputs, audio, *_args):
        assert received_inputs is inputs
        events.append(("book-caption-and-encode-entry", audio.id))
        return {"artifact_ids": ["book-export"]}

    monkeypatch.setattr(workflow_export, "render_output_sounds", render_mix)
    monkeypatch.setattr(workflow_book_export, "export_book", export_book)

    result = workflow_export.export(
        context,
        {
            "session_id": inputs.session_id,
            "settings": inputs.settings,
            "display_subtitle_snapshot": None,
        },
        progress=lambda *_args: None,
        cancel_event=threading.Event(),
    )

    assert events == [
        ("mix", dry_audio.id),
        ("book-caption-and-encode-entry", mixed_audio.id),
    ]
    assert result == {"artifact_ids": ["book-export"]}


def test_timed_video_mixes_effects_after_the_voice_soundtrack_master(
    tmp_path, monkeypatch
):
    upload_media = SimpleNamespace(id="source-video")
    voice_audio = SimpleNamespace(id="voice-audio")
    master_audio = SimpleNamespace(id="soundtrack-master")
    mixed_audio = SimpleNamespace(id="mixed-audio")
    voice_path = tmp_path / "voice.wav"
    mixed_path = tmp_path / "mixed.wav"
    events = []
    resolved = {
        voice_audio.id: (voice_audio, voice_path),
        mixed_audio.id: (mixed_audio, mixed_path),
    }
    context = SimpleNamespace(_resolve_input=lambda artifact_id: resolved[artifact_id])
    settings = {
        "audio_match_source_duration": False,
        "mix_ducking": "strong",
        "mix_voice_gain_db": -4,
    }
    inputs = SimpleNamespace(
        session_id="session-id",
        settings=settings,
        record=SimpleNamespace(storage_key="test-book"),
    )
    selection = SimpleNamespace(
        upload_media=upload_media,
        dubbing_audio=voice_audio,
        audio_mode="mixed",
    )

    def ensure_master(_context, **kwargs):
        assert kwargs["speech"] is voice_audio
        assert kwargs["audio_mode"] == "mixed"
        assert kwargs["settings"] == settings
        events.append(("master", master_audio.id))
        return master_audio

    def render_mix(_context, audio, **kwargs):
        assert audio is master_audio
        assert kwargs["timeline_artifact_id"] == voice_audio.id
        events.append(("sound-effects", audio.id))
        return mixed_audio

    def build_mux(_video, audio_path, _destination, **_kwargs):
        events.append(("mux-input", audio_path))
        return ["mux"]

    monkeypatch.setattr(
        soundtrack_export,
        "probe_soundtrack_media",
        lambda _path: {"duration": 1.0, "video_duration": 1.0, "has_video": False},
    )
    monkeypatch.setattr(soundtrack_export, "ensure_soundtrack_master", ensure_master)
    monkeypatch.setattr(sound_workflow_outputs, "has_output_sounds", lambda *_args: True)
    monkeypatch.setattr(sound_workflow_outputs, "render_output_sounds", render_mix)
    monkeypatch.setattr(
        "pandrator.logic.dubbing.video_muxing.build_replace_video_audio_command",
        build_mux,
    )
    monkeypatch.setattr(
        export_video,
        "run_video_command",
        lambda command, _cancel_event: events.append(("mux-run", command)),
    )

    prepared = export_video._prepare_video_audio(
        context,
        inputs,
        selection,
        media_path=tmp_path / "source.mp4",
        scratch_dir=tmp_path,
        ffmpeg_executable="ffmpeg-test",
        video_encoder="libx264",
        video_audio_bitrate="192k",
        progress=lambda *_args: None,
        cancel_event=threading.Event(),
    )

    assert events == [
        ("master", master_audio.id),
        ("sound-effects", master_audio.id),
        ("mux-input", str(mixed_path)),
        ("mux-run", ["mux"]),
    ]
    assert mixed_audio.id in prepared.parent_ids
    assert voice_audio.id in prepared.parent_ids


def test_source_only_video_mixes_effects_over_the_source_master(
    tmp_path, monkeypatch
):
    upload_media = SimpleNamespace(id="source-video")
    master_audio = SimpleNamespace(id="source-master")
    mixed_audio = SimpleNamespace(id="mixed-audio")
    mixed_path = tmp_path / "mixed-source.wav"
    events = []
    context = SimpleNamespace(
        _resolve_input=lambda artifact_id: (mixed_audio, mixed_path)
        if artifact_id == mixed_audio.id
        else pytest.fail(f"unexpected audio resolution: {artifact_id}")
    )
    settings = {"audio_match_source_duration": False}
    inputs = SimpleNamespace(
        session_id="session-id",
        settings=settings,
        record=SimpleNamespace(storage_key="test-book"),
    )
    selection = SimpleNamespace(
        upload_media=upload_media,
        dubbing_audio=None,
        audio_mode="source",
    )

    def ensure_master(_context, **kwargs):
        assert kwargs["source"] is upload_media
        assert kwargs["speech"] is None
        assert kwargs["audio_mode"] == "source"
        events.append(("source-master", master_audio.id))
        return master_audio

    def render_mix(_context, audio, **kwargs):
        assert audio is master_audio
        assert kwargs["timeline_artifact_id"] is None
        events.append(("source-time-effects", audio.id))
        return mixed_audio

    def build_mux(_video, audio_path, _destination, **_kwargs):
        events.append(("mux-input", audio_path))
        return ["mux"]

    monkeypatch.setattr(soundtrack_export, "ensure_soundtrack_master", ensure_master)
    monkeypatch.setattr(sound_workflow_outputs, "has_output_sounds", lambda *_args: True)
    monkeypatch.setattr(sound_workflow_outputs, "render_output_sounds", render_mix)
    monkeypatch.setattr(
        "pandrator.logic.dubbing.video_muxing.build_replace_video_audio_command",
        build_mux,
    )
    monkeypatch.setattr(
        export_video,
        "run_video_command",
        lambda command, _cancel_event: events.append(("mux-run", command)),
    )

    prepared = export_video._prepare_video_audio(
        context,
        inputs,
        selection,
        media_path=tmp_path / "source.mp4",
        scratch_dir=tmp_path,
        ffmpeg_executable="ffmpeg-test",
        video_encoder="libx264",
        video_audio_bitrate="192k",
        progress=lambda *_args: None,
        cancel_event=threading.Event(),
    )

    assert events == [
        ("source-master", master_audio.id),
        ("source-time-effects", master_audio.id),
        ("mux-input", str(mixed_path)),
        ("mux-run", ["mux"]),
    ]
    assert prepared.parent_ids == [upload_media.id, master_audio.id, mixed_audio.id]
