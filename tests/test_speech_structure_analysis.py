import json
import tempfile
import threading
from pathlib import Path
from unittest import mock

import pytest

from pandrator.logic.llm_handler import ChatCompletionResult
from pandrator.web.database import Database
from pandrator.web.generation_controls import (
    get_generation_controls,
    save_generation_controls,
)
from pandrator.web.sessions import SessionService
from pandrator.web.speech_structure_analysis import annotate_speech_units
from tests.web_test_support import prepare_web_test_data_root


def _completion(items, *, proposals=None):
    return ChatCompletionResult(
        content=json.dumps(
            {"items": items, "character_proposals": proposals or []},
            ensure_ascii=False,
        ),
        usage={"prompt_tokens": 3, "completion_tokens": 2},
        cost=0.01,
        cost_source="test",
    )


@pytest.fixture
def database_and_session():
    with tempfile.TemporaryDirectory() as temporary:
        paths = prepare_web_test_data_root(Path(temporary))
        database = Database(paths.database)
        record = SessionService(database).create("Speech structure")
        try:
            yield database, record.id
        finally:
            database.dispose()


def test_annotation_preserves_order_and_exact_text(database_and_session):
    database, session_id = database_and_session
    responses = iter(
        [
            _completion(
                [
                    {
                        "unit_id": 1,
                        "text": "One",
                        "speech_xml": '<segment id="1">One</segment>',
                    },
                    {
                        "unit_id": 2,
                        "text": "Two",
                        "speech_xml": '<segment id="2">Two</segment>',
                    },
                ]
            )
        ]
    )
    with mock.patch(
        "pandrator.web.speech_structure_analysis.chat_completion_with_metadata",
        side_effect=lambda **_kwargs: next(responses),
    ):
        result = annotate_speech_units(
            database,
            session_id,
            ["One", "Two"],
            mode="dialogue",
            llm_settings={},
            model_name="test/model",
            cancel_event=threading.Event(),
        )
    assert result == ['<segment id="1">One</segment>', '<segment id="2">Two</segment>']


def test_invalid_markup_retries_with_validation_error(database_and_session):
    database, session_id = database_and_session
    responses = iter(
        [
            _completion([{"unit_id": 1, "text": "Hello", "speech_xml": "<broken"}]),
            _completion(
                [
                    {
                        "unit_id": 1,
                        "text": "Hello",
                        "speech_xml": '<segment id="1">Hello</segment>',
                    }
                ]
            ),
        ]
    )
    calls = []

    def complete(**kwargs):
        calls.append(kwargs["messages"][0]["content"])
        return next(responses)

    with mock.patch(
        "pandrator.web.speech_structure_analysis.chat_completion_with_metadata",
        side_effect=complete,
    ):
        result = annotate_speech_units(
            database,
            session_id,
            ["Hello"],
            mode="speakers",
            llm_settings={},
            model_name="test/model",
            cancel_event=threading.Event(),
        )
    assert result == ['<segment id="1">Hello</segment>']
    assert len(calls) == 2
    assert "malformed XML" in calls[1]


def test_invalid_result_rolls_back_character_proposal(database_and_session):
    database, session_id = database_and_session
    response = _completion(
        [
            {
                "unit_id": 1,
                "text": "Hello",
                "speech_xml": '<segment id="1"><speaker ref="c-missing">Hello</speaker></segment>',
            }
        ],
        proposals=[{"id": "c-new", "display_name": "New", "voice_category": "female"}],
    )
    with (
        mock.patch(
            "pandrator.web.speech_structure_analysis.chat_completion_with_metadata",
            return_value=response,
        ),
        pytest.raises(ValueError, match="after two attempts"),
    ):
        annotate_speech_units(
            database,
            session_id,
            ["Hello"],
            mode="speakers",
            llm_settings={},
            model_name="test/model",
            cancel_event=threading.Event(),
        )
    with database.session() as session:
        assert get_generation_controls(session, session_id)["characters"] == []


def test_cancellation_after_model_call_does_not_merge_proposals(database_and_session):
    database, session_id = database_and_session
    cancelled = threading.Event()

    def complete(**_kwargs):
        cancelled.set()
        return _completion(
            [
                {
                    "unit_id": 1,
                    "text": "Hello",
                    "speech_xml": '<segment id="1">Hello</segment>',
                }
            ],
            proposals=[
                {"id": "c-new", "display_name": "New", "voice_category": "female"}
            ],
        )

    with mock.patch(
        "pandrator.web.speech_structure_analysis.chat_completion_with_metadata",
        side_effect=complete,
    ):
        assert (
            annotate_speech_units(
                database,
                session_id,
                ["Hello"],
                mode="speakers",
                llm_settings={},
                model_name="test/model",
                cancel_event=cancelled,
            )
            == []
        )
    with database.session() as session:
        assert get_generation_controls(session, session_id)["characters"] == []


