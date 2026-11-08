"""Ordinary Member generation reaches native speech under current project rights."""

from __future__ import annotations

import json
import socket
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from sqlalchemy import select

from pandrator.logic import tts_handler
from pandrator.logic.tts_retry import TtsRetryGate
from pandrator.web import models as m
from pandrator.web.credentials import tts_service_credential_key, upsert_credential
from pandrator.web.jobs import Worker
from pandrator.web.speech_input_contracts import PROJECT_SPEECH_BINDING_KEY
from pandrator.web.speech_inputs import speech_input_adapter
from tests.test_evidence_access_routes import body, headers
from tests.test_google_design_workspace_http import MODEL, SECRET
from tests.test_google_design_workspace_http import case as case  # noqa: F401
from tests.test_google_design_workspace_http import design as design  # noqa: F401
from tests.test_google_design_workspace_http import no_network as no_network  # noqa: F401
from tests.test_settings_workspace_access import put
from tests.test_shared_tts_preview import fake_post


@pytest.fixture
def generation_case(design, monkeypatch, request):
    f = design
    options = getattr(request, "param", {})
    if options.get("profile"):
        body(
            put(
                f.users.owner,
                f.users.oh,
                "services.tts",
                {"provider_configs": [options["profile"]]},
                revision=1,
                workspace=f.users.workspace_id,
            )
        )
    body(
        put(
            f.users.owner,
            f.users.oh,
            "defaults.tts",
            {
                "service": "gemini",
                "model": MODEL,
                "xtts_model": MODEL,
                "voice": "Kore",
                "speaker": "Kore",
                "language": "en",
                "max_attempts": 1,
                "performance_enabled": False,
                "casting_enabled": False,
                "tts_context_mode": "off",
                "speech_block_early_repair_enabled": False,
                "speech_block_regroup_enabled": False,
                **options.get("defaults", {}),
            },
            workspace=f.users.workspace_id,
        )
    )
    source = body(
        f.users.alice.post(
            "/api/v1/sessions",
            json={
                "name": "Shared narration execution",
                "workflow_kind": options.get("workflow_kind", "voiceover"),
                "source_language": "en",
                "target_language": "en",
                "workspace_id": f.users.workspace_id,
            },
            headers=headers(f.users.ah, "generation-member-session"),
        ),
        201,
    )
    # Supply reviewed source topology in the fixture; admission and execution
    # below use public HTTP and the actual registered worker.
    plan = f.services.generation.create_plan(
        source["id"],
        source_revision_id=None,
        settings={},
        segments=[{"text": "A shared narration sentence.", "language": "en"}],
    )
    extension = f.case.app.extensions["pandrator"]
    handlers = extension["workflow_handlers"]
    run = Worker(f.services.jobs, "generation-workspace-worker", handlers.handlers())
    monkeypatch.setenv("GEMINI_API_KEY", "forbidden-ambient-generation-key")
    monkeypatch.setattr(tts_handler, "_TTS_RETRY_GATE", TtsRetryGate())
    monkeypatch.setattr(
        tts_handler,
        "wait_for_retry",
        lambda *_a, **_k: pytest.fail("Unexpected retry in offline generation acceptance"),
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_k: pytest.fail("Unexpected DNS in offline generation acceptance"),
    )
    queued = body(
        f.users.alice.post(
            f"/api/v1/sessions/{source['id']}/generation-admissions",
            json={},
            headers=headers(f.users.ah, "generation-member-admission"),
        ),
        202,
    )
    assert run.run_once()
    admitted = f.services.jobs.get(queued["id"])
    assert admitted.status == "succeeded", admitted.error_message
    marker = dict(admitted.result_json)
    segment = f.services.generation.list_segments(source["id"])["items"][0]
    keep = (
        handlers._session_dir(source["id"])
        / "generation"
        / plan["active_revision_id"]
        / segment["id"]
        / "earlier-kept.wav"
    )
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_bytes(b"Earlier output must remain intact")
    wav_before = set(f.services.paths.root.rglob("*.wav"))
    return SimpleNamespace(
        f=f,
        source=source,
        marker=marker,
        worker=run,
        handlers=handlers,
        keep=keep,
        wav_before=wav_before,
    )


