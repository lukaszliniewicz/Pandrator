"""HTTP admission and idempotency checks for finished-run timing repair."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from pandrator.web.models import GenerationPlanRevision, GenerationRun, Job, utcnow
from pandrator.web.projects.models import Project
from tests.test_evidence_access_routes import body
from tests.test_generation_workspace_access import project
from tests.test_google_design_workspace_http import case as case  # noqa: F401
from tests.test_google_design_workspace_http import design as design  # noqa: F401
from tests.test_google_design_workspace_http import no_network as no_network  # noqa: F401


def _ready(design):
    p = project(design)
    with design.case.database.immediate_session() as session:
        run = session.get(GenerationRun, p.run_id)
        run.settings_snapshot_json = {
            "tts": {
                **run.settings_snapshot_json["tts"],
                "speech_block_generation_mode": "passage",
                "speech_block_early_repair_enabled": True,
                "tts_context_mode": "both",
                "performance_context_before": 1,
                "performance_context_after": 1,
            }
        }
    return p


def _install_completed_root_job(design, p) -> str:
    with design.case.database.immediate_session() as session:
        project_row = session.get(Project, p.source["project_id"])
        job = Job(
            workspace_id=project_row.workspace_id,
            project_id=p.source["project_id"],
            session_id=p.source["id"],
            kind="generation.run",
            status="succeeded",
            payload_json={
                "generation_run_id": p.run_id,
                "operation": "generate",
                "segment_ids": [],
            },
            result_json={
                "repaired_blocks": 0,
                "repair_checked_blocks": 2,
                "repair_rejection_reasons": {},
            },
            progress=1.0,
            attempts=1,
        )
        session.add(job)
        session.flush()
        session.get(GenerationRun, p.run_id).job_id = job.id
        return job.id


def _claim(design, job_id: str):
    claimed = design.services.jobs.claim(
        "repair-http-worker", lease_seconds=300
    )
    assert claimed is not None and claimed.id == job_id
    return claimed


def _add_repair_child(design, p, job_id: str, *, status: str) -> str:
    with design.case.database.immediate_session() as session:
        root = session.get(GenerationRun, p.run_id)
        sequence_number = int(
            session.scalar(
                select(func.max(GenerationRun.sequence_number)).where(
                    GenerationRun.session_id == root.session_id
                )
            )
            or 0
        ) + 1
        child = GenerationRun(
            session_id=root.session_id,
            plan_revision_id=root.plan_revision_id,
            source_generation_run_id=root.id,
            job_id=job_id,
            sequence_number=sequence_number,
            operation="generate",
            status=status,
            settings_snapshot_json={"early_repair_parent_run_id": root.id},
        )
        session.add(child)
        session.flush()
        return child.id


def _mark_repair_revision_applied(design, p) -> None:
    with design.case.database.immediate_session() as session:
        root = session.get(GenerationRun, p.run_id)
        revision = session.get(GenerationPlanRevision, root.plan_revision_id)
        revision.operation_json = {
            "reason": "early_timing_repair",
            "source_generation_run_id": root.id,
            "repair_status": "applied",
        }


def test_timing_repair_admission_is_idempotent_and_queues_only_one_job(design):
    p = _ready(design)
    url = f"/api/v1/generation-runs/{p.run_id}/repair-timing"
    headers = {**design.users.oh, "Idempotency-Key": "timing-finished-lecture-v1"}
    first = design.users.owner.post(url, json={}, headers=headers)
    created = body(first, 202)
    assert created["generation_run_id"] == p.run_id
    assert created["reused_job"] is False
    repeated = design.users.owner.post(url, json={}, headers=headers)
    assert body(repeated, 202) == created
    assert repeated.headers.get("Idempotency-Replayed") == "true"
    later = body(design.users.owner.post(url, json={}, headers=design.users.oh), 200)
    assert later["job_id"] == created["job_id"]
    assert later["reused_job"] is True
    with design.case.database.snapshot_session() as session:
        jobs = list(session.scalars(
            select(Job).where(
                Job.session_id == p.source["id"],
                Job.kind == "generation.repair-existing",
            )
        ))
        assert len(jobs) == 1
        assert jobs[0].id == created["job_id"]
        assert jobs[0].payload_json == {
            "session_id": p.source["id"],
            "generation_run_id": p.run_id,
        }


def test_timing_repair_rejects_active_runs_and_extra_body_parameters(design):
    p = _ready(design)
    url = f"/api/v1/generation-runs/{p.run_id}/repair-timing"
    assert design.users.owner.post(
        url, json={"force": True}, headers=design.users.oh
    ).status_code == 422
    with design.case.database.immediate_session() as session:
        run = session.get(GenerationRun, p.run_id)
        run.status = "paused"
    response = design.users.owner.post(url, json={}, headers=design.users.oh)
    assert response.status_code == 409


def test_timing_repair_failure_is_visible_and_keeps_root_generation_job(design):
    p = _ready(design)
    root_job_id = _install_completed_root_job(design, p)
    url = f"/api/v1/generation-runs/{p.run_id}/repair-timing"
    created = body(design.users.owner.post(url, json={}, headers=design.users.oh), 202)
    claimed = _claim(design, created["job_id"])
    assert design.services.jobs.fail(
        claimed.id,
        "repair-http-worker",
        "synthetic_repair_failure",
        "Synthetic repair failure.",
        lease_generation=claimed.lease_generation,
    )

    detail = body(
        design.users.owner.get(f"/api/v1/generation-runs/{p.run_id}"), 200
    )
    repair = detail["timing_repair"]
    assert repair["status"] == "failed"
    assert repair["error_code"] == "synthetic_repair_failure"
    assert repair["error_message"] == "Synthetic repair failure."
    assert detail["job_id"] == root_job_id
    with design.case.database.snapshot_session() as session:
        assert session.get(GenerationRun, p.run_id).job_id == root_job_id


def test_timing_repair_skip_is_visible_and_retryable(design):
    p = _ready(design)
    url = f"/api/v1/generation-runs/{p.run_id}/repair-timing"
    created = body(design.users.owner.post(url, json={}, headers=design.users.oh), 202)
    claimed = _claim(design, created["job_id"])
    design.services.jobs.complete(
        claimed.id,
        "repair-http-worker",
        {
            "repair_checked_blocks": 0,
            "repaired_blocks": 0,
            "repair_rejection_reasons": {"missing_source_semantic_context": 1},
        },
        lease_generation=claimed.lease_generation,
    )

    detail = body(
        design.users.owner.get(f"/api/v1/generation-runs/{p.run_id}"), 200
    )
    repair = detail["timing_repair"]
    assert repair["status"] == "skipped"
    assert repair["reason"] == "missing_source_semantic_context"
    assert repair["repair_rejection_reasons"] == {
        "missing_source_semantic_context": 1
    }

    retried = body(
        design.users.owner.post(url, json={}, headers=design.users.oh), 202
    )
    assert retried["reused_job"] is False
    assert retried["job_id"] != created["job_id"]

    again = _claim(design, retried["job_id"])
    design.services.jobs.complete(
        again.id,
        "repair-http-worker",
        {
            "repair_checked_blocks": 3,
            "repaired_blocks": 0,
            "repair_rejection_reasons": {},
        },
        lease_generation=again.lease_generation,
    )
    reused = body(
        design.users.owner.post(url, json={}, headers=design.users.oh), 200
    )
    assert reused["reused_job"] is True
    assert reused["job_id"] == retried["job_id"]


def test_timing_repair_legacy_stopped_receipt_is_retryable(design):
    """Older stopped receipts lack repair_completed but are still retryable."""

    p = _ready(design)
    _install_completed_root_job(design, p)
    url = f"/api/v1/generation-runs/{p.run_id}/repair-timing"
    created = body(design.users.owner.post(url, json={}, headers=design.users.oh), 202)
    claimed = _claim(design, created["job_id"])
    design.services.jobs.complete(
        claimed.id,
        "repair-http-worker",
        {
            "repair_checked_blocks": 2,
            "repaired_blocks": 0,
            "repair_rejection_reasons": {"generation_stopped": 1},
        },
        lease_generation=claimed.lease_generation,
    )

    detail = body(
        design.users.owner.get(f"/api/v1/generation-runs/{p.run_id}"), 200
    )
    repair = detail["timing_repair"]
    assert repair["status"] == "partial"
    # The legacy receipt never recorded repair_completed; it stays unknown
    # rather than being invented as a completed pass.
    assert repair["pass_completed"] is None

    retried = body(
        design.users.owner.post(url, json={}, headers=design.users.oh), 202
    )
    assert retried["reused_job"] is False
    assert retried["job_id"] != created["job_id"]


def test_timing_repair_cancel_closes_staged_child(design):
    p = _ready(design)
    url = f"/api/v1/generation-runs/{p.run_id}/repair-timing"
    created = body(design.users.owner.post(url, json={}, headers=design.users.oh), 202)
    child_id = _add_repair_child(design, p, created["job_id"], status="queued")

    design.services.jobs.request_cancel(created["job_id"])

    with design.case.database.snapshot_session() as session:
        assert session.get(Job, created["job_id"]).status == "canceled"
        assert session.get(GenerationRun, child_id).status == "canceled"


def test_timing_repair_stale_job_fails_staged_child(design):
    p = _ready(design)
    url = f"/api/v1/generation-runs/{p.run_id}/repair-timing"
    created = body(design.users.owner.post(url, json={}, headers=design.users.oh), 202)
    child_id = _add_repair_child(design, p, created["job_id"], status="running")
    with design.case.database.immediate_session() as session:
        job = session.get(Job, created["job_id"])
        job.status = "running"
        job.lease_owner = "dead-repair-worker"
        job.lease_expires_at = utcnow() - timedelta(minutes=1)
        job.attempts = job.max_attempts

    design.services.jobs.reconcile()

    with design.case.database.snapshot_session() as session:
        assert session.get(Job, created["job_id"]).status == "failed"
        assert session.get(GenerationRun, child_id).status == "failed"


def test_timing_repair_pause_stops_active_child_and_resumes_root(design):
    p = _ready(design)
    root_job_id = _install_completed_root_job(design, p)
    created = body(
        design.users.owner.post(
            f"/api/v1/generation-runs/{p.run_id}/repair-timing",
            json={},
            headers=design.users.oh,
        ),
        202,
    )
    child_id = _add_repair_child(design, p, created["job_id"], status="queued")

    paused = body(
        design.users.owner.post(
            f"/api/v1/generation-runs/{child_id}/pause", headers=design.users.oh
        ),
        202,
    )
    assert paused["status"] == "paused"
    assert paused["job_id"] == created["job_id"]
    with design.case.database.snapshot_session() as session:
        root = session.get(GenerationRun, p.run_id)
        assert root.status == "paused"
        assert root.pause_requested is True
        assert root.job_id == root_job_id
        assert session.get(Job, created["job_id"]).status == "canceled"
        assert session.get(GenerationRun, child_id).status == "canceled"

    resumed = body(
        design.users.owner.post(
            f"/api/v1/generation-runs/{p.run_id}/resume", headers=design.users.oh
        ),
        202,
    )
    assert resumed["status"] == "queued"
    with design.case.database.snapshot_session() as session:
        assert session.get(GenerationRun, p.run_id).status == "queued"
        assert session.get(Job, resumed["job_id"]).kind == "generation.run"


def test_timing_repair_cancel_stops_active_child_without_canceling_root(design):
    p = _ready(design)
    root_job_id = _install_completed_root_job(design, p)
    created = body(
        design.users.owner.post(
            f"/api/v1/generation-runs/{p.run_id}/repair-timing",
            json={},
            headers=design.users.oh,
        ),
        202,
    )
    child_id = _add_repair_child(design, p, created["job_id"], status="queued")

    canceled = body(
        design.users.owner.post(
            f"/api/v1/generation-runs/{child_id}/cancel", headers=design.users.oh
        ),
        202,
    )
    assert canceled["status"] == "canceled"
    assert canceled["job_id"] == created["job_id"]
    with design.case.database.snapshot_session() as session:
        root = session.get(GenerationRun, p.run_id)
        assert root.status == "completed"
        assert root.cancel_requested is False
        assert root.job_id == root_job_id
        assert session.get(Job, created["job_id"]).status == "canceled"
        assert session.get(GenerationRun, child_id).status == "canceled"


def test_timing_repair_cancel_leaves_completed_run_unmodified(design):
    p = _ready(design)
    root_job_id = _install_completed_root_job(design, p)
    with design.case.database.snapshot_session() as session:
        before = session.get(GenerationRun, p.run_id)
        before_updated_at = before.updated_at
    canceled = body(
        design.users.owner.post(
            f"/api/v1/generation-runs/{p.run_id}/cancel", headers=design.users.oh
        ),
        202,
    )
    assert canceled["status"] == "completed"
    assert canceled["job_id"] == root_job_id
    with design.case.database.snapshot_session() as session:
        root = session.get(GenerationRun, p.run_id)
        assert root.status == "completed"
        assert root.updated_at == before_updated_at
        assert root.cancel_requested is False
        assert root.pause_requested is False
        assert session.get(Job, root_job_id).status == "succeeded"


def test_timing_repair_controls_ignore_another_root_or_session_job(design):
    p = _ready(design)
    created = body(
        design.users.owner.post(
            f"/api/v1/generation-runs/{p.run_id}/repair-timing",
            json={},
            headers=design.users.oh,
        ),
        202,
    )
    with design.case.database.immediate_session() as session:
        root = session.get(GenerationRun, p.run_id)
        other = GenerationRun(
            session_id=root.session_id,
            plan_revision_id=root.plan_revision_id,
            job_id=created["job_id"],
            sequence_number=2,
            operation="generate",
            status="queued",
            settings_snapshot_json={"tts": {"model": "test"}},
        )
        session.add(other)
        session.flush()
        other_id = other.id

    canceled = body(
        design.users.owner.post(
            f"/api/v1/generation-runs/{other_id}/cancel", headers=design.users.oh
        ),
        202,
    )
    assert canceled["status"] == "canceled"
    with design.case.database.snapshot_session() as session:
        assert session.get(Job, created["job_id"]).status == "queued"
        assert session.get(GenerationRun, p.run_id).status == "completed"
        assert session.get(GenerationRun, other_id).status == "canceled"

    with design.case.database.snapshot_session() as session:
        project_row = session.get(Project, p.source["project_id"])
        workspace_id = project_row.workspace_id
    foreign_source = design.services.sessions.create(
        "Foreign timing root",
        workflow_kind="voiceover",
        project_id=p.source["project_id"],
        workspace_id=workspace_id,
    )
    foreign_plan = design.services.generation.create_plan(
        foreign_source.id,
        source_revision_id=None,
        settings={},
        segments=[{"text": "Foreign root text."}],
    )
    with design.case.database.immediate_session() as session:
        foreign_root = GenerationRun(
            session_id=foreign_source.id,
            plan_revision_id=foreign_plan["active_revision_id"],
            status="completed",
            settings_snapshot_json={"tts": {"speech_block_early_repair_enabled": True}},
        )
        session.add(foreign_root)
        session.flush()
        foreign_job = design.services.jobs.enqueue_in_session(
            session,
            "generation.repair-existing",
            {
                "session_id": foreign_source.id,
                "generation_run_id": foreign_root.id,
            },
            session_id=foreign_source.id,
        )
        other = session.get(GenerationRun, other_id)
        other.status = "queued"
        other.job_id = foreign_job.id
        foreign_root_id, foreign_job_id = foreign_root.id, foreign_job.id
    canceled = body(
        design.users.owner.post(
            f"/api/v1/generation-runs/{other_id}/cancel", headers=design.users.oh
        ),
        202,
    )
    assert canceled["status"] == "canceled"
    with design.case.database.snapshot_session() as session:
        assert session.get(Job, foreign_job_id).status == "queued"
        assert session.get(GenerationRun, foreign_root_id).status == "completed"


def test_timing_repair_history_probe_reads_audio_enabled_override(design):
    p = _ready(design)
    with design.case.database.immediate_session() as session:
        root = session.get(GenerationRun, p.run_id)
        tts = dict(root.settings_snapshot_json["tts"])
        tts.pop("speech_block_early_repair_enabled", None)
        root.settings_snapshot_json = {
            **root.settings_snapshot_json,
            "tts": tts,
            "audio": {
                **dict(root.settings_snapshot_json.get("audio") or {}),
                "speech_block_early_repair_enabled": True,
            },
        }
    with design.case.database.immediate_session() as session:
        job = design.services.jobs.enqueue_in_session(
            session,
            "generation.repair-existing",
            {"session_id": p.source["id"], "generation_run_id": p.run_id},
            session_id=p.source["id"],
        )
        job_id = job.id
    detail = body(
        design.users.owner.get(f"/api/v1/generation-runs/{p.run_id}"), 200
    )
    assert detail["status"] == "running"
    assert detail["phase"] == "repairing_timing"
    assert detail["timing_repair"]["status"] == "running"
    with design.case.database.snapshot_session() as session:
        assert session.get(Job, job_id).status == "queued"


def test_repair_job_api_exposes_its_generation_run_id(design):
    p = _ready(design)
    url = f"/api/v1/generation-runs/{p.run_id}/repair-timing"
    created = body(design.users.owner.post(url, json={}, headers=design.users.oh), 202)
    job = body(design.users.owner.get(f"/api/v1/jobs/{created['job_id']}"), 200)
    assert job["kind"] == "generation.repair-existing"
    assert job["generation_run_id"] == p.run_id


def test_workflow_snapshot_marks_active_repair_as_the_generation_job(design):
    p = _ready(design)
    url = f"/api/v1/generation-runs/{p.run_id}/repair-timing"
    created = body(design.users.owner.post(url, json={}, headers=design.users.oh), 202)
    _claim(design, created["job_id"])

    snapshot = body(
        design.users.owner.get(f"/api/v1/sessions/{p.source['id']}/workflow"), 200
    )
    stage = next(
        item for item in snapshot["stages"] if item["key"] == "generate_audio"
    )
    assert stage["job_id"] == created["job_id"]
    assert stage["status"] == "running"


def test_timing_repair_history_ignores_canceled_attempt_after_resume(design):
    """A resumed generation owns history; its canceled repair is not the result."""

    p = _ready(design)
    root_job_id = _install_completed_root_job(design, p)
    url = f"/api/v1/generation-runs/{p.run_id}/repair-timing"
    created = body(design.users.owner.post(url, json={}, headers=design.users.oh), 202)
    repair_claim = _claim(design, created["job_id"])
    _add_repair_child(design, p, created["job_id"], status="running")

    paused = body(
        design.users.owner.post(
            f"/api/v1/generation-runs/{p.run_id}/pause", headers=design.users.oh
        ),
        202,
    )
    assert paused["status"] == "paused"
    assert design.services.jobs.cancel_owned(
        created["job_id"],
        repair_claim.lease_owner,
        lease_generation=repair_claim.lease_generation,
    )

    resumed = body(
        design.users.owner.post(
            f"/api/v1/generation-runs/{p.run_id}/resume", headers=design.users.oh
        ),
        202,
    )
    resumed_job_id = resumed["job_id"]
    resume_claim = _claim(design, resumed_job_id)
    repaired_id = _add_repair_child(design, p, resumed_job_id, status="completed")
    _mark_repair_revision_applied(design, p)
    design.services.jobs.complete(
        resumed_job_id,
        resume_claim.lease_owner,
        {
            "source_generation_run_id": p.run_id,
            "repair_checked_blocks": 2,
            "repaired_blocks": 1,
            "repair_rejection_reasons": {},
        },
        lease_generation=resume_claim.lease_generation,
    )

    detail = body(
        design.users.owner.get(f"/api/v1/generation-runs/{p.run_id}"), 200
    )
    assert detail["status"] == "completed"
    assert detail["job_id"] == resumed_job_id
    repair = detail["timing_repair"]
    assert repair["status"] == "completed"
    assert repair["result_generation_run_id"] == repaired_id
    assert detail["result_generation_run_id"] == repaired_id
    assert any(
        version["generation_run_id"] == repaired_id
        and version["repair_status"] == "applied"
        for version in repair["versions"]
    )
    with design.case.database.snapshot_session() as session:
        root = session.get(GenerationRun, p.run_id)
        assert root.status == "completed"
        assert root.job_id == resumed_job_id
        assert root.job_id != root_job_id


def test_timing_repair_stale_cancel_requested_job_never_hijacks_resumed_root(design):
    """Immediate pause/resume/cancel must control the new generation attempt."""

    p = _ready(design)
    _install_completed_root_job(design, p)
    created = body(
        design.users.owner.post(
            f"/api/v1/generation-runs/{p.run_id}/repair-timing",
            json={},
            headers=design.users.oh,
        ),
        202,
    )
    _claim(design, created["job_id"])
    _add_repair_child(design, p, created["job_id"], status="running")
    body(
        design.users.owner.post(
            f"/api/v1/generation-runs/{p.run_id}/pause", headers=design.users.oh
        ),
        202,
    )
    with design.case.database.snapshot_session() as session:
        assert session.get(Job, created["job_id"]).status == "cancel_requested"

    resumed = body(
        design.users.owner.post(
            f"/api/v1/generation-runs/{p.run_id}/resume", headers=design.users.oh
        ),
        202,
    )
    canceled = body(
        design.users.owner.post(
            f"/api/v1/generation-runs/{p.run_id}/cancel", headers=design.users.oh
        ),
        202,
    )
    assert canceled["job_id"] == resumed["job_id"]
    with design.case.database.snapshot_session() as session:
        root = session.get(GenerationRun, p.run_id)
        assert root.status == "canceled"
        assert root.cancel_requested is False
        assert session.get(Job, resumed["job_id"]).status == "canceled"
        # The superseded repair still owns its own cancellation handshake.
        assert session.get(Job, created["job_id"]).status == "cancel_requested"


def test_timing_repair_stale_job_cannot_pause_completed_resumed_root(design):
    """A newer generation job fences an unacknowledged stale repair."""

    p = _ready(design)
    _install_completed_root_job(design, p)
    created = body(
        design.users.owner.post(
            f"/api/v1/generation-runs/{p.run_id}/repair-timing",
            json={},
            headers=design.users.oh,
        ),
        202,
    )
    _claim(design, created["job_id"])
    body(
        design.users.owner.post(
            f"/api/v1/generation-runs/{p.run_id}/pause", headers=design.users.oh
        ),
        202,
    )
    resumed = body(
        design.users.owner.post(
            f"/api/v1/generation-runs/{p.run_id}/resume", headers=design.users.oh
        ),
        202,
    )
    resume_claim = _claim(design, resumed["job_id"])
    design.services.jobs.complete(
        resumed["job_id"],
        resume_claim.lease_owner,
        {
            "source_generation_run_id": p.run_id,
            "repair_checked_blocks": 2,
            "repaired_blocks": 0,
            "repair_rejection_reasons": {},
        },
        lease_generation=resume_claim.lease_generation,
    )

    response = design.users.owner.post(
        f"/api/v1/generation-runs/{p.run_id}/pause", headers=design.users.oh
    )
    assert response.status_code == 409
    with design.case.database.snapshot_session() as session:
        root = session.get(GenerationRun, p.run_id)
        assert root.status == "completed"
        assert root.pause_requested is False
        assert session.get(Job, created["job_id"]).status == "cancel_requested"


def test_timing_repair_request_waits_for_cancellation_then_starts_fresh(design):
    """Never reuse a cancel_requested repair; retry once it is terminal."""

    p = _ready(design)
    _install_completed_root_job(design, p)
    url = f"/api/v1/generation-runs/{p.run_id}/repair-timing"
    created = body(design.users.owner.post(url, json={}, headers=design.users.oh), 202)
    repair_claim = _claim(design, created["job_id"])
    canceled = body(
        design.users.owner.post(
            f"/api/v1/generation-runs/{p.run_id}/cancel", headers=design.users.oh
        ),
        202,
    )
    assert canceled["status"] == "completed"
    with design.case.database.snapshot_session() as session:
        assert session.get(Job, created["job_id"]).status == "cancel_requested"

    blocked = design.users.owner.post(url, json={}, headers=design.users.oh)
    assert blocked.status_code == 409
    with design.case.database.snapshot_session() as session:
        jobs = list(
            session.scalars(
                select(Job).where(
                    Job.session_id == p.source["id"],
                    Job.kind == "generation.repair-existing",
                )
            )
        )
        assert [job.id for job in jobs] == [created["job_id"]]

    assert design.services.jobs.cancel_owned(
        created["job_id"],
        repair_claim.lease_owner,
        lease_generation=repair_claim.lease_generation,
    )
    retried = body(
        design.users.owner.post(url, json={}, headers=design.users.oh), 202
    )
    assert retried["reused_job"] is False
    assert retried["job_id"] != created["job_id"]
    with design.case.database.snapshot_session() as session:
        jobs = list(
            session.scalars(
                select(Job).where(
                    Job.session_id == p.source["id"],
                    Job.kind == "generation.repair-existing",
                )
            )
        )
        assert len(jobs) == 2
        assert session.get(Job, created["job_id"]).status == "canceled"
