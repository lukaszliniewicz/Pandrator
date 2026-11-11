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

The pass also repeated language and controls work per segment.  Measured with
``_count_work`` on the code before a pass remembered language support and read
the controls in place, the identity pass over 12 segments made 14
``package_metadata`` calls, 14 language-support lookups that reach
``_selected_endpoint`` and 12 deep copies of a controls mapping (7, 7 and 6 for
6 segments).  The cast freeze over the same 12 segments made 12, 12 and 25 (49
when carrying a snapshot).  Now the identity pass makes 1, 1 and 0 for 6 and
for 12 segments, and the fresh freeze makes 1, 1 and 2; a carried freeze still
stores one copy of the controls per segment and keeps the deep copies it needs.
``character_validations`` counts ``_character_model`` calls, one per character
entry: the reference pass (no memos, controls copied) validates the fixture's
three characters 14 times over 12 segments (42 calls), and the identity pass
validates them once (3 calls), also for 6.
"""

from __future__ import annotations

import builtins
import copy
from collections.abc import Callable, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import event

from pandrator.logic import (
    audio_cpp_catalogue,
    speech_performance,
    tts_language_preflight,
    tts_service_catalogue,
)
from pandrator.web import (
    generation_audio_identity,
    generation_controls,
    generation_rendering,
    speech_inputs,
)
from pandrator.web import generation_cast_runtime as cast_runtime
from pandrator.web import models as m
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


_language_support = tts_language_preflight.resolve_tts_language_support
_build_render_parts = generation_rendering.build_render_parts


def _language_support_without_memo(settings, endpoint=None, _service_config_cache=None):
    """Language support as it was resolved before a pass remembered it."""
    return _language_support(settings, endpoint)


def _build_render_parts_copying_controls(*args, **kwargs):
    """Render parts as they were planned before controls were read in place."""
    kwargs["_read_only_controls"] = False
    kwargs["_validated_controls"] = None
    return _build_render_parts(*args, **kwargs)


class _BuiltinDictCheck(type):
    def __instancecheck__(cls, instance: object) -> bool:
        return isinstance(instance, builtins.dict)


class _CopyingDict(dict, metaclass=_BuiltinDictCheck):
    """``dict`` as the freeze saw it before segments shared one controls copy.

    Patched over the name ``dict`` in the cast runtime: a controls mapping (one
    with ``characters`` and ``cast``) is deep-copied, so every segment gets its
    own copy as it used to; everything else, ``isinstance`` checks included, is
    the builtin.
    """

    def __new__(cls, *args, **kwargs):
        if args and _is_controls(args[0]):
            return cast_runtime.deepcopy(args[0])
        return builtins.dict(*args, **kwargs)


def _copy_record_by_deepcopy(record, memo=None):
    return copy.deepcopy(record, memo)


def _reference_patches(stack: ExitStack) -> None:
    stack.enter_context(patch.object(cast_runtime, "dict", _CopyingDict, create=True))
    stack.enter_context(
        patch.object(speech_performance, "copy_record", _copy_record_by_deepcopy)
    )
    stack.enter_context(
        patch.object(generation_audio_identity, "copy_record", _copy_record_by_deepcopy)
    )
    stack.enter_context(patch.object(cast_runtime, "resolve_capabilities", _uncached))
    stack.enter_context(patch.object(
        tts_language_preflight, "resolve_tts_language_support", _language_support_without_memo,
    ))
    stack.enter_context(patch.object(
        generation_rendering, "build_render_parts", _build_render_parts_copying_controls,
    ))
    stack.enter_context(
        patch.object(cast_runtime, "validate_retained_cast_resolution", _without_proof_memo)
    )


def _count_builds():
    return patch.object(
        tts_service_catalogue,
        "_default_service_config_templates",
        wraps=tts_service_catalogue._default_service_config_templates,
    )


@dataclass
class Work:
    """Per-segment work a pass repeats: lookups and copies of the controls."""

    package_metadata: int = 0
    endpoint_lookups: int = 0
    controls_copies: int = 0
    # ``_character_model`` calls: one per character entry validated.
    character_validations: int = 0


def _is_controls(value: Any) -> bool:
    return isinstance(value, Mapping) and "characters" in value and "cast" in value


@contextmanager
def _count_work():
    """Count package-metadata lookups, endpoint lookups for language support and
    deep copies of a controls mapping made by the rendering and cast runtime."""
    work = Work()
    real_package = audio_cpp_catalogue.package_metadata
    real_endpoint = tts_language_preflight._selected_endpoint
    real_character = generation_controls._character_model

    def package_metadata(*args, **kwargs):
        work.package_metadata += 1
        return real_package(*args, **kwargs)

    def selected_endpoint(*args, **kwargs):
        work.endpoint_lookups += 1
        return real_endpoint(*args, **kwargs)

    def character_model(*args, **kwargs):
        work.character_validations += 1
        return real_character(*args, **kwargs)

    def counting_deepcopy(value, *args, **kwargs):
        if _is_controls(value):
            work.controls_copies += 1
        return copy.deepcopy(value, *args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch.object(audio_cpp_catalogue, "package_metadata", package_metadata))
        stack.enter_context(
            patch.object(tts_language_preflight, "_selected_endpoint", selected_endpoint)
        )
        stack.enter_context(patch.object(generation_controls, "_character_model", character_model))
        stack.enter_context(patch.object(cast_runtime, "deepcopy", counting_deepcopy))
        stack.enter_context(patch.object(generation_rendering, "deepcopy", counting_deepcopy))
        yield work


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


def work_pass(plan: Plan, actor, session_factory, segment_ids=None) -> Work:
    with execution_actor_scope(actor), _count_work() as work, session_factory() as session:
        plan.run(session, segment_ids)
    return work


def _factory(plan: Plan) -> Callable:
    return plan.c.r.f.case.database.immediate_session


def test_cast_identities_match_the_uncached_reference(varied_plan):
    plan, actor = varied_plan
    # Identity passes read the pinned controls in place; none may write into them.
    unchanged = copy.deepcopy(plan.snapshot)
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
    assert plan.snapshot == unchanged
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


def _freeze_fresh(
    plan: Plan, actor, *, reference: bool, carried=None, work_out: list[Work] | None = None,
):
    """Freeze the revision once from a fresh snapshot; return it, its builds and pin proofs.

    ``work_out`` receives the lookups and controls copies the freeze made."""
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
        work = stack.enter_context(_count_work())
        if work_out is not None:
            work_out.append(work)
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


def test_identity_pass_repeats_no_lookup_or_copy_per_segment(varied_plan):
    plan, actor = varied_plan
    full = work_pass(plan, actor, _factory(plan))
    half = work_pass(plan, actor, _factory(plan), segment_ids=plan.ids[:6])
    # Six more segments that repeat the first six kinds add nothing, and the
    # whole pass looks each thing up at most a small constant number of times.
    assert full == half, (full, half)
    assert full.package_metadata <= 2 and full.endpoint_lookups <= 2, full
    assert full.controls_copies <= 2, full
    assert full.character_validations <= 12, full
    with _count_work() as reference:
        reference_pass(plan, actor, _factory(plan))
    assert reference.package_metadata >= len(KINDS) and reference.controls_copies >= len(KINDS)
    assert reference.character_validations > full.character_validations


def test_cast_freeze_repeats_no_lookup_or_copy_per_segment(varied_plan):
    plan, actor = varied_plan
    out: list[Work] = []
    _freeze_fresh(plan, actor, reference=False, work_out=out)
    work = out[0]
    assert work.package_metadata <= 2 and work.endpoint_lookups <= 2, work
    # The controls are copied once for the shared segments and once for the snapshot.
    assert work.controls_copies <= 4 < len(KINDS), work
    assert work.character_validations <= 12, work
    reference_out: list[Work] = []
    _freeze_fresh(plan, actor, reference=True, work_out=reference_out)
    assert reference_out[0].controls_copies >= len(KINDS), reference_out[0]


def test_freezing_neither_mutates_the_controls_nor_changes_the_snapshot(varied_plan):
    from copy import deepcopy

    from pandrator.web.generation_controls import get_generation_controls

    plan, actor = varied_plan
    sid = plan.c.p.source["id"]
    received: list[tuple[Any, Any]] = []

    def recording(*args, **kwargs):
        controls = get_generation_controls(*args, **kwargs)
        received.append((controls, deepcopy(controls)))
        return controls

    with execution_actor_scope(actor), _factory(plan)() as session:
        before = deepcopy(get_generation_controls(session, sid))
        scoped = {plan.ids[1]: deepcopy(before)}
        scoped[plan.ids[1]]["cast"] = {}
        passed = deepcopy(scoped)
        settings = performance_runtime_settings(plan.snapshot)
        frozen = []
        for _attempt in range(2):
            snapshot = deepcopy(plan.snapshot)
            with patch.object(cast_runtime, "get_generation_controls", recording):
                cast_runtime.freeze_cast_snapshot(
                    session, plan.revision_id, snapshot, settings, controls_by_segment=scoped,
                )
            frozen.append(snapshot["generation_control_snapshot"])
        # The object each freeze actually received is untouched by it.
        assert len(received) == 2
        for controls, original in received:
            assert controls == original == before
        assert scoped == passed
    assert frozen[0] == frozen[1]
    entries = frozen[0]["segments"]
    assert entries[plan.ids[1]]["controls"]["cast"] == {}
    assert all("controls" not in entries[i] for i in plan.ids if i != plan.ids[1])
    assert frozen[0]["controls"] == before


def test_a_package_without_a_tts_route_raises_each_time_and_is_not_remembered():
    settings = {"service": "audio.cpp", "model": "edit_only_package", "language": "en"}
    package = {"language_support_by_operation": {"edit": {"languages": ["en"]}}}
    cache: dict = {}
    with patch.object(audio_cpp_catalogue, "package_metadata", return_value=package) as lookup:
        for _attempt in range(2):
            with pytest.raises(ValueError, match="has no text-to-speech request route"):
                tts_language_preflight.validate_tts_language(settings, _service_config_cache=cache)
    assert lookup.call_count == 2
    assert not any(key[0] == "tts_language_support" for key in cache)


def test_unknown_models_and_missing_models_are_remembered_with_their_record():
    cache: dict = {}
    for settings in (
        {"service": "Silero", "silero_model": "v9_not_a_model"},
        {"service": "future-service"},
    ):
        first = tts_language_preflight.resolve_tts_language_support(
            settings, _service_config_cache=cache,
        )
        again = tts_language_preflight.resolve_tts_language_support(
            settings, _service_config_cache=cache,
        )
        assert first == again == tts_language_preflight.resolve_tts_language_support(settings)
    with patch.object(
        tts_language_preflight, "_selected_endpoint",
        side_effect=AssertionError("a remembered lookup was repeated"),
    ):
        tts_language_preflight.resolve_tts_language_support(
            {"service": "future-service"}, _service_config_cache=cache,
        )


_ENDPOINTS = [
    {"id": name, "api_base": f"http://{name}.example/v1", "adapter": "openai_compatible",
     "default_model": f"model-{name}"}
    for name in ("alpha", "beta")
]
# Alpha's model moved: the same selectors, different catalogue inputs.
_MOVED_ENDPOINTS = [{**_ENDPOINTS[0], "default_model": "model-gamma"}, _ENDPOINTS[1]]
# Each setting the lookup reads, and the catalogue inputs, separate at least
# one pair of variants that differ in nothing else.
_LANGUAGE_VARIANTS = [
    {"service": "audio.cpp", "model": "qwen3_tts_1_7b_base_q8_0"},
    {"service": "audio.cpp", "model": "magpie_tts_q8_0"},
    {"service": "Silero", "silero_model": "v3_en"},
    {"service": "Silero", "silero_model": "v5_cis_base"},
    {"service": "Silero", "model": "v3_en"},
    {"service": "ElevenLabs", "model": "v3_en"},
    {"silero_model": "v3_en"},
    {"tts_service": "Silero", "silero_model": "v3_en"},
    {"tts_service": "Silero", "silero_model": "v5_cis_base"},
    {"service": "Silero", "xtts_model": "v3_en"},
    {"service": "Silero", "xtts_model": "v5_cis_base"},
    {"service": "ElevenLabs", "elevenlabs_model": "eleven_multilingual_v2"},
    {"service": "ElevenLabs", "elevenlabs_model": "eleven_flash_v2_5"},
    {"service": "audio.cpp", "model": "magpie_tts_q8_0", "audio_cpp_base_url": "http://a:1"},
    {"service": "zzz", "openai_audio_endpoint": "alpha", "provider_configs": _ENDPOINTS},
    {"service": "zzz", "openai_audio_endpoint": "beta", "provider_configs": _ENDPOINTS},
    {"service": "zzz", "openai_audio_endpoint": "alpha", "provider_configs": _MOVED_ENDPOINTS},
]


def _remember_variants() -> tuple[int, dict]:
    """Look every variant up in one cache; return the distinct keys and the cache."""
    cache: dict = {}
    for settings in _LANGUAGE_VARIANTS:
        remembered = tts_language_preflight.resolve_tts_language_support(
            settings, _service_config_cache=cache,
        )
        assert remembered == tts_language_preflight.resolve_tts_language_support(settings)
    return len({key for key in cache if key[0] == "tts_language_support"}), cache


class _NoCatalogueKey:
    """The catalogue module with its cache key dropped from the language memo."""

    def __getattr__(self, name: str) -> Any:
        return getattr(tts_service_catalogue, name)

    @staticmethod
    def _service_config_cache_key(_settings) -> str:
        return ""


def test_language_support_is_keyed_by_every_setting_it_reads():
    count, cache = _remember_variants()
    assert count == len(_LANGUAGE_VARIANTS)
    by_endpoint = [
        tts_language_preflight.resolve_tts_language_support(
            {"service": "zzz", "openai_audio_endpoint": name, "provider_configs": configs},
            _service_config_cache=cache,
        )["model_id"]
        for name, configs in (
            ("alpha", _ENDPOINTS), ("beta", _ENDPOINTS), ("alpha", _MOVED_ENDPOINTS),
        )
    ]
    assert by_endpoint == ["model-alpha", "model-beta", "model-gamma"]


@pytest.mark.parametrize(
    "dropped", [*tts_language_preflight._LANGUAGE_SUPPORT_SETTING_KEYS, "catalogue"],
)
def test_language_variants_notice_any_key_component_dropped(dropped):
    """Without any one key component, two variants share a memo entry."""
    if dropped == "catalogue":
        narrowed = patch.object(tts_language_preflight, "catalogue", _NoCatalogueKey())
    else:
        narrowed = patch.object(
            tts_language_preflight, "_LANGUAGE_SUPPORT_SETTING_KEYS",
            tuple(
                name for name in tts_language_preflight._LANGUAGE_SUPPORT_SETTING_KEYS
                if name != dropped
            ),
        )
    with narrowed, pytest.raises(AssertionError):
        count, _cache = _remember_variants()
        assert count == len(_LANGUAGE_VARIANTS)


def test_a_remembered_language_support_is_a_private_copy():
    cache: dict = {}
    settings = {"service": "audio.cpp", "model": "qwen3_tts_1_7b_base_q8_0"}
    first = tts_language_preflight.resolve_tts_language_support(
        settings, _service_config_cache=cache,
    )
    assert first
    expected = copy.deepcopy(first)
    first.clear()
    again = tts_language_preflight.resolve_tts_language_support(
        settings, _service_config_cache=cache,
    )
    assert again == expected
    again.clear()
    assert tts_language_preflight.resolve_tts_language_support(
        settings, _service_config_cache=cache,
    ) == expected


def test_cast_voice_resolution_remembers_validated_controls_by_identity():
    from pandrator.web.generation_controls import resolve_cast_voice

    controls = {
        "characters": [{"id": "fred", "display_name": "Fred"}],
        "cast": {"characters": {"fred": {"voice": "fred-voice"}}, "narrator": {"voice": "nar"}},
    }
    memo: dict = {}
    with _count_work() as work:
        first = resolve_cast_voice(controls, speaker_id="fred", dialogue=True, _validated=memo)
        again = resolve_cast_voice(controls, speaker_id="fred", dialogue=True, _validated=memo)
        narrator = resolve_cast_voice(controls, _validated=memo)
    assert work.character_validations == 1
    assert first == again == resolve_cast_voice(controls, speaker_id="fred", dialogue=True)
    assert narrator == resolve_cast_voice(controls)
    first["binding"]["voice"] = "changed"
    assert resolve_cast_voice(controls, speaker_id="fred", _validated=memo) == again
    # Another characters list is validated on its own, whatever the memo holds.
    other = {**controls, "characters": [{"id": "wilma", "display_name": "Wilma"}], "cast": controls["cast"]}
    with pytest.raises(ValueError, match="Unknown character ID: fred"):
        resolve_cast_voice(other, speaker_id="fred", _validated=memo)
    assert resolve_cast_voice(other, speaker_id="wilma", _validated=memo)["source"] == "narrator"


def test_cast_voice_resolution_does_not_remember_a_failed_validation():
    from pandrator.web.generation_controls import resolve_cast_voice

    controls = {"characters": [{"id": "fred", "display_name": "Fred"}] * 2, "cast": {}}
    memo: dict = {}
    for _attempt in range(2):
        with pytest.raises(ValueError, match="Duplicate character ID: fred"):
            resolve_cast_voice(controls, _validated=memo)
    assert memo == {}
    controls = {"characters": [{"id": "fred", "display_name": "Fred"}], "cast": {"narrator": "not a binding"}}
    for _attempt in range(2):
        with pytest.raises(ValueError):
            resolve_cast_voice(controls, _validated=memo)
    assert "cast" not in memo