def assert_denied_without_outputs(g):
    f = g.f
    job = f.services.jobs.get(g.marker["job_id"])
    assert job.status == "failed", (job.status, job.error_message)
    with f.case.database.snapshot_session() as session:
        assert not session.scalars(
            select(m.Artifact.id).where(
                m.Artifact.session_id == g.source["id"], m.Artifact.role == "generation_take"
            )
        ).all()
        assert not session.scalars(
            select(m.UsageEvent.id).where(m.UsageEvent.session_id == g.source["id"])
        ).all()
    assert g.keep.read_bytes() == b"Earlier output must remain intact"
    assert set(f.services.paths.root.rglob("*.wav")) == g.wav_before


def test_member_admission_registered_worker_publishes_owned_take(generation_case, monkeypatch):
    g = generation_case
    calls = fake_post(monkeypatch)
    assert g.worker.run_once()
    job = g.f.services.jobs.get(g.marker["job_id"])
    assert job.status == "succeeded", job.error_message
    with g.f.case.database.snapshot_session() as session:
        run = session.get_one(m.GenerationRun, g.marker["generation_run_id"])
        assert run.status in {"completed", "partial"}, run.status
        assert session.get_one(m.JobActorRecord, job.id).user_id == g.f.users.alice_id
        takes = session.scalars(
            select(m.AudioTake).where(m.AudioTake.generation_run_id == run.id)
        ).all()
        assert len(takes) == 1
        artifact = session.get_one(m.Artifact, takes[0].artifact_id)
        assert artifact.workspace_id == g.f.users.workspace_id
        assert (
            artifact.project_id == g.source["project_id"] and artifact.session_id == g.source["id"]
        )
        usages = session.scalars(select(m.UsageEvent).where(m.UsageEvent.job_id == job.id)).all()
        assert usages and all(
            u.workspace_id == artifact.workspace_id and u.project_id == artifact.project_id
            for u in usages
        )
        assert SECRET not in str(artifact.metadata_json)
    assert len(calls) == 1
    assert g.keep.read_bytes() == b"Earlier output must remain intact"


@pytest.mark.parametrize(
    "generation_case",
    [
        {
            "profile": {
                "id": "xtts",
                "enabled": True,
                "models": ["xtts_v2"],
                "default_model": "xtts_v2",
                "api_base": "https://prepared.invalid",
                "connection_mode": "external",
                "adapter": "openai_compatible",
            },
            "defaults": {"service": "XTTS", "model": "xtts_v2", "xtts_model": "xtts_v2"},
        }
    ],
    indirect=True,
)
def test_native_generation_uses_the_approved_connection_for_its_actual_route(
    generation_case, monkeypatch
):
    from tests.test_voice_lifecycle_idempotency import silent_wav

    g = generation_case
    calls = []

    def post(url, **options):
        assert url == "https://prepared.invalid/v1/audio/speech/prepared"
        assert options["allow_redirects"] is False
        calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.headers.update(
            {"Content-Type": "audio/wav", "X-Pandrator-Model-Policy": "prepared-only"}
        )
        response._content = silent_wav()
        response._content_consumed = True
        return response

    monkeypatch.setattr("pandrator.logic.native_speech_http.requests.post", post)
    assert g.worker.run_once()
    job = g.f.services.jobs.get(g.marker["job_id"])
    assert job.status == "succeeded", job.error_message
    assert len(calls) == 1