def test_authored_voice_is_preserved(database_and_session):
    database, session_id = database_and_session
    with database.session() as session:
        save_generation_controls(
            session,
            session_id,
            expected_revision=0,
            characters=[
                {
                    "id": "c-alice",
                    "display_name": "Alice",
                    "voice_category": "female",
                    "locked": True,
                }
            ],
        )
    source = '<segment id="generation-guid"><speaker ref="c-alice" voice="voice-a">Hello</speaker></segment>'
    result_xml = '<segment id="1"><speaker ref="c-alice" voice="voice-a">Hello</speaker></segment>'
    response = _completion([{"unit_id": 1, "text": "Hello", "speech_xml": result_xml}])
    with mock.patch(
        "pandrator.web.speech_structure_analysis.chat_completion_with_metadata",
        return_value=response,
    ):
        result = annotate_speech_units(
            database,
            session_id,
            ["Hello"],
            mode="speakers",
            llm_settings={},
            model_name="test/model",
            cancel_event=threading.Event(),
            source_markup={"1": source},
        )
    assert result == [result_xml]


def test_shared_authored_scope_may_be_subdivided_for_speakers(database_and_session):
    database, session_id = database_and_session
    with database.session() as session:
        save_generation_controls(
            session,
            session_id,
            expected_revision=0,
            characters=[
                {
                    "id": "c-alice",
                    "display_name": "Alice",
                    "voice_category": "female",
                }
            ],
        )
    source = '<segment id="author-guid"><span><ins>calm</ins>Hello world</span></segment>'
    result_xml = (
        '<segment id="1"><span><ins>calm</ins><dialogue>'
        '<speaker ref="c-alice">Hello</speaker> world</dialogue></span></segment>'
    )
    response = _completion([{"unit_id": 1, "speech_xml": result_xml}])
    with mock.patch(
        "pandrator.web.speech_structure_analysis.chat_completion_with_metadata",
        return_value=response,
    ):
        result = annotate_speech_units(
            database,
            session_id,
            ["Hello world"],
            mode="speakers",
            llm_settings={},
            model_name="test/model",
            cancel_event=threading.Event(),
            source_markup={"1": source},
        )
    assert result == [result_xml]


@pytest.mark.parametrize(
    "speech_xml",
    [
        '<segment id="1"><ins>urgent</ins>Hello</segment>',
        '<segment id="1"><event kind="pause"/>Hello</segment>',
        '<segment id="1"><speaker voice="voice-a">Hello</speaker></segment>',
    ],
)
def test_structural_annotation_rejects_new_controls_events_and_voices(
    database_and_session, speech_xml
):
    database, session_id = database_and_session
    response = _completion([{"unit_id": 1, "speech_xml": speech_xml}])
    with mock.patch(
        "pandrator.web.speech_structure_analysis.chat_completion_with_metadata",
        return_value=response,
    ), pytest.raises(ValueError, match="after two attempts"):
        annotate_speech_units(
            database,
            session_id,
            ["Hello"],
            mode="speakers",
            llm_settings={},
            model_name="test/model",
            cancel_event=threading.Event(),
        )


def test_dialogue_mode_rejects_new_known_speaker(database_and_session):
    database, session_id = database_and_session
    with database.session() as session:
        save_generation_controls(
            session,
            session_id,
            expected_revision=0,
            characters=[
                {
                    "id": "c-alice",
                    "display_name": "Alice",
                    "voice_category": "female",
                }
            ],
        )
    response = _completion(
        [
            {
                "unit_id": 1,
                "speech_xml": (
                    '<segment id="1"><dialogue><speaker ref="c-alice">'
                    "Hello</speaker></dialogue></segment>"
                ),
            }
        ]
    )
    with mock.patch(
        "pandrator.web.speech_structure_analysis.chat_completion_with_metadata",
        return_value=response,
    ), pytest.raises(ValueError, match="after two attempts"):
        annotate_speech_units(
            database,
            session_id,
            ["Hello"],
            mode="dialogue",
            llm_settings={},
            model_name="test/model",
            cancel_event=threading.Event(),
        )


