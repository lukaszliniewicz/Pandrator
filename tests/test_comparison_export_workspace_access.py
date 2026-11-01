"""Direct current comparison-export evidence; synthetic PCM and disposable services."""

from __future__ import annotations

import json
import struct
import threading
import wave
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import delete, func, select, text

from pandrator.web import comparison_export_render as render_module
from pandrator.web import models as m
from pandrator.web.access.comparison_export_policy import require_comparison_export_access
from pandrator.web.access.contracts import AccessContext, AccessDenied
from pandrator.web.accounts.commands import AccountActor, AccountCommands
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.accounts.execution_actor import UserActor, execution_actor_scope
from pandrator.web.accounts.models import Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import ApiTokenProjectGrant
from pandrator.web.accounts.workspace_commands import WorkspaceCommands
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import AuthService
from pandrator.web.comparison_export_render import ComparisonExportContext, render_comparison_export
from pandrator.web.comparison_exports import prepare_comparison_export
from pandrator.web.database import Database
from pandrator.web.job_execution import JobLease, current_job_lease
from pandrator.web.jobs import JobQueue
from pandrator.web.media_process import MediaProcessCancelled
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.sessions import SessionService
from tests.web_test_support import prepare_web_test_data_root

SCOPES = frozenset({"app.read", "app.run", "app.write", "app.workspaces.enroll"})


def _actor(auth, user_id, name):
    browser = auth.browser_sessions.issue(
        user_id, 1, subject=name, principal_kind="owner_session", scopes=SCOPES
    )
    return UserActor(
        user_id, 1, name, "owner_session", SCOPES, browser_session_id=browser.record_id
    )


def _pcm(path: Path, amplitude: int):
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(struct.pack("<h", amplitude) * 800)