def test_canonical_speech_adapter_does_not_accept_stale_endpoint_alias() -> None:
    # This must fail with the former preview-first routing and remain narrowed
    # even if an old preview choice points at a different audio.cpp endpoint.
    adapter, registrations = speech_input_adapter(
        {"service": "gemini", "preview_service_id": "audio_cpp"}
    )
    assert adapter == "gemini"
    assert registrations == {"audio_cpp"}

    adapter, registrations = speech_input_adapter(
        {"service": "audio.cpp", "preview_service_id": "stale-other-endpoint"}
    )
    assert adapter == "audio_cpp"
    assert registrations == {"audio_cpp"}


@pytest.mark.parametrize(
    "generation_case",
    [
        {
            "defaults": {
                "preview_service_id": "audio_cpp",
                "service_id": "kobold_qwen",
                "preview_api_base": "https://unapproved.invalid",
            }
        }
    ],
    indirect=True,
)
def test_generation_ignores_saved_preview_routing_metadata(generation_case, monkeypatch):
    g = generation_case
    calls = fake_post(monkeypatch)
    capabilities = g.handlers.tts_providers.synthesis_capabilities
    routed = []

    def capture_route(settings, **kwargs):
        routed.append(g.handlers.tts_providers.service_id_for_settings(settings))
        assert routed[-1] == "google_gemini"
        assert not set(settings) & {
            "preview_service_id",
            "service_id",
            "preview_adapter",
            "preview_api_base",
        }
        return capabilities(settings, **kwargs)

    monkeypatch.setattr(g.handlers.tts_providers, "synthesis_capabilities", capture_route)
    with g.f.case.database.snapshot_session() as session:
        snapshot = session.get_one(m.GenerationRun, g.marker["generation_run_id"])
        recorded = dict(snapshot.settings_snapshot_json.get("tts") or {})
    # The stale preview route must not fabricate an audio.cpp retained binding
    # for the canonical Gemini selection that dispatch actually uses.
    assert recorded.get(PROJECT_SPEECH_BINDING_KEY) is None, recorded.get(
        PROJECT_SPEECH_BINDING_KEY
    )
    assert g.worker.run_once()
    job = g.f.services.jobs.get(g.marker["job_id"])
    assert job.status == "succeeded", job.error_message
    assert routed and len(calls) == 1


@pytest.mark.parametrize("loss", ["membership", "settings", "credential"])
def test_native_send_rechecks_late_generation_changes(generation_case, monkeypatch, loss):
    g = generation_case
    calls = fake_post(monkeypatch)
    original = tts_handler._resolve_openai_audio_api_key
    changed = []

    def resolve_then_change(endpoint):
        key = original(endpoint)
        if not changed:
            changed.append(True)
            with g.f.case.database.immediate_session() as session:
                if loss == "membership":
                    session.delete(
                        session.get_one(
                            m.WorkspaceMembership, (g.f.users.workspace_id, g.f.users.alice_id)
                        )
                    )
                elif loss == "settings":
                    setting = session.get_one(
                        m.WorkspaceSetting, (g.f.users.workspace_id, "services.tts")
                    )
                    value = dict(setting.value_json)
                    providers = [dict(provider) for provider in value["provider_configs"]]
                    next(provider for provider in providers if provider["id"] == "gemini")[
                        "enabled"
                    ] = False
                    setting.value_json = {**value, "provider_configs": providers}
                    setting.revision += 1
                else:
                    upsert_credential(
                        session,
                        tts_service_credential_key("gemini"),
                        "Synthetic rotated",
                        "new-workspace-generation-key",
                        workspace_id=g.f.users.workspace_id,
                    )
        return key

    monkeypatch.setattr(tts_handler, "_resolve_openai_audio_api_key", resolve_then_change)
    assert g.worker.run_once()
    assert changed and not calls
    assert_denied_without_outputs(g)


