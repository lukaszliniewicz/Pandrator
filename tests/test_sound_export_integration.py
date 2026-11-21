"""Sound mix handoff into the audiobook subtitle/video export pipeline."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from werkzeug.utils import secure_filename

from pandrator.web import (
    export_video,
    sound_workflow_outputs,
    soundtrack_export,
    workflow_book_export,
    workflow_export,
)
from pandrator.web.database import Database
from pandrator.web.export_names import export_file_stem
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def output_storage_database(tmp_path):
    """Real admission database; the fake context still owns all IO callbacks."""
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    try:
        yield database
    finally:
        database.dispose()


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
        by_role={},
        attached_sources=[],
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
    tmp_path, monkeypatch, output_storage_database
):
    upload_media = SimpleNamespace(id="source-video")
    voice_audio = SimpleNamespace(id="voice-audio")
    master_audio = SimpleNamespace(id="soundtrack-master")
    mixed_audio = SimpleNamespace(id="mixed-audio")
    voice_path = tmp_path / "voice.wav"
    mixed_path = tmp_path / "mixed.wav"
    voice_path.write_bytes(b"voice-audio")
    mixed_path.write_bytes(b"mixed-audio")
    media_path = tmp_path / "source.mp4"
    media_path.write_bytes(b"source-video")
    events = []
    resolved = {
        voice_audio.id: (voice_audio, voice_path),
        mixed_audio.id: (mixed_audio, mixed_path),
    }
    context = SimpleNamespace(
        _resolve_input=lambda artifact_id: resolved[artifact_id],
        database=output_storage_database,
    )
    settings = {
        "audio_match_source_duration": False,
        "mix_ducking": "strong",
        "mix_voice_gain_db": -4,
    }
    inputs = SimpleNamespace(
        session_id="session-id",
        settings=settings,
        record=SimpleNamespace(
            storage_key="test-book", target_language=None, source_language=None
        ),
    )
    selection = SimpleNamespace(
        upload_media=upload_media,
        upload_audio=None,
        dubbing_audio=voice_audio,
        selected_subtitles=[],
        export_mode="video",
        subtitle_format="srt",
        subtitle_mode="off",
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
        media_path=media_path,
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
    tmp_path, monkeypatch, output_storage_database
):
    upload_media = SimpleNamespace(id="source-video")
    master_audio = SimpleNamespace(id="source-master")
    mixed_audio = SimpleNamespace(id="mixed-audio")
    mixed_path = tmp_path / "mixed-source.wav"
    mixed_path.write_bytes(b"mixed-source-audio")
    media_path = tmp_path / "source.mp4"
    media_path.write_bytes(b"source-video")
    events = []
    context = SimpleNamespace(
        _resolve_input=lambda artifact_id: (mixed_audio, mixed_path)
        if artifact_id == mixed_audio.id
        else pytest.fail(f"unexpected audio resolution: {artifact_id}"),
        database=output_storage_database,
    )
    settings = {"audio_match_source_duration": False}
    inputs = SimpleNamespace(
        session_id="session-id",
        settings=settings,
        record=SimpleNamespace(storage_key="test-book"),
    )
    selection = SimpleNamespace(
        upload_media=upload_media,
        upload_audio=None,
        dubbing_audio=None,
        selected_subtitles=[],
        export_mode="video",
        subtitle_format="srt",
        subtitle_mode="off",
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
        media_path=media_path,
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


class TestExportFileStem:
    """The export stem stays well inside file-name and path limits."""

    def test_short_name_is_unchanged(self):
        assert export_file_stem("My_Book-01", "fallback") == "My_Book-01"

    def test_longest_project_name_is_capped(self):
        stem = export_file_stem("x" * 255, "fallback")

        assert len(stem) == 80
        assert stem == "x" * 80

    @pytest.mark.parametrize("separator", [".", "_", "-"])
    def test_cap_never_leaves_a_trailing_separator(self, separator):
        stem = export_file_stem("x" * 79 + separator + "tail", "fallback")

        assert stem == "x" * 79
        assert not stem.endswith(separator)

    def test_name_without_printable_characters_uses_the_fallback(self):
        assert export_file_stem("...", "storage-key") == "storage-key"
        assert export_file_stem("!!!", "storage-key") == "storage-key"

    @pytest.mark.parametrize(
        "name",
        [
            "Ordinary project",
            "  leading and trailing  ",
            "dots.and_underscores-and-dashes",
            "ünïcode ïn ä näme",
            "a/b\\c:d*e?f",
            "x" * 255,
            "x" * 79 + ".y",
            "...",
            "!!!",
        ],
    )
    def test_result_is_already_a_secure_filename(self, name):
        stem = export_file_stem(name, "fallback")

        assert stem == secure_filename(stem)
        assert len(stem) <= 80
