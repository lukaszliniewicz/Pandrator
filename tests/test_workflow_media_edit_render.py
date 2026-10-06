"""Disposable witnesses for media-edit publication and probe cancellation."""

import json
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import inspect, select

from pandrator.logic.dubbing.audio_sync import media_has_audio_stream
from pandrator.web.artifact_selection import selected_artifacts
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.media_process import MediaProcessTimeout, run_media_process
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    Document,
    DocumentRevision,
    Segment,
    SessionStageSelection,
    TimedWord,
)
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.web_test_support import prepare_web_test_data_root


def _row(row):
    return {
        column.key: deepcopy(getattr(row, column.key))
        for column in inspect(row).mapper.column_attrs
    }


def _snapshot(case):
    with case.database.session() as session:
        rows = [
            (model.__tablename__, identity, _row(session.get(model, identity)))
            for model, identity in case.protected_rows
        ]
        selections = [
            _row(row)
            for row in session.scalars(
                select(SessionStageSelection)
                .where(SessionStageSelection.session_id == case.session.id)
                .order_by(SessionStageSelection.stage_key)
            )
        ]
        selected = {
            key: artifact.id
            for key, artifact in selected_artifacts(session, case.session.id).items()
        }
    return {
        "rows": rows,
        "selections": selections,
        "selected": selected,
        "files": {str(path): path.read_bytes() for path in case.protected_files},
    }


def _artifact(case, name, text, role, kind, parents=()):
    path = case.paths.root / name
    path.write_text(text, encoding="utf-8")
    return case.artifacts.register(
        path, kind=kind, role=role, session_id=case.session.id, parent_ids=list(parents)
    ), path


@pytest.fixture
def case(tmp_path):
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    value = SimpleNamespace(
        paths=paths,
        database=database,
        session=SessionService(database).create(
            "Render publication witness", workflow_kind="media_edit"
        ),
        artifacts=ArtifactService(database, paths),
        handlers=WorkflowHandlers(database, paths),
    )
    try:
        value.source, value.source_path = _artifact(
            value, "source.mp4", "Original source bytes", "upload", "video"
        )
        value.previous, previous_path = _artifact(
            value,
            "previous.srt",
            "1\n00:00:00,000 --> 00:00:01,000\nPrevious\n",
            "media_edit_subtitles",
            "srt",
            [value.source.id],
        )
        _document, previous_revision = value.handlers._store_srt_document(
            value.session.id, value.previous, "media_edit_subtitles", speaker_overrides={1: "Alice"}
        )
        words = {
            "schema": "pandrator.transcript.v1",
            "segments": [
                {
                    "text": "Previous",
                    "start_ms": 0,
                    "end_ms": 1000,
                    "speaker": "Alice",
                    "words": [{"text": "Previous", "start_ms": 0, "end_ms": 1000}],
                }
            ],
        }
        value.previous_words, words_path = _artifact(
            value,
            "previous-words.json",
            json.dumps(words),
            "media_edit_word_timestamps",
            "json",
            [value.previous.id],
        )
        value.handlers._store_timed_words(previous_revision, words_path)
        value.child, child_path = _artifact(
            value,
            "corrected.srt",
            "1\n00:00:00,000 --> 00:00:01,000\nReviewed previous\n",
            "correction",
            "srt",
            [value.previous.id],
        )
        value.handlers._store_srt_document(
            value.session.id, value.child, "correction", parent_artifact=value.previous
        )
        value.previous_media, media_path = _artifact(
            value,
            "previous.mp4",
            "Previous encoded media",
            "media_edit_media",
            "video",
            [value.source.id],
        )
        value.protected_rows = []
        with database.session() as session:
            for model in (Artifact, Document, DocumentRevision, Segment, TimedWord, ArtifactEdge):
                value.protected_rows.extend(
                    (model, inspect(row).identity) for row in session.scalars(select(model))
                )
        value.protected_files = [
            value.source_path,
            previous_path,
            words_path,
            child_path,
            media_path,
        ]
        value.revision = {
            "reviewed": True,
            "plan_id": "fixture-plan",
            "revision_id": "fixture-revision",
            "source_media_artifact": {"id": value.source.id},
            "keep_ranges": [{"id": "keep", "start_ms": 500, "end_ms": 2500}],
            "cues": [
                {
                    "id": "cue",
                    "start_ms": 1100,
                    "end_ms": 1900,
                    "text": "Hello world",
                    "speaker": "Alice",
                    "words": [
                        {"text": "Hello", "start_ms": 1100, "end_ms": 1400},
                        {"text": "world", "start_ms": 1500, "end_ms": 1900},
                    ],
                }
            ],
        }
        value.before = _snapshot(value)
        assert value.before["selected"]["edit_media"] == value.previous.id
        assert value.before["selected"]["correct"] == value.child.id
        yield value
    finally:
        database.dispose()


