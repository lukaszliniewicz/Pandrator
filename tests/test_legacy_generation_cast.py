"""Legacy audiobook generation uses composite cast requests per segment."""

import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from pydub import AudioSegment
from sqlalchemy import select

from pandrator.web import models as m
from tests.test_generation_cast_runtime import seed_cast
from tests.test_performance_plans import case as performance_case

case = performance_case


def _source_artifact(case, xml: str):
    with case["services"]["database"].session() as session:
        storage_key = session.get(m.SessionRecord, case["session_id"]).storage_key
    path = Path(case["services"]["paths"].sessions) / storage_key / "legacy-cast.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "text": "He said, “Bah!” Then left.",
                    "original_sentence": "He said, “Bah!” Then left.",
                    "speech_xml": xml,
                }
            ]
        ),
        encoding="utf-8",
    )
    return case["services"]["artifacts"].register(
        path,
        kind="json",
        role="tts_optimized",
        session_id=case["session_id"],
    )


def _settings():
    return {
        "service": "gemini",
        "model": "gemini-2.5-flash-tts",
        "voice": "Kore",
        "casting_enabled": True,
        "performance_enabled": False,
        "tts_batch_size": 4,
    }


def test_legacy_generation_composites_cast_parts_into_one_take(case):
    _plan, xml = seed_cast(case)
    artifact = _source_artifact(case, xml)
    handlers = case["services"]["workflow_handlers"]
    requests = []

    def synthesize(text, settings, **_kwargs):
        requests.append((text, settings["voice"]))
        return AudioSegment.silent(duration=20)

    with (
        patch.object(handlers.tts_providers, "synthesize", side_effect=synthesize),
        patch.object(
            handlers.tts_providers,
            "synthesize_batch",
            side_effect=AssertionError("casting must not use batch synthesis"),
        ),
    ):
        result = handlers.generate_audiobook_audio(
            {
                "session_id": case["session_id"],
                "source_artifact_id": artifact.id,
                "settings": _settings(),
            },
            lambda *_args: None,
            threading.Event(),
        )

    assert [voice for _text, voice in requests] == ["Kore", "Puck", "Kore"]
    assert "".join(text for text, _voice in requests) == "He said, “Bah!” Then left."
    with case["services"]["database"].session() as session:
        takes = list(
            session.scalars(
                select(m.AudioTake).where(
                    m.AudioTake.generation_segment_id.is_not(None)
                )
            )
        )
        assert len(takes) == 1
        take_artifact = session.get(m.Artifact, takes[0].artifact_id)
        assert takes[0].duration_ms == 60
        assert len(take_artifact.metadata_json["render_parts"]) == 3
        assert all(
            "settings" not in part
            for part in take_artifact.metadata_json["render_parts"]
        )
    assert result["artifact_id"]


def test_legacy_generation_part_failure_publishes_no_take(case):
    _plan, xml = seed_cast(case)
    artifact = _source_artifact(case, xml)
    handlers = case["services"]["workflow_handlers"]
    calls = []

    def synthesize(text, settings, **_kwargs):
        calls.append((text, settings["voice"]))
        if len(calls) == 2:
            raise RuntimeError("character voice unavailable")
        return AudioSegment.silent(duration=20)

    with (
        patch.object(handlers.tts_providers, "synthesize", side_effect=synthesize),
        patch.object(
            handlers.tts_providers,
            "synthesize_batch",
            side_effect=AssertionError("casting must not use batch synthesis"),
        ),
    ):
        with pytest.raises(RuntimeError, match="character voice unavailable"):
            handlers.generate_audiobook_audio(
                {
                    "session_id": case["session_id"],
                    "source_artifact_id": artifact.id,
                    "settings": _settings(),
                },
                lambda *_args: None,
                threading.Event(),
            )

    with case["services"]["database"].session() as session:
        assert not list(session.scalars(select(m.AudioTake)))
        assert not list(
            session.scalars(
                select(m.Artifact).where(m.Artifact.role == "generation_take")
            )
        )


def test_legacy_cast_cancellation_during_verification_publishes_no_take(case):
    from pandrator.web.media_process import MediaProcessCancelled

    _plan, xml = seed_cast(case)
    artifact = _source_artifact(case, xml)
    handlers = case["services"]["workflow_handlers"]
    event = threading.Event()

    def verify(*_args, **_kwargs):
        event.set()
        return None

    with (
        patch.object(
            handlers.tts_providers,
            "synthesize",
            return_value=AudioSegment.silent(duration=20),
        ),
        patch.object(handlers, "_verification_metadata", side_effect=verify),
        pytest.raises(MediaProcessCancelled),
    ):
        handlers.generate_audiobook_audio(
            {
                "session_id": case["session_id"],
                "source_artifact_id": artifact.id,
                "settings": _settings(),
            },
            lambda *_args: None,
            event,
        )
    with case["services"]["database"].session() as session:
        assert not list(session.scalars(select(m.AudioTake)))
        assert not list(
            session.scalars(
                select(m.Artifact).where(m.Artifact.role == "generation_take")
            )
        )
