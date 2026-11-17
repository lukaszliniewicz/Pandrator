"""Speech-plan source signatures and the compatibility they decide."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pandrator.web import models as m
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.speech_plan_source import (
    plan_source_compatibility,
    source_plan_settings,
    source_signatures,
)

SRT = (
    "1\n00:00:01,000 --> 00:00:03,000\nHello, world.\n\n"
    "2\n00:00:04,000 --> 00:00:06,000\nA second complete thought.\n"
)


@pytest.fixture
def app_case(tmp_path):
    bootstrap = BootstrapTokenStore()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=bootstrap)
    services = app.extensions["pandrator"]["services"]
    record = services.sessions.create(
        "Source signatures", workflow_kind="subtitles", source_language="nl"
    )
    yield services, record.id
    services.database.dispose()


def _timed_artifact(services, session_id, name, *, cues, stage="translation"):
    """Register a subtitle artifact whose document holds exactly ``cues``."""
    with services.database.session() as db:
        record = db.get(m.SessionRecord, session_id)
        directory = services.paths.sessions / record.storage_key
    directory.mkdir(parents=True, exist_ok=True)
    path = services.artifacts.next_available_path(directory / name)
    path.write_text(SRT, encoding="utf-8")
    artifact = services.artifacts.register(
        path, kind="srt", role=stage, session_id=session_id, metadata={"language": "nl"}
    )
    with services.database.immediate_session() as db:
        document = m.Document(session_id=session_id, stage=stage, language="nl")
        db.add(document)
        db.flush()
        revision = m.DocumentRevision(
            document_id=document.id, revision_number=1, content_hash=artifact.content_hash
        )
        db.add(revision)
        db.flush()
        document.active_revision_id = revision.id
        for ordinal, (start, end, text) in enumerate(cues):
            db.add(
                m.Segment(
                    revision_id=revision.id,
                    ordinal=ordinal,
                    node_kind="subtitle_cue",
                    start_ms=start,
                    end_ms=end,
                    text=text,
                )
            )
        db.get(m.Artifact, artifact.id).metadata_json = {
            "language": "nl",
            "revision_id": revision.id,
        }
    return artifact


def _signatures(services, artifact):
    with services.database.session() as db:
        return source_signatures(db, artifact)


def test_signatures_separate_text_from_timing(app_case):
    services, session_id = app_case
    original = _timed_artifact(
        services,
        session_id,
        "translation.srt",
        cues=[(1000, 3000, "Hello, world."), (4000, 6000, "Second thought.")],
    )
    retimed = _timed_artifact(
        services,
        session_id,
        "retimed.srt",
        cues=[(1000, 2920, "Hello, world."), (4000, 5920, "Second thought.")],
    )
    first = _signatures(services, original)
    shifted = _signatures(services, retimed)
    assert first["version"] == 1
    assert shifted["text"] == first["text"]
    assert shifted["timing"] != first["timing"]


def test_one_word_edit_changes_only_the_text_signature(app_case):
    services, session_id = app_case
    original = _timed_artifact(
        services,
        session_id,
        "translation.srt",
        cues=[(1000, 3000, "Hello, world."), (4000, 6000, "Second thought.")],
    )
    edited_artifact = _timed_artifact(
        services,
        session_id,
        "edited.srt",
        cues=[(1000, 3000, "Hello, world."), (4000, 6000, "Third thought.")],
    )
    first = _signatures(services, original)
    edited = _signatures(services, edited_artifact)
    assert edited["text"] != first["text"]
    assert edited["timing"] == first["timing"]


def test_whitespace_only_differences_keep_the_text_signature(app_case):
    services, session_id = app_case
    original = _timed_artifact(
        services,
        session_id,
        "translation.srt",
        cues=[(1000, 3000, "Hello, world."), (4000, 6000, "Second thought.")],
    )
    spaced_artifact = _timed_artifact(
        services,
        session_id,
        "spaced.srt",
        cues=[(1000, 3000, "  Hello,   world. "), (4000, 6000, "Second\tthought.")],
    )
    first = _signatures(services, original)
    spaced = _signatures(services, spaced_artifact)
    assert spaced == first


def test_narration_source_has_no_signatures_and_no_plan_fields(app_case):
    services, session_id = app_case
    with services.database.session() as db:
        record = db.get(m.SessionRecord, session_id)
        directory = services.paths.sessions / record.storage_key
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "narration.json"
    path.write_text('[{"text": "A spoken paragraph."}]\n', encoding="utf-8")
    narration = services.artifacts.register(
        path, kind="json", role="prepared_text", session_id=session_id
    )
    with services.database.session() as db:
        assert source_signatures(db, narration) is None
        assert source_plan_settings(db, narration) == {}


def test_speech_optimized_source_matches_by_identity_only(app_case):
    """Two optimizations of the same subtitles must not share a text signature."""
    services, session_id = app_case
    display = _timed_artifact(
        services, session_id, "translation.srt", cues=[(1000, 3000, "Hello, world.")]
    )
    with services.database.session() as db:
        record = db.get(m.SessionRecord, session_id)
        directory = services.paths.sessions / record.storage_key
    optimized = []
    for name, text in (("first.srt", "Hello world."), ("second.srt", "Hi, world.")):
        path = directory / name
        path.write_text(f"1\n00:00:01,000 --> 00:00:03,000\n{text}\n", encoding="utf-8")
        optimized.append(
            services.artifacts.register(
                path,
                kind="srt",
                role="tts_optimized",
                session_id=session_id,
                metadata={"language": "nl", "source_artifact_id": display.id},
            )
        )
    first, second = optimized
    with services.database.session() as db:
        assert source_signatures(db, first) is None
        stored = source_plan_settings(db, first)
        assert stored == {"_source_content_hash": first.content_hash}
        revision = {"_source_artifact_id": first.id, **stored}
        assert (
            plan_source_compatibility(revision, second, source_signatures(db, second))
            == "incompatible"
        )


def test_plan_source_settings_record_hash_and_signatures(app_case):
    services, session_id = app_case
    artifact = _timed_artifact(
        services, session_id, "translation.srt", cues=[(1000, 3000, "Hello, world.")]
    )
    signatures = _signatures(services, artifact)
    with services.database.session() as db:
        stored = source_plan_settings(db, artifact)
    assert stored == {
        "_source_content_hash": artifact.content_hash,
        "_source_text_signature": signatures["text"],
        "_source_timing_signature": signatures["timing"],
    }


def test_shared_store_records_signatures_for_subtitle_sources(app_case):
    services, session_id = app_case
    artifact = _timed_artifact(
        services,
        session_id,
        "translation.srt",
        cues=[(1000, 3000, "Hello, world."), (4000, 6000, "Second thought.")],
    )
    signatures = _signatures(services, artifact)
    revision_id, _segment_ids = services.workflow_handlers._store_generation_plan(
        session_id,
        [{"text": "Hello, world.", "language": "nl", "node_kind": "subtitle_cue"}],
        settings={"speech_block_max_chars": 350},
        source_artifact_id=artifact.id,
    )
    with services.database.session() as db:
        settings = db.get(m.GenerationPlanRevision, revision_id).settings_json
    assert settings["_source_artifact_id"] == artifact.id
    assert settings["_source_content_hash"] == artifact.content_hash
    assert settings["_source_text_signature"] == signatures["text"]
    assert settings["_source_timing_signature"] == signatures["timing"]


def test_shared_store_leaves_narration_sources_without_signatures(app_case):
    services, session_id = app_case
    with services.database.session() as db:
        record = db.get(m.SessionRecord, session_id)
        directory = services.paths.sessions / record.storage_key
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "narration.json"
    path.write_text('[{"text": "A spoken paragraph."}]\n', encoding="utf-8")
    narration = services.artifacts.register(
        path, kind="json", role="prepared_text", session_id=session_id
    )
    revision_id, _segment_ids = services.workflow_handlers._store_generation_plan(
        session_id,
        [{"text": "A spoken paragraph.", "language": "nl"}],
        settings={},
        source_artifact_id=narration.id,
    )
    with services.database.session() as db:
        settings = db.get(m.GenerationPlanRevision, revision_id).settings_json
    assert settings["_source_artifact_id"] == narration.id
    assert "_source_text_signature" not in settings
    assert "_source_timing_signature" not in settings


def _current(artifact_id="artifact-2", content_hash="hash-2"):
    return SimpleNamespace(id=artifact_id, content_hash=content_hash)


def _settings(**values):
    return {
        "_source_artifact_id": "artifact-1",
        "_source_content_hash": "hash-1",
        "_source_text_signature": "text",
        "_source_timing_signature": "timing",
        **values,
    }


def test_same_artifact_and_hash_is_exact():
    assert (
        plan_source_compatibility(
            _settings(),
            _current("artifact-1", "hash-1"),
            {"text": "text", "timing": "timing"},
        )
        == "exact"
    )


def test_same_text_and_timing_in_another_artifact_is_exact():
    assert (
        plan_source_compatibility(
            _settings(), _current(), {"text": "text", "timing": "timing"}
        )
        == "exact"
    )


def test_same_text_with_different_timing_is_timing_only():
    assert (
        plan_source_compatibility(
            _settings(), _current(), {"text": "text", "timing": "retimed"}
        )
        == "timing_only"
    )


def test_old_revision_without_signatures_falls_back_to_identity():
    without_signatures = {
        "_source_artifact_id": "artifact-1",
        "_source_content_hash": "hash-1",
    }
    assert (
        plan_source_compatibility(
            without_signatures, _current("artifact-1", "hash-1"), None
        )
        == "exact"
    )
    assert (
        plan_source_compatibility(
            without_signatures, _current(), {"text": "text", "timing": "timing"}
        )
        == "incompatible"
    )
    assert (
        plan_source_compatibility(
            without_signatures, _current("artifact-1", "changed"), None
        )
        == "incompatible"
    )


def test_revision_without_a_source_artifact_stays_compatible():
    assert plan_source_compatibility({}, _current(), None) == "exact"
    assert (
        plan_source_compatibility(
            {"_source_content_hash": "hash-1"}, _current(), None
        )
        == "exact"
    )
