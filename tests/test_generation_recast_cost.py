"""A recast preview's cost stays bounded, and an abandoned preview stops.

A preview used to rebuild and deep-copy the whole TTS service catalogue for every
cast span and re-prove every retained pin for every segment, so a long book took
minutes; it is a read-only POST, so its client leaving never stopped it. The tests
count catalogue builds, access checks, pin checks and segment steps, never time.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest import mock

import pytest
from sqlalchemy import select

from pandrator.logic import tts_service_catalogue
from pandrator.web import generation_recast, speech_inputs
from pandrator.web import models as m
from pandrator.web.abandoned_requests import (
    RequestAbandoned,
    bind_disconnect_probe,
    reset_disconnect_probe,
)
from pandrator.web.access import current_session_policy
from pandrator.web.accounts.execution_actor import execution_actor_scope
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
    with old_effective(), counting() as old:
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
