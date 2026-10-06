"""Lossless passive annotation transport and atomic submission checks."""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pandrator.logic.speech_annotation_spans import source_text_sha256
from pandrator.logic.speech_markup import parse_speech_markup
from pandrator.web.dispatch import DispatchError
from pandrator.web.generation_controls import get_generation_controls
from pandrator.web.models import SpeechOptimizationDispatchBatch
from pandrator.web.schemas import SpeechOptimizationDispatchItem
from pandrator.web.speech_optimization_dispatch import SpeechOptimizationDispatchRunService
from pandrator_mcp.schemas.speech_optimization_dispatch import (
    ClaimSpeechOptimizationDispatchBatchInput,
    SpeechOptimizationDispatchItemInput,
)
from pandrator_mcp.tools.speech_optimization_dispatch import (
    claim_speech_optimization_dispatch_batch,
)
from tests import test_web_speech_optimization_dispatch as web_support


def _normalize(text="Hello", annotations=None, **overrides):
    source = {"unit_id": 1, "text": text, "language": "en"}
    source.update(overrides.pop("source", {}))
    item = {
        "unit_id": 1,
        "annotations": [] if annotations is None else annotations,
        "source_sha256": source_text_sha256(text),
        **overrides.pop("item", {}),
    }
    return SpeechOptimizationDispatchRunService._normalize_result(
        SimpleNamespace(input_json={"units": [source]}),
        {"kind": "speech_optimization", "items": [item]},
        annotation_only=overrides.pop("annotation_only", True),
        annotation_mode=overrides.pop("annotation_mode", "speakers"),
        **overrides,
    )[0]


def test_exact_unicode_source_slices_and_known_speaker_spans():
    text = 'Intro 😀 e\u0301 <&> "Hi"\r\nThen “Bye”.'
    first = text.index('"Hi"')
    second = text.index("“Bye”")
    spans = [
        {"start": first, "end": first + 4, "speaker_ref": "c-alice"},
        {"start": second, "end": second + 5, "speaker_ref": "c-bob"},
    ]
    characters = [
        {"id": "c-alice", "display_name": "Alice", "voice_category": "female"},
        {"id": "c-bob", "display_name": "Bob", "voice_category": "male"},
    ]
    normalized = _normalize(
        text, spans, item={"boundary_after": "paragraph"}, characters=characters
    )
    assert normalized["text"] == text
    assert "annotations" not in normalized and "source_sha256" not in normalized
    parsed = parse_speech_markup(
        normalized["speech_xml"], expected_segment_id="1", characters=characters
    )
    assert parsed.transcript == text
    assert parsed.segment_id == "1"
    assert parsed.boundary_after == "paragraph"
    dialogue = [span for span in parsed.spans if span.dialogue]
    assert [(span.start, span.end, span.speaker_id) for span in dialogue] == [
        (first, first + 4, "c-alice"),
        (second, second + 5, "c-bob"),
    ]
    assert all(span.narrator for span in parsed.spans if not span.dialogue)


@pytest.mark.parametrize("mode", ["speakers", "dialogue"])
def test_empty_spans_and_explicit_unknown_identity(mode):
    narrator = parse_speech_markup(
        _normalize(annotation_mode=mode)["speech_xml"], expected_segment_id="1"
    )
    assert narrator.transcript == "Hello"
    assert all(span.narrator and not span.dialogue for span in narrator.spans)
    unknown = parse_speech_markup(
        _normalize(
            annotations=[{"start": 0, "end": 5, "speaker_ref": None}],
            annotation_mode=mode,
        )["speech_xml"],
        expected_segment_id="1",
    )
    assert unknown.spans[0].dialogue and unknown.spans[0].speaker_id is None


@pytest.mark.parametrize(
    "annotations",
    [
        "bad",
        [None],
        [{"start": 0, "end": 2, "extra": True}],
        [{"start": True, "end": 2}],
        [{"start": 0, "end": False}],
        [{"start": 0.0, "end": 2}],
        [{"start": -1, "end": 2}],
        [{"start": 2, "end": 2}],
        [{"start": 0, "end": 6}],
        [{"start": 2, "end": 4}, {"start": 1, "end": 2}],
        [{"start": 0, "end": 3}, {"start": 2, "end": 4}],
        [{"start": 0, "end": 1, "speaker_ref": "unknown"}],
        [{"start": 0, "end": 1}] * 501,
    ],
)
def test_malformed_ranges_and_speakers_are_rejected(annotations):
    with pytest.raises(DispatchError) as error:
        _normalize(annotations=annotations)
    assert error.value.code == "invalid_annotation_spans"


@pytest.mark.parametrize(
    "overrides",
    [
        {"item": {"source_sha256": "0" * 64}},
        {"item": {"source_sha256": "A" * 64}},
        {"item": {"source_sha256": None}},
        {"item": {"text": "Hello"}},
        {"item": {"speech_xml": '<segment id="1">Hello</segment>'}},
        {"item": {"boundary_after": "bad"}},
        {"annotation_only": False},
        {"annotation_mode": "off"},
    ],
)
def test_invalid_span_variant_is_rejected(overrides):
    with pytest.raises(DispatchError) as error:
        _normalize(**overrides)
    assert error.value.code == "invalid_annotation_spans"