@pytest.fixture
def comparison(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PANDRATOR_AUDIO_ASSEMBLER", "streaming")
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    auth = AuthService(database)
    initial = auth.initial_account()
    administrator = AccountActor(initial.user_id, initial.auth_revision, frozenset({"app.admin"}))
    accounts = AccountCommands(database)
    alice = accounts.create_user(administrator, login_name="comparison-alice", display_name="Alice")
    bob = accounts.create_user(administrator, login_name="comparison-bob", display_name="Bob")
    actor = _actor(auth, alice.user_id, "Alice")
    peer = _actor(auth, bob.user_id, "Bob")
    workspace_commands = WorkspaceCommands(audit=lambda *args, **kwargs: None)
    with database.immediate_session() as unit:
        workspace = workspace_commands.create(unit, AccessContext(actor), name="Comparison team")
        unit.add(
            WorkspaceMembership(workspace_id=workspace["id"], user_id=bob.user_id, role="member")
        )
    sessions = SessionService(database)
    target = sessions.create("Comparison", workspace_id=workspace["id"])
    other = sessions.create("Other comparison", workspace_id=workspace["id"])
    artifacts = ArtifactService(database, paths)
    fixture = SimpleNamespace(
        paths=paths,
        database=database,
        auth=auth,
        alice=alice,
        bob=bob,
        actor=actor,
        peer=peer,
        workspace=workspace["id"],
        target=target,
        other=other,
        artifacts=artifacts,
        context=ComparisonExportContext(database, paths, artifacts),
    )
    yield fixture
    database.dispose()


def _generation(f, record=None):
    record = record or f.target
    artifacts = []
    for amplitude in (1000, 2000):
        path = f.paths.uploads / f"{m.new_id()}.wav"
        _pcm(path, amplitude)
        artifacts.append(
            f.artifacts.register(path, kind="audio", role="take", session_id=record.id)
        )
    with f.database.immediate_session() as unit:
        plan = m.GenerationPlan(session_id=record.id)
        unit.add(plan)
        unit.flush()
        revision = m.GenerationPlanRevision(
            plan_id=plan.id, revision_number=1, content_hash="plan-hash"
        )
        unit.add(revision)
        unit.flush()
        segment = m.GenerationSegment(
            plan_revision_id=revision.id,
            ordinal=0,
            text="Hello.",
            source_segment_ids_json=["owned-source"],
            language="en",
        )
        unit.add(segment)
        unit.flush()
        runs = [
            m.GenerationRun(
                session_id=record.id,
                plan_revision_id=revision.id,
                sequence_number=sequence,
                status="completed",
                settings_snapshot_json={"voice": "owned"},
            )
            for sequence in (1, 2)
        ]
        unit.add_all(runs)
        unit.flush()
        takes = [
            m.AudioTake(
                generation_segment_id=segment.id,
                generation_run_id=run.id,
                artifact_id=artifact.id,
                status="completed",
                duration_ms=100,
            )
            for run, artifact in zip(runs, artifacts, strict=True)
        ]
        unit.add_all(takes)
        unit.flush()
        plan.active_revision_id = revision.id
        result = SimpleNamespace(
            record=record,
            revision_id=revision.id,
            segment_id=segment.id,
            runs=[row.id for row in runs],
            takes=[row.id for row in takes],
            artifacts=artifacts,
        )
        for artifact in artifacts:
            unit.execute(
                text("UPDATE artifacts SET metadata_json = :bad WHERE id = :id"),
                {"bad": "{private-unrelated-invalid", "id": artifact.id},
            )
    return result


def _request(g, *, run=1, take=None, side="after"):
    return {
        "clips": [
            {
                "generation_run_id": g.runs[run],
                "segment_id": g.segment_id,
                "audio_version_side": side,
                **({"take_id": take} if take else {}),
            }
        ],
        "gap_ms": 25,
    }


def _prepare(f, request):
    with execution_actor_scope(f.actor):
        return prepare_comparison_export(f.database, f.paths, f.target.id, request)


def _render(f, prepared, progress=None, cancel=None):
    with execution_actor_scope(f.actor):
        return render_comparison_export(
            f.context,
            prepared.job_payload(),
            progress or (lambda *_args: None),
            cancel or threading.Event(),
        )


def _token(f, scopes=frozenset({"app.read", "app.run"})):
    token, _raw = f.auth.create_api_token(
        "Comparison token",
        user_id=f.alice.user_id,
        scopes=scopes,
        resource_grants=CredentialResources(project_ids=frozenset({f.target.project_id})),
    )
    f.actor = UserActor(
        f.alice.user_id, 1, str(token.subject), "api_token", scopes, token_id=token.id
    )
    return token


@contextmanager
def _lease(f, payload, *, kind="export.comparison", session_id=None):
    queue = JobQueue(f.database)
    with execution_actor_scope(f.actor):
        queued = queue.enqueue(kind, payload, session_id=session_id or f.target.id)
        job = queue.claim("comparison-worker", lease_seconds=900)
    assert job is not None and queued.id == job.id
    value: JobLease = {
        "job_id": job.id,
        "owner_id": "comparison-worker",
        "lease_generation": job.lease_generation,
    }
    marker = current_job_lease.set(value)
    try:
        with execution_actor_scope(f.actor):
            yield job, {**payload, "_job_id": job.id, "_lease_generation": job.lease_generation}
    finally:
        current_job_lease.reset(marker)


def _output(f, result):
    with f.database.snapshot_session() as unit:
        paths = list(
            unit.execute(
                select(m.Artifact.id, m.Artifact.relative_path).where(
                    m.Artifact.id.in_(list(result["artifact_ids"].values()))
                )
            )
        )
    by_id = dict(paths)
    return f.paths.managed_path(by_id[result["output_artifact_id"]]), f.paths.managed_path(
        by_id[result["manifest_artifact_id"]]
    )


def _count(f):
    with f.database.snapshot_session() as unit:
        return unit.scalar(select(func.count()).select_from(m.Artifact))


@pytest.mark.parametrize("mode", ["solo", "member", "peer_departure", "read_run_token"])
def test_current_actual_job_ordinary_roots_and_membership(comparison, mode):
    f = comparison
    if mode == "solo":
        with f.database.snapshot_session() as unit:
            home = unit.scalar(
                select(Workspace.id).where(Workspace.personal_owner_user_id == f.alice.user_id)
            )
        f.target = SessionService(f.database).create("Solo comparison", workspace_id=home)
    elif mode in {"member", "peer_departure"}:
        if mode == "peer_departure":
            with f.database.immediate_session() as unit:
                unit.execute(
                    delete(WorkspaceMembership).where(
                        WorkspaceMembership.user_id == f.alice.user_id,
                        WorkspaceMembership.workspace_id == f.workspace,
                    )
                )
        f.actor = f.peer
    else:
        _token(f)
    g = _generation(f)
    prepared = _prepare(f, _request(g))
    with _lease(f, prepared.job_payload()) as (_job, payload):
        result = render_comparison_export(
            f.context, payload, lambda *_args: None, threading.Event()
        )
    audio, manifest_path = _output(f, result)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["clips"][0]["take_id"] == g.takes[1]
    assert manifest["sound_mix_included"] is False and result["clip_count"] == 1
    with wave.open(str(audio), "rb") as reader:
        assert reader.getnframes() == 800
        assert struct.unpack("<h", reader.readframes(1))[0] == 2000
    with f.database.snapshot_session() as unit:
        roots = list(
            unit.execute(
                select(m.Artifact.session_id, m.Artifact.project_id, m.Artifact.workspace_id).where(
                    m.Artifact.id.in_(list(result["artifact_ids"].values()))
                )
            )
        )
        assert all(
            row.session_id == f.target.id and row.project_id == f.target.project_id for row in roots
        )


def _recast(f, g):
    job = JobQueue(f.database).enqueue(
        "generation.run", {"session_id": f.target.id}, session_id=f.target.id
    )
    with f.database.immediate_session() as unit:
        managed_job = unit.get(m.Job, job.id)
        assert managed_job is not None
        managed_job.status = "succeeded"
        run = unit.get(m.GenerationRun, g.runs[1])
        assert run is not None
        baseline = m.AudioTake(
            generation_segment_id=g.segment_id,
            generation_run_id=run.id,
            artifact_id=g.artifacts[0].id,
            parent_take_id=g.takes[0],
            status="completed",
            duration_ms=100,
        )
        unit.add(baseline)
        unit.flush()
        run.job_id = job.id
        run.audio_version_json = {
            "version": 1,
            "kind": "recast",
            "before_take_ids": {g.segment_id: baseline.id},
            "after_take_ids": {g.segment_id: g.takes[1]},
        }
    return job.id


def test_cutoff_explicit_historical_take_and_recast_sides_remain_exact(comparison):
    f = comparison
    g = _generation(f)
    request = {"clips": [_request(g, run=0)["clips"][0], _request(g)["clips"][0]], "gap_ms": 25}
    selected = _prepare(f, request)
    assert [row["take_id"] for row in selected.clips] == g.takes
    rendered = _render(f, selected)
    audio, _manifest = _output(f, rendered)
    with wave.open(str(audio), "rb") as reader:
        assert reader.getnframes() == 1800
    historical = _prepare(f, _request(g, take=g.takes[0]))
    assert historical.clips[0]["artifact_id"] == g.artifacts[0].id
    _recast(f, g)
    for side, artifact in (("before", g.artifacts[0]), ("after", g.artifacts[1])):
        recast = _prepare(f, _request(g, side=side))
        assert recast.clips[0]["artifact_id"] == artifact.id
        assert recast.clips[0]["audio_version_selection_hash"]
        assert _render(f, recast)["artifact_ids"]


@pytest.mark.parametrize("denial", ["private", "viewer", "foreign_workspace", "credential"])
def test_denied_target_before_request_validation_metadata_or_source_io(
    comparison, monkeypatch, denial
):
    f = comparison
    g = _generation(f)
    if denial in {"private", "viewer"}:
        with f.database.immediate_session() as unit:
            unit.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.user_id == f.alice.user_id,
                    WorkspaceMembership.workspace_id == f.workspace,
                )
            )
            if denial == "viewer":
                unit.add(
                    ProjectMembership(
                        project_id=f.target.project_id, user_id=f.alice.user_id, role="viewer"
                    )
                )
    elif denial == "foreign_workspace":
        f.actor = _actor(f.auth, f.bob.user_id, "Bob")
        with f.database.snapshot_session() as unit:
            home = unit.scalar(
                select(Workspace.id).where(Workspace.personal_owner_user_id == f.alice.user_id)
            )
        f.target = SessionService(f.database).create("Private solo", workspace_id=home)
    else:
        _token(f)
        with f.database.immediate_session() as unit:
            unit.execute(
                delete(ApiTokenProjectGrant).where(
                    ApiTokenProjectGrant.token_id == f.actor.token_id
                )
            )
    forbidden = Mock(side_effect=AssertionError("denied source IO"))
    monkeypatch.setattr(render_module, "sha256_file", forbidden)
    with execution_actor_scope(f.actor), pytest.raises((AccessDenied, KeyError, PermissionError)):
        prepare_comparison_export(f.database, f.paths, f.target.id, {"deliberately": "invalid"})
    forbidden.assert_not_called()
    assert g.artifacts[0].id


