"""A recast preview's cost stays bounded, and an abandoned preview stops.

A preview used to rebuild and deep-copy the whole TTS service catalogue for every
cast span and re-prove every retained pin for every segment, so a long book took
minutes; it is a read-only POST, so its client leaving never stopped it. It also
worked out the same cast fingerprint again for every segment sharing a speaker,
and validated the whole character list again on every cast lookup. The tests
count catalogue builds, access checks, pin checks, fingerprints, cast
resolutions, character validations and segment steps, never time.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from typing import Any
from unittest import mock

import pytest
from sqlalchemy import select

from pandrator.logic import tts_service_catalogue
from pandrator.web import generation_controls, generation_recast, speech_inputs
from pandrator.web import models as m
from pandrator.web.abandoned_requests import (
    RequestAbandoned,
    bind_disconnect_probe,
    reset_disconnect_probe,
)
from pandrator.web.access import current_session_policy
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.accounts.execution_actor import execution_actor_scope
from pandrator.web.generation_audio_identity import plan_audio_identities
from pandrator.web.generation_cast_runtime import cast_resolution_key
from pandrator.web.projects.identity import session_project
from pandrator.web.speech_input_contracts import (
    PROJECT_SPEECH_BINDING_KEY,
    PROJECT_SPEECH_INPUT_KEY,
    PROJECT_SPEECH_SELECTOR_KEY,
    PROJECT_SPEECH_SOURCE_KEY,
    canonical_json,
)
from pandrator.web.speech_plan_workspace import freeze_speech_snapshot
from tests.test_abandoned_reads import TurnsTrueAfter
from tests.test_audio_identity_cost import (  # noqa: F401
    Plan,
    case,
    cast_project,
    design,
    no_network,
    project,
    retained,
    varied_plan,
)


class Counts:
    def __init__(self) -> None:
        self.builds = 0
        self.access_checks = 0
        self.pin_checks = 0


@contextmanager
def counting() -> Iterator[Counts]:
    counts = Counts()
    real_build = tts_service_catalogue._default_service_config_templates
    real_access = current_session_policy.require_current_session_access
    real_pin = speech_inputs.check_speech_input

    def build(*args: Any, **kwargs: Any):
        counts.builds += 1
        return real_build(*args, **kwargs)

    def access(*args: Any, **kwargs: Any):
        counts.access_checks += 1
        return real_access(*args, **kwargs)

    def pin(*args: Any, **kwargs: Any):
        counts.pin_checks += 1
        return real_pin(*args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(tts_service_catalogue, "_default_service_config_templates", build)
        )
        stack.enter_context(
            mock.patch.object(current_session_policy, "require_current_session_access", access)
        )
        stack.enter_context(mock.patch.object(speech_inputs, "check_speech_input", pin))
        yield counts


def _dropping(function, *names: str):
    def call(*args: Any, **kwargs: Any):
        for name in names:
            kwargs.pop(name, None)
        return function(*args, **kwargs)

    return call


@contextmanager
def old_effective() -> Iterator[None]:
    """The preview as it was: no catalogue cache and no proof memo per call."""
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(
            generation_recast, "apply_resolved_binding",
            _dropping(generation_recast.apply_resolved_binding, "_service_config_cache"),
        ))
        for name in ("resolve_project_cast_binding", "validate_retained_cast_resolution"):
            stack.enter_context(mock.patch.object(
                generation_recast, name,
                _dropping(getattr(generation_recast, name), "_proofs", "_registry"),
            ))
        yield


def _preview(plan: Plan, actor) -> dict[str, Any]:
    services = plan.c.r.f.services
    with execution_actor_scope(actor):
        return generation_recast.prepare_recast(
            plan.c.r.f.case.database, services.workspace_settings, plan.c.p.source["id"],
        )["preview"]


def _cast_segments(plan: Plan) -> int:
    with plan.c.r.f.case.database.session() as session:
        return len(session.scalars(select(m.GenerationSegment.id).where(
            m.GenerationSegment.plan_revision_id == plan.revision_id,
            m.GenerationSegment.removed.is_(False),
        )).all())


def test_preview_matches_the_old_preview_with_bounded_work(varied_plan):  # noqa: F811
    plan, actor = varied_plan
    with unmemoized(), old_effective(), counting() as old:
        reference = _preview(plan, actor)
    with counting() as new:
        preview = _preview(plan, actor)
    assert preview == reference
    segments = _cast_segments(plan)
    assert segments >= 12
    # The old preview rebuilt the catalogue for every resolved span and proved
    # every retained pin and the session's access again for every segment
    # (14 builds, 20 pin checks and 40 access checks on this plan).
    assert old.builds > segments, vars(old)
    assert old.pin_checks > segments, vars(old)
    assert old.access_checks > 2 * segments, vars(old)
    # Now the catalogue is built once per distinct settings input, and each pin
    # (two characters here) and the session's access once per preview.
    assert new.builds <= 4, vars(new)
    assert new.pin_checks <= 2, vars(new)
    assert new.access_checks <= 2, vars(new)


class CastWork:
    def __init__(self) -> None:
        self.fingerprints = 0
        self.project_resolutions = 0
        self.character_validations = 0


@contextmanager
def counting_cast_work() -> Iterator[CastWork]:
    """Count fingerprints worked out, current cast resolutions and validated characters."""
    work = CastWork()
    real_effective = generation_recast._effective
    real_resolution = generation_recast.resolve_project_cast_binding
    real_character = generation_controls._character_model

    def effective(*args: Any, **kwargs: Any):
        work.fingerprints += 1
        return real_effective(*args, **kwargs)

    def resolution(*args: Any, **kwargs: Any):
        work.project_resolutions += 1
        return real_resolution(*args, **kwargs)

    def character(*args: Any, **kwargs: Any):
        work.character_validations += 1
        return real_character(*args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(generation_recast, "_effective", effective))
        stack.enter_context(
            mock.patch.object(generation_recast, "resolve_project_cast_binding", resolution)
        )
        stack.enter_context(mock.patch.object(generation_controls, "_character_model", character))
        yield work


class _Forgetful:
    """A pass memo that remembers nothing: every segment computes again."""

    def __call__(self, _key: Any, compute):
        return compute()


@contextmanager
def unmemoized() -> Iterator[None]:
    """The preview as it was before a pass remembered its cast work."""
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(generation_recast, "_PassMemo", _Forgetful))
        stack.enter_context(mock.patch.object(
            generation_recast, "resolve_cast_voice",
            _dropping(generation_recast.resolve_cast_voice, "_validated"),
        ))
        yield


def _record(
    plan: Plan,
    actor,
    segment_ids: list[str],
    *,
    narrator: str | None = None,
    edit: Callable[[dict[str, Any], str], None] | None = None,
) -> str:
    """Store one completed run's recordings for ``segment_ids`` as a generation run
    would have left them, frozen with its own base ``narrator`` voice if given.

    ``edit(frozen, segment_id)`` changes a segment's frozen entry before the
    run's identities are worked out, as a recast run freezes each target's own
    controls and cast resolutions.
    """
    database = plan.c.r.f.case.database
    services = plan.c.r.f.services
    session_id = plan.c.p.source["id"]
    with execution_actor_scope(actor), database.immediate_session() as session:
        owner = session_project(session, session_id)
        snapshot, _digest = services.workspace_settings.resolve_in_session(session, session_id)
        if narrator is not None:
            snapshot["tts"]["voice"] = narrator
        freeze_speech_snapshot(session, plan.revision_id, snapshot, explicit=True)
        if edit is not None:
            for segment_id in segment_ids:
                edit(snapshot["generation_control_snapshot"], segment_id)
        identities = plan_audio_identities(session, plan.revision_id, snapshot)
        snapshot["generation_audio_identities"] = identities
        run = m.GenerationRun(
            session_id=session_id, plan_revision_id=plan.revision_id, status="completed",
            sequence_number=len(session.scalars(select(m.GenerationRun.id).where(
                m.GenerationRun.session_id == session_id,
            )).all()) + 1,
            settings_snapshot_json=deepcopy(snapshot),
        )
        session.add(run)
        session.flush()
        for segment_id in segment_ids:
            artifact = m.Artifact(
                workspace_id=owner.workspace_id, project_id=owner.id, session_id=session_id,
                kind="audio", relative_path=f"{segment_id}.wav", content_hash="waveform",
                metadata_json={
                    "generation_audio_identity": identities[segment_id],
                    "generation_task_run_id": run.id,
                },
            )
            session.add(artifact)
            session.flush()
            session.add(m.AudioTake(
                generation_segment_id=segment_id, generation_run_id=run.id,
                artifact_id=artifact.id, is_active=True, status="completed",
            ))
            session.get_one(m.GenerationSegment, segment_id).status = "completed"
        return run.id


@pytest.fixture
def recorded_plan(varied_plan):  # noqa: F811
    """Every segment recorded, by two runs whose cast snapshots differ."""
    plan, actor = varied_plan
    half = len(plan.ids) // 2
    _record(plan, actor, plan.ids[:half])
    _record(plan, actor, plan.ids[half:], narrator="earlier-narrator")
    return plan, actor


def _selector(preview: dict[str, Any], key: str) -> dict[str, Any]:
    return next(item for item in preview["selectors"] if item["key"] == key)


def test_recorded_preview_matches_the_unmemoized_preview_with_work_per_assignment(
    recorded_plan,
):
    plan, actor = recorded_plan
    with unmemoized(), counting_cast_work() as old:
        reference = _preview(plan, actor)
    with counting_cast_work() as new:
        preview = _preview(plan, actor)
    assert preview == reference
    segments = _cast_segments(plan)
    # The two runs' snapshots differ only in their base narrator voice, so the
    # narrator scopes of the second run's segments changed and the first's did not.
    narrator = _selector(preview, "narrator")
    assert narrator["missing_count"] == 0
    assert 0 < narrator["changed_count"] < narrator["affected_count"], narrator
    # Unmemoized, every scope worked out a current and a recorded fingerprint (28
    # over 12 segments), resolved fred's and wilma's current pins again (10), and
    # every cast lookup validated all three characters again (108 in the preview).
    assert old.fingerprints > 2 * segments, vars(old)
    assert old.project_resolutions >= 10, vars(old)
    assert old.character_validations > 6 * segments, vars(old)
    # Now each distinct assignment is worked out once: three current fingerprints
    # (no cast binding, fred, wilma) and the same three per run (9); fred's and
    # wilma's pins resolve once each; the loop validates each controls object, the
    # current one and each run's, once (9), and the target pass reuses the current
    # one's (27 in all, 33 when it validated again; each target's recorded copy
    # and the freeze make the rest).
    assert new.fingerprints <= 9, vars(new)
    assert new.project_resolutions <= 2, vars(new)
    assert new.character_validations <= 27, vars(new)


@pytest.mark.parametrize("failing", ["current", "recorded"])
def test_a_failing_assignment_fails_every_segment_that_shares_it(recorded_plan, failing):
    plan, actor = recorded_plan
    fred_slot = 'cast:["characters","fred"]'
    real_effective = generation_recast._effective
    raised = []

    def effective(session, context, binding, base, *, frozen=None, slot_key=None, **kwargs):
        if slot_key == fred_slot and (frozen is None) == (failing == "current"):
            raised.append(slot_key)
            if failing == "current":
                raise AccessDenied()
            raise ValueError("The recorded cast reference is no longer owned.")
        return real_effective(
            session, context, binding, base, frozen=frozen, slot_key=slot_key, **kwargs,
        )

    with mock.patch.object(generation_recast, "_effective", effective):
        with unmemoized():
            reference = _preview(plan, actor)
        unmemoized_raises = len(raised)
        raised.clear()
        preview = _preview(plan, actor)
    assert preview == reference
    fred = _selector(preview, "character:fred")
    message = (
        "The requested action is not permitted."
        if failing == "current"
        else "The recorded cast reference is no longer owned."
    )
    assert fred["error"] == message
    assert fred["unverified_count"] == fred["affected_count"] > 0, fred
    # Every one of fred's 6 segments met the failure, but it was worked out once
    # per distinct input: once for the current cast, or once per run for the
    # recordings (both runs froze the same resolution for fred).
    assert unmemoized_raises == fred["affected_count"]
    assert len(raised) == (1 if failing == "current" else 2)


FRED_SLOT = "cast:" + canonical_json(["characters", "fred"])
WILMA_SLOT = "cast:" + canonical_json(["characters", "wilma"])
RETAINED_MARKERS = (
    PROJECT_SPEECH_INPUT_KEY,
    PROJECT_SPEECH_BINDING_KEY,
    PROJECT_SPEECH_SELECTOR_KEY,
    PROJECT_SPEECH_SOURCE_KEY,
)


def _own_entry(frozen: dict[str, Any], segment_id: str) -> dict[str, Any]:
    """The segment's frozen entry, with its own copy of the run's controls and
    cast resolutions, as a recast run freezes them for each target."""
    entry = frozen["segments"][segment_id]
    entry.setdefault("controls", deepcopy(frozen["controls"]))
    entry.setdefault("resolved_bindings", deepcopy(frozen["resolved_bindings"]))
    return entry


def _earlier_narrator(frozen: dict[str, Any], segment_id: str) -> None:
    # A recast freezes every base voice key; this recording's narrator had a
    # plain voice of its own and no retained reference.
    _own_entry(frozen, segment_id)["controls"]["_base_voice_settings"] = {
        **dict.fromkeys(generation_recast._BASE_KEYS), "voice": "earlier-narrator",
    }


def _wilmas_pin_for_fred(frozen: dict[str, Any], segment_id: str) -> None:
    binding = frozen["controls"]["cast"]["characters"]["fred"]
    resolutions = _own_entry(frozen, segment_id)["resolved_bindings"]
    resolutions[cast_resolution_key(binding, FRED_SLOT, schema_version=2)] = deepcopy(
        resolutions[cast_resolution_key(binding, WILMA_SLOT, schema_version=2)]
    )


def _unpinned_fred(frozen: dict[str, Any], segment_id: str) -> dict[str, Any]:
    """Freeze fred without his retained pin, as a plain voice cast records it."""
    entry = _own_entry(frozen, segment_id)
    binding = entry["controls"]["cast"]["characters"]["fred"]
    key = cast_resolution_key(binding, FRED_SLOT, schema_version=2)
    entry["resolved_bindings"][key] = {
        name: value for name, value in entry["resolved_bindings"][key].items()
        if name not in RETAINED_MARKERS
    }
    return entry


def _described_fred(frozen: dict[str, Any], segment_id: str) -> None:
    # The same unpinned resolution under fred's earlier description: only the
    # recorded binding tells this recording from the others.
    entry = _unpinned_fred(frozen, segment_id)
    binding = entry["controls"]["cast"]["characters"]["fred"]
    resolved = entry["resolved_bindings"][cast_resolution_key(binding, FRED_SLOT, schema_version=2)]
    binding["voice_description"] = "An earlier description."
    entry["resolved_bindings"][cast_resolution_key(binding, FRED_SLOT, schema_version=2)] = (
        deepcopy(resolved)
    )


@pytest.mark.parametrize(
    ("edit", "unedited", "selector"),
    [
        (_earlier_narrator, _own_entry, "narrator"),
        (_wilmas_pin_for_fred, _own_entry, "character:fred"),
        (_described_fred, _unpinned_fred, "character:fred"),
    ],
    ids=["base-voice", "resolution", "unpinned-binding"],
)
def test_recordings_of_one_run_keep_their_own_recorded_cast(
    varied_plan, edit, unedited, selector,  # noqa: F811
):
    """A run may freeze different controls or resolutions for its segments, so
    segments of one run that share a speaker can still differ in what they recorded."""
    plan, actor = varied_plan
    edited = plan.ids[::2]
    _record(
        plan, actor, plan.ids,
        edit=lambda frozen, segment_id: (
            edit(frozen, segment_id) if segment_id in edited else unedited(frozen, segment_id)
        ),
    )
    with unmemoized():
        reference = _preview(plan, actor)
    preview = _preview(plan, actor)
    assert preview == reference
    counts = _selector(preview, selector)
    assert "error" not in counts, counts["error"]
    assert counts["missing_count"] == counts["unverified_count"] == 0, counts
    # Only the edited segments recorded something other than today's cast.
    assert 0 < counts["changed_count"] < counts["affected_count"], counts


@contextmanager
def probed(probe) -> Iterator[mock.MagicMock]:
    """Bind ``probe`` as the request's disconnect probe and count segment steps."""
    token = bind_disconnect_probe(probe)
    try:
        with mock.patch.object(
            generation_recast, "_scopes", wraps=generation_recast._scopes,
        ) as scoped:
            yield scoped
    finally:
        reset_disconnect_probe(token)