def _assert_pair(case, result=None):
    with case.database.session() as session:
        subtitle = session.scalar(
            select(Artifact).where(
                Artifact.session_id == case.session.id,
                Artifact.role == "media_edit_subtitles",
                Artifact.state == "current",
            )
        )
        words_artifact = session.scalar(
            select(Artifact).where(
                Artifact.session_id == case.session.id,
                Artifact.role == "media_edit_word_timestamps",
                Artifact.state == "current",
            )
        )
        assert subtitle is not None and subtitle.id != case.previous.id
        assert words_artifact is not None and words_artifact.id != case.previous_words.id
        metadata = subtitle.metadata_json
        assert metadata["stage"] == "media_edit_subtitles"
        assert metadata["has_speaker_metadata"] is True and metadata["speaker_count"] == 1
        revision = session.get(DocumentRevision, metadata["revision_id"])
        document = session.get(Document, metadata["document_id"])
        assert revision.document_id == document.id and document.active_revision_id == revision.id
        segments = list(
            session.scalars(
                select(Segment).where(Segment.revision_id == revision.id).order_by(Segment.ordinal)
            )
        )
        words = list(
            session.scalars(
                select(TimedWord)
                .where(TimedWord.revision_id == revision.id)
                .order_by(TimedWord.ordinal)
            )
        )
        assert [(word.text, word.start_ms, word.end_ms, word.speaker) for word in words] == [
            ("Hello", 600, 900, "Alice"),
            ("world", 1000, 1400, "Alice"),
        ]
        assert len(segments) == 1 and segments[0].speaker == "Alice"
        assert [word.segment_id for word in words] == [segments[0].id, segments[0].id]
        assert session.get(ArtifactEdge, (subtitle.id, words_artifact.id)) is not None
        assert selected_artifacts(session, case.session.id)["edit_media"].id == subtitle.id
        for prior in (case.previous, case.previous_words, case.child):
            assert session.get(Artifact, prior.id).state == "stale"
        if result is not None:
            assert result["subtitle_artifact_id"] == subtitle.id
            assert result["document_id"] == document.id
            assert result["document_revision_id"] == revision.id
            assert result["word_timestamps_artifact_id"] == words_artifact.id
            assert result["word_count"] == 2 and result["duration_ms"] == 2000
    assert (case.paths.root / subtitle.relative_path).is_file()
    assert (case.paths.root / words_artifact.relative_path).is_file()


def _run(
    case,
    *,
    subtitles_only=False,
    event=None,
    progress=None,
    predicate=None,
    encode=None,
    probe_process=None,
):
    event = event if event is not None else threading.Event()

    def build(source, output, ranges, **kwargs):
        assert Path(source) == case.source_path and ranges == ((500, 2500),)
        assert kwargs["include_progress"] is True
        return ["fixture-ffmpeg", "-progress", "pipe:1", output]

    def default_encode(command, **kwargs):
        _assert_pair(case)
        with case.database.session() as session:
            assert session.get(Artifact, case.previous_media.id).state == "current"
        kwargs["progress_callback"]({"out_time_us": "1000000", "progress": "continue"})
        Path(command[-1]).write_bytes(b"Encoded fixture video")

    case.encode = Mock(side_effect=encode or default_encode)

    def process(command, **kwargs):
        if command[0] == "fixture-ffmpeg":
            return case.encode(command, **kwargs)
        assert probe_process is not None, "Unexpected process outside the fake encode"
        return probe_process(command, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(case.handlers.media_edit, "revision", return_value=case.revision)
        )
        stack.enter_context(
            patch("pandrator.web.capabilities.ffmpeg_video_encoder_ids", return_value={"libx264"})
        )
        stack.enter_context(
            patch(
                "pandrator.web.media_process.resolve_ffmpeg_executable",
                return_value="fixture-ffmpeg",
            )
        )
        stack.enter_context(
            patch(
                "pandrator.web.media_process.resolve_ffprobe_executable",
                return_value="fixture-ffprobe",
            )
        )
        stack.enter_context(
            patch(
                "pandrator.logic.dubbing.audio_sync.media_has_audio_stream",
                side_effect=predicate or (lambda *a, **k: True),
            )
        )
        stack.enter_context(
            patch(
                "pandrator.logic.dubbing.video_muxing.build_removal_only_video_command",
                side_effect=build,
            )
        )
        stack.enter_context(
            patch("pandrator.web.media_process.run_media_process", side_effect=process)
        )
        return case.handlers.media_edit_render(
            {
                "session_id": case.session.id,
                "revision": 1,
                "subtitles_only": subtitles_only,
                "settings": {"burn_video_encoder": "libx264"},
            },
            progress or (lambda *_: None),
            event,
        )