def test_existing_xml_requires_xml_variant_and_original_unit_order():
    xml = '<segment id="1"><narrator><em>calm</em>Hello</narrator></segment>'
    with pytest.raises(DispatchError, match="return the XML variant"):
        _normalize(source={"speech_xml": xml})
    batch = SimpleNamespace(
        input_json={
            "units": [
                {"unit_id": 1, "text": "Hello", "speech_xml": xml},
            ]
        }
    )
    normalized = SpeechOptimizationDispatchRunService._normalize_result(
        batch,
        {"kind": "speech_optimization", "items": [{"unit_id": 1, "speech_xml": xml}]},
        annotation_only=True,
        annotation_mode="dialogue",
    )
    assert normalized[0]["speech_xml"] == xml
    batch.input_json = {"units": [{"unit_id": i, "text": "Hello"} for i in (1, 2)]}
    for ids in ([2, 1], [1, 1], [1]):
        with pytest.raises(DispatchError, match="exactly once"):
            SpeechOptimizationDispatchRunService._normalize_result(
                batch,
                {
                    "kind": "speech_optimization",
                    "items": [
                        {
                            "unit_id": i,
                            "annotations": [],
                            "source_sha256": source_text_sha256("Hello"),
                        }
                        for i in ids
                    ],
                },
                annotation_only=True,
                annotation_mode="dialogue",
            )


@pytest.mark.parametrize(
    "schema", [SpeechOptimizationDispatchItem, SpeechOptimizationDispatchItemInput]
)
def test_http_and_mcp_schema_variant_constraints(schema):
    valid = {"unit_id": 1, "annotations": [], "source_sha256": source_text_sha256("Hello")}
    assert schema.model_validate(valid).annotations == []
    for invalid in (
        {**valid, "text": "Hello"},
        {**valid, "speech_xml": "x"},
        {"unit_id": 1, "annotations": []},
        {"unit_id": 1, "source_sha256": valid["source_sha256"]},
        {"unit_id": 1, "boundary_after": "chapter"},
        {**valid, "annotations": [{"start": True, "end": 1}]},
        {**valid, "annotations": [{"start": 1, "end": 1}]},
    ):
        with pytest.raises(ValidationError):
            schema.model_validate(invalid)


@pytest.fixture
def web():
    support = web_support.SpeechOptimizationDispatchWebTests()
    support.setUp()
    try:
        yield support
    finally:
        support.tearDown()


def test_http_finalize_compact_hash_and_proposal_rollback(web):
    text = "😀 e\u0301 <&> Hello"
    record, _, _ = web._create_source(
        workflow_kind="audiobook",
        role="prepared_text",
        filename="prepared.json",
        content=json.dumps([{"processed_sentence": text, "language": "en"}]),
    )
    run = web._create_run(record.id, annotation_only=True, annotation_mode="speakers")
    claimed = web._claim(run["id"], 1)
    assert claimed["batch"]["units"][0]["source_sha256"] == source_text_sha256(text)
    runtime = SimpleNamespace(
        require_application=lambda: SimpleNamespace(
            claim_speech_optimization_dispatch_batch=lambda *_args, **_kwargs: claimed,
        )
    )
    compact = claim_speech_optimization_dispatch_batch(
        runtime,
        ClaimSpeechOptimizationDispatchBatchInput(
            run_id=run["id"],
            idempotency_key="span-claim-compact",
        ),
    )
    assert compact["batch"]["units"][0]["source_sha256"] == source_text_sha256(text)
    assert "never UTF-16" in compact["manifest"]["result_contract"]["annotations"]["offsets"]
    endpoint = f"/api/v1/speech-optimization-dispatch-batches/{claimed['batch_id']}/submit"
    proposal = {"id": "c-new", "display_name": "New", "voice_category": "unspecified"}
    item = {
        "unit_id": 1,
        "source_sha256": "0" * 64,
        "annotations": [
            {"start": text.index("Hello"), "end": len(text), "speaker_ref": "c-new"},
        ],
    }
    body = {
        "lease_token": claimed["lease_token"],
        "character_proposals": [proposal],
        "result": {"kind": "speech_optimization", "items": [item]},
    }
    rejected = web.client.post(endpoint, json=body, headers=web._headers("span-submit-bad"))
    assert rejected.status_code == 422, rejected.get_json()
    with web.extension["database"].session() as session:
        controls = get_generation_controls(session, record.id)
        assert controls["characters"] == []
        assert session.get(SpeechOptimizationDispatchBatch, claimed["batch_id"]).status == "leased"
    item["source_sha256"] = source_text_sha256(text)
    accepted = web.client.post(endpoint, json=body, headers=web._headers("span-submit-good"))
    assert accepted.status_code == 200, accepted.get_json()
    final = accepted.get_json()
    assert final["finalized"]
    _, path = web.extension["artifacts"].resolve(final["final_artifact_id"])
    output = json.loads(path.read_text(encoding="utf-8"))
    assert output[0]["tts_optimized_sentence"] == text
    with web.extension["database"].session() as session:
        controls = get_generation_controls(session, record.id)
        assert controls["characters"][0]["id"] == "c-new"
    parsed = parse_speech_markup(
        output[0]["speech_xml"], expected_segment_id="1", characters=controls["characters"]
    )
    assert parsed.transcript == text
    assert [span.speaker_id for span in parsed.spans if span.dialogue] == ["c-new"]