def _checkpoints_before_the_segments(plan: Plan, actor) -> int:
    """The probe calls a connected preview makes before its first segment's check."""
    connected = TurnsTrueAfter(10**6)
    seen: list[int] = []
    real_scopes = generation_recast._scopes

    def scopes(*args: Any, **kwargs: Any):
        seen.append(connected.calls)
        return real_scopes(*args, **kwargs)

    token = bind_disconnect_probe(connected)
    try:
        with mock.patch.object(generation_recast, "_scopes", scopes):
            _preview(plan, actor)
    finally:
        reset_disconnect_probe(token)
    # Each segment's check runs just before its ``_scopes`` call.
    return seen[0] - 1


def test_preview_stops_at_the_next_segment_once_its_client_leaves(varied_plan):  # noqa: F811
    plan, actor = varied_plan
    segments = _cast_segments(plan)
    connected = TurnsTrueAfter(10**6)
    with probed(connected) as scoped:
        _preview(plan, actor)
    assert scoped.call_count == segments
    before_loop = _checkpoints_before_the_segments(plan, actor)
    assert before_loop >= 0
    # The client leaves after the first segment's checkpoint.
    probe = TurnsTrueAfter(before_loop + 1)
    with probed(probe) as scoped, pytest.raises(RequestAbandoned):
        _preview(plan, actor)
    assert probe.calls == before_loop + 2
    assert scoped.call_count == 1