@pytest.mark.parametrize("point", ["response", "prepared"])
def test_late_generation_loss_leaves_no_outputs_or_owned_orphans(
    generation_case, monkeypatch, point
):
    g = generation_case

    def revoke():
        with g.f.case.database.immediate_session() as session:
            session.delete(
                session.get_one(m.WorkspaceMembership, (g.f.users.workspace_id, g.f.users.alice_id))
            )

    calls = fake_post(monkeypatch, after_send=revoke if point == "response" else None)
    if point == "prepared":
        prepare = g.handlers.artifacts.prepare_registration

        def prepare_then_revoke(path: Path, **kwargs):
            result = prepare(path, **kwargs)
            revoke()
            return result

        monkeypatch.setattr(g.handlers.artifacts, "prepare_registration", prepare_then_revoke)
    assert g.worker.run_once()
    assert len(calls) == 1
    assert_denied_without_outputs(g)


def during_identity_check(monkeypatch, action, *, identities=None):
    """Run ``action`` inside the check that opens ``generation.run``."""
    from pandrator.web import generation_audio_identity

    original = generation_audio_identity.plan_audio_identities
    calls = []

    def wrapped(session, *args, **kwargs):
        first = not calls
        calls.append(True)
        if first:
            action(session)
        result = original(session, *args, **kwargs)
        return identities if first and identities is not None else result

    monkeypatch.setattr(generation_audio_identity, "plan_audio_identities", wrapped)
    return calls


def job_events(g, job_id):
    with g.f.case.database.snapshot_session() as session:
        return session.scalars(select(m.JobEvent).where(m.JobEvent.job_id == job_id)).all()


def test_identity_check_keeps_heartbeats_and_renewed_leases_working(generation_case, monkeypatch):
    g = generation_case
    calls = fake_post(monkeypatch)
    queue = g.f.services.jobs
    job_id = g.marker["job_id"]
    observed = {}

    def renew_like_the_monitor(session):
        job = queue.get(job_id)
        with queue.database.snapshot_session() as other:
            observed["run_status"] = other.get_one(
                m.GenerationRun, g.marker["generation_run_id"]
            ).status
        observed["renewed"] = queue.heartbeat(
            job_id,
            job.lease_owner,
            lease_generation=job.lease_generation,
            lease_seconds=3600,
        )
        # The lease proof reads through this session, so it has to see renewals
        # committed after the check began. A pinned snapshot would keep failing.
        # The real monitor may also renew here, so compare with the earlier lease.
        seen = session.scalar(select(m.Job.lease_expires_at).where(m.Job.id == job_id))
        observed["renewal_visible"] = seen > job.lease_expires_at

    during_identity_check(monkeypatch, renew_like_the_monitor)
    assert g.worker.run_once()

    assert observed["run_status"] == "running"
    assert observed["renewed"] is True
    assert observed["renewal_visible"] is True
    job = queue.get(job_id)
    assert job.status == "succeeded", job.error_message
    assert len(calls) == 1


def test_lost_lease_during_identity_check_is_not_reported_as_voice_change(
    generation_case, monkeypatch
):
    g = generation_case
    calls = fake_post(monkeypatch)
    queue = g.f.services.jobs
    job_id = g.marker["job_id"]

    def expire_lease(_session):
        with queue.database.immediate_session() as session:
            session.get_one(m.Job, job_id).lease_expires_at = m.utcnow() - timedelta(seconds=1)

    # An expired lease denies voice access. The check swallows that denial, so
    # the identities come back different even though no voice changed.
    during_identity_check(monkeypatch, expire_lease, identities={})
    assert g.worker.run_once()

    job = queue.get(job_id)
    assert (job.status, job.error_code) == ("failed", "worker_lease_expired")
    assert not [
        event
        for event in job_events(g, job_id)
        if "voice reference changed" in str(event.payload_json)
    ]
    with g.f.case.database.snapshot_session() as session:
        assert session.get_one(m.GenerationRun, g.marker["generation_run_id"]).status == "failed"
    assert not calls


def test_identity_change_with_a_live_lease_still_fails_the_run(generation_case, monkeypatch):
    g = generation_case
    calls = fake_post(monkeypatch)
    during_identity_check(monkeypatch, lambda _session: None, identities={})
    assert g.worker.run_once()

    job = g.f.services.jobs.get(g.marker["job_id"])
    assert job.status == "failed"
    assert "voice reference changed" in job.error_message
    with g.f.case.database.snapshot_session() as session:
        assert session.get_one(m.GenerationRun, g.marker["generation_run_id"]).status == "failed"
    assert not calls