@pytest.mark.parametrize("poison", ["run", "revision", "segment", "take", "artifact", "recast_job"])
def test_foreign_json_is_filtered_before_hydration_and_recast_job_is_scalar(comparison, poison):
    f = comparison
    g = _generation(f)
    foreign = _generation(f, f.other)
    _token(f)
    request = _request(g)
    if poison == "recast_job":
        job_id = _recast(f, g)
        with f.database.immediate_session() as unit:
            unit.execute(
                text("UPDATE jobs SET payload_json = :bad WHERE id = :id"),
                {"bad": "{foreign-invalid", "id": job_id},
            )
        assert _prepare(f, request).clips[0]["take_id"] == g.takes[1]
        return
    with f.database.immediate_session() as unit:
        unit.execute(
            text("UPDATE generation_runs SET settings_snapshot_json = :bad WHERE id = :id"),
            {"bad": "{foreign-invalid", "id": foreign.runs[1]},
        )
        unit.execute(
            text("UPDATE generation_plan_revisions SET settings_json = :bad WHERE id = :id"),
            {"bad": "{foreign-invalid", "id": foreign.revision_id},
        )
        unit.execute(
            text("UPDATE generation_segments SET speech_plan_json = :bad WHERE id = :id"),
            {"bad": "{foreign-invalid", "id": foreign.segment_id},
        )
        if poison == "run":
            request["clips"][0]["generation_run_id"] = foreign.runs[1]
        elif poison == "revision":
            unit.execute(
                text("UPDATE generation_runs SET plan_revision_id = :revision WHERE id = :id"),
                {"revision": foreign.revision_id, "id": g.runs[1]},
            )
        elif poison == "segment":
            request["clips"][0]["segment_id"] = foreign.segment_id
        elif poison == "take":
            request["clips"][0]["take_id"] = foreign.takes[1]
        else:
            unit.execute(
                text("UPDATE audio_takes SET artifact_id = :artifact WHERE id = :id"),
                {"artifact": foreign.artifacts[1].id, "id": g.takes[1]},
            )
    with pytest.raises((KeyError, ValueError, AccessDenied)):
        _prepare(f, request)