def test_preview_stops_in_the_target_pass_once_its_client_leaves(varied_plan):  # noqa: F811
    plan, actor = varied_plan
    segments = _cast_segments(plan)
    before_loop = _checkpoints_before_the_segments(plan, actor)
    # The first pass checks once per segment, and so does the pass that builds
    # each target's controls: a connected preview makes both.
    connected = TurnsTrueAfter(10**6)
    with probed(connected):
        _preview(plan, actor)
    assert connected.calls >= before_loop + 2 * segments
    # The client leaves after the target pass's first check.
    probe = TurnsTrueAfter(before_loop + segments + 1)
    with probed(probe) as scoped, pytest.raises(RequestAbandoned):
        _preview(plan, actor)
    assert probe.calls == before_loop + segments + 2
    assert scoped.call_count == segments


def test_an_abandoned_preview_post_answers_499(varied_plan):  # noqa: F811
    plan, _actor = varied_plan
    users = plan.c.r.f.users
    url = f"/api/v1/sessions/{plan.c.p.source['id']}/generation-recast/preview"
    connected = mock.Mock(return_value=False)
    kept = users.alice.post(
        url, json={}, headers=users.ah,
        environ_overrides={"waitress.client_disconnected": connected},
    )
    assert kept.status_code == 200, kept.get_json()
    assert connected.call_count >= 1
    gone = mock.Mock(return_value=True)
    response = users.alice.post(
        url, json={}, headers=users.ah,
        environ_overrides={"waitress.client_disconnected": gone},
    )
    assert response.status_code == 499, response.get_json()
    assert response.get_json()["error"]["code"] == "client_closed_request"
    gone.assert_called_once()