def test_plan_change_with_a_live_lease_still_fails_the_run(generation_case, monkeypatch):
    g = generation_case
    calls = fake_post(monkeypatch)
    segment_id = g.f.services.generation.list_segments(g.source["id"])["items"][0]["id"]
    with g.f.case.database.immediate_session() as session:
        session.get_one(m.GenerationSegment, segment_id).text = "A different sentence."
    assert g.worker.run_once()

    job = g.f.services.jobs.get(g.marker["job_id"])
    assert job.status == "failed"
    assert "speech plan changed" in job.error_message
    with g.f.case.database.snapshot_session() as session:
        assert session.get_one(m.GenerationRun, g.marker["generation_run_id"]).status == "failed"
    assert not calls


def test_failed_identity_check_clears_a_pause_requested_during_it(generation_case, monkeypatch):
    g = generation_case
    fake_post(monkeypatch)
    run_id = g.marker["generation_run_id"]

    def request_pause(_session):
        with g.f.case.database.immediate_session() as session:
            run = session.get_one(m.GenerationRun, run_id)
            run.status, run.pause_requested = "pausing", True

    during_identity_check(monkeypatch, request_pause, identities={})
    assert g.worker.run_once()

    with g.f.case.database.snapshot_session() as session:
        run = session.get_one(m.GenerationRun, run_id)
        assert (run.status, run.pause_requested) == ("failed", False)


def test_cancel_during_identity_check_stops_before_provider_send(generation_case, monkeypatch):
    g = generation_case
    calls = fake_post(monkeypatch)
    queue = g.f.services.jobs
    job_id = g.marker["job_id"]
    during_identity_check(monkeypatch, lambda _session: queue.request_cancel(job_id))
    assert g.worker.run_once()

    assert queue.get(job_id).status == "canceled"
    with g.f.case.database.snapshot_session() as session:
        assert session.get_one(m.GenerationRun, g.marker["generation_run_id"]).status == "canceled"
    assert not calls


def test_take_name_collision_preserves_existing_file(generation_case, monkeypatch):
    from pandrator.web import workflow_generation_execution

    g = generation_case
    kept = g.keep.with_name("tts-fixed-attempt.wav")
    kept.write_bytes(b"Preexisting bytes must never be overwritten")
    g.wav_before.add(kept)
    calls = fake_post(monkeypatch)
    monkeypatch.setattr(workflow_generation_execution, "new_id", lambda: "fixed-attempt")
    assert g.worker.run_once()
    assert len(calls) == 1
    assert kept.read_bytes() == b"Preexisting bytes must never be overwritten"
    assert_denied_without_outputs(g)


def test_rollback_preserves_a_replaced_take_inode(generation_case, monkeypatch):
    g = generation_case
    calls = fake_post(monkeypatch)
    prepare = g.handlers.artifacts.prepare_registration
    replaced = []

    def replace_then_revoke(path: Path, **kwargs):
        result = prepare(path, **kwargs)
        replacement = path.with_name("replacement.wav")
        replacement.write_bytes(b"Replacement owned by another writer")
        replacement.replace(path)
        replaced.append(path)
        with g.f.case.database.immediate_session() as session:
            session.delete(
                session.get_one(m.WorkspaceMembership, (g.f.users.workspace_id, g.f.users.alice_id))
            )
        return result

    monkeypatch.setattr(g.handlers.artifacts, "prepare_registration", replace_then_revoke)
    assert g.worker.run_once()
    assert len(calls) == 1 and len(replaced) == 1
    assert g.f.services.jobs.get(g.marker["job_id"]).status == "failed"
    assert replaced[0].read_bytes() == b"Replacement owned by another writer"
    assert g.keep.read_bytes() == b"Earlier output must remain intact"
    with g.f.case.database.snapshot_session() as session:
        assert not session.scalars(
            select(m.Artifact.id).where(
                m.Artifact.session_id == g.source["id"], m.Artifact.role == "generation_take"
            )
        ).all()


