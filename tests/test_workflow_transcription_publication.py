"""Disposable regression witnesses for transcription publication boundaries."""

import inspect as signature_inspect
import json
import threading
import wave
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import inspect, select

from pandrator.logic.cancellable_process import ProcessCancelled
from pandrator.logic.dubbing.transcript_normalization import normalize_transcript
from pandrator.web.artifact_selection import selected_artifacts
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    Document,
    DocumentRevision,
    Segment,
    SessionSource,
    SessionStageSelection,
    SourceAsset,
    TimedWord,
)
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.web_test_support import prepare_web_test_data_root


def _row_payload(row):
    return {
        attribute.key: deepcopy(getattr(row, attribute.key))
        for attribute in inspect(row).mapper.column_attrs
    }


def _artifact(case, name, text, role, *, parents=()):
    path = case.paths.root / name
    path.write_text(text, encoding="utf-8")
    artifact = case.artifacts.register(
        path,
        kind="video" if name.endswith(".mp4") else "srt",
        role=role,
        session_id=case.session.id,
        parent_ids=list(parents),
    )
    return artifact, path


def _attach(case, artifact, role, kind):
    with case.database.session() as session:
        asset = SourceAsset(
            artifact_id=artifact.id, display_name=artifact.relative_path, kind=kind, state="current"
        )
        session.add(asset)
        session.flush()
        session.add(
            SessionSource(
                session_id=case.session.id, source_asset_id=asset.id, role=role, is_current=True
            )
        )


