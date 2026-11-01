"""Final voiceover containers omit inherited chapters and report measured media."""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

from pandrator.logic.dubbing import video_muxing
from pandrator.web import export_video_commands as commands
from pandrator.web import soundtrack_export
from tests import test_export_video_cleanup as cleanup

_REAL_PROBE = soundtrack_export.probe_soundtrack_media


def _values(command, flag):
    return [command[index + 1] for index, value in enumerate(command[:-1]) if value == flag]


@pytest.mark.parametrize(
    "builder,args,kwargs",
    [
        (video_muxing.build_video_transcode_command, ("in.mp4", "out.mp4"), {}),
        (video_muxing.build_web_optimized_remux_command, ("in.mp4", "out.mp4"), {}),
        (video_muxing.build_add_subtitles_command, ("in.mp4", "sub.srt", "out.mp4"), {}),
        (
            video_muxing.build_add_subtitles_command,
            ("in.mp4", "sub.srt", "out.mp4"),
            {"subtitle_mode": "burned"},
        ),
        (
            video_muxing.build_multi_soft_subtitle_command,
            ("in.mp4", [{"path": "sub.srt", "language": "en"}], "out.mp4"),
            {},
        ),
    ],
)
def test_chapter_omission_is_opt_in(builder, args, kwargs):
    legacy = builder(*args, **kwargs)
    assert legacy == builder(*args, **kwargs, drop_chapters=False)
    assert "-map_chapters" not in legacy
    command = builder(*args, **kwargs, drop_chapters=True)
    assert _values(command, "-map_chapters") == ["-1"]
    assert command[-3:] == ["-movflags", "+faststart", "out.mp4"]
    assert _values(command, "-map") == (
        ["0:v:0", "0:a?", "1:0"]
        if "sub.srt" in command and kwargs.get("subtitle_mode") != "burned"
        else ["0:v:0", "0:a?"]
    )
    if builder == video_muxing.build_add_subtitles_command:
        assert "-map" not in legacy


@pytest.mark.parametrize(
    "mode,fallback",
    [
        ("web", False),
        ("web", True),
        ("soft", False),
        ("soft", True),
        ("soft_transcode", False),
        ("transcode", False),
        ("burned", False),
    ],
)
def test_all_final_render_commands_omit_chapters(mode, fallback):
    options = commands.VideoEncodingOptions(
        "ffmpeg", "libx264", 18, "fast", "source", "copy", "192k"
    )
    effect = [subprocess.CalledProcessError(1, ["ffmpeg"]), mock.DEFAULT] if fallback else None
    with (
        mock.patch(
            "pandrator.web.export_video_commands.run_cancellable", side_effect=effect
        ) as run,
        mock.patch("pandrator.web.capabilities.ffmpeg_video_encoder_ids", return_value={"libx264"}),
        mock.patch(
            "pandrator.logic.dubbing_handler.resolve_ffmpeg_for_burned_subtitles",
            return_value="ffmpeg",
        ),
    ):
        kwargs = {"progress": mock.Mock(), "cancel_event": threading.Event()}
        source, destination = Path("in.mp4"), Path("out.mp4")
        if mode.startswith("soft"):
            commands.render_soft_subtitle_video(
                source,
                [{"path": "sub.srt", "language": "en"}],
                destination,
                options,
                transcode_video=mode == "soft_transcode",
                **kwargs,
            )
        elif mode == "burned":
            commands.render_burned_subtitle_video(
                source, Path("sub.srt"), destination, options, language="en", **kwargs
            )
        elif mode == "transcode":
            commands.render_transcoded_video(source, destination, options, **kwargs)
        else:
            commands.render_web_video(source, destination, options, tail_extension_ms=0, **kwargs)
    assert run.call_count == (2 if fallback else 1)
    for call in run.call_args_list:
        assert _values(call.args[0], "-map_chapters") == ["-1"]


@pytest.mark.parametrize(
    "stream_duration,expected",
    [(None, None), ("N/A", None), ("nan", None), ("inf", None), ("-1", None), ("1.125", 1.125)],
)
def test_probe_additive_fields_preserve_unknown_stream_duration(stream_duration, expected):
    data = {
        "streams": [
            {"codec_type": "video", "duration": "1"},
            {"codec_type": "audio", "duration": stream_duration},
            {"codec_type": "data"},
        ],
        "format": {"duration": "1.25"},
        "chapters": [{"id": 0}, {"id": 1}],
    }
    guard = mock.Mock()
    with (
        mock.patch.object(soundtrack_export, "_executable", return_value="ffprobe"),
        mock.patch.object(
            soundtrack_export.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, json.dumps(data)),
        ) as run,
    ):
        result = _REAL_PROBE(Path("synthetic.mp4"), before_io=guard)
    guard.assert_called_once()
    assert "-show_chapters" in run.call_args.args[0]
    assert result["stream_types"] == ["video", "audio", "data"]
    assert result["chapter_count"] == 2
    assert result["container_duration"] == 1.25
    assert result["video_duration"] == 1
    assert result["audio_duration"] == expected
    assert result["has_video"] and result["has_audio"]