def test_unrelated_user_cannot_read_private_generation_or_settings(design, monkeypatch):
    f = design
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-projection-env")
    source = body(
        f.users.owner.post(
            "/api/v1/sessions",
            json={
                "name": "Private narration metadata",
                "workflow_kind": "voiceover",
            },
            headers=f.users.oh,
        ),
        201,
    )
    plan = f.services.generation.create_plan(
        source["id"],
        source_revision_id=None,
        settings={},
        segments=[{"text": "Private project text."}],
    )
    with f.case.database.immediate_session() as session:
        run = m.GenerationRun(
            session_id=source["id"],
            plan_revision_id=plan["active_revision_id"],
            status="completed",
            settings_snapshot_json={
                "tts": {
                    "service": "gemini",
                    "model": MODEL,
                    "provider_configs": [
                        {
                            "id": "gemini",
                            "api_base": "https://private-config.invalid",
                            "secret_ref": "database:private-projection-reference",
                        }
                    ],
                }
            },
        )
        session.add(run)
        session.flush()
        run_id = run.id
    paths = [
        f"/api/v1/sessions/{source['id']}/generation-runs/latest",
        f"/api/v1/sessions/{source['id']}/generation-runs",
        f"/api/v1/generation-runs/{run_id}",
        f"/api/v1/sessions/{source['id']}/settings/tts",
    ]
    statuses = {path: f.users.bob.get(path).status_code for path in paths}
    resolve = f"/api/v1/sessions/{source['id']}/settings/resolve"
    statuses[resolve] = f.users.bob.post(resolve, json={}, headers=f.users.bh).status_code
    assert set(statuses.values()) == {404}, statuses


def test_rotated_generation_key_cannot_escape_provider_failure(
    generation_case, monkeypatch, caplog
):
    g = generation_case
    redactor = g.f.services.jobs.secret_redactor
    redactor.values()
    rotated = "synthetic-new-generation-workspace-key"
    with g.f.case.database.immediate_session() as session:
        upsert_credential(
            session,
            tts_service_credential_key("gemini"),
            "Rotated fixture key",
            rotated,
            workspace_id=g.f.users.workspace_id,
        )
    calls = []

    def post(url, **options):
        assert options["headers"]["x-goog-api-key"] == rotated
        calls.append(url)
        response = requests.Response()
        response.status_code = 400
        response.url = url
        response._content = json.dumps(
            {"error": {"message": f"Provider rejected {rotated}"}}
        ).encode()
        return response

    monkeypatch.setattr("pandrator.logic.native_speech_http.requests.post", post)
    monkeypatch.setattr(
        tts_handler,
        "_get_litellm_speech_client",
        lambda: pytest.fail("Scoped generation reached an opaque SDK"),
    )
    assert g.worker.run_once()
    assert calls
    job = g.f.services.jobs.get(g.marker["job_id"])
    assert job.status == "failed"
    assert rotated not in (job.error_message or "")
    assert rotated not in caplog.text
    for path in (f"/api/v1/jobs/{job.id}", f"/api/v1/jobs/{job.id}/logs"):
        assert rotated not in json.dumps(body(g.f.users.bob.get(path)))
    assert_denied_without_outputs(g)


def test_rvc_admission_does_not_require_the_previous_speech_credential(
    generation_case, monkeypatch
):
    g = generation_case
    calls = fake_post(monkeypatch)
    assert g.worker.run_once() and len(calls) == 1
    with g.f.case.database.immediate_session() as session:
        credential = session.get_one(
            m.StoredCredential, (g.f.users.workspace_id, tts_service_credential_key("gemini"))
        )
        session.delete(credential)
    response = body(
        g.f.users.alice.post(
            f"/api/v1/sessions/{g.source['id']}/generation-runs",
            json={
                "operation": "rvc",
                "generation_run_id": g.marker["generation_run_id"],
                "run_override": {"rvc": {"model": "disposable-rvc-model"}},
            },
            headers=headers(g.f.users.ah, "rvc-without-tts-key"),
        ),
        202,
    )
    assert response["operation"] == "rvc"
    assert response["status"] == "queued" and len(calls) == 1