def _snapshot(case):
    with case.database.session() as session:
        rows = [
            (model.__tablename__, identity, _row_payload(session.get(model, identity)))
            for model, identity in case.prior_rows
        ]
        selections = [
            _row_payload(row)
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


def _assert_previous_preserved(case):
    assert _snapshot(case) == case.before


@pytest.fixture
def case(tmp_path, request):
    mode = getattr(request, "param", "caption-asr")
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    record = SessionService(database).create(
        "Transcription publication witness",
        workflow_kind="voiceover" if mode == "plain-asr" else "media_edit",
    )
    value = SimpleNamespace(
        paths=paths,
        database=database,
        session=record,
        mode=mode,
        artifacts=ArtifactService(database, paths),
        handlers=WorkflowHandlers(database, paths),
    )
    try:
        value.source, value.source_path = _artifact(
            value, "source.mp4", "original recording bytes", "upload"
        )
        _attach(value, value.source, "primary", "video")
        value.caption, value.caption_path = _artifact(
            value,
            "captions.srt",
            "1\n00:00:01,000 --> 00:00:02,000\nAlice: Hello, world!\n",
            "captions",
        )
        _attach(value, value.caption, "transcript", "srt")
        value.previous, previous_path = _artifact(
            value,
            "previous.srt",
            "1\n00:00:00,000 --> 00:00:01,000\nPrevious\n",
            "transcription",
            parents=[value.source.id],
        )
        document_id, revision_id = value.handlers._store_srt_document(
            record.id, value.previous, "transcription", language="en"
        )
        previous_words_path = paths.root / "previous-words.json"
        previous_words_path.write_text(
            json.dumps(
                {
                    "schema": "pandrator.transcript.v1",
                    "segments": [
                        {
                            "text": "Previous",
                            "start_ms": 0,
                            "end_ms": 1000,
                            "words": [{"text": "Previous", "start_ms": 0, "end_ms": 1000}],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        value.handlers._store_timed_words(revision_id, previous_words_path)
        value.child, child_path = _artifact(
            value,
            "corrected.srt",
            "1\n00:00:00,000 --> 00:00:01,000\nReviewed previous\n",
            "correction",
            parents=[value.previous.id],
        )
        child_document_id, child_revision_id = value.handlers._store_srt_document(
            record.id, value.child, "correction", language="en", parent_artifact=value.previous
        )
        value.prior_rows = [
            (Artifact, value.previous.id),
            (Artifact, value.child.id),
            (Document, document_id),
            (DocumentRevision, revision_id),
            (Document, child_document_id),
            (DocumentRevision, child_revision_id),
            (ArtifactEdge, (value.source.id, value.previous.id)),
            (ArtifactEdge, (value.previous.id, value.child.id)),
        ]
        with database.session() as session:
            value.prior_rows.extend(
                (Segment, row.id)
                for row in session.scalars(
                    select(Segment).where(Segment.revision_id.in_([revision_id, child_revision_id]))
                )
            )
            value.prior_rows.extend(
                (TimedWord, row.id)
                for row in session.scalars(
                    select(TimedWord).where(TimedWord.revision_id == revision_id)
                )
            )
        value.protected_files = [
            previous_path,
            child_path,
            previous_words_path,
            value.source_path,
            value.caption_path,
        ]
        value.raw_srt_path = paths.root / "raw-asr.srt"
        value.raw_srt_path.write_text(
            "1\n00:00:01,100 --> 00:00:01,900\nHELLO WORLD\n", encoding="utf-8"
        )
        value.raw_words_path = paths.root / "raw-asr.json"
        value.raw_words_path.write_text(
            json.dumps(
                {
                    "schema": "pandrator.transcript.v1",
                    "segments": [
                        {
                            "id": "raw",
                            "text": "HELLO WORLD",
                            "start_ms": 1100,
                            "end_ms": 1900,
                            "words": [
                                {"text": "HELLO", "start_ms": 1100, "end_ms": 1400},
                                {"text": "WORLD", "start_ms": 1500, "end_ms": 1900},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        value.result = SimpleNamespace(
            srt_path=str(value.raw_srt_path),
            word_timestamps_path=str(value.raw_words_path),
            engine="fixture-asr",
            compute_backend="cpu",
            resolved_language="en",
            routing={},
        )
        value.before = _snapshot(value)
        assert value.before["selected"]["transcribe"] == value.previous.id
        assert value.before["selected"]["correct"] == value.child.id
        yield value
    finally:
        database.dispose()


def _run(case, *, event=None, progress=None, transcriber=None):
    event = event if event is not None else threading.Event()
    progress = progress if progress is not None else lambda *_: None
    settings = {
        "caption_alignment_method": "ctc" if case.mode == "ctc" else "asr",
        "crispasr_vad_enabled": False,
    }
    arguments = {
        "session_id": case.session.id,
        "source_artifact_id": case.source.id,
        "settings": settings,
    }
    if case.mode != "ctc":
        with patch(
            "pandrator.logic.dubbing.transcription.transcribe_source_file_with_metadata",
            side_effect=transcriber if transcriber is not None else lambda *_a, **_k: case.result,
        ):
            return case.handlers.transcribe(arguments, progress, event)

    def extract(_source, directory, _name, **_kwargs):
        path = directory / "normalized.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\0\0" * 16000 * 3)
        return str(path)

    with (
        patch("pandrator.logic.dubbing.transcription.extract_audio", side_effect=extract),
        patch(
            "pandrator.logic.dubbing.crispasr.run_ctc_alignment",
            return_value=[
                {"word": "Hello,", "start": 1.1, "end": 1.4},
                {"word": "world!", "start": 1.5, "end": 1.9},
            ],
        ),
    ):
        return case.handlers.transcribe(arguments, progress, event)


def _assert_receipt(case, result, *, speaker_count, surfaces):
    with case.database.session() as session:
        artifact = session.get(Artifact, result["artifact_id"])
        metadata = artifact.metadata_json
        assert metadata.get("revision_id") == result["revision_id"]
        revision = session.get(DocumentRevision, result["revision_id"])
        document = session.get(Document, revision.document_id)
        assert metadata["document_id"] == document.id
        assert metadata["stage"] == document.stage == "transcription"
        assert metadata["has_speaker_metadata"] is bool(speaker_count)
        assert metadata["speaker_count"] == speaker_count
        segments = list(
            session.scalars(
                select(Segment)
                .where(Segment.revision_id == result["revision_id"])
                .order_by(Segment.ordinal)
            )
        )
        words = list(
            session.scalars(
                select(TimedWord)
                .where(TimedWord.revision_id == result["revision_id"])
                .order_by(TimedWord.ordinal)
            )
        )
        assert [word.text for word in words] == surfaces
        assert [(word.start_ms, word.end_ms) for word in words] == [(1100, 1400), (1500, 1900)]
        assert len(segments) == 1
        assert all(word.segment_id == segments[0].id for word in words)
        assert [word.speaker for word in words] == (
            ["Alice", "Alice"] if speaker_count else [None, None]
        )
        assert segments[0].speaker == ("Alice" if speaker_count else None)
        assert result["word_count"] == len(words) == 2


def test_caption_asr_success_retains_native_revision_receipt(case):
    result = _run(case)
    _assert_receipt(case, result, speaker_count=1, surfaces=["Hello,", "world!"])


def test_caption_asr_facade_recaptures_ports_and_forwards_identity(case):
    from pandrator.web.workflow_caption_alignment import transcribe_media_edit_with_caption

    arguments = {
        "session_id": case.session.id,
        "source_artifact": case.source,
        "caption_artifact": case.caption,
        "transcription_result": case.result,
        "submitted_settings": {"stt_language": "en"},
        "progress": lambda *_: None,
    }
    contexts = []
    for cancel_event in (None, threading.Event()):
        token_counter = Mock(return_value=2)
        with (
            patch(
                "pandrator.web.workflow_handlers._transcribe_media_edit_with_caption_impl",
                autospec=True,
                return_value={"fixture": True},
            ) as owner,
            patch.object(case.handlers, "_operation_dir") as operation_dir,
            patch.object(case.handlers, "_store_srt_document") as store_document,
            patch.object(case.handlers, "_store_timed_words") as store_words,
            patch("pandrator.web.workflow_handlers._media_edit_token_count", token_counter),
        ):
            supplied = dict(arguments)
            if cancel_event is not None:
                supplied["cancel_event"] = cancel_event
            result = case.handlers._transcribe_media_edit_with_caption(**supplied)
            forwarded = (
                signature_inspect.signature(transcribe_media_edit_with_caption)
                .bind(*owner.call_args.args, **owner.call_args.kwargs)
                .arguments
            )
            context = forwarded.pop("context")
            contexts.append(context)
            assert context.artifacts is case.handlers.artifacts
            assert context._operation_dir is operation_dir
            assert context._store_srt_document is store_document
            assert context._store_timed_words is store_words
            assert context._media_edit_token_count is token_counter
            assert forwarded.keys() == {**arguments, "cancel_event": cancel_event}.keys()
            for name, value in {**arguments, "cancel_event": cancel_event}.items():
                assert forwarded[name] is value
            assert result is owner.return_value
    assert contexts[0] is not contexts[1]


@pytest.mark.parametrize("case", ["caption-asr", "plain-asr"], indirect=True)
def test_cancel_when_asr_returns_preserves_previous_output(case):
    event = threading.Event()

    def transcriber(*_args, cancel_event, **_kwargs):
        cancel_event.set()
        return case.result

    with pytest.raises(ProcessCancelled):
        _run(case, event=event, transcriber=transcriber)
    _assert_previous_preserved(case)


@pytest.mark.parametrize("case", ["caption-asr", "plain-asr"], indirect=True)
def test_cancel_at_publication_preparation_preserves_previous_output(case):
    event = threading.Event()
    reached = []
    threshold = 0.86 if case.mode == "caption-asr" else 0.9

    def progress(value, _detail=None):
        if value == threshold:
            reached.append(value)
            event.set()

    with pytest.raises(ProcessCancelled):
        _run(case, event=event, progress=progress)
    assert reached == [threshold]
    _assert_previous_preserved(case)


@pytest.mark.parametrize("case", ["caption-asr", "plain-asr", "ctc"], indirect=True)
def test_cancel_after_native_word_storage_preserves_previous_output(case):
    event = threading.Event()
    store = case.handlers._store_timed_words
    stored = []

    def store_then_cancel(*args, **kwargs):
        count = store(*args, **kwargs)
        stored.append((args[0], count))
        event.set()
        return count

    with patch.object(case.handlers, "_store_timed_words", side_effect=store_then_cancel):
        with pytest.raises(ProcessCancelled):
            _run(case, event=event)
    assert len(stored) == 1 and stored[0][1] == 2
    with case.database.session() as session:
        assert session.get(DocumentRevision, stored[0][0]) is not None
    _assert_previous_preserved(case)


@pytest.mark.parametrize("case", ["caption-asr", "plain-asr"], indirect=True)
def test_native_word_storage_failure_preserves_previous_output(case):
    error = RuntimeError("sentinel word storage failure")
    with patch.object(case.handlers, "_store_timed_words", side_effect=error):
        with pytest.raises(RuntimeError) as caught:
            _run(case)
    assert caught.value is error
    _assert_previous_preserved(case)


@pytest.mark.parametrize("case", ["plain-asr"], indirect=True)
def test_plain_asr_success_preserves_receipt_and_replaces_selection(case):
    result = _run(case)
    _assert_receipt(case, result, speaker_count=0, surfaces=["HELLO", "WORLD"])
    with case.database.session() as session:
        assert (
            selected_artifacts(session, case.session.id)["transcribe"].id == result["artifact_id"]
        )
        assert session.get(Artifact, case.previous.id).state == "stale"
        assert session.get(Artifact, case.child.id).state == "stale"
        assert (
            session.get(
                ArtifactEdge, (result["artifact_id"], result["word_timestamps_artifact_id"])
            )
            is not None
        )
    for path in case.protected_files:
        assert path.read_bytes() == case.before["files"][str(path)]


def test_caption_native_moss_list_boundary_preserves_raw_bytes(case):
    """Exercise an injected/native result boundary, without a live-provider claim."""
    raw = json.dumps(
        [
            {"text": "HELLO", "start_ms": 1100, "end_ms": 1400, "moss_segment_id": "raw"},
            {"text": "WORLD", "start_ms": 1500, "end_ms": 1900, "moss_segment_id": "raw"},
        ]
    ).encode("utf-8")
    case.raw_words_path.write_bytes(raw)
    result = _run(case)
    assert case.raw_words_path.read_bytes() == raw
    _assert_receipt(case, result, speaker_count=1, surfaces=["Hello,", "world!"])


def test_caption_truthy_scalar_is_controlled_failure_with_evidence(case):
    case.raw_words_path.write_text(json.dumps("not a transcript"), encoding="utf-8")
    with pytest.raises(ValueError, match="could not be parsed and aligned"):
        _run(case)
    _assert_previous_preserved(case)
    with case.database.session() as session:
        evidence = list(
            session.scalars(
                select(Artifact).where(
                    Artifact.session_id == case.session.id,
                    Artifact.role.in_(["transcription_evidence", "recognition_word_timestamps"]),
                )
            )
        )
        assert {artifact.role for artifact in evidence} == {
            "transcription_evidence",
            "recognition_word_timestamps",
        }


@pytest.mark.parametrize("invalid", ["Infinity", "-Infinity"])
def test_normalizer_skips_nonfinite_word_and_retains_valid_span(invalid):
    transcript = normalize_transcript(
        {
            "schema": "pandrator.transcript.v1",
            "segments": [
                {
                    "id": "raw",
                    "text": "HELLO WORLD",
                    "start_ms": 1100,
                    "end_ms": 1900,
                    "words": [
                        {"text": "HELLO", "start_ms": invalid, "end_ms": 1400},
                        {"text": "WORLD", "start_ms": 1500, "end_ms": 1900},
                    ],
                }
            ],
        }
    )
    assert [(word.text, word.start_ms, word.end_ms) for word in transcript.words] == [
        ("WORLD", 1500, 1900),
    ]
    assert [(segment.start_ms, segment.end_ms) for segment in transcript.segments] == [(1100, 1900)]