@pytest.mark.parametrize(
    "wrong", ["kind", "root", "private", "expired", "generation", "lost_actor", "payload"]
)
def test_real_job_headers_private_selectors_and_context_precede_payload_reads(
    comparison, monkeypatch, wrong
):
    f = comparison
    g = _generation(f)
    prepared = _prepare(f, _request(g))
    with _lease(
        f,
        prepared.job_payload(),
        kind="text.prepare" if wrong == "kind" else "export.comparison",
        session_id=f.other.id if wrong == "root" else None,
    ) as (job, payload):
        with f.database.immediate_session() as unit:
            managed = unit.get(m.Job, job.id)
            assert managed is not None
            if wrong == "expired":
                managed.lease_expires_at = m.utcnow() - timedelta(seconds=1)
            elif wrong == "generation":
                managed.lease_generation += 1
            elif wrong == "payload":
                managed.payload_json = {**managed.payload_json, "gap_ms": 26}
            unit.flush()
            if wrong in {"kind", "root"}:
                unit.execute(
                    text("UPDATE jobs SET payload_json = :bad WHERE id = :id"),
                    {"bad": "{foreign-invalid", "id": job.id},
                )
        if wrong == "private":
            payload["_job_id"] = m.new_id()
        forbidden = Mock(side_effect=AssertionError("bad lease reached media"))
        monkeypatch.setattr(render_module, "sha256_file", forbidden)
        with (
            execution_actor_scope(None if wrong == "lost_actor" else f.actor),
            pytest.raises((AccessDenied, PermissionError, InterruptedError)),
        ):
            render_comparison_export(f.context, payload, lambda *_args: None, threading.Event())
        forbidden.assert_not_called()
    assert _count(f) == 2


