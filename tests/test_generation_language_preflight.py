"""Frozen language/model bindings match the speech synthesis worker inputs."""

from types import SimpleNamespace

import pytest

from pandrator.web import models as m
from pandrator.web.speech_plan_workspace import freeze_speech_snapshot


class FrozenSession:
    def __init__(self, revision, segments):
        self.revision = revision
        self.segments = segments

    def get(self, model, _key):
        if model is m.GenerationPlanRevision:
            return self.revision
        if model is m.GenerationPlan:
            return SimpleNamespace(session_id="session")
        if model is m.SessionRecord:
            return SimpleNamespace(id="session", workflow_kind="other")
        return None

    def scalar(self, _statement):
        return None

    def scalars(self, _statement):
        selected_ids = _statement.compile().params.get("id_1")
        if selected_ids is None:
            return self.segments
        return [segment for segment in self.segments if segment.id in selected_ids]


def make_revision():
    return SimpleNamespace(
        id="plan-revision",
        plan_id="plan",
        source_revision_id="source-revision",
        operation_json={},
        settings_json={},
    )


def make_segment(segment_id, language="", ordinal=0):
    return SimpleNamespace(
        id=segment_id,
        language=language,
        ordinal=ordinal,
        removed=False,
        text=f"Segment {segment_id}",
        optimized_text=None,
    )


def test_selected_alternate_model_and_language_are_validated_before_freeze():
    revision = make_revision()
    session = FrozenSession(revision, [make_segment("segment-1", "fr")])
    snapshot = {
        "tts": {
            "service": "Silero",
            "silero_model": "v5_cis_base",
            "language": "az",
            "performance_enabled": False,
            "casting_enabled": False,
        },
        "selected_segment_override": {
            "tts": {"silero_model": "v3_en", "language": "fr"}
        },
    }

    with pytest.raises(ValueError, match="v3_en does not support 'fr'"):
        freeze_speech_snapshot(session, revision.id, snapshot)

    assert revision.source_revision_id == "source-revision"
    assert "tts_language_snapshot" not in snapshot


def test_segment_language_overrides_base_and_language_records_deduplicate():
    revision = make_revision()
    session = FrozenSession(
        revision,
        [
            make_segment("one", "az", 0),
            make_segment("two", "myv", 1),
            make_segment("three", "az", 2),
        ],
    )
    snapshot = {
        "tts": {
            "service": "Silero",
            "silero_model": "v5_cis_base",
            "language": "az",
            "performance_enabled": False,
            "casting_enabled": False,
        }
    }

    freeze_speech_snapshot(session, revision.id, snapshot)
    frozen = snapshot["tts_language_snapshot"]

    assert len(frozen["records"]) == 2
    assert frozen["bindings"]["one"]["record_key"] == frozen["bindings"]["three"]["record_key"]
    assert frozen["bindings"]["one"]["language"] == "az"
    assert frozen["bindings"]["two"]["language"] == "myv"
    assert all(not item["unresolved_model"] for item in frozen["bindings"].values())


def test_selected_alternate_language_precedes_stored_segment_language():
    revision = make_revision()
    session = FrozenSession(revision, [make_segment("one", "fr")])
    snapshot = {
        "tts": {
            "service": "Silero",
            "silero_model": "v5_cis_base",
            "language": "fr",
            "performance_enabled": False,
            "casting_enabled": False,
        },
        "selected_segment_override": {
            "tts": {"silero_model": "v3_en", "language": "en"}
        },
    }

    freeze_speech_snapshot(session, revision.id, snapshot)

    binding = snapshot["tts_language_snapshot"]["bindings"]["one"]
    assert binding["model_id"] == "v3_en"
    assert binding["language"] == "en"


def test_unresolved_model_is_frozen_without_a_fake_model_record():
    revision = make_revision()
    session = FrozenSession(revision, [make_segment("one", "fil")])
    snapshot = {
        "tts": {
            "service": "custom-service",
            "language": "en",
            "performance_enabled": False,
            "casting_enabled": False,
        }
    }

    freeze_speech_snapshot(session, revision.id, snapshot)

    frozen = snapshot["tts_language_snapshot"]
    assert frozen["records"] == []
    assert frozen["bindings"]["one"] == {
        "language": "fil",
        "operation": "tts",
        "decision": "unverified",
        "unresolved_model": True,
    }


def test_selected_only_freeze_skips_unselected_unsupported_language():
    revision = make_revision()
    session = FrozenSession(
        revision,
        [make_segment("selected", "en", 0), make_segment("other", "fr", 1)],
    )
    snapshot = {
        "tts": {
            "service": "Silero",
            "silero_model": "v3_en",
            "language": "en",
            "performance_enabled": False,
            "casting_enabled": False,
        }
    }

    freeze_speech_snapshot(session, revision.id, snapshot, segment_ids=["selected"])

    bindings = snapshot["tts_language_snapshot"]["bindings"]
    assert set(bindings) == {"selected"}
    assert bindings["selected"]["language"] == "en"