@pytest.mark.parametrize("subtitles_only", [False, True])
def test_pre_cancelled_render_preserves_previous_pair(case, subtitles_only):
    event = threading.Event()
    event.set()
    assert _run(case, subtitles_only=subtitles_only, event=event) == {}
    case.encode.assert_not_called()
    assert _snapshot(case) == case.before


@pytest.mark.parametrize("subtitles_only", [False, True])
def test_cancel_at_publication_preparation_preserves_previous_pair(case, subtitles_only):
    event = threading.Event()
    updates = []

    def progress(value, *_):
        updates.append(value)
        if value == 0.18:
            event.set()

    assert _run(case, subtitles_only=subtitles_only, event=event, progress=progress) == {}
    assert 0.18 in updates
    case.encode.assert_not_called()
    assert _snapshot(case) == case.before


@pytest.mark.parametrize("port", ["_store_srt_document", "_store_timed_words"])
def test_native_storage_failure_preserves_previous_pair(case, port):
    failure = RuntimeError("native storage failed")
    with (
        patch.object(case.handlers, port, side_effect=failure),
        pytest.raises(RuntimeError) as caught,
    ):
        _run(case, subtitles_only=True)
    assert caught.value is failure
    case.encode.assert_not_called()
    assert _snapshot(case) == case.before


@pytest.mark.parametrize("subtitles_only", [False, True])
def test_cancel_after_native_words_preserves_previous_pair(case, subtitles_only):
    event = threading.Event()
    native = case.handlers._store_timed_words

    def store(*args, **kwargs):
        result = native(*args, **kwargs)
        event.set()
        return result

    with patch.object(case.handlers, "_store_timed_words", side_effect=store):
        assert _run(case, subtitles_only=subtitles_only, event=event) == {}
    case.encode.assert_not_called()
    assert _snapshot(case) == case.before


def test_pair_registration_failure_rolls_back_previous_pair(case):
    native = case.handlers.artifacts.register_in_session
    failure = RuntimeError("pair registration failed after write")

    def register(*args, **kwargs):
        artifact = native(*args, **kwargs)
        if kwargs.get("role") == "media_edit_word_timestamps":
            raise failure
        return artifact

    with (
        patch.object(case.handlers.artifacts, "register_in_session", side_effect=register),
        pytest.raises(RuntimeError) as caught,
    ):
        _run(case, subtitles_only=True)
    assert caught.value is failure
    assert _snapshot(case) == case.before


def test_cancel_during_pair_promotion_rolls_back_previous_pair(case):
    event = threading.Event()
    native = case.handlers.artifacts.register_in_session

    def register(*args, **kwargs):
        artifact = native(*args, **kwargs)
        if kwargs.get("role") == "media_edit_subtitles":
            event.set()
        return artifact

    with patch.object(case.handlers.artifacts, "register_in_session", side_effect=register):
        assert _run(case, subtitles_only=True, event=event) == {}
    case.encode.assert_not_called()
    assert _snapshot(case) == case.before


@pytest.mark.parametrize("subtitles_only", [False, True])
def test_success_publishes_complete_native_pair_before_encoding(case, subtitles_only):
    result = _run(case, subtitles_only=subtitles_only)
    _assert_pair(case, result)
    with case.database.session() as session:
        prior = session.get(Artifact, case.previous_media.id)
        if subtitles_only:
            case.encode.assert_not_called()
            assert result["subtitles_only"] is True and "media_artifact_id" not in result
            assert prior.state == "current"
        else:
            case.encode.assert_called_once()
            assert prior.state == "stale"
            assert session.get(Artifact, result["media_artifact_id"]).state == "current"
    assert {str(path): path.read_bytes() for path in case.protected_files} == case.before["files"]