def test_dialogue_mode_keeps_authored_speaker_identity(database_and_session):
    database, session_id = database_and_session
    with database.session() as session:
        save_generation_controls(
            session,
            session_id,
            expected_revision=0,
            characters=[
                {
                    "id": "c-alice",
                    "display_name": "Alice",
                    "voice_category": "female",
                }
            ],
        )
    source = '<segment id="author-guid"><speaker ref="c-alice">Hello</speaker></segment>'
    result_xml = (
        '<segment id="1"><dialogue><speaker ref="c-alice">'
        "Hello</speaker></dialogue></segment>"
    )
    response = _completion([{"unit_id": 1, "speech_xml": result_xml}])
    with mock.patch(
        "pandrator.web.speech_structure_analysis.chat_completion_with_metadata",
        return_value=response,
    ):
        result = annotate_speech_units(
            database,
            session_id,
            ["Hello"],
            mode="dialogue",
            llm_settings={},
            model_name="test/model",
            cancel_event=threading.Event(),
            source_markup={"1": source},
        )
    assert result == [result_xml]


def test_dialogue_mode_allows_category_only_speaker(database_and_session):
    database, session_id = database_and_session
    result_xml = (
        '<segment id="1"><dialogue><speaker g="female">'
        "Hello</speaker></dialogue></segment>"
    )
    response = _completion([{"unit_id": 1, "speech_xml": result_xml}])
    with mock.patch(
        "pandrator.web.speech_structure_analysis.chat_completion_with_metadata",
        return_value=response,
    ):
        result = annotate_speech_units(
            database,
            session_id,
            ["Hello"],
            mode="dialogue",
            llm_settings={},
            model_name="test/model",
            cancel_event=threading.Event(),
        )
    assert result == [result_xml]


@pytest.mark.parametrize(
    ("source", "result_xml"),
    [
        (
            '<segment id="author-guid"><speaker ref="c-alice" voice="voice-a">Hello</speaker></segment>',
            '<segment id="1"><speaker ref="c-alice">Hello</speaker></segment>',
        ),
        (
            '<segment id="author-guid"><span><emphasis>strong</emphasis>Hello</span></segment>',
            '<segment id="1"><span><emphasis>light</emphasis>Hello</span></segment>',
        ),
        (
            '<segment id="author-guid"><event kind="pause"/>Hello</segment>',
            '<segment id="1">Hello</segment>',
        ),
        (
            '<segment id="author-guid" boundary_after="paragraph">Hello</segment>',
            '<segment id="1">Hello</segment>',
        ),
    ],
)
def test_authored_markup_removal_or_change_is_rejected(
    database_and_session, source, result_xml
):
    database, session_id = database_and_session
    with database.session() as session:
        save_generation_controls(
            session,
            session_id,
            expected_revision=0,
            characters=[
                {
                    "id": "c-alice",
                    "display_name": "Alice",
                    "voice_category": "female",
                }
            ],
        )
    response = _completion([{"unit_id": 1, "speech_xml": result_xml}])
    with mock.patch(
        "pandrator.web.speech_structure_analysis.chat_completion_with_metadata",
        return_value=response,
    ), pytest.raises(ValueError, match="after two attempts"):
        annotate_speech_units(
            database,
            session_id,
            ["Hello"],
            mode="speakers",
            llm_settings={},
            model_name="test/model",
            cancel_event=threading.Event(),
            source_markup={"1": source},
        )


def test_replayed_proposal_is_idempotent(database_and_session):
    database, session_id = database_and_session
    response = _completion(
        [
            {
                "unit_id": 1,
                "text": "Hello",
                "speech_xml": '<segment id="1"><speaker ref="c-new">Hello</speaker></segment>',
            }
        ],
        proposals=[
            {
                "id": "c-new",
                "display_name": "New",
                "aliases": ["N"],
                "voice_category": "female",
            }
        ],
    )
    with mock.patch(
        "pandrator.web.speech_structure_analysis.chat_completion_with_metadata",
        return_value=response,
    ):
        first = annotate_speech_units(
            database,
            session_id,
            ["Hello"],
            mode="speakers",
            llm_settings={},
            model_name="test/model",
            cancel_event=threading.Event(),
        )
        with database.session() as session:
            first_revision = get_generation_controls(session, session_id)["revision"]
        second = annotate_speech_units(
            database,
            session_id,
            ["Hello"],
            mode="speakers",
            llm_settings={},
            model_name="test/model",
            cancel_event=threading.Event(),
        )
    with database.session() as session:
        second_revision = get_generation_controls(session, session_id)["revision"]
    assert first == second
    assert first_revision == second_revision
