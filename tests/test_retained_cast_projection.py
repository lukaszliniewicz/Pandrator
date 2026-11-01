"""Optional public cast hints do not require or reveal the original library."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import event, select

from pandrator.web import models as m
from pandrator_mcp.schemas.generation_controls import GenerationControlsResponse
from tests.test_evidence_access_routes import body, headers
from tests.test_project_cast_retention import (
    case as case,  # noqa: F401
)
from tests.test_project_cast_retention import (
    delete_origin,
    references,
    save,
)
from tests.test_project_cast_retention import (
    design as design,  # noqa: F401
)
from tests.test_project_cast_retention import (
    narrator_project as narrator_project,  # noqa: F401
)
from tests.test_project_cast_retention import (
    no_network as no_network,  # noqa: F401
)
from tests.test_project_cast_retention import (
    project as project,  # noqa: F401
)
from tests.test_project_cast_retention import (
    retained as retained,  # noqa: F401
)


def _read(p, *, include=True, client=None):
    return (client or p.r.f.users.bob).get(
        p.path, query_string={"include_retained": "true"} if include else {}
    )


def test_retained_cast_hints_survive_library_removal_and_default_mcp_stays_unchanged(
    project, monkeypatch
):
    p = project
    body(save(p))
    pins = references(p)
    delete_origin(p)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Read-only cast hints cannot resolve input files or a library")

    monkeypatch.setattr("pandrator.web.speech_inputs.read_verified_file", forbidden)
    queries = []

    def observe(_c, _cu, sql, _p, _co, _many):
        queries.append(sql)

    event.listen(p.r.f.case.database.engine, "before_cursor_execute", observe)
    try:
        plain = body(_read(p, include=False))
        assert "retained_cast" not in plain
        GenerationControlsResponse.model_validate(plain)
        assert not any("FROM project_speech_references" in sql for sql in queries)
        queries.clear()
        projected = body(_read(p))
    finally:
        event.remove(p.r.f.case.database.engine, "before_cursor_execute", observe)
    assert set(projected["retained_cast"]) == set(pins)
    assert all(value == {"model": next(iter(pins.values())).binding["model"]}
        for value in projected["retained_cast"].values())
    assert len([sql for sql in queries if "FROM project_speech_references" in sql]) == 1
    assert not any(f"FROM {name}" in sql for sql in queries
        for name in ("voices", "voice_samples", "stored_credentials", "provider_models"))
    hints = json.dumps(projected["retained_cast"])
    for row in pins.values():
        pin = row.pin
        assert pin is not None and pin["artifact_id"] not in hints
    assert "artifact_id" not in hints and "selector_hash" not in hints
    assert "relative_path" not in hints and "voice_id" not in hints


@pytest.mark.parametrize("change", ["selector", "pin_hash", "deleted", "foreign_session", "null_pin"])
def test_retained_cast_hints_omit_unavailable_or_mismatched_headers(project, change):
    p = project
    body(save(p))
    pins = references(p)
    slot = "cast:narrator"
    foreign = None
    if change == "foreign_session":
        foreign = body(p.r.f.users.alice.post("/api/v1/sessions", json={
            "name": "Other team source", "workflow_kind": "subtitles",
            "workspace_id": p.r.f.users.workspace_id,
        }, headers=headers(p.r.f.users.ah, "cast-projection-other-source")), 201)
    with p.r.f.case.database.immediate_session() as session:
        reference = session.get_one(m.ProjectSpeechReference, (p.source["id"], slot))
        artifact = session.get_one(m.Artifact, reference.input_artifact_id)
        if change == "selector":
            reference.selector_hash = "f" * 64
        elif change == "pin_hash":
            reference.input_content_hash = "f" * 64
        elif change == "deleted":
            artifact.state = "deleted"
        elif change == "foreign_session":
            assert foreign is not None
            # Existing artifact ownership is immutable. Exercise a forged pin
            # locator to a separately owned header without weakening that guard.
            foreign_id = m.new_id()
            values = {column.name: getattr(artifact, column.name)
                for column in m.Artifact.__table__.columns}
            values.update(id=foreign_id, session_id=foreign["id"],
                project_id=foreign["project_id"], relative_path=f"artifacts/{foreign_id}.json")
            session.add(m.Artifact(**values))
            session.flush()
            reference.input_artifact_id = foreign_id
        else:
            reference.input_artifact_id = reference.input_content_hash = None
    hints = body(_read(p))["retained_cast"]
    assert slot not in hints
    if change in {"selector", "pin_hash", "null_pin", "foreign_session"}:
        assert set(hints) == set(pins) - {slot}
    else:
        assert hints == {}


def test_empty_cast_does_not_query_retained_headers(project):
    p = project
    with p.r.f.case.database.immediate_session() as session:
        ledger = session.scalar(select(m.KnowledgeLedger).where(
            m.KnowledgeLedger.session_id == p.source["id"],
            m.KnowledgeLedger.kind == "generation_controls"))
        assert ledger is None
    queries = []

    def observe(_c, _cu, sql, _p, _co, _many):
        queries.append(sql)

    event.listen(p.r.f.case.database.engine, "before_cursor_execute", observe)
    try:
        assert body(_read(p))["retained_cast"] == {}
    finally:
        event.remove(p.r.f.case.database.engine, "before_cursor_execute", observe)
    assert not any("FROM project_speech_references" in sql for sql in queries)


def test_optional_cast_hint_query_keeps_private_source_guard(project, monkeypatch):
    p = project
    foreign = body(p.r.f.users.alice.post("/api/v1/sessions", json={
        "name": "Private casting source", "workflow_kind": "subtitles",
    }, headers=headers(p.r.f.users.ah, "cast-projection-private-source")), 201)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("A denied project must not reach retained-header projection")

    monkeypatch.setattr("pandrator.web.generation_control_routes._retained_cast_projection", forbidden)
    response = p.r.f.users.bob.get(f"/api/v1/sessions/{foreign['id']}/generation-controls",
        query_string={"include_retained": "true"})
    assert response.status_code == 404
    assert b"Private casting source" not in response.data


@pytest.mark.parametrize('value,valid', [
    ({'emotion_intensity': None}, True),
    ({'emotion': 'calm', 'emotion_intensity': 1.5}, True),
    ({'emotion_intensity': 1.0}, False),
    ({'emotion': 'calm', 'emotion_intensity': 3.0}, False),
    ({'emotion': 'calm', 'emotion_intensity': float('nan')}, False),
])
def test_portable_character_delivery_matches_the_application_contract(value, valid):
    from pydantic import ValidationError

    from pandrator.logic.speech_performance import Delivery
    from pandrator_mcp.schemas.generation_controls import CharacterDelivery

    if valid:
        assert CharacterDelivery.model_validate(value).model_dump() == Delivery.model_validate(value).model_dump()
    else:
        for model in (CharacterDelivery, Delivery):
            with pytest.raises(ValidationError):
                model.model_validate(value)