@pytest.fixture
def export_case():
    case = cleanup.VideoExportCleanupTests()
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


def _final_probe(case, measured):
    def probe(path, **kwargs):
        if kwargs.get("before_io") is not None:
            kwargs["before_io"]()
        return measured if "-render-" in Path(path).name else case.probe(path)

    return probe


def test_final_metadata_uses_measured_durations(export_case):
    measured = {
        "has_video": True,
        "container_duration": 2.12,
        "video_duration": 2.08,
        "audio_duration": 2.015,
        "stream_types": ["video", "audio"],
        "chapter_count": 0,
    }
    with mock.patch.object(
        soundtrack_export, "probe_soundtrack_media", side_effect=_final_probe(export_case, measured)
    ):
        result = export_case.export()
    artifact, _path = export_case.artifacts.resolve(result["artifact_ids"][0])
    assert artifact.metadata_json["media_durations_ms"] == pytest.approx(
        {"container": 2120, "video": 2080, "audio": 2015}
    )
    assert artifact.metadata_json["media_stream_types"] == ["video", "audio"]
    assert artifact.metadata_json["chapter_count"] == 0
    assert artifact.metadata_json["chapter_policy"] == "omit"
    assert artifact.metadata_json["tail_extension_ms"] == 1040


@pytest.mark.parametrize(
    "measured,expected",
    [
        ({"has_video": True}, {"container": None, "video": None, "audio": None}),
        (
            {
                "has_video": True,
                "container_duration": float("nan"),
                "video_duration": -1,
                "audio_duration": float("inf"),
            },
            {"container": None, "video": None, "audio": None},
        ),
        (
            {
                "has_video": True,
                "container_duration": 0,
                "video_duration": "bad",
                "audio_duration": "0.5",
            },
            {"container": 0, "video": None, "audio": 500},
        ),
    ],
)
def test_final_metadata_does_not_fabricate_missing_evidence(export_case, measured, expected):
    with mock.patch.object(
        soundtrack_export, "probe_soundtrack_media", side_effect=_final_probe(export_case, measured)
    ):
        result = export_case.export()
    artifact, _path = export_case.artifacts.resolve(result["artifact_ids"][0])
    assert artifact.metadata_json["media_durations_ms"] == expected
    assert artifact.metadata_json["media_stream_types"] is None
    assert artifact.metadata_json["chapter_count"] is None


@pytest.mark.parametrize(
    "measured,error",
    [
        ({"has_video": False, "chapter_count": 0}, "no video"),
        ({"has_video": True, "chapter_count": 1}, "contains chapters"),
    ],
)
def test_invalid_final_container_is_rejected_before_publication(export_case, measured, error):
    with (
        mock.patch.object(
            soundtrack_export,
            "probe_soundtrack_media",
            side_effect=_final_probe(export_case, measured),
        ),
        mock.patch("pandrator.web.export_video.publish_video_export") as publish,
    ):
        with pytest.raises(ValueError, match=error):
            export_case.export()
    publish.assert_not_called()
    export_case.assert_clean()


def test_cancel_during_final_probe_prevents_publication(export_case):
    def probe(path, **kwargs):
        if "-render-" in Path(path).name:
            export_case.cancel_event.set()
        return export_case.probe(path)

    with (
        mock.patch.object(soundtrack_export, "probe_soundtrack_media", side_effect=probe),
        mock.patch("pandrator.web.export_video.publish_video_export") as publish,
    ):
        with pytest.raises(InterruptedError):
            export_case.export()
    publish.assert_not_called()
    export_case.assert_clean()


@pytest.fixture(scope="module")
def native_media(tmp_path_factory):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("Native FFmpeg/FFprobe unavailable on PATH")
    started = time.monotonic()

    def run(command):
        assert time.monotonic() - started < 60, "Native protocol exceeded 60 seconds"
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=15)
        assert time.monotonic() - started < 60, "Native protocol exceeded 60 seconds"
        return result

    encoders = run([ffmpeg, "-hide_banner", "-encoders"]).stdout
    if "libx264" not in encoders or " aac " not in encoders:
        pytest.skip("Native protocol requires libx264 and AAC encoders")
    directory = tmp_path_factory.mktemp("native-containers")
    metadata = directory / "chapters.ffmeta"
    metadata.write_text(
        ";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=Synthetic chapter\n",
        encoding="utf-8",
    )
    subtitle = directory / "literal.srt"
    subtitle.write_text(
        "1\n00:00:00,100 --> 00:00:00,800\nContainer integrity literal.\n", encoding="utf-8"
    )
    source = directory / "source.mp4"
    run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=25:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
            "-i",
            str(metadata),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map_chapters",
            "2",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-t",
            "1",
            str(source),
        ]
    )

    def probe(path):
        return json.loads(
            run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-show_chapters",
                    "-of",
                    "json",
                    str(path),
                ]
            ).stdout
        )

    source_info = probe(source)
    assert len(source_info["chapters"]) == 1
    assert "data" in [stream["codec_type"] for stream in source_info["streams"]]
    return {
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "run": run,
        "probe": probe,
        "source": source,
        "subtitle": subtitle,
        "directory": directory,
    }


