"""Direct finite-account/lease speech execution tests with synthetic completions."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import delete, event, func, select, text

from pandrator.logic import llm_handler
from pandrator.logic.llm_handler import ChatCompletionResult
from pandrator.web import models as m
from pandrator.web.access.contracts import AccessContext, AccessDenied, ProjectAction
from pandrator.web.access.current_session_policy import require_current_session_access
from pandrator.web.access.delivery_execution_policy import capture_delivery_execution
from pandrator.web.access.source_preparation_policy import (
    require_speech_optimization_access,
    source_processing_child_scope,
)
from pandrator.web.accounts.commands import AccountActor, AccountCommands
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.accounts.execution_actor import (
    HostActor,
    UserActor,
    current_execution_actor,
    execution_actor_scope,
)
from pandrator.web.accounts.models import Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import (
    ApiTokenProjectGrant,
    ApiTokenWorkspaceGrant,
)
from pandrator.web.accounts.workspace_commands import WorkspaceCommands
from pandrator.web.agentic_runs import (
    AgenticRunStore,
    operation_settings_snapshot,
    stable_payload_hash,
)
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import AuthService
from pandrator.web.database import Database
from pandrator.web.job_execution import JobLease, current_job_lease
from pandrator.web.jobs import JobQueue
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.pronunciations import PronunciationLibrary
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_speech_execution import (
    SpeechOptimizationExecution,
    capture_speech_optimization,
)
from pandrator.web.workflow_speech_optimization import (
    SpeechOptimizationContext,
    optimize_tts,
    save_speech_plan_proposals,
)
from tests.web_test_support import prepare_web_test_data_root

SCOPES = frozenset({"app.read", "app.write", "app.run", "app.workspaces.enroll"})
SRT = "1\n00:00:00,000 --> 00:00:01,000\nHello there.\n"


def _interactive(auth, user_id, name):
    issued = auth.browser_sessions.issue(
        user_id, 1, subject=name, principal_kind="owner_session", scopes=SCOPES
    )
    return UserActor(user_id, 1, name, "owner_session", SCOPES, browser_session_id=issued.record_id)


@pytest.fixture
def speech(tmp_path: Path, monkeypatch):
    from pandrator.web import speech_optimization_llm, speech_planning

    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    auth = AuthService(database)
    initial = auth.initial_account()
    commands = AccountCommands(database)
    admin = AccountActor(initial.user_id, initial.auth_revision, frozenset({"app.admin"}))
    alice = commands.create_user(admin, login_name="speech-alice", display_name="Alice")
    bob = commands.create_user(admin, login_name="speech-bob", display_name="Bob")
    actor = _interactive(auth, alice.user_id, "Alice")
    peer = _interactive(auth, bob.user_id, "Bob")
    workspace_commands = WorkspaceCommands(audit=lambda *args, **kwargs: None)
    with database.immediate_session() as unit:
        workspace = workspace_commands.create(unit, AccessContext(actor), name="Speech team")
        unit.add(
            WorkspaceMembership(workspace_id=workspace["id"], user_id=bob.user_id, role="member")
        )
    sessions = SessionService(database)
    target = sessions.create("Speech target", workspace_id=workspace["id"])
    other = sessions.create("Other target", workspace_id=workspace["id"])
    artifacts = ArtifactService(database, paths)
    settings = dict(
        llm_provider_configs=[],
        llm_default_model="synthetic",
        request_timeout_seconds=180,
        llm_tts_document_batch_size=1,
        llm_concurrent_calls=1,
        apply_reviewed_pronunciations=False,
    )
    hydrate = Mock(side_effect=lambda requested, *_args, **_kwargs: {**settings, **requested})
    record_usage = Mock()

    def begin(*, payload, kind, source_artifact, requested_settings, instructions, usage_settings):
        store = AgenticRunStore(database)
        digest, stored = operation_settings_snapshot(
            kind=kind,
            requested_settings=requested_settings,
            instructions=instructions,
            usage_settings=usage_settings,
        )
        run = store.start(
            kind=kind,
            session_id=payload["session_id"],
            source_artifact=source_artifact,
            settings_hash=digest,
            settings=stored,
            job_id=payload.get("_job_id"),
            requested_run_id=payload.get("_agent_run_id"),
        )

        def checkpoint(unit_key, output, **kwargs):
            store.checkpoint(
                run.id,
                unit_key=unit_key,
                ordinal=0,
                input_value={
                    "kind": output.get("kind"),
                    "stage": output.get("stage"),
                    "source_hash": output.get("source_hash"),
                    "original_indices": output.get("original_indices", []),
                },
                output=output,
            )

        return store, run, checkpoint

    context = SpeechOptimizationContext(
        database,
        artifacts,
        artifacts.resolve,
        lambda session_id: paths.sessions / target.storage_key,
        Mock(),
        hydrate,
        begin,
        save_speech_plan_proposals,
        stable_payload_hash,
        record_usage,
        lambda callback, start, end: (
            lambda value, message=None: callback(start + (end - start) * value, message)
        ),
        m.new_id,
        m.utcnow,
    )
    details = {
        "model": "approved-model",
        "provider_key": "approved-provider",
        "request_overrides": {},
        "model_record": {},
    }
    build = Mock(return_value=(SimpleNamespace(request_timeout_seconds=180), "synthetic"))
    monkeypatch.setattr(speech_optimization_llm, "build_llm_settings", build)
    monkeypatch.setattr(llm_handler, "_resolve_model_request_details", lambda *_args: dict(details))
    monkeypatch.setattr(speech_planning, "_hunspell_unknown_words", lambda *_args: set())
    seen = []

    def response_for(messages):
        user = next(row["content"] for row in messages if row["role"] == "user")
        payload = json.loads(user[user.index("{") :])
        if "Input JSON:" in user:
            return {
                "items": [{"index": row["index"], "text": row["text"]} for row in payload["items"]]
            }

        def plan(case):
            return {
                "case_id": case["case_id"],
                **(
                    {"speech_template": case["protected_speech_template"]}
                    if "protected_speech_template" in case
                    else {}
                ),
                "decisions": [
                    {"span_id": row["id"], "action": "keep", "confidence": "high"}
                    for row in case["unresolved_candidates"]
                ],
                "discoveries": [],
                "prosody": [],
            }

        if "items" in payload:
            return {
                "items": [
                    {"batch_item_id": row["batch_item_id"], "plan": plan(row["case"])}
                    for row in payload["items"]
                ]
            }
        return plan(payload)

    def transport(
        *, messages, model_name, llm_settings, cancel_event, before_request, response_callback
    ):
        before_request()
        seen.append((current_execution_actor.get(), current_job_lease.get()))
        result = ChatCompletionResult(
            content=json.dumps(response_for(messages)),
            model="UNTRUSTED_MODEL_SENTINEL",
            usage={
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "secret": "RAW_SENTINEL",
                "prompt_tokens_details": {"cached_tokens": 3, "secret": "RAW_SENTINEL"},
            },
            cost=0.25,
            cost_source="response_usage",
        )
        response_callback(result)
        return result

    transport_mock = Mock(side_effect=transport)
    monkeypatch.setattr(llm_handler, "chat_completion_with_metadata", transport_mock)
    fixture = SimpleNamespace(
        paths=paths,
        database=database,
        auth=auth,
        actor=actor,
        peer=peer,
        alice=alice,
        bob=bob,
        workspace=workspace["id"],
        target=target,
        other=other,
        artifacts=artifacts,
        context=context,
        settings=settings,
        hydrate=hydrate,
        build=build,
        details=details,
        seen=seen,
        transport=transport_mock,
        transport_func=transport,
        usage=record_usage,
    )
    yield fixture
    database.dispose()


def _source(f, suffix=".txt", *, record=None, metadata=None):
    record = record or f.target
    value = (
        SRT
        if suffix == ".srt"
        else (
            json.dumps([{"text": "Hello there.", "language": "en"}])
            if suffix == ".json"
            else "Hello there."
        )
    )
    path = f.paths.uploads / f"{m.new_id()}{suffix}"
    path.write_text(value, encoding="utf-8")
    artifact = f.artifacts.register(
        path,
        kind="srt" if suffix == ".srt" else "source",
        role="transcription",
        session_id=record.id,
        metadata=metadata,
    )
    return artifact, path


def _payload(f, source, **extra):
    return dict(session_id=f.target.id, source_artifact_id=source.id, **extra)


def _run(f, source, **extra):
    with execution_actor_scope(f.actor):
        return optimize_tts(
            f.context, _payload(f, source, **extra), lambda *_args: None, threading.Event()
        )


def _counts(f):
    with f.database.snapshot_session() as unit:
        return tuple(
            unit.scalar(select(func.count()).select_from(model)) or 0
            for model in (m.Artifact, m.AgentRun, m.AgentStep, m.UsageEvent, m.PronunciationEntry)
        )


def _deactivate(f):
    with f.database.immediate_session() as unit:
        unit.execute(text("UPDATE users SET is_active = 0 WHERE id = :id"), {"id": f.alice.user_id})


def _project_actor(f, *, scopes=SCOPES):
    token, _raw = f.auth.create_api_token(
        "Speech scoped",
        user_id=f.alice.user_id,
        scopes=scopes,
        resource_grants=CredentialResources(project_ids=frozenset({f.target.project_id})),
    )
    return UserActor(f.alice.user_id, 1, str(token.subject), "api_token", scopes, token_id=token.id)


@contextmanager
def _lease(f, source, *, kind="text.optimize_tts", payload=None, session_id=None):
    jobs = JobQueue(f.database)
    with execution_actor_scope(f.actor):
        queued = jobs.enqueue(
            kind, payload or _payload(f, source), session_id=session_id or f.target.id
        )
        job = jobs.claim("speech-worker", lease_seconds=900)
    assert job is not None and job.id == queued.id
    value: JobLease = {
        "job_id": job.id,
        "owner_id": "speech-worker",
        "lease_generation": job.lease_generation,
    }
    token = current_job_lease.set(value)
    try:
        with execution_actor_scope(f.actor):
            yield (
                job,
                {
                    **_payload(f, source),
                    "_job_id": job.id,
                    "_lease_generation": job.lease_generation,
                },
            )
    finally:
        current_job_lease.reset(token)


@pytest.mark.parametrize(
    "suffix,peer", [(".txt", False), (".json", False), (".srt", False), (".srt", True)]
)
def test_authorized_current_outputs_and_peer_after_creator_departure(speech, suffix, peer):
    f = speech
    source, source_path = _source(f, suffix)
    if peer:
        with f.database.immediate_session() as unit:
            unit.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.user_id == f.alice.user_id,
                    WorkspaceMembership.workspace_id == f.workspace,
                )
            )
        f.actor = f.peer
    result = _run(f, source)
    with f.database.snapshot_session() as unit:
        output = unit.get(m.Artifact, result["artifact_id"])
        run = unit.get(m.AgentRun, result["agent_run_id"])
        receipts = list(unit.scalars(select(m.UsageEvent)))
        assert output is not None and output.role == "tts_optimized"
        assert output.session_id == f.target.id and output.workspace_id == f.workspace
        assert output.metadata_json["requested_settings_hash"] == stable_payload_hash({})
        assert run is not None and run.status == "completed" and run.result_artifact_id == output.id
        assert len(receipts) == 1 and receipts[0].model_id == "approved-model"
        assert receipts[0].raw_usage_json == {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": 3},
        }
        assert result["usage"] == {
            "input_tokens": 11,
            "output_tokens": 7,
            "cached_input_tokens": 0,
            "total_tokens": 18,
            "response_count": 1,
        }
        if suffix == ".srt":
            assert output.metadata_json["revision_id"]
    assert source_path.exists()
    assert all(actor == f.actor for actor, _lease_value in f.seen)
    f.usage.assert_not_called()


@pytest.mark.parametrize("denial", ["private", "viewer", "no_run", "foreign_workspace"])
def test_denial_before_metadata_body_settings_run_or_model(speech, monkeypatch, denial):
    f = speech
    source, _path = _source(f)
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
        f.actor = _interactive(f.auth, f.alice.user_id, "Alice")
    elif denial == "no_run":
        f.actor = _project_actor(f, scopes=frozenset({"app.read", "app.write"}))
    else:
        with f.database.snapshot_session() as unit:
            home = unit.scalar(
                select(Workspace.id).where(Workspace.personal_owner_user_id == f.bob.user_id)
            )
        foreign = SessionService(f.database).create("Private", workspace_id=home)
        source, _path = _source(f, record=foreign)
    before = _counts(f)
    monkeypatch.setattr(Path, "read_text", Mock(side_effect=AssertionError("source read")))
    with execution_actor_scope(f.actor), pytest.raises((AccessDenied, KeyError, PermissionError)):
        optimize_tts(f.context, _payload(f, source), Mock(), threading.Event())
    assert _counts(f) == before
    f.hydrate.assert_not_called()
    f.transport.assert_not_called()


def _document(f, source, session_id, *, poison=False):
    with f.database.immediate_session() as unit:
        document = m.Document(session_id=session_id, stage="transcription")
        unit.add(document)
        unit.flush()
        revision = m.DocumentRevision(
            document_id=document.id, revision_number=1, content_hash="source"
        )
        unit.add(revision)
        unit.flush()
        segment = m.Segment(
            revision_id=revision.id,
            ordinal=0,
            start_ms=0,
            end_ms=1000,
            text="Hello there.",
            speaker="SECRET" if poison else "Alice",
        )
        unit.add(segment)
        unit.flush()
        managed = unit.get(m.Artifact, source.id)
        assert managed is not None
        managed.metadata_json = {
            "revision_id": revision.id,
            "speech_markup": {"1": '<segment id="1"><narrator>Hello there.</narrator></segment>'},
        }
        if poison:
            unit.execute(
                text("UPDATE segments SET metadata_json = '{broken' WHERE id = :id"),
                {"id": segment.id},
            )
        return revision.id, segment.id


@pytest.mark.parametrize("bytes_only", [False, True])
def test_poisoned_foreign_parent_never_hydrates_and_byte_only_skips_source_json(speech, bytes_only):
    f = speech
    source, source_path = _source(f, ".srt", record=f.other if bytes_only else f.target)
    _document(f, source, f.other.id if not bytes_only else f.target.id, poison=True)
    if bytes_only:
        with f.database.immediate_session() as unit:
            asset = m.SourceAsset(
                workspace_id=f.workspace, artifact_id=source.id, display_name="Attached", kind="srt"
            )
            unit.add(asset)
            unit.flush()
            unit.add(m.SessionSource(session_id=f.target.id, source_asset_id=asset.id))
            unit.execute(
                text("UPDATE artifacts SET metadata_json = '{broken' WHERE id = :id"),
                {"id": source.id},
            )
        f.actor = _project_actor(f)
    statements = []

    def capture(_connection, _cursor, sql, parameters, _context, _many):
        statements.append((sql.lower(), parameters))

    event.listen(f.database.engine, "before_cursor_execute", capture)
    try:
        result = _run(f, source)
    finally:
        event.remove(f.database.engine, "before_cursor_execute", capture)
    with f.database.snapshot_session() as unit:
        output = unit.get(m.Artifact, result["artifact_id"])
        assert output is not None
        child = unit.scalar(
            select(m.Segment).where(m.Segment.revision_id == output.metadata_json["revision_id"])
        )
        assert child is not None and child.speaker != "SECRET"
        assert unit.scalar(select(func.count()).select_from(m.SegmentLineage)) == 0
    if bytes_only:
        assert not any(
            "select" in sql and "artifacts.metadata_json" in sql and source.id in parameters
            for sql, parameters in statements
        )
        assert not any("select" in sql and "from segments" in sql for sql, _ in statements)
    assert source_path.exists()


@pytest.mark.parametrize(
    "boundary", ["progress", "attempt", "checkpoint", "annotation", "publication"]
)
def test_revocation_at_effect_boundaries_preserves_only_authorized_prior_work(
    speech, monkeypatch, boundary
):
    f = speech
    source, source_path = _source(f)
    settings = {"llm_tts_annotation_mode": "dialogue"} if boundary == "annotation" else {}

    def progress(*_args):
        if boundary == "progress":
            _deactivate(f)

    if boundary == "progress":
        pass
    elif boundary == "attempt":

        def transport(**kwargs):
            kwargs["before_request"]()
            _deactivate(f)
            kwargs["before_request"]()
            raise AssertionError("revoked transport")

        f.transport.side_effect = transport
    elif boundary == "checkpoint":
        original = SpeechOptimizationExecution.checkpoint

        def checkpoint(self, *args, **kwargs):
            _deactivate(f)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(SpeechOptimizationExecution, "checkpoint", checkpoint)
    elif boundary == "annotation":
        from pandrator.web import speech_structure_analysis

        def annotate(database, _session_id, _texts, **kwargs):
            _deactivate(f)
            with database.immediate_session() as unit:
                kwargs["before_data_access"](unit)
            raise AssertionError("revoked annotation")

        monkeypatch.setattr(speech_structure_analysis, "annotate_speech_units", annotate)
    else:
        original = f.artifacts.prepare_registration

        def prepare(*args, **kwargs):
            result = original(*args, **kwargs)
            _deactivate(f)
            return result

        monkeypatch.setattr(f.artifacts, "prepare_registration", prepare)
    with execution_actor_scope(f.actor), pytest.raises((AccessDenied, PermissionError, KeyError)):
        optimize_tts(f.context, _payload(f, source, settings=settings), progress, threading.Event())
    counts = _counts(f)
    assert counts[0] == 1
    assert counts[3] == (0 if boundary in {"progress", "attempt"} else 1)
    assert counts[2] == (1 if boundary in {"annotation", "publication"} else 0)
    assert source_path.exists()
    assert not list((f.paths.sessions / f.target.storage_key).glob("tts-optimized-*"))


@pytest.mark.parametrize(
    "wrong",
    [
        "kind",
        "source",
        "session",
        "root",
        "owner",
        "generation",
        "expired",
        "missing_injected",
        "forged_injected",
        "missing_context",
    ],
)
def test_actual_lease_and_payload_binding_are_required(speech, wrong):
    f = speech
    source, _path = _source(f)
    queued_payload = None
    queued_session = None
    if wrong == "root":
        other_source, _other_path = _source(f, record=f.other)
        queued_payload = {"session_id": f.other.id, "source_artifact_id": other_source.id}
        queued_session = f.other.id
    with _lease(
        f,
        source,
        kind="text.prepare" if wrong == "kind" else "text.optimize_tts",
        payload=queued_payload,
        session_id=queued_session,
    ) as (
        job,
        payload,
    ):
        if wrong in {"source", "owner", "generation", "expired"}:
            with f.database.immediate_session() as unit:
                managed = unit.get(m.Job, job.id)
                assert managed is not None
                if wrong == "source":
                    managed.payload_json = {
                        **managed.payload_json,
                        "source_artifact_id": m.new_id(),
                    }
                elif wrong == "owner":
                    managed.lease_owner = "replacement"
                elif wrong == "generation":
                    managed.lease_generation += 1
                else:
                    managed.lease_expires_at = m.utcnow() - timedelta(seconds=1)
        if wrong in {"kind", "session", "root"}:
            with f.database.immediate_session() as unit:
                unit.execute(
                    text("UPDATE jobs SET payload_json = :bad WHERE id = :id"),
                    {"bad": "{foreign-invalid", "id": job.id},
                )
        if wrong == "missing_injected":
            payload.pop("_lease_generation")
        elif wrong == "forged_injected":
            payload["_job_id"] = m.new_id()
        elif wrong == "session":
            payload["session_id"] = f.other.id
        with (
            execution_actor_scope(None if wrong == "missing_context" else f.actor),
            pytest.raises((AccessDenied, PermissionError, KeyError)),
        ):
            optimize_tts(f.context, payload, Mock(), threading.Event())
    assert _counts(f)[1] == 0
    f.hydrate.assert_not_called()


def test_valid_continuation_child_binds_exact_scope_and_resets(speech):
    f = speech
    source, _path = _source(f)
    with _lease(f, source, kind="workflow.continue", payload={"session_id": f.target.id}) as (
        _job,
        payload,
    ):
        with f.database.snapshot_session() as unit:
            parent = capture_delivery_execution(
                unit,
                {
                    "session_id": f.target.id,
                    "_job_id": _job.id,
                    "_lease_generation": _job.lease_generation,
                },
                entry="continue_workflow",
            )
        with source_processing_child_scope(parent, "text.optimize_tts", payload):
            result = optimize_tts(f.context, payload, Mock(), threading.Event())
            assert result["artifact_id"]
            with pytest.raises(AccessDenied):
                with source_processing_child_scope(parent, "tts.preview", payload):
                    pass
        with f.database.snapshot_session() as unit, pytest.raises(AccessDenied):
            require_speech_optimization_access(unit, f.target.id, source.id, payload=payload)


@pytest.mark.parametrize("mode,batch", [("guarded", 1), ("guarded", 2), ("flexible", 1)])
def test_structured_custom_completions_capture_actor_and_lease(speech, mode, batch):
    f = speech
    source, path = _source(f, ".json")
    if batch == 2:
        path.write_text(
            json.dumps([{"text": "Hello there."}, {"text": "Welcome home."}]), encoding="utf-8"
        )
        source = f.artifacts.register(
            path, kind="source", role="transcription", session_id=f.target.id
        )
    with _lease(f, source) as (job, payload):
        payload["settings"] = {
            "speech_optimization_mode": mode,
            "llm_tts_document_batch_size": batch,
        }
        result = optimize_tts(f.context, payload, Mock(), threading.Event())
    assert result["artifact_id"]
    assert all(actor == f.actor and lease["job_id"] == job.id for actor, lease in f.seen)
    assert _counts(f)[3] == len(f.seen)


def test_model_replacement_latches_even_when_engine_swallows_value_error(speech, monkeypatch):
    from pandrator.web import tts_optimization

    f = speech
    source, _path = _source(f)

    def swallow(texts, _settings, llm_settings, model_name, *_args, **kwargs):
        completion = kwargs["completion_func"]
        f.details["request_overrides"] = {"api_base": "changed"}
        with pytest.raises(ValueError, match="model connection"):
            completion(messages=[], model_name=model_name, llm_settings=llm_settings)
        return texts, tts_optimization.OptimizationUsage()

    monkeypatch.setattr(tts_optimization, "optimize_texts", swallow)
    with pytest.raises(
        ValueError, match="The speech-optimization model connection is no longer available"
    ):
        _run(f, source, settings={"speech_optimization_mode": "guarded"})
    assert _counts(f)[0] == 1


@pytest.mark.parametrize("after_response", ["revoke", "cancel", "invalid"])
def test_incurred_receipt_survives_post_response_failure_without_arbitrary_fields(
    speech, after_response
):
    f = speech
    source, _path = _source(f)
    cancel = threading.Event()

    def transport(**kwargs):
        assert kwargs["cancel_event"] is cancel
        kwargs["before_request"]()
        response = ChatCompletionResult(
            content='{"items":[]}'
            if after_response == "invalid"
            else '{"items":[{"index":0,"text":"Hello there."}]}',
            model="MODEL_SENTINEL",
            usage={"prompt_tokens": 4, "completion_tokens": 2, "RAW_SENTINEL": "secret"},
            cost=0.5,
            cost_source="response_usage",
        )
        if after_response == "revoke":
            _deactivate(f)
        elif after_response == "cancel":
            cancel.set()
        with f.database.immediate_session() as unit:
            unit.execute(
                text("UPDATE agent_runs SET settings_json = :bad"),
                {"bad": "{private-settings-invalid"},
            )
        kwargs["response_callback"](response)
        return response

    f.transport.side_effect = transport
    with (
        execution_actor_scope(f.actor),
        pytest.raises((AccessDenied, PermissionError, KeyError, RuntimeError)),
    ):
        optimize_tts(
            f.context,
            _payload(f, source, settings={"tts_structured_max_attempts": 1}),
            Mock(),
            cancel,
        )
    assert _counts(f)[0] == 1
    with f.database.snapshot_session() as unit:
        receipt = unit.scalar(select(m.UsageEvent))
        assert receipt is not None
        assert receipt.raw_usage_json == {"prompt_tokens": 4, "completion_tokens": 2}
        assert receipt.model_id == "approved-model"
        assert receipt.cost_usd == 0.5
        status = unit.scalar(select(m.AgentRun.status).where(m.AgentRun.id == receipt.agent_run_id))
        assert status == ("interrupted" if after_response == "cancel" else "failed")


def test_project_token_filters_poisoned_global_and_snapshots_once_per_language(speech):
    f = speech
    source, path = _source(f, ".json")
    path.write_text(
        json.dumps(
            [
                {"text": "Imaoka arrived.", "language": "en"},
                {"text": "Imaoka left.", "language": "en"},
            ]
        ),
        encoding="utf-8",
    )
    source = f.artifacts.register(path, kind="source", role="transcription", session_id=f.target.id)
    library = PronunciationLibrary(f.database)
    global_entry = library.create(
        dict(source_form="Imaoka", phonetic="ee-mah-oh-kah", language="en"),
        workspace_id=f.workspace,
    )
    session_entry = library.create(
        dict(
            source_form="Imaoka",
            phonetic="ee-mah-oh-kah",
            language="en",
            scope="session",
            session_id=f.target.id,
        ),
        workspace_id=f.workspace,
    )
    with f.database.immediate_session() as unit:
        unit.execute(
            text("UPDATE pronunciation_entries SET metadata_json = '{broken' WHERE id = :id"),
            {"id": global_entry["id"]},
        )
    f.actor = _project_actor(f)
    queries = []

    def capture(_connection, _cursor, sql, _parameters, _context, _many):
        if "FROM pronunciation_entries" in sql:
            queries.append(sql)

    event.listen(f.database.engine, "before_cursor_execute", capture)
    try:
        result = _run(f, source, settings={"apply_reviewed_pronunciations": True})
    finally:
        event.remove(f.database.engine, "before_cursor_execute", capture)
    assert result["artifact_id"] and len(queries) == 1
    assert session_entry["status"] == "reviewed"


def test_srt_same_namespace_lineage_markup_annotation_only_guard(speech):
    f = speech
    source, _path = _source(f, ".srt")
    _revision, parent_id = _document(f, source, f.target.id)
    result = _run(f, source, settings={"llm_tts_annotation_only": True})
    with f.database.snapshot_session() as unit:
        output = unit.get(m.Artifact, result["artifact_id"])
        assert output is not None
        child = unit.scalar(
            select(m.Segment).where(m.Segment.revision_id == output.metadata_json["revision_id"])
        )
        assert child is not None and child.speaker == "Alice"
        assert "<narrator>Hello there.</narrator>" in child.metadata_json["speech_xml"]
        assert unit.get(m.SegmentLineage, (parent_id, child.id)) is not None
    f.transport.assert_not_called()


@pytest.mark.parametrize("failure", ["plan", "document", "finish"])
def test_final_atomic_faults_rollback_pair_document_selection_and_finish(
    speech, monkeypatch, failure
):
    from pandrator.web import workflow_speech_execution

    f = speech
    source, source_path = _source(f, ".srt")
    previous = _run(f, source)
    previous_path = f.paths.managed_path(previous["path"])
    original_bytes = previous_path.read_bytes()
    baseline = _counts(f)
    if failure == "plan":
        original = f.artifacts.register_in_session

        def register(unit, path, **kwargs):
            artifact = original(unit, path, **kwargs)
            if kwargs["role"] == "speech_plan":
                raise RuntimeError("plan fault")
            return artifact

        monkeypatch.setattr(f.artifacts, "register_in_session", register)
    elif failure == "document":
        original = workflow_speech_execution.store_srt_document_in_session

        def document(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("document fault")

        monkeypatch.setattr(workflow_speech_execution, "store_srt_document_in_session", document)
    else:
        original = AgenticRunStore.finish_in_session

        def finish(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("finish fault")

        monkeypatch.setattr(AgenticRunStore, "finish_in_session", finish)
    with pytest.raises(RuntimeError, match="fault"):
        _run(f, source, settings={"speech_optimization_mode": "guarded"})
    assert _counts(f)[0] == baseline[0]
    assert previous_path.read_bytes() == original_bytes and source_path.exists()
    with f.database.snapshot_session() as unit:
        retained = unit.get(m.Artifact, previous["artifact_id"])
        assert retained is not None
        assert retained.state == "current"
        completed = unit.get(m.AgentRun, previous["agent_run_id"])
        assert completed is not None
        assert completed.status == "completed"


def test_preexisting_output_collision_and_uncertain_committed_bytes_are_preserved(
    speech, monkeypatch
):
    f = speech
    source, _path = _source(f)
    destination = f.paths.sessions / f.target.storage_key / "tts-optimized-fixed.txt"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("KEEP", encoding="utf-8")
    f.context = replace(f.context, new_id=lambda: "fixed")
    original = SpeechOptimizationExecution.publish

    committed_operations = []

    def publish(self, *args, **kwargs):
        original_unit = f.database.immediate_session

        @contextmanager
        def uncertain_commit(*unit_args, **unit_kwargs):
            with original_unit(*unit_args, **unit_kwargs) as unit:
                yield unit
            committed_operations.append(self)
            raise RuntimeError("uncertain post-commit")

        with monkeypatch.context() as patcher:
            patcher.setattr(f.database, "immediate_session", uncertain_commit)
            return original(self, *args, **kwargs)

    monkeypatch.setattr(SpeechOptimizationExecution, "publish", publish)
    with pytest.raises(RuntimeError, match="uncertain"):
        _run(f, source)
    assert len(committed_operations) == 1 and not committed_operations[0].committed
    assert destination.read_text() == "KEEP"
    assert destination.with_name("tts-optimized-fixed-2.txt").exists()
    with f.database.snapshot_session() as unit:
        run = unit.scalar(select(m.AgentRun))
        assert run is not None
        assert run.status == "completed" and run.result_artifact_id is not None


@pytest.mark.parametrize("host", [False, True])
def test_none_legacy_ports_have_no_current_snapshot_or_new_completion_kwargs(
    speech, monkeypatch, host
):
    from pandrator.web import tts_optimization

    f = speech
    source, _path = _source(f)
    (f.paths.sessions / f.target.storage_key).mkdir(parents=True, exist_ok=True)
    original = tts_optimization.optimize_texts
    engine = Mock(wraps=original)
    monkeypatch.setattr(tts_optimization, "optimize_texts", engine)

    def completion(**_kwargs):
        return ChatCompletionResult(content='{"items":[{"index":0,"text":"Hello there."}]}')

    monkeypatch.setattr(tts_optimization, "chat_completion_with_metadata", completion)
    snapshots = Mock(wraps=f.database.snapshot_session)
    if not host:
        snapshots.side_effect = AssertionError("legacy snapshot")
    monkeypatch.setattr(f.database, "snapshot_session", snapshots)
    if not host:
        require_speech_optimization_access(Mock(), "ignored", "ignored")
    with execution_actor_scope(None):
        assert (
            capture_speech_optimization(
                f.context,
                {
                    **_payload(f, source),
                    "_job_id": "old-internal-bookkeeping",
                    "_lease_generation": 7,
                },
                threading.Event(),
            )
            is None
        )
    with execution_actor_scope(HostActor("pandrator-cli") if host else None):
        result = optimize_tts(f.context, _payload(f, source), Mock(), threading.Event())
    assert result["artifact_id"]
    assert "completion_func" not in engine.call_args.kwargs
    assert snapshots.call_count == (1 if host else 0)


def _current_operation(f, source, *, settings=None, payload=None, cancel=None):
    payload = payload or _payload(f, source)
    operation = capture_speech_optimization(f.context, payload, cancel or threading.Event())
    assert operation is not None
    operation.load_source()
    selected = {**f.settings, **(settings or {})}
    operation.begin({}, "", selected)
    operation.prepare_units(["en"])
    return operation


def _plan_output():
    from pandrator.web.speech_planning import SPEECH_PROMPT_REVISION

    return {
        "kind": "tts_optimization_plan",
        "stage": 0,
        "original_indices": [0],
        "items": [{"index": 0, "text": "ee mah oh kah"}],
        "plan": {
            "prompt_revision": SPEECH_PROMPT_REVISION,
            "language": "en",
            "candidates": [{"id": "c1", "text": "Imaoka"}],
            "decisions": [
                {
                    "span_id": "c1",
                    "action": "pronounce",
                    "spoken": "ee-mah-oh-kah",
                    "confidence": "high",
                }
            ],
        },
        "cost": 0.25,
        "response_count": 1,
        "usage": {"prompt_tokens": 11},
        "cost_sources": ["response_usage"],
    }


def test_proposal_and_checkpoint_share_rollback_then_resume_deduplicates_older_unit(
    speech, monkeypatch
):
    f = speech
    source, _path = _source(f)
    with execution_actor_scope(f.actor):
        operation = _current_operation(f, source)
        original = AgenticRunStore.checkpoint_in_session

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("checkpoint fault")

        with monkeypatch.context() as patcher:
            patcher.setattr(AgenticRunStore, "checkpoint_in_session", fail)
            with pytest.raises(RuntimeError, match="checkpoint fault"):
                operation.checkpoint("unit", _plan_output())
        assert _counts(f)[2:] == (0, 0, 0)
        output = _plan_output()
        operation.checkpoint("unit", output)
        proposal_id = output["plan"]["proposals"][0]["id"]
        second_output = _plan_output()
        second_output.update(
            original_indices=[1],
            items=[{"index": 1, "text": "second"}],
            cost=0.75,
            response_count=2,
        )
        operation.checkpoint("second", second_output)
        assert second_output["plan"]["proposals"][0]["id"] == proposal_id
        with f.database.immediate_session() as unit:
            step = unit.scalar(select(m.AgentStep).where(m.AgentStep.unit_key == "unit"))
            assert step is not None
            old = dict(step.output_json)
            old["plan"] = {key: value for key, value in old["plan"].items() if key != "proposals"}
            step.output_json = old
        operation.fail(RuntimeError("later unit failed"))
        assert operation.run is not None
        resumed = _current_operation(
            f, source, payload=_payload(f, source, _agent_run_id=operation.run.id)
        )
        assert resumed.run is not None
        assert resumed.run.resumed
        assert resumed.run.completed_units["unit"]["plan"]["proposals"][0]["id"] == proposal_id
        assert resumed.run.completed_units["second"]["plan"]["proposals"][0]["id"] == proposal_id
        obsolete = deepcopy(resumed.run.completed_units["unit"])
        obsolete["plan"]["prompt_revision"] -= 1
        obsolete["plan"].pop("proposals")
        resumed.run.completed_units["unit"] = obsolete
        with monkeypatch.context() as patcher:
            forbidden = Mock(side_effect=AssertionError("obsolete plan repaired"))
            patcher.setattr(resumed, "checkpoint", forbidden)
            resumed.prepare_units(["en", "en"])
            forbidden.assert_not_called()
    with f.database.snapshot_session() as unit:
        steps = list(unit.scalars(select(m.AgentStep).order_by(m.AgentStep.ordinal)))
        assert [
            (step.unit_key, step.ordinal, step.cost_usd, step.output_json["response_count"])
            for step in steps
        ] == [("unit", 0, 0.25, 1), ("second", 1, 0.75, 2)]
        assert all(step.output_json["plan"]["proposals"][0]["id"] == proposal_id for step in steps)
        assert unit.scalar(select(func.count()).select_from(m.PronunciationEntry)) == 1
    f.transport.assert_not_called()


@pytest.mark.parametrize("binding", ["replacement", "root"])
def test_replacement_worker_cannot_account_or_fail_predecessor(speech, binding):
    f = speech
    source, _path = _source(f)
    with _lease(f, source) as (job, payload):
        operation = _current_operation(f, source, payload=payload)
        with f.database.immediate_session() as unit:
            managed = unit.get(m.Job, job.id)
            assert managed is not None
            if binding == "replacement":
                managed.lease_generation += 1
                managed.lease_owner = "replacement"
            elif binding == "root":
                operation.captured = replace(operation.captured, project_id=f.other.project_id)
            unit.flush()
            unit.execute(
                text("UPDATE jobs SET payload_json = :bad WHERE id = :id"),
                {"bad": "{foreign-invalid", "id": job.id},
            )
        with pytest.raises((AccessDenied, PermissionError)):
            operation.record_response(
                usage={"prompt_tokens": 2},
                provider_key="approved-provider",
                model_id="approved-model",
                cost=0.25,
                cost_source="response_usage",
            )
        operation.fail(RuntimeError("old worker error"))
    assert _counts(f)[3] == 0
    with f.database.snapshot_session() as unit:
        assert operation.run is not None
        run = unit.get(m.AgentRun, operation.run.id)
        assert run is not None
        assert run.status == "running" and run.error_message is None


@pytest.mark.parametrize("change", ["source", "credential", "missing_context"])
def test_expected_proof_rechecks_source_credential_and_context_before_legacy_paths(speech, change):
    f = speech
    source, _path = _source(f)
    f.actor = _project_actor(f)
    with execution_actor_scope(f.actor):
        operation = _current_operation(f, source)
        if change == "source":
            with f.database.immediate_session() as unit:
                unit.execute(
                    text("UPDATE artifacts SET content_hash = :hash WHERE id = :id"),
                    {"hash": "changed", "id": source.id},
                )
        elif change == "credential":
            with f.database.immediate_session() as unit:
                unit.execute(
                    delete(ApiTokenProjectGrant).where(
                        ApiTokenProjectGrant.token_id == f.actor.token_id
                    )
                )
        with (
            execution_actor_scope(None if change == "missing_context" else f.actor),
            pytest.raises((AccessDenied, KeyError, PermissionError)),
        ):
            operation.prove()


def test_authorized_workspace_global_and_session_pronunciations_both_apply(speech):
    f = speech
    source, path = _source(f)
    path.write_text("Imaoka met Austen.", encoding="utf-8")
    source = f.artifacts.register(path, kind="source", role="transcription", session_id=f.target.id)
    library = PronunciationLibrary(f.database)
    library.create(
        {"source_form": "Austen", "phonetic": "aws-ten", "language": "en"},
        workspace_id=f.workspace,
    )
    library.create(
        {
            "source_form": "Imaoka",
            "phonetic": "ee-mah-oh-kah",
            "language": "en",
            "scope": "session",
            "session_id": f.target.id,
        },
        workspace_id=f.workspace,
    )
    result = _run(f, source, settings={"apply_reviewed_pronunciations": True})
    assert (
        f.paths.managed_path(result["path"]).read_text(encoding="utf-8") == "eemahohkah met awsten."
    )


def test_successful_publish_has_no_late_plain_session_or_cleanup_lookup(speech, monkeypatch):
    f = speech
    source, source_path = _source(f)
    original = SpeechOptimizationExecution.publish
    forbidden = Mock(side_effect=AssertionError("session opened after successful publish"))
    operations = []

    def publish(self, *args, **kwargs):
        artifact = original(self, *args, **kwargs)
        assert self.committed
        operations.append(self)
        monkeypatch.setattr(f.database, "session", forbidden)
        return artifact

    monkeypatch.setattr(SpeechOptimizationExecution, "publish", publish)
    result = _run(f, source)
    assert len(operations) == 1
    assert f.paths.managed_path(result["path"]).read_text(encoding="utf-8") == "Hello there."
    assert source_path.exists()
    forbidden.assert_not_called()


def test_consumed_global_requires_workspace_grant_even_when_target_run_survives(
    speech, monkeypatch
):
    f = speech
    source, path = _source(f)
    path.write_text("Austen came.", encoding="utf-8")
    source = f.artifacts.register(path, kind="source", role="transcription", session_id=f.target.id)
    entry = PronunciationLibrary(f.database).create(
        {"source_form": "Austen", "phonetic": "aws-ten", "language": "en"},
        workspace_id=f.workspace,
    )
    token, _raw = f.auth.create_api_token(
        "Speech workspace and project",
        user_id=f.alice.user_id,
        scopes=SCOPES,
        resource_grants=CredentialResources(
            workspace_ids=frozenset({f.workspace}),
            project_ids=frozenset({f.target.project_id}),
        ),
    )
    f.actor = UserActor(
        f.alice.user_id, 1, str(token.subject), "api_token", SCOPES, token_id=token.id
    )
    original = SpeechOptimizationExecution.reviewed
    consumed = []

    def reviewed(self, *args, **kwargs):
        matches = original(self, *args, **kwargs)
        assert [(row["id"], row["scope"]) for row in matches] == [(entry["id"], "global")]
        consumed.extend(matches)
        with f.database.immediate_session() as unit:
            unit.execute(
                delete(ApiTokenWorkspaceGrant).where(ApiTokenWorkspaceGrant.token_id == token.id)
            )
        with f.database.snapshot_session() as unit:
            require_current_session_access(unit, f.target.id, ProjectAction.RUN)
        return matches

    monkeypatch.setattr(SpeechOptimizationExecution, "reviewed", reviewed)
    with pytest.raises(AccessDenied):
        _run(f, source, settings={"apply_reviewed_pronunciations": True})
    assert len(consumed) == 1
    f.transport.assert_not_called()
    assert _counts(f)[0] == 1
