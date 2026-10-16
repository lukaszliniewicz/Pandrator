"""Focused passive speech-optimization markup contract checks."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.dispatch import DispatchError
from pandrator.web.generation_controls import (
    get_generation_controls,
    save_generation_controls,
)
from pandrator.web.models import SpeechOptimizationDispatchBatch, utcnow
from pandrator.web.schemas import (
    SpeechOptimizationDispatchBatchSubmitRequest,
    SpeechOptimizationDispatchRunCreateRequest,
)
from pandrator.web.speech_optimization_dispatch import (
    SpeechOptimizationDispatchRunService,
)
from pandrator_mcp.schemas.speech_optimization_dispatch import (
    CreateSpeechOptimizationDispatchRunInput,
    SpeechOptimizationDispatchItemInput,
    SpeechOptimizationDispatchResultInput,
)
from pandrator_mcp.tools.speech_optimization_dispatch import _claim
from tests.web_test_support import prepare_web_test_data_root


def _batch(*, text: str = "Hello", speech_xml: str | None = None):
    unit = {"unit_id": 1, "text": text, "language": "en", "speaker": None}
    if speech_xml is not None:
        unit["speech_xml"] = speech_xml
    return SimpleNamespace(input_json={"units": [unit]})


@pytest.fixture
def passive_dispatch_environment(tmp_path):
    data_root = tmp_path / "passive-dispatch"
    prepare_web_test_data_root(str(data_root))
    bootstrap = BootstrapTokenStore()
    app = create_app(
        data_root=data_root,
        testing=True,
        bootstrap_tokens=bootstrap,
        background_maintenance=False,
    )
    client = app.test_client()
    token = bootstrap.issue()
    csrf = client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()[
        "csrf_token"
    ]
    extension = app.extensions["pandrator"]
    record = extension["sessions"].create(
        "Passive markup contract",
        workflow_kind="audiobook",
        source_language="en",
    )
    directory = extension["paths"].sessions / record.storage_key
    directory.mkdir(parents=True, exist_ok=True)
    source_path = directory / "prepared.json"
    source_path.write_text(
        json.dumps([{"processed_sentence": "Hello.", "language": "en"}]),
        encoding="utf-8",
    )
    source = extension["artifacts"].register(
        source_path,
        kind="json",
        role="prepared_text",
        session_id=record.id,
    )
    try:
        yield {
            "client": client,
            "csrf": csrf,
            "extension": extension,
            "record_id": record.id,
            "source_id": source.id,
        }
    finally:
        extension["database"].dispose()


def _start_dispatch(environment, *, annotation_mode: str):
    response = environment["client"].post(
        f"/api/v1/sessions/{environment['record_id']}/speech-optimization-dispatch-runs",
        json={
            "instructions": "Preserve the source text exactly.",
            "source_artifact_id": environment["source_id"],
            "annotation_mode": annotation_mode,
            "annotation_only": True,
            "max_units_per_batch": 1,
        },
        headers={"X-CSRF-Token": environment["csrf"], "Idempotency-Key": "create-run"},
    )
    assert response.status_code == 201, response.get_json()
    run = response.get_json()
    claim = environment["client"].post(
        f"/api/v1/speech-optimization-dispatch-runs/{run['id']}/claim",
        json={"lease_seconds": 900},
        headers={"X-CSRF-Token": environment["csrf"], "Idempotency-Key": "claim-run"},
    )
    assert claim.status_code == 200, claim.get_json()
    return claim.get_json()


def _submit_dispatch(environment, claim, xml: str, *, key: str, proposals=None):
    body = {
        "lease_token": claim["lease_token"],
        "result": {
            "kind": "speech_optimization",
            "items": [{"unit_id": 1, "speech_xml": xml}],
        },
    }
    if proposals is not None:
        body["character_proposals"] = proposals
    return environment["client"].post(
        f"/api/v1/speech-optimization-dispatch-batches/{claim['batch_id']}/submit",
        json=body,
        headers={"X-CSRF-Token": environment["csrf"], "Idempotency-Key": key},
    )


def test_annotation_only_requires_exact_source_text_and_canonicalizes_xml():
    xml = '<segment id="1"><dialogue>Hello</dialogue></segment>'
    normalized = SpeechOptimizationDispatchRunService._normalize_result(
        _batch(text="Hello", speech_xml=xml),
        {
            "kind": "speech_optimization",
            "items": [{"unit_id": 1, "text": "Hello"}],
        },
        annotation_only=True,
    )
    assert normalized[0]["text"] == "Hello"
    assert normalized[0]["speech_xml"] == xml

    xml_only = SpeechOptimizationDispatchRunService._normalize_result(
        _batch(text="Hello", speech_xml=xml),
        {
            "kind": "speech_optimization",
            "items": [{"unit_id": 1, "speech_xml": xml}],
        },
        annotation_only=True,
    )
    assert xml_only[0]["text"] == "Hello"
    assert xml_only[0]["speech_xml"] == xml

    with pytest.raises(DispatchError, match="preserve the source text exactly"):
        SpeechOptimizationDispatchRunService._normalize_result(
            _batch(text="Hello", speech_xml=xml),
            {
                "kind": "speech_optimization",
                "items": [{"unit_id": 1, "text": "Hello!"}],
            },
            annotation_only=True,
        )


def test_supplied_markup_must_survive_text_rewrite_and_unknown_speaker_is_rejected():
    source_xml = '<segment id="1"><speaker ref="c-missing">Hello</speaker></segment>'
    with pytest.raises(DispatchError, match="unresolved speaker"):
        SpeechOptimizationDispatchRunService._normalize_result(
            _batch(text="Hello", speech_xml=source_xml),
            {
                "kind": "speech_optimization",
                "items": [{"unit_id": 1, "text": "Hello"}],
            },
            characters=[],
        )


def test_passive_annotation_cannot_strip_authored_markup():
    source_xml = '<segment id="generation-guid"><speaker ref="c-alice" voice="voice-a">Hello</speaker></segment>'
    with pytest.raises(DispatchError, match="authored speech markup"):
        SpeechOptimizationDispatchRunService._normalize_result(
            _batch(text="Hello", speech_xml=source_xml),
            {
                "kind": "speech_optimization",
                "items": [{"unit_id": 1, "speech_xml": '<segment id="1">Hello</segment>'}],
            },
            characters=[
                {
                    "id": "c-alice",
                    "display_name": "Alice",
                    "voice_category": "female",
                }
            ],
        )

    with pytest.raises(DispatchError, match="revise the clean text"):
        SpeechOptimizationDispatchRunService._normalize_result(
            _batch(text="Hello", speech_xml=source_xml),
            {
                "kind": "speech_optimization",
                "items": [
                    {
                        "unit_id": 1,
                        "speech_xml": '<segment id="1"><speaker ref="c-alice" voice="voice-a">Hi</speaker></segment>',
                    }
                ],
            },
            characters=[
                {
                    "id": "c-alice",
                    "display_name": "Alice",
                    "voice_category": "female",
                }
            ],
        )

    with pytest.raises(DispatchError, match="markup would be lost"):
        SpeechOptimizationDispatchRunService._normalize_result(
            _batch(text="Hello", speech_xml='<segment id="1">Hello</segment>'),
            {
                "kind": "speech_optimization",
                "items": [{"unit_id": 1, "text": "Hi"}],
            },
        )


@pytest.mark.parametrize(
    ("annotation_mode", "speech_xml", "characters"),
    [
        (
            "dialogue",
            '<segment id="1"><dialogue><speaker ref="c-alice">Hello</speaker></dialogue></segment>',
            [{"id": "c-alice", "display_name": "Alice", "voice_category": "female"}],
        ),
        ("dialogue", '<segment id="1"><ins>urgent</ins>Hello</segment>', []),
        ("dialogue", '<segment id="1"><event kind="laugh"/>Hello</segment>', []),
        ("dialogue", '<segment id="1"><speaker voice="voice-a">Hello</speaker></segment>', []),
        ("speakers", '<segment id="1"><ins>urgent</ins>Hello</segment>', []),
        ("speakers", '<segment id="1"><event kind="laugh"/>Hello</segment>', []),
        ("speakers", '<segment id="1"><speaker voice="voice-a">Hello</speaker></segment>', []),
    ],
)
def test_passive_structural_modes_reject_new_identity_or_controls(
    annotation_mode, speech_xml, characters
):
    with pytest.raises(DispatchError) as raised:
        SpeechOptimizationDispatchRunService._normalize_result(
            _batch(text="Hello"),
            {
                "kind": "speech_optimization",
                "items": [{"unit_id": 1, "speech_xml": speech_xml}],
            },
            annotation_mode=annotation_mode,
            annotation_only=True,
            characters=characters,
        )
    assert raised.value.code == "invalid_speech_markup"


def test_passive_dialogue_preserves_authored_metadata_and_allows_category_marks():
    characters = [
        {"id": "c-alice", "display_name": "Alice", "voice_category": "female"}
    ]
    source_xml = (
        '<segment id="1"><speaker ref="c-alice" voice="voice-a">'
        "Hello</speaker></segment>"
    )
    result_xml = (
        '<segment id="1"><dialogue><speaker ref="c-alice" voice="voice-a">'
        "Hello</speaker></dialogue></segment>"
    )
    preserved = SpeechOptimizationDispatchRunService._normalize_result(
        _batch(text="Hello", speech_xml=source_xml),
        {
            "kind": "speech_optimization",
            "items": [{"unit_id": 1, "speech_xml": result_xml}],
        },
        annotation_mode="dialogue",
        annotation_only=True,
        characters=characters,
    )
    assert preserved[0]["speech_xml"] == result_xml

    category_xml = (
        '<segment id="1"><dialogue><speaker g="female">'
        "Hello</speaker></dialogue></segment>"
    )
    categorized = SpeechOptimizationDispatchRunService._normalize_result(
        _batch(text="Hello"),
        {
            "kind": "speech_optimization",
            "items": [{"unit_id": 1, "speech_xml": category_xml}],
        },
        annotation_mode="dialogue",
        annotation_only=True,
    )
    assert categorized[0]["speech_xml"] == category_xml


def test_passive_speakers_allows_named_identity_and_preserves_authored_controls():
    characters = [
        {"id": "c-alice", "display_name": "Alice", "voice_category": "female"}
    ]
    source_xml = (
        '<segment id="1"><speaker ref="c-alice" voice="voice-a">'
        "Hello</speaker></segment>"
    )
    result_xml = (
        '<segment id="1"><dialogue><speaker ref="c-alice" voice="voice-a">'
        "Hello</speaker></dialogue></segment>"
    )
    preserved = SpeechOptimizationDispatchRunService._normalize_result(
        _batch(text="Hello", speech_xml=source_xml),
        {
            "kind": "speech_optimization",
            "items": [{"unit_id": 1, "speech_xml": result_xml}],
        },
        annotation_mode="speakers",
        annotation_only=True,
        characters=characters,
    )
    assert preserved[0]["speech_xml"] == result_xml

    new_identity_xml = (
        '<segment id="1"><dialogue><speaker ref="c-alice">'
        "Hello</speaker></dialogue></segment>"
    )
    identified = SpeechOptimizationDispatchRunService._normalize_result(
        _batch(text="Hello"),
        {
            "kind": "speech_optimization",
            "items": [{"unit_id": 1, "speech_xml": new_identity_xml}],
        },
        annotation_mode="speakers",
        annotation_only=True,
        characters=characters,
    )
    assert identified[0]["speech_xml"] == new_identity_xml


def test_passive_dialogue_rejections_leave_batch_open_and_valid_replay_is_idempotent(
    passive_dispatch_environment,
):
    environment = passive_dispatch_environment
    with environment["extension"]["database"].immediate_session() as session:
        save_generation_controls(
            session,
            environment["record_id"],
            expected_revision=0,
            characters=[
                {
                    "id": "c-alice",
                    "display_name": "Alice",
                    "voice_category": "female",
                }
            ],
        )
    claim = _start_dispatch(environment, annotation_mode="dialogue")
    instructions = claim["task"]["instructions"]
    assert "regardless of evidence" in instructions
    assert "return an empty character_proposals list" in instructions
    assert "Set every speaker_ref to null" in instructions

    proposal = _submit_dispatch(
        environment,
        claim,
        '<segment id="1"><dialogue>Hello.</dialogue></segment>',
        key="dialogue-proposal",
        proposals=[
            {"id": "c-new", "display_name": "New", "voice_category": "female"}
        ],
    )
    assert proposal.status_code == 422
    assert proposal.get_json()["error"]["code"] == "invalid_character_proposals"

    rejected_xml = [
        '<segment id="1"><dialogue><speaker ref="c-alice">Hello.</speaker></dialogue></segment>',
        '<segment id="1"><ins>urgent</ins>Hello.</segment>',
        '<segment id="1"><event kind="laugh"/>Hello.</segment>',
        '<segment id="1"><speaker voice="voice-a">Hello.</speaker></segment>',
    ]
    for index, xml in enumerate(rejected_xml):
        response = _submit_dispatch(
            environment,
            claim,
            xml,
            key=f"dialogue-invalid-{index}",
        )
        assert response.status_code == 422, response.get_json()
        assert response.get_json()["error"]["code"] == "invalid_speech_markup"

    database = environment["extension"]["database"]
    with database.session() as session:
        characters = get_generation_controls(session, environment["record_id"])[
            "characters"
        ]
        batch = session.get(SpeechOptimizationDispatchBatch, claim["batch_id"])
        assert [item["id"] for item in characters] == ["c-alice"]
        assert batch.status == "leased"

    accepted_xml = (
        '<segment id="1"><dialogue><speaker g="female">Hello.</speaker>'
        "</dialogue></segment>"
    )
    accepted = _submit_dispatch(
        environment,
        claim,
        accepted_xml,
        key="dialogue-valid",
    )
    assert accepted.status_code in {200, 202}, accepted.get_json()
    assert accepted.get_json()["accepted"] is True
    replay = _submit_dispatch(
        environment,
        claim,
        accepted_xml,
        key="dialogue-valid",
    )
    assert replay.status_code in {200, 202}, replay.get_json()
    assert replay.get_json()["accepted"] is True


def test_passive_speakers_invalid_markup_rolls_back_merged_proposal(
    passive_dispatch_environment,
):
    environment = passive_dispatch_environment
    claim = _start_dispatch(environment, annotation_mode="speakers")
    response = _submit_dispatch(
        environment,
        claim,
        '<segment id="1"><ins>urgent</ins>Hello.</segment>',
        key="speakers-invalid-markup",
        proposals=[
            {"id": "c-new", "display_name": "New", "voice_category": "female"}
        ],
    )
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "invalid_speech_markup"

    database = environment["extension"]["database"]
    with database.session() as session:
        assert get_generation_controls(session, environment["record_id"])[
            "characters"
        ] == []
        batch = session.get(SpeechOptimizationDispatchBatch, claim["batch_id"])
        assert batch.status == "leased"


def test_web_and_mcp_requests_expose_independent_markup_controls_and_proposals():
    web = SpeechOptimizationDispatchRunCreateRequest(
        annotation_mode="speakers",
        annotation_only=True,
    )
    assert web.annotation_mode == "speakers"
    assert web.annotation_only is True
    submit = SpeechOptimizationDispatchBatchSubmitRequest(
        lease_token="lease",
        result={"kind": "speech_optimization", "items": [{"unit_id": 1, "text": "x"}]},
        character_proposals=[{"id": "c-alice", "display_name": "Alice"}],
    )
    assert submit.character_proposals[0]["id"] == "c-alice"
    mcp = CreateSpeechOptimizationDispatchRunInput(
        session_id="session",
        annotation_mode="dialogue",
        annotation_only=False,
        idempotency_key="speech:create-xml",
    )
    assert mcp.annotation_mode == "dialogue"
    assert "speech_xml" in SpeechOptimizationDispatchItemInput.model_fields
    assert "speech_xml" in SpeechOptimizationDispatchResultInput.model_fields["items"].annotation.__args__[0].model_fields


def test_mcp_claim_projection_keeps_dictionary_and_unit_markup_without_private_fields():
    payload = {
        "run_id": "run-1",
        "batch_id": "batch-1",
        "task": {
            "kind": "speech_optimization",
            "output_role": "tts_optimized",
            "annotation_mode": "speakers",
            "annotation_only": False,
            "instructions": "Keep markup.",
        },
        "character_dictionary": {
            "revision": 4,
            "entries": [{"id": "c-alice", "display_name": "Alice"}],
        },
        "batch": {
            "valid_unit_ids": [1],
            "units": [
                {
                    "unit_id": 1,
                    "text": "Hello",
                    "language": "en",
                    "speech_xml": '<segment id="1">Hello</segment>',
                }
            ],
        },
    }
    projected = _claim(payload)
    assert projected["character_dictionary"]["revision"] == 4
    assert projected["character_dictionary"]["entries"][0]["id"] == "c-alice"
    assert projected["batch"]["units"][0]["speech_xml"].startswith("<segment")
    assert "private" not in projected


class _FakeSession:
    def __init__(self, batch):
        self.batch = batch
        self.flush_count = 0

    def get(self, _model, _batch_id):
        return self.batch

    def flush(self):
        self.flush_count += 1


def test_release_rejects_expired_lease_without_mutating_batch():
    old_updated_at = utcnow() - timedelta(minutes=2)
    batch = SimpleNamespace(
        id="batch-1",
        status="leased",
        lease_token="lease-1",
        claim_key="claim-1",
        lease_expires_at=utcnow() - timedelta(seconds=1),
        updated_at=old_updated_at,
    )
    session = _FakeSession(batch)

    with pytest.raises(DispatchError) as raised:
        SpeechOptimizationDispatchRunService.release_in_session(
            object(), session, batch_id=batch.id, lease_token="lease-1"
        )

    assert raised.value.code == "lease_expired"
    assert raised.value.retryable is True
    assert batch.status == "leased"
    assert batch.lease_token == "lease-1"
    assert batch.claim_key == "claim-1"
    assert batch.lease_expires_at < utcnow()
    assert batch.updated_at == old_updated_at
    assert session.flush_count == 0


def test_release_rejects_missing_expiry_as_expired():
    batch = SimpleNamespace(
        id="batch-missing-expiry",
        status="leased",
        lease_token="lease-missing-expiry",
        claim_key="claim-missing-expiry",
        lease_expires_at=None,
        updated_at=None,
    )
    session = _FakeSession(batch)

    with pytest.raises(DispatchError) as raised:
        SpeechOptimizationDispatchRunService.release_in_session(
            object(), session, batch_id=batch.id, lease_token=batch.lease_token
        )

    assert raised.value.code == "lease_expired"
    assert raised.value.retryable is True
    assert batch.status == "leased"
    assert batch.lease_token == "lease-missing-expiry"
    assert batch.claim_key == "claim-missing-expiry"
    assert batch.lease_expires_at is None
    assert batch.updated_at is None
    assert session.flush_count == 0


def test_release_resets_current_unexpired_lease():
    batch = SimpleNamespace(
        id="batch-2",
        status="leased",
        lease_token="lease-2",
        claim_key="claim-2",
        lease_expires_at=utcnow() + timedelta(minutes=5),
        updated_at=None,
    )
    session = _FakeSession(batch)

    released = SpeechOptimizationDispatchRunService.release_in_session(
        object(), session, batch_id=batch.id, lease_token="lease-2"
    )

    assert released == {
        "batch_id": "batch-2",
        "status": "ready",
        "lease_expires_at": None,
    }
    assert batch.status == "ready"
    assert batch.lease_token is None
    assert batch.claim_key is None
    assert batch.lease_expires_at is None
    assert batch.updated_at is not None
    assert session.flush_count == 1
