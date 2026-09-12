import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from pandrator.logic.dubbing.video_muxing import build_removal_only_video_command


def test_removal_builder_with_audio_uses_exact_trim_concat_and_aac():
    command = build_removal_only_video_command(
        "input.mp4",
        "output.mp4",
        [(0, 1250), (2500, 3000)],
        has_audio=True,
    )
    rendered = " ".join(command)
    assert "trim=start=0.000:end=1.250" in rendered
    assert "atrim=start=2.500:end=3.000" in rendered
    assert "setpts=PTS-STARTPTS" in rendered
    assert "asetpts=PTS-STARTPTS" in rendered
    assert "concat=n=2:v=1:a=1" in rendered
    assert "-c:a aac" in rendered
    assert "-b:a 192k" in rendered
    assert "-map_metadata 0" in rendered
    assert "-movflags +faststart" in rendered


def test_removal_builder_without_audio_omits_audio_mapping():
    command = build_removal_only_video_command(
        "input.mp4",
        "output.mp4",
        [(100, 900)],
        has_audio=False,
    )
    rendered = " ".join(command)
    assert "concat=n=1:v=1:a=0" in rendered
    assert "[0:a]" not in rendered
    assert "-map [aout]" not in rendered
    assert "-map [vout]" in rendered


def test_removal_builder_integrates_output_scaling_into_complex_graph():
    command = build_removal_only_video_command(
        "input.mp4",
        "output.mp4",
        [(100, 900)],
        has_audio=True,
        video_resolution="720p",
    )
    rendered = " ".join(command)
    assert "[vconcat]scale=-2:720:flags=lanczos[vout]" in rendered
    assert " -vf " not in f" {rendered} "


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg integration requires ffmpeg and ffprobe",
)
def test_removal_command_renders_exact_duration_with_audio_and_scaling():
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory, "source.mp4")
        output = Path(directory, "edited.mp4")
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=640x480:r=25:d=3",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=3",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-shortest",
                str(source),
            ],
            check=True,
            capture_output=True,
        )
        command = build_removal_only_video_command(
            str(source),
            str(output),
            [(0, 1000), (2000, 3000)],
            has_audio=True,
            video_resolution="360p",
        )

        subprocess.run(command, check=True, capture_output=True)
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,height",
                "-of",
                "json",
                str(output),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        metadata = json.loads(probe.stdout)

        assert float(metadata["format"]["duration"]) == pytest.approx(2.0, abs=0.08)
        assert {stream["codec_type"] for stream in metadata["streams"]} == {
            "audio",
            "video",
        }
        video = next(
            stream for stream in metadata["streams"] if stream["codec_type"] == "video"
        )
        assert video["height"] == 360


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg integration requires ffmpeg and ffprobe",
)
def test_converted_voiceover_exports_cut_footage_and_retimed_subtitles(tmp_path):
    from pandrator.logic.dubbing.srt_utils import parse_srt
    from pandrator.web.artifacts import ArtifactService
    from pandrator.web.database import Database
    from pandrator.web.jobs import JobQueue
    from pandrator.web.models import Artifact, SessionSource, SourceAsset
    from pandrator.web.sessions import SessionService
    from pandrator.web.workflow_handlers import WorkflowHandlers
    from pandrator.web.workflows import WorkflowService
    from tests.web_test_support import prepare_web_test_data_root

    def run(*arguments):
        return subprocess.run(
            list(arguments), check=True, capture_output=True, text=True, timeout=30
        ).stdout

    def video_hash(path):
        return run(
            "ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0",
            "-c:v", "rawvideo", "-f", "hash", "-hash", "sha256", "-",
        )

    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    try:
        sessions = SessionService(database)
        record = sessions.create("Cut voiceover", workflow_kind="media_edit")
        directory = paths.sessions / record.storage_key
        directory.mkdir()
        artifacts = ArtifactService(database, paths)
        source_path = directory / "source.mp4"
        run(
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            "testsrc2=size=160x90:rate=25:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:v", "libx264", "-c:a", "aac", "-shortest", str(source_path),
        )
        source = artifacts.register(
            source_path, kind="video", role="upload", session_id=record.id,
        )
        with database.session() as session:
            asset = SourceAsset(
                artifact_id=source.id, display_name=source_path.name,
                kind="mp4", mime_type="video/mp4",
            )
            session.add(asset)
            session.flush()
            session.add(SessionSource(
                session_id=record.id, source_asset_id=asset.id,
                role="primary", is_current=True,
            ))
        transcript_path = directory / "transcription.srt"
        transcript_path.write_text(
            "1\n00:00:00,200 --> 00:00:00,600\nBefore cut\n\n"
            "2\n00:00:02,200 --> 00:00:02,600\nAfter cut\n",
            encoding="utf-8",
        )
        artifacts.register(
            transcript_path, kind="srt", role="transcription",
            session_id=record.id, parent_ids=[source.id],
        )
        handlers = WorkflowHandlers(database, paths)
        plan = handlers.media_edit.prepare(record.id)["plan"]
        plan = handlers.media_edit.update(
            record.id, plan["revision"], reviewed=True,
            keep_ranges=[
                {"start_ms": 0, "end_ms": 1000},
                {"start_ms": 2000, "end_ms": 3000},
            ],
        )["plan"]
        rendered = handlers.media_edit_render(
            {"session_id": record.id, "revision": plan["revision"], "settings": {}},
            lambda *_args: None, threading.Event(),
        )
        with database.session() as session:
            edited = session.get(Artifact, rendered["media_artifact_id"])
            edited_path = paths.root / edited.relative_path
        expected_frames = video_hash(edited_path)
        assert expected_frames != video_hash(source_path)
        record = sessions.update(record.id, record.revision, {"workflow_kind": "voiceover"})
        speech_path = directory / "speech.wav"
        run(
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            "sine=frequency=880:duration=2", str(speech_path),
        )
        artifacts.register(
            speech_path, kind="audio", role="assembled_audio", session_id=record.id,
        )
        workflow = WorkflowService(database, JobQueue(database))
        for audio_mode in ("preserve", "mixed", "dubbing_only"):
            resolved = workflow.resolve_stage(record.id, "export", {
                "export_mode": "media", "audio_mode": audio_mode,
                "subtitle_mode": "soft", "subtitle_selection": "source",
                "subtitle_min_duration_ms": 250, "subtitle_max_cps": 40,
            })
            assert resolved.payload["export_contract"]["source_artifact_id"] == edited.id
            result = handlers.export(resolved.payload, lambda *_args: None, threading.Event())
            with database.session() as session:
                exported = session.get(Artifact, result["artifact_ids"][-1])
                output_path = paths.root / exported.relative_path
            # Duration alone can miss an export that truncates the original
            # footage to the speech length. Every decoded frame must match.
            assert video_hash(output_path) == expected_frames, audio_mode
            probe = json.loads(run(
                "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output_path),
            ))
            for stream_type in ("video", "audio"):
                stream = next(s for s in probe["streams"] if s["codec_type"] == stream_type)
                assert float(stream["duration"]) == pytest.approx(2, abs=0.1), audio_mode
            subtitles = parse_srt(run(
                "ffmpeg", "-v", "error", "-i", str(output_path),
                "-map", "0:s:0", "-f", "srt", "-",
            ))
            assert [(cue.start_ms, cue.end_ms, cue.text) for cue in subtitles] == [
                (200, 600, "Before cut"), (1200, 1600, "After cut"),
            ], audio_mode
    finally:
        database.dispose()
