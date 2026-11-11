"""Shared audio identity work stays cheap on a cast plan with retained pins.

Before the change every cast span rebuilt and deep-copied the whole TTS service catalogue
(``apply_resolved_binding`` called ``resolve_capabilities`` without the
request's cache) and re-proved its retained pin against the database
(``validate_retained_cast_resolution``).  Measured on the unchanged code with the
12-segment plan below, ``plan_audio_identities`` made 11 catalogue builds
(``_default_service_config_templates``) and 271 statements for 12 segments
against 139 for 6, so statements grew by 132 for 6 more segments.  After the
change the same pass makes 1 build and 75 statements against 63 for 6.  No
wall-clock assertions: the tests count builds and statements.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import event

from pandrator.logic import speech_performance, tts_service_catalogue
from pandrator.web import generation_cast_runtime as cast_runtime
from pandrator.web import models as m
from pandrator.web import speech_inputs
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.accounts.execution_actor import execution_actor_scope
from pandrator.web.generation_audio_identity import (
    AudioIdentityContext,
    _hash,
    plan_audio_identities,
)
from pandrator.web.generation_performance_snapshot import performance_runtime_settings
from pandrator.web.speech_plan_workspace import freeze_speech_snapshot
from tests.test_cast_retention_integration import cast_project as cast_project  # noqa: F401
from tests.test_evidence_access_routes import body, headers
from tests.test_project_tts_retention import case as case  # noqa: F401
from tests.test_project_tts_retention import design as design  # noqa: F401
from tests.test_project_tts_retention import no_network as no_network  # noqa: F401
from tests.test_project_tts_retention import project as project  # noqa: F401
from tests.test_project_tts_retention import retained as retained  # noqa: F401
from tests.test_voice_sample_execution_access import api_actor

# Two segments of each kind, ordered so the first six hold one of every kind.
KINDS = ["narration", "fred", "both", "wilma", "german", "uncast"] * 2


def _segment_text(kind: str, index: int) -> str:
    return f"First line {index}. Second line {index}." if kind == "both" else f"Line {index} here."


def _speech_xml(segment_id: str, kind: str, text: str) -> str | None:
    if kind == "narration":
        return None
    if kind == "both":
        first, second = text.split(". ", 1)
        inner = (
            f'<dialogue><speaker ref="fred">{first}.</speaker></dialogue> '
            f'<dialogue><speaker ref="wilma">{second}</speaker></dialogue>'
        )
    else:
        speaker = {"fred": "fred", "wilma": "wilma", "german": "fred", "uncast": "zed"}[kind]
        inner = f'<dialogue><speaker ref="{speaker}">{text}</speaker></dialogue>'
    return f'<segment id="{segment_id}">{inner}</segment>'


class Plan:
    def __init__(self, c, revision_id: str, ids: list[str], snapshot: dict[str, Any]):
        self.c = c
        self.revision_id = revision_id
        self.ids = ids
        self.snapshot = snapshot

    def run(self, session, segment_ids=None):
        return plan_audio_identities(
            session, self.revision_id, self.snapshot, segment_ids=segment_ids,
        )


@pytest.fixture
def varied_plan(cast_project):  # noqa: F811
    c = cast_project
    p, r = c.p, c.r
    # A character the plan can name but the cast never assigns a voice to.
    controls = body(p.r.f.users.alice.get(f"/api/v1/sessions/{p.source['id']}/generation-controls"))
    body(p.r.f.users.alice.put(
        f"/api/v1/sessions/{p.source['id']}/generation-controls",
        json={"expected_revision": controls["revision"], "characters": [
            {"id": "fred", "display_name": "Fred"}, {"id": "wilma", "display_name": "Wilma"},
            {"id": "zed", "display_name": "Zed"},
        ]},
        headers=headers(p.r.f.users.ah, "uncast-character"),
    ))
    texts = [_segment_text(kind, index) for index, kind in enumerate(KINDS)]
    segments = [
        {"text": text, **({"language": "de"} if kind == "german" else {})}
        for text, kind in zip(texts, KINDS, strict=True)
    ]
    plan = r.f.services.generation.create_plan(
        p.source["id"], source_revision_id=None, settings={}, segments=segments,
    )
    ids = [row["id"] for row in r.f.services.generation.list_segments(p.source["id"])["items"]]
    assert len(ids) == len(KINDS)
    with r.f.case.database.immediate_session() as session:
        for segment_id, kind, text in zip(ids, KINDS, texts, strict=True):
            segment = session.get_one(m.GenerationSegment, segment_id)
            xml = _speech_xml(segment_id, kind, text)
            if xml is not None:
                segment.speech_plan_json = {"speech_xml": xml}
    actor, _token = api_actor(r.f.case, r.f.users.alice_id, r.f.users.workspace_id)
    with execution_actor_scope(actor), r.f.case.database.immediate_session() as session:
        snapshot, _digest = r.f.services.workspace_settings.resolve_in_session(
            session, p.source["id"],
        )
        freeze_speech_snapshot(session, plan["active_revision_id"], snapshot)
    return Plan(c, plan["active_revision_id"], ids, snapshot), actor


def _uncached(*args, **kwargs):
    """Resolve capabilities with any cache dropped.

    This strips the cache from every capability lookup in the cast runtime,
    including those that already used it before the change, so the reference
    builds the catalogue more often than the old code did. Identity equality
    with it shows the cache never changes a result.
    """
    original = speech_performance.resolve_capabilities
    settings = args[0]
    endpoint = args[1] if len(args) > 1 else kwargs.get("endpoint")
    return original(settings, endpoint)


def _without_proof_memo(session, session_id, binding, settings, resolved, *args, _proofs=None, **kwargs):
    """The retained-pin validator as it behaved before proofs were remembered."""
    return _validate(session, session_id, binding, settings, resolved, *args, **kwargs)


_validate = cast_runtime.validate_retained_cast_resolution


def _reference_patches(stack: ExitStack) -> None:
    stack.enter_context(patch.object(cast_runtime, "resolve_capabilities", _uncached))
    stack.enter_context(
        patch.object(cast_runtime, "validate_retained_cast_resolution", _without_proof_memo)
    )


def _count_builds():
    return patch.object(
        tts_service_catalogue,
        "_default_service_config_templates",
        wraps=tts_service_catalogue._default_service_config_templates,
    )


def reference_pass(plan: Plan, actor, session_factory, *, one_at_a_time=True):
    """No catalogue cache and no proof memo; by default each segment in a fresh context."""
    result: dict[str, dict[str, Any]] = {}
    with ExitStack() as stack:
        _reference_patches(stack)
        stack.enter_context(execution_actor_scope(actor))
        session = stack.enter_context(session_factory())
        if not one_at_a_time:
            return plan.run(session)
        for segment_id in plan.ids:
            segment = session.get_one(m.GenerationSegment, segment_id)
            context = AudioIdentityContext(
                session, plan.snapshot, _live_cast_material=False,
            )
            result[segment_id] = context.for_segment(segment)
    return result


def measured_pass(plan: Plan, actor, session_factory, segment_ids=None):
    statements: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    engine = plan.c.r.f.case.database.engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        with execution_actor_scope(actor), _count_builds() as build, \
                session_factory() as session:
            identities = plan.run(session, segment_ids)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return identities, build.call_count, len(statements)


def _factory(plan: Plan) -> Callable:
    return plan.c.r.f.case.database.immediate_session


def test_cast_identities_match_the_uncached_reference(varied_plan):
    plan, actor = varied_plan
    reference = reference_pass(plan, actor, _factory(plan))
    whole = reference_pass(plan, actor, _factory(plan), one_at_a_time=False)
    identities, _builds, _statements = measured_pass(plan, actor, _factory(plan))
    assert identities == reference == whole
    # Listings and history read with live cast material, one shared context.
    with execution_actor_scope(actor), _factory(plan)() as session:
        context = AudioIdentityContext(session, plan.snapshot)
        live = {
            segment_id: context.for_segment(session.get_one(m.GenerationSegment, segment_id))
            for segment_id in plan.ids
        }
    assert live == identities
    assert all("performance_error" not in value for value in identities.values()), identities
    assert len({value["settings_hash"] for value in identities.values()}) >= 3
    by_kind = dict(zip(plan.ids, KINDS, strict=True))
    fred = next(i for i, kind in by_kind.items() if kind == "fred")
    wilma = next(i for i, kind in by_kind.items() if kind == "wilma")
    assert identities[fred]["voice_reference_hash"] != identities[wilma]["voice_reference_hash"]
    # A single-part cast identity hashes the retained pin of its character
    # (`_with_performance`: one request, references sorted by hash).
    for segment_id, pin in ((fred, plan.c.fred.pin), (wilma, plan.c.wilma.pin)):
        retained = _hash({"retained_content_hash": pin["content_hash"]})
        assert identities[segment_id]["voice_reference_hash"] == _hash([[retained]])


def test_identity_pass_cost_stays_bounded(varied_plan):
    plan, actor = varied_plan
    _identities, builds, full = measured_pass(plan, actor, _factory(plan))
    _identities, _builds, half = measured_pass(
        plan, actor, _factory(plan), segment_ids=plan.ids[:6],
    )
    # The catalogue is built once per distinct settings input, never per span.
    assert builds <= 2, builds
    # Six more segments that repeat the first six kinds add a small constant
    # number of statements each (2 measured), not the 22 each of the old proof.
    assert full - half <= 6 * 6, (full, half)


def test_deleted_pin_fails_identically_for_every_user_of_it(varied_plan):
    plan, actor = varied_plan
    c = plan.c
    with c.r.f.case.database.immediate_session() as session:
        session.get_one(m.Artifact, c.fred.pin["artifact_id"]).state = "deleted"
    one_at_a_time = reference_pass(plan, actor, _factory(plan))
    whole = reference_pass(plan, actor, _factory(plan), one_at_a_time=False)
    identities, _builds, _statements = measured_pass(plan, actor, _factory(plan))
    # A shared context keeps the first failure for the whole revision, exactly
    # as before; the change must not alter which segments carry the error.
    assert identities == whole
    users = [
        segment_id for segment_id, kind in zip(plan.ids, KINDS, strict=True)
        if kind in {"fred", "both", "german"}
    ]
    assert len(users) == 6
    for segment_id in users:
        assert identities[segment_id].get("performance_error"), identities[segment_id]
        assert identities[segment_id] == one_at_a_time[segment_id]
    assert len({identities[segment_id]["performance_error"] for segment_id in users}) == 1
    for segment_id, kind in zip(plan.ids, KINDS, strict=True):
        if kind in {"narration", "wilma", "uncast"}:
            assert "performance_error" not in one_at_a_time[segment_id]


FRED_SLOT = 'cast:["characters","fred"]'


def _fred_resolution(plan: Plan, session):
    settings = performance_runtime_settings(plan.snapshot)
    resolved = cast_runtime.resolve_project_cast_binding(
        session, plan.c.p.source["id"], plan.c.binding, settings, FRED_SLOT,
    )
    assert resolved is not None
    return settings, resolved


def test_proof_memo_skips_only_repeated_successful_proofs(varied_plan):
    plan, actor = varied_plan
    sid = plan.c.p.source["id"]
    with execution_actor_scope(actor), _factory(plan)() as session:
        settings, resolved = _fred_resolution(plan, session)
        proofs: set = set()
        first = cast_runtime.validate_retained_cast_resolution(
            session, sid, plan.c.binding, settings, resolved, {}, _proofs=proofs,
        )
        assert {key[0] for key in proofs} == {"access", "input"}
        with patch(
            "pandrator.web.speech_inputs.check_speech_input",
            side_effect=AssertionError("a proven pin was checked again"),
        ), patch(
            "pandrator.web.access.current_session_policy.require_current_session_access",
            side_effect=AssertionError("a proven session was checked again"),
        ):
            again = cast_runtime.validate_retained_cast_resolution(
                session, sid, plan.c.binding, settings, resolved, {}, _proofs=proofs,
            )
        assert again == first


def test_proof_memo_never_hides_a_settings_mismatch(varied_plan):
    plan, actor = varied_plan
    sid = plan.c.p.source["id"]
    with execution_actor_scope(actor), _factory(plan)() as session:
        settings, resolved = _fred_resolution(plan, session)
        proofs: set = set()
        cast_runtime.validate_retained_cast_resolution(
            session, sid, plan.c.binding, settings, resolved, {}, _proofs=proofs,
        )
        other = {**settings, "model": "another-model", "xtts_model": "another-model"}
        for memo in (None, proofs):
            with pytest.raises(ValueError, match="incompatible"):
                cast_runtime.validate_retained_cast_resolution(
                    session, sid, plan.c.binding, other, resolved, {}, _proofs=memo,
                )


def test_failed_access_and_pin_proofs_are_not_remembered(varied_plan):
    plan, actor = varied_plan
    sid = plan.c.p.source["id"]
    with execution_actor_scope(actor), _factory(plan)() as session:
        settings, resolved = _fred_resolution(plan, session)
        proofs: set = set()
        with patch(
            "pandrator.web.access.current_session_policy.require_current_session_access",
            side_effect=AccessDenied(),
        ) as access:
            for _attempt in range(2):
                with pytest.raises(AccessDenied):
                    cast_runtime.validate_retained_cast_resolution(
                        session, sid, plan.c.binding, settings, resolved, {}, _proofs=proofs,
                    )
        assert access.call_count == 2 and proofs == set()
        session.get_one(m.Artifact, plan.c.fred.pin["artifact_id"]).state = "deleted"
        session.flush()
        errors = []
        for _attempt in range(2):
            with pytest.raises(ValueError) as raised:
                cast_runtime.validate_retained_cast_resolution(
                    session, sid, plan.c.binding, settings, resolved, {}, _proofs=proofs,
                )
            errors.append(str(raised.value))
        assert errors[0] == errors[1]
        assert {key[0] for key in proofs} == {"access"}


def _freeze_fresh(plan: Plan, actor, *, reference: bool, carried=None):
    """Freeze the revision once from a fresh snapshot; return it, its builds and pin proofs."""
    r = plan.c.r
    with ExitStack() as stack:
        stack.enter_context(execution_actor_scope(actor))
        session = stack.enter_context(r.f.case.database.immediate_session())
        snapshot, _digest = r.f.services.workspace_settings.resolve_in_session(
            session, plan.c.p.source["id"],
        )
        build = stack.enter_context(_count_builds())
        proof = stack.enter_context(patch(
            "pandrator.web.speech_inputs.check_speech_input",
            wraps=speech_inputs.check_speech_input,
        ))
        if reference:
            _reference_patches(stack)
        freeze_speech_snapshot(
            session, plan.revision_id, snapshot, _carried_cast_snapshot=carried,
        )
    return snapshot, build.call_count, proof.call_count


@pytest.mark.parametrize("carry", [False, True])
def test_cast_freeze_matches_the_uncached_reference(varied_plan, carry):
    plan, actor = varied_plan
    carried = plan.snapshot["generation_control_snapshot"] if carry else None
    reference, reference_builds, reference_proofs = _freeze_fresh(
        plan, actor, reference=True, carried=carried,
    )
    frozen, builds, proofs = _freeze_fresh(plan, actor, reference=False, carried=carried)
    # The freeze writes no timestamp or generated id into the snapshot, so every
    # frozen key (cast snapshot, performance snapshot, language bindings) is compared.
    assert frozen.keys() == reference.keys()
    assert frozen["generation_control_snapshot"]["resolved_bindings"]
    for key in frozen:
        assert frozen[key] == reference[key], key
    # Measured: 3 builds against 18 (fresh) and 32 (carried) in the reference,
    # and 2 pin proofs against 10 in the carried reference.
    assert builds <= 4 and builds < reference_builds, (builds, reference_builds)
    if carry:
        # Each carried pin (Fred's and Wilma's) is proven once, not once per segment.
        assert proofs == 2 and reference_proofs > proofs, (proofs, reference_proofs)


def _with_segment_voices(plan: Plan) -> dict[str, Any]:
    """Give every segment its own frozen voice choice, retained from Fred's pin."""
    from copy import deepcopy

    snapshot = deepcopy(plan.snapshot)
    frozen = snapshot["generation_control_snapshot"]
    fred_key = cast_runtime.cast_resolution_key(plan.c.binding, FRED_SLOT, schema_version=2)
    for segment_id in plan.ids:
        entry = frozen["segments"].setdefault(segment_id, {})
        resolutions = entry.get("resolved_bindings", frozen["resolved_bindings"])
        fred = resolutions.get(fred_key) or frozen["resolved_bindings"][fred_key]
        slot = cast_runtime.cast_resolution_key(
            plan.c.binding, f"segment:{segment_id}", schema_version=2,
        )
        resolutions[slot] = deepcopy(fred)
        entry["voice_binding"] = dict(plan.c.binding)
    return snapshot