def test_source_header_change_at_media_boundary_aborts(comparison, monkeypatch):
    f = comparison
    g = _generation(f)
    prepared = _prepare(f, _request(g))
    original = render_module.assemble_audio_plan

    def assemble(*args, **kwargs):
        with f.database.immediate_session() as unit:
            unit.execute(
                text("UPDATE artifacts SET size_bytes = size_bytes + 1 WHERE id = :id"),
                {"id": g.artifacts[1].id},
            )
        kwargs["before_io"]()
        return original(*args, **kwargs)

    monkeypatch.setattr(render_module, "assemble_audio_plan", assemble)
    with pytest.raises(AccessDenied):
        _render(f, prepared)
    assert _count(f) == 2


@pytest.mark.parametrize("boundary", ["media_role", "progress_token", "writer_resource"])
def test_current_role_token_and_resource_loss_abort_without_cancel_conversion(
    comparison, monkeypatch, boundary
):
    f = comparison
    g = _generation(f)
    if boundary != "media_role":
        _token(f)
    prepared = _prepare(f, _request(g))

    def progress(value, _message):
        pass

    if boundary == "media_role":
        original = render_module.assemble_audio_plan

        def assemble(*args, **kwargs):
            with f.database.immediate_session() as unit:
                unit.execute(
                    delete(WorkspaceMembership).where(
                        WorkspaceMembership.user_id == f.alice.user_id,
                        WorkspaceMembership.workspace_id == f.workspace,
                    )
                )
            kwargs["before_io"]()
            return original(*args, **kwargs)

        monkeypatch.setattr(render_module, "assemble_audio_plan", assemble)
    elif boundary == "progress_token":

        def progress(value, _message):
            if value == 0.03:
                with f.database.immediate_session() as unit:
                    unit.execute(
                        text("UPDATE api_tokens SET scopes_json = :scopes WHERE id = :id"),
                        {"scopes": '["app.read"]', "id": f.actor.token_id},
                    )
    else:
        original = f.database.immediate_session

        @contextmanager
        def immediate(*args, **kwargs):
            with original(*args, **kwargs) as unit:
                unit.execute(
                    delete(ApiTokenProjectGrant).where(
                        ApiTokenProjectGrant.token_id == f.actor.token_id
                    )
                )
                yield unit

        monkeypatch.setattr(f.database, "immediate_session", immediate)
    with pytest.raises((AccessDenied, KeyError, PermissionError)) as error:
        _render(f, prepared, progress=progress)
    assert not isinstance(error.value, MediaProcessCancelled)
    assert _count(f) == 2


@pytest.mark.parametrize("drift", ["bytes", "provenance"])
def test_source_or_provenance_drift_cannot_publish(comparison, drift):
    f = comparison
    g = _generation(f)
    prepared = _prepare(f, _request(g))

    def progress(value, _message):
        if value == 0.82:
            if drift == "bytes":
                _pcm(f.paths.managed_path(g.artifacts[1].relative_path), 3000)
            else:
                with f.database.immediate_session() as unit:
                    unit.execute(
                        text(
                            "UPDATE generation_segments SET revision = revision + 1 WHERE id = :id"
                        ),
                        {"id": g.segment_id},
                    )

    with pytest.raises(ValueError):
        _render(f, prepared, progress=progress)
    assert _count(f) == 2