def _assert_native_output(media, path, expected_types, *, copied_video):
    info = media["probe"](path)
    assert info["chapters"] == []
    assert [stream["codec_type"] for stream in info["streams"]] == expected_types
    data = path.read_bytes()
    atoms = []
    offset = 0
    while offset + 8 <= len(data):
        size, kind = struct.unpack_from(">I4s", data, offset)
        if size == 1:
            size = struct.unpack_from(">Q", data, offset + 8)[0]
        atoms.append(kind)
        if size == 0:
            break
        offset += size
    assert atoms.index(b"moov") < atoms.index(b"mdat")
    media["run"](
        [
            media["ffmpeg"],
            "-v",
            "error",
            "-xerror",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-f",
            "null",
            "-",
        ]
    )
    if copied_video:

        def packet_hashes(target):
            result = media["run"](
                [
                    media["ffprobe"],
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_packets",
                    "-show_data_hash",
                    "sha256",
                    "-show_entries",
                    "packet=data_hash",
                    "-of",
                    "json",
                    str(target),
                ]
            )
            hashes = [item["data_hash"] for item in json.loads(result.stdout)["packets"]]
            assert hashes
            return hashes

        assert packet_hashes(path) == packet_hashes(media["source"])
    return info


def test_native_final_copy_and_soft_container_metadata(native_media, export_case):
    media = native_media
    shutil.copyfile(media["source"], export_case.source_path)
    export_case.settings["audio_mode"] = "preserve"
    observed = []
    writer_depth = 0
    immediate_session = export_case.database.immediate_session

    @contextmanager
    def writer(*args, **kwargs):
        nonlocal writer_depth
        writer_depth += 1
        try:
            with immediate_session(*args, **kwargs) as session:
                yield session
        finally:
            writer_depth -= 1

    native_subprocess_run = subprocess.run

    def bounded_probe_run(*args, **kwargs):
        kwargs["timeout"] = min(kwargs.get("timeout", 15), 15)
        return native_subprocess_run(*args, **kwargs)

    def measured_probe(path, **kwargs):
        assert writer_depth == 0
        observed.append(Path(path))
        with mock.patch.object(soundtrack_export.subprocess, "run", side_effect=bounded_probe_run):
            return _REAL_PROBE(path, **kwargs)

    def native_command(command, **kwargs):
        return media["run"](command)

    with (
        mock.patch.dict(
            "os.environ",
            {"PANDRATOR_FFMPEG_EXE": media["ffmpeg"], "PANDRATOR_FFPROBE_EXE": media["ffprobe"]},
        ),
        mock.patch.object(soundtrack_export, "probe_soundtrack_media", side_effect=measured_probe),
        mock.patch(
            "pandrator.web.export_video_commands.run_cancellable", side_effect=native_command
        ),
        mock.patch.object(export_case.database, "immediate_session", side_effect=writer),
    ):
        result = export_case.export()
    artifact, final = export_case.artifacts.resolve(result["artifact_ids"][0])
    assert len(observed) == 1 and "-render-" in observed[0].name
    info = _assert_native_output(media, final, ["video", "audio"], copied_video=True)
    metadata = artifact.metadata_json
    assert metadata["chapter_count"] == 0
    assert metadata["media_stream_types"] == ["video", "audio"]
    for key, raw in [
        ("container", info["format"]),
        ("video", info["streams"][0]),
        ("audio", info["streams"][1]),
    ]:
        assert metadata["media_durations_ms"][key] == pytest.approx(float(raw["duration"]) * 1000)
    soft = media["directory"] / "soft.mp4"
    media["run"](
        video_muxing.build_multi_soft_subtitle_command(
            str(media["source"]),
            [{"path": str(media["subtitle"]), "language": "en"}],
            str(soft),
            ffmpeg_executable=media["ffmpeg"],
            drop_chapters=True,
        )
    )
    soft_info = _assert_native_output(
        media, soft, ["video", "audio", "subtitle"], copied_video=True
    )
    assert soft_info["streams"][2]["codec_name"] == "mov_text"
    extracted = media["run"](
        [media["ffmpeg"], "-v", "error", "-i", str(soft), "-map", "0:s:0", "-f", "srt", "-"]
    ).stdout
    assert "Container integrity literal." in extracted


def test_native_burned_container(native_media):
    media = native_media
    filters = media["run"]([media["ffmpeg"], "-hide_banner", "-filters"]).stdout
    if " subtitles " not in filters:
        pytest.skip("Native FFmpeg lacks subtitles/libass filter")
    burned = media["directory"] / "burned.mp4"
    command = video_muxing.build_add_subtitles_command(
        str(media["source"]),
        str(media["subtitle"]),
        str(burned),
        subtitle_mode="burned",
        ffmpeg_executable=media["ffmpeg"],
        drop_chapters=True,
    )
    media["run"](command)
    _assert_native_output(media, burned, ["video", "audio"], copied_video=False)