def test_audio_probe_cancellation_reaps_native_child_and_retains_published_pair(case):
    event = threading.Event()
    started = case.paths.root / "probe-started"
    finished = case.paths.root / "probe-finished"
    code = "import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));print('0',flush=True);time.sleep(4);pathlib.Path(sys.argv[2]).write_text('finished')"
    command = [sys.executable, "-c", code, str(started), str(finished)]
    native_popen = subprocess.Popen
    children, watchers, watcher_errors = [], [], []

    def launch(*args, **kwargs):
        child = native_popen(*args, **kwargs)
        children.append(child)
        return child

    def watch():
        deadline = time.monotonic() + 2
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not started.exists():
            watcher_errors.append("Disposable probe child did not start within two seconds")
            return
        event.set()

    def predicate(path, *, ffprobe_executable, run_func=None):
        watcher = threading.Thread(target=watch, name="fixture-probe-cancellation")
        watchers.append(watcher)
        watcher.start()

        def redirected(_command, **kwargs):
            if run_func is not None:
                return run_func(command, **kwargs)
            return subprocess.run(command, **kwargs)

        return media_has_audio_stream(
            path, ffprobe_executable=ffprobe_executable, run_func=redirected
        )

    before = time.monotonic()
    try:
        with patch("pandrator.web.media_process.subprocess.Popen", side_effect=launch):
            result = _run(case, event=event, predicate=predicate, probe_process=run_media_process)
        elapsed = time.monotonic() - before
    finally:
        for watcher in watchers:
            watcher.join(timeout=3)
        for child in children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=2)
    assert not watcher_errors, watcher_errors
    assert len(children) == 1 and all(not watcher.is_alive() for watcher in watchers)
    assert int(started.read_text()) == children[0].pid
    assert result == {}
    assert elapsed < 3
    assert not finished.exists() and children[0].poll() is not None
    case.encode.assert_not_called()
    _assert_pair(case)
    with case.database.session() as session:
        assert session.get(Artifact, case.previous_media.id).state == "current"


def test_audio_probe_adapter_uses_cancellation_capture_and_watchdog(case):
    event = threading.Event()
    failure = MediaProcessTimeout("fixture probe timed out")
    calls = []

    def boundary(command, **kwargs):
        calls.append((command, kwargs))
        raise failure

    def predicate(path, *, ffprobe_executable, run_func=None):
        assert run_func is not None, "Render must supply a cancellable audio-probe run_func"
        return media_has_audio_stream(
            path, ffprobe_executable=ffprobe_executable, run_func=run_func
        )

    with pytest.raises(ValueError, match="could not be inspected for audio") as caught:
        _run(case, event=event, predicate=predicate, probe_process=boundary)
    assert caught.value.__cause__ is failure
    assert len(calls) == 1
    assert calls[0][1]["capture_stdout"] is True
    assert calls[0][1]["timeout_seconds"] == 30.0
    assert calls[0][1]["cancel_event"] is event
    case.encode.assert_not_called()
    _assert_pair(case)
    with case.database.session() as session:
        assert session.get(Artifact, case.previous_media.id).state == "current"


def test_changed_source_hash_preserves_previous_pair(case):
    case.source_path.write_bytes(b"Changed source bytes")
    before = _snapshot(case)
    with pytest.raises(ValueError, match="changed after the revision was created"):
        _run(case)
    case.encode.assert_not_called()
    assert _snapshot(case) == before


def test_render_facade_recaptures_ports_and_preserves_result_and_error_identity():
    from pandrator.web import workflow_handlers as root
    from pandrator.web.workflow_media_edit_render import media_edit_render

    assert root._media_edit_render_impl is media_edit_render
    handlers = WorkflowHandlers.__new__(WorkflowHandlers)
    payload, progress, event = {}, Mock(), threading.Event()
    result, failure, contexts = object(), RuntimeError("owner sentinel"), []
    instance_ports = (
        "database",
        "artifacts",
        "media_edit",
        "_resolve_input",
        "_media_edit_cues",
        "_operation_dir",
        "_store_srt_document",
        "_store_timed_words",
    )

    def delegate(context, passed_payload, passed_progress, passed_event):
        assert passed_payload is payload
        assert passed_progress is progress
        assert passed_event is event
        contexts.append(context)
        if len(contexts) == 3:
            raise failure
        return result

    with patch.object(root, "_media_edit_render_impl", side_effect=delegate):
        for _ in range(2):
            ports = {name: Mock() for name in instance_ports}
            for name, value in ports.items():
                setattr(handlers, name, value)
            hash_port, endpoint_port, clock_port = Mock(), Mock(), Mock()
            with (
                patch.object(root, "sha256_file", hash_port),
                patch.object(root, "_required_media_edit_time", endpoint_port),
                patch.object(root.time, "monotonic", clock_port),
            ):
                assert handlers.media_edit_render(payload, progress, event) is result
            captured = contexts[-1]
            assert all(getattr(captured, name) is value for name, value in ports.items())
            assert captured._sha256_file is hash_port
            assert captured._required_media_edit_time is endpoint_port
            assert captured._monotonic is clock_port
        with pytest.raises(RuntimeError) as caught:
            handlers.media_edit_render(payload, progress, event)
    assert caught.value is failure
    assert len({id(context) for context in contexts}) == 3