def test_segment_voices_build_the_catalogue_once_per_pass(varied_plan):
    plan, actor = varied_plan
    voiced = Plan(plan.c, plan.revision_id, plan.ids, _with_segment_voices(plan))
    reference = reference_pass(voiced, actor, _factory(plan), one_at_a_time=False)
    identities, builds, _statements = measured_pass(voiced, actor, _factory(plan))
    assert identities == reference
    assert all("performance_error" not in value for value in identities.values()), identities
    assert builds <= 2, builds


def test_voice_change_pauses_build_the_catalogue_once(varied_plan):
    from copy import deepcopy

    from pandrator.web.speech_boundaries import freeze_boundaries

    plan, actor = varied_plan
    snapshot = deepcopy(plan.snapshot)
    snapshot["audio"] = {**(snapshot.get("audio") or {}), "voice_change_silence_ms": 250}
    with execution_actor_scope(actor), _factory(plan)() as session:
        reference = deepcopy(snapshot)
        with ExitStack() as stack:
            _reference_patches(stack)
            freeze_boundaries(session, plan.revision_id, reference)
        with _count_builds() as build:
            freeze_boundaries(session, plan.revision_id, snapshot)
    assert snapshot["speech_boundaries"] == reference["speech_boundaries"]
    assert any(
        entry["silence_after_ms"] >= 250 for entry in snapshot["speech_boundaries"].values()
    )
    assert build.call_count <= 1, build.call_count


def test_proof_memo_consults_a_pending_proof_once_per_revision(varied_plan):
    plan, actor = varied_plan
    sid = plan.c.p.source["id"]
    calls: list[str | None] = []

    def pending(_session, _owner, revision_id, _pin, _binding):
        calls.append(revision_id)
        return revision_id == "proven-revision"

    with execution_actor_scope(actor), _factory(plan)() as session:
        settings, resolved = _fred_resolution(plan, session)
        proofs: set = set()
        with patch(
            "pandrator.web.speech_inputs.check_speech_input",
            wraps=speech_inputs.check_speech_input,
        ) as graph:
            for revision_id in (
                "proven-revision", "proven-revision", "other-revision", "other-revision",
            ):
                cast_runtime.validate_retained_cast_resolution(
                    session, sid, plan.c.binding, settings, resolved, {},
                    _pending_proof=pending, _pending_revision_id=revision_id, _proofs=proofs,
                )
    # A hit skips the pending proof, another revision is proven on its own, and
    # a pending proof that declines falls through to the graph check once.
    assert calls == ["proven-revision", "other-revision"]
    assert graph.call_count == 1