@pytest.mark.parametrize("failed_role", ["comparison_export.audio", "comparison_export.manifest"])
def test_partial_pair_rollback_preserves_previous_pair_and_sources(
    comparison, monkeypatch, failed_role
):
    f = comparison
    g = _generation(f)
    prepared = _prepare(f, _request(g))
    previous = _render(f, prepared)
    old_paths = _output(f, previous)
    old_bytes = [path.read_bytes() for path in old_paths]
    original = f.artifacts.register_in_session

    def register(*args, **kwargs):
        artifact = original(*args, **kwargs)
        if kwargs["role"] == failed_role:
            raise RuntimeError("pair registration fault")
        return artifact

    monkeypatch.setattr(f.artifacts, "register_in_session", register)
    with pytest.raises(RuntimeError, match="pair registration fault"):
        _render(f, prepared)
    assert _count(f) == 4
    assert [path.read_bytes() for path in old_paths] == old_bytes
    assert all(f.paths.managed_path(row.relative_path).exists() for row in g.artifacts)
    assert sorted(path.name for path in old_paths) == sorted(
        path.name for path in old_paths[0].parent.iterdir() if path.is_file()
    )


def test_real_cancel_event_aborts_publication(comparison):
    f = comparison
    g = _generation(f)
    prepared = _prepare(f, _request(g))
    cancel = threading.Event()

    def progress(value, _message):
        if value == 0.03:
            cancel.set()

    with pytest.raises(MediaProcessCancelled):
        _render(f, prepared, progress=progress, cancel=cancel)
    assert _count(f) == 2


@pytest.mark.parametrize("boundary", ["uncertain", "success_no_cleanup", "late_progress"])
def test_committed_pair_is_retained_without_success_cleanup_queries(
    comparison, monkeypatch, boundary
):
    f = comparison
    g = _generation(f)
    prepared = _prepare(f, _request(g))
    forbidden = Mock(side_effect=AssertionError("successful cleanup SQL"))
    if boundary != "late_progress":
        original = f.database.immediate_session

        @contextmanager
        def immediate(*args, **kwargs):
            with original(*args, **kwargs) as unit:
                yield unit
            if boundary == "uncertain":
                raise RuntimeError("uncertain commit")
            monkeypatch.setattr(f.database, "session", forbidden)

        monkeypatch.setattr(f.database, "immediate_session", immediate)

    def progress(value, _message):
        if value == 1.0 and boundary == "late_progress":
            raise RuntimeError("late progress")

    if boundary == "success_no_cleanup":
        result = _render(f, prepared, progress=progress)
        assert result["artifact_ids"]
        forbidden.assert_not_called()
    else:
        with pytest.raises(RuntimeError, match="uncertain|late progress"):
            _render(f, prepared, progress=progress)
    with f.database.snapshot_session() as unit:
        paths = list(
            unit.scalars(
                select(m.Artifact.relative_path).where(
                    m.Artifact.role.in_(("comparison_export.audio", "comparison_export.manifest"))
                )
            )
        )
    assert len(paths) == 2 and all(f.paths.managed_path(path).exists() for path in paths)


def test_none_has_zero_policy_sql_and_original_media_kwargs(comparison, monkeypatch):
    f = comparison
    g = _generation(f)
    with execution_actor_scope(None):
        unit = Mock()
        assert require_comparison_export_access(unit, "ignored") is None
        assert not unit.mock_calls
        prepared = prepare_comparison_export(f.database, f.paths, f.target.id, _request(g))
        preferred = Mock(wraps=render_module.preferred_pcm_format)
        assemble = Mock(wraps=render_module.assemble_audio_plan)
        monkeypatch.setattr(render_module, "preferred_pcm_format", preferred)
        monkeypatch.setattr(render_module, "assemble_audio_plan", assemble)
        result = render_comparison_export(
            f.context, prepared.job_payload(), lambda *_args: None, threading.Event()
        )
    assert result["artifact_ids"]
    assert "before_io" not in preferred.call_args.kwargs
    assert "before_io" not in assemble.call_args.kwargs
