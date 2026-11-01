"""Retained input cleanup respects durable publication after a late commit error."""

from contextlib import contextmanager

import pytest
from sqlalchemy import select

from pandrator.web import models as m
from pandrator.web import speech_inputs
from pandrator.web.speech_input_contracts import SpeechInputOwner
from tests.test_evidence_access_routes import body
from tests.test_retained_preview_workspace import case as case  # noqa: F401
from tests.test_retained_preview_workspace import design as design  # noqa: F401
from tests.test_retained_preview_workspace import no_network as no_network  # noqa: F401
from tests.test_retained_preview_workspace import retained as retained  # noqa: F401
from tests.test_retained_preview_workspace import submit
from tests.test_retained_speech_inputs import harness as harness  # noqa: F401


def test_committed_reference_survives_a_late_writer_error_and_exact_retry(retained, monkeypatch):
    r = retained
    database = r.f.case.database
    original_writer = database.immediate_session
    original_publish = speech_inputs.publish_speech_input
    published = False
    failed = False

    def publish(*args, **kwargs):
        nonlocal published
        pin = original_publish(*args, **kwargs)
        published = True
        return pin

    @contextmanager
    def commit_then_raise(**kwargs):
        nonlocal failed
        with original_writer(**kwargs) as session:
            yield session
        if published and not failed:
            failed = True
            raise RuntimeError("synthetic failure after retained-input commit")

    monkeypatch.setattr(speech_inputs, "publish_speech_input", publish)
    monkeypatch.setattr(database, "immediate_session", commit_then_raise)
    with pytest.raises(RuntimeError, match="after retained-input commit"):
        submit(r, key="uncertain-retained-input-commit")

    with database.snapshot_session() as session:
        job = session.scalar(select(m.Job).where(m.Job.kind == "tts.preview"))
        assert job is not None
        pin = dict(job.payload_json["speech_input"])
        settings = dict(job.payload_json["settings"])
        retained_files = session.scalars(
            select(m.Artifact.relative_path).where(
                m.Artifact.role.in_(["speech_input", "speech_reference_audio"])
            )
        ).all()
        job_id = job.id
    assert len(retained_files) == 2
    assert all(r.f.services.paths.managed_path(path).is_file() for path in retained_files)
    loaded = speech_inputs.load_speech_input(
        database,
        r.f.services.paths,
        pin,
        settings,
        owner=SpeechInputOwner(r.f.users.workspace_id),
        before_io=lambda: None,
    )
    assert loaded["audio_cpp_reference_text"] == "The selected original reference."
    monkeypatch.setattr(
        speech_inputs,
        "capture_speech_input",
        lambda *_a, **_k: pytest.fail("Exact retry reread the mutable voice library"),
    )
    replay = submit(r, key="uncertain-retained-input-commit")
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert body(replay, 202)["id"] == job_id


def test_rolled_back_reference_removes_only_its_unpublished_files(retained, monkeypatch):
    r = retained
    original_prepare = speech_inputs.prepare_speech_input
    created = []

    def prepare(*args, **kwargs):
        prepared = original_prepare(*args, **kwargs)
        created.extend([prepared.audio.relative_path, prepared.snapshot.relative_path])
        return prepared

    def fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("synthetic failure before retained-input commit")

    monkeypatch.setattr(speech_inputs, "prepare_speech_input", prepare)
    monkeypatch.setattr(r.f.services.jobs, "enqueue_in_session", fail_enqueue)
    with pytest.raises(RuntimeError, match="before retained-input commit"):
        submit(r, key="rolled-back-retained-input")
    with r.f.case.database.snapshot_session() as session:
        assert session.scalar(select(m.Artifact.id).where(m.Artifact.role == "speech_input")) is None
        assert session.scalar(select(m.Job.id).where(m.Job.kind == "tts.preview")) is None
    assert created and all(not r.f.services.paths.managed_path(path).exists() for path in created)
    assert r.path.is_file(), "Rollback removed the original reusable library sample"


def test_cleanup_preserves_staged_bytes_when_commit_status_cannot_be_read(harness, monkeypatch):
    prepared = harness.prepare()

    def unavailable():
        raise RuntimeError("synthetic unavailable publication state")

    monkeypatch.setattr(harness.database, "snapshot_session", unavailable)
    speech_inputs.cleanup_unpublished_speech_input(harness.database, prepared)
    assert harness.paths.managed_path(prepared.audio.relative_path).is_file()
    assert harness.paths.managed_path(prepared.snapshot.relative_path).is_file()


def test_cleanup_of_borrowed_files_never_queries_or_removes_them(harness, monkeypatch):
    capture = harness.capture()
    first = harness.prepare(capture)
    harness.publish(capture, first)
    borrowed = harness.prepare(capture)
    assert not borrowed.audio.owned and not borrowed.snapshot.owned

    def unexpected():
        pytest.fail("Borrowed reference cleanup queried the database")

    monkeypatch.setattr(harness.database, "snapshot_session", unexpected)
    speech_inputs.cleanup_unpublished_speech_input(harness.database, borrowed)
    assert harness.paths.managed_path(borrowed.audio.relative_path).is_file()
    assert harness.paths.managed_path(borrowed.snapshot.relative_path).is_file()