def test_registered_member_job_can_execute_its_internal_derived_child(generation_case, monkeypatch):
    from pandrator.web import workflow_handlers as runtime_handlers
    from pandrator.web.generation_audio_identity import plan_audio_identities
    from pandrator.web.generation_topology import GenerationTopologyService
    from pandrator.web.settings_policy import stable_hash
    from pandrator.web.speech_plan_workspace import freeze_speech_snapshot

    g = generation_case
    calls = fake_post(monkeypatch)
    children = []
    monkeypatch.setattr(
        runtime_handlers,
        "_voiceover_second_pass",
        lambda snapshot, **_kwargs: (
            None if snapshot.get("disposable_derived_acceptance") else "regroup"
        ),
    )

    def derive(parent_id, _progress, cancel_event):
        with g.f.case.database.immediate_session() as session:
            parent = session.get_one(m.GenerationRun, parent_id)
            segment = session.scalar(
                select(m.GenerationSegment).where(
                    m.GenerationSegment.plan_revision_id == parent.plan_revision_id
                )
            )
            assert segment is not None
            revised = GenerationTopologyService(
                g.f.case.database, g.f.services.jobs
            ).revise_topology_in_session(
                session,
                g.source["id"],
                parent.plan_revision_id,
                {"action": "split", "segment_id": segment.id, "cursor": 9},
                activate=False,
            )
            revision_id = revised["plan_revision_id"]
            snapshot = deepcopy(parent.settings_snapshot_json)
            snapshot["disposable_derived_acceptance"] = True
            freeze_speech_snapshot(session, revision_id, snapshot, explicit=True)
            snapshot["generation_audio_identities"] = plan_audio_identities(
                session, revision_id, snapshot
            )
            child = m.GenerationRun(
                session_id=parent.session_id,
                plan_revision_id=revision_id,
                source_generation_run_id=parent.id,
                job_id=parent.job_id,
                sequence_number=2,
                operation="generate",
                status="queued",
                settings_snapshot_json=snapshot,
                settings_hash=stable_hash(snapshot),
            )
            session.add(child)
            session.flush()
            child_id = child.id
        generated = g.handlers.run_generation(
            {"generation_run_id": child_id, "operation": "resume", "segment_ids": []},
            lambda *_args: None,
            cancel_event,
        )
        children.append(generated)
        return {
            "regroup_status": "completed",
            "regrouped_groups": 1,
            "regrouped_generation_run_id": child_id,
        }

    monkeypatch.setattr(g.handlers, "_regroup_generation_blocks", derive)
    assert g.worker.run_once()
    job = g.f.services.jobs.get(g.marker["job_id"])
    assert job.status == "succeeded", job.error_message
    assert job.result_json.get("regroup_status") == "completed", job.result_json
    assert len(children) == 1 and children[0]["status"] == "completed"
    assert len(calls) == 3
    with g.f.case.database.snapshot_session() as session:
        child = session.get_one(m.GenerationRun, children[0]["generation_run_id"])
        assert (
            child.job_id == job.id
            and child.source_generation_run_id == g.marker["generation_run_id"]
        )
        takes = session.scalars(
            select(m.AudioTake).where(m.AudioTake.generation_run_id == child.id)
        ).all()
        assert len(takes) == 2
        for take in takes:
            artifact = session.get_one(m.Artifact, take.artifact_id)
            assert artifact.workspace_id == g.f.users.workspace_id
            assert (
                artifact.project_id == g.source["project_id"]
                and artifact.session_id == g.source["id"]
            )
