"""Current native cleaning authority, response receipts and atomic publication."""

from __future__ import annotations

import builtins
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text

from pandrator.web import models as m
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.access.session_policy import SessionNotFound
from pandrator.web.access.source_preparation_policy import require_source_cleaning_access
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.job_execution import current_job_lease
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.workflow_source import clean_source
from pandrator.web.workflow_source_cleaning import _load_source
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_provider_workspace_access import model, provider
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_source_library_workspace_http import library as library  # noqa: F401
from tests.test_source_preparation_workspace_access import (
    FULL_SCOPES,
    _actor,
    _attach,
    _context,
    _detach_current_source,
    _document_revision,
    _leased_actor,
    _project,
)


def _input(f: Any, target: dict[str, Any], *, suffix: str = ".txt", metadata=None):
    path = f.services.paths.uploads / f"native-cleaning-{uuid4()}{suffix}"
    path.write_text("Chapter One\n\nSource bytes remain intact.", encoding="utf-8", newline="\n")
    with execution_actor_scope(HostActor("pandrator-cli")):
        source = f.services.artifacts.register(
            path,
            kind="source",
            role="upload",
            session_id=target["id"],
            metadata=metadata,
        )
    return source, path


def _counts(f):
    with f.case.database.snapshot_session() as unit:
        return tuple(
            unit.scalar(select(func.count()).select_from(model)) or 0
            for model in (m.Artifact, m.AgentStep, m.UsageEvent)
        )


def _payload(target, source, **extra):
    return {"session_id": target["id"], "source_artifact_id": source.id, **extra}


def _fake_pipeline(monkeypatch):
    from pandrator.logic import source_cleaning
    from pandrator.logic.source_cleaning.models import PhaseResult, PipelineResult
    from pandrator.web import provider_settings, source_cleaning_llm

    settings = Mock(return_value=(SimpleNamespace(), "synthetic"))
    monkeypatch.setattr(provider_settings, "build_llm_settings", settings)
    monkeypatch.setattr(source_cleaning_llm, "build_llm_settings", settings)
    pipeline = Mock(return_value=PipelineResult(phases=[PhaseResult("metadata", "Metadata")]))
    monkeypatch.setattr(source_cleaning, "run_cleaning_pipeline", pipeline)
    return pipeline


def test_trusted_none_keeps_original_callbacks_and_omits_completion_adapter(library, monkeypatch):
    from pandrator.logic import source_cleaning
    from pandrator.logic.source_cleaning.pdf_text_adapter import build_source_document_from_text

    f, u = library, library.u
    target = _project(u.alice, u.ah)
    source, path = _input(f, target, suffix=".pdf")
    context = _context(f)
    document = build_source_document_from_text(
        "Chapter One\n\nSource bytes remain intact.", filename=path.name
    )
    monkeypatch.setattr(source_cleaning, "build_source_document", Mock(return_value=document))
    pipeline = _fake_pipeline(monkeypatch)
    progress, fraction_callback, phase_callback = Mock(), Mock(), Mock()
    fraction, phases = Mock(return_value=fraction_callback), Mock(return_value=phase_callback)
    resolver = Mock(wraps=context._resolve_input)
    snapshots = Mock(wraps=f.case.database.snapshot_session)
    monkeypatch.setattr(f.case.database, "snapshot_session", snapshots)
    context = replace(
        context,
        _resolve_input=resolver,
        _fraction_message_callback=fraction,
        _source_cleaning_progress_callback=phases,
    )
    cancel = threading.Event()
    unit = Mock()
    with execution_actor_scope(None):
        require_source_cleaning_access(unit, "ignored", "ignored", agent_run_id="ignored")
        result = clean_source(
            context,
            _payload(target, source, settings={"agentic": True, "phase_names": ["metadata"]}),
            progress,
            cancel,
        )
    assert unit.mock_calls == []
    snapshots.assert_not_called()
    resolver.assert_called_once_with(source.id)
    assert [call.args for call in fraction.call_args_list] == [
        (progress, 0.05, 0.35),
        (progress, 0.4, 0.45),
    ]
    assert phases.call_args.args == (progress, 0.45, 0.9)
    assert pipeline.call_args.kwargs["progress_callback"] is phase_callback
    assert pipeline.call_args.kwargs["stop_event"] is cancel
    assert "completion_func" not in pipeline.call_args.kwargs
    assert set(result) == {"artifact_id", "path", "characters", "report"}


@pytest.mark.parametrize("denial", ["foreign_admin", "viewer", "no_run"])
def test_denial_precedes_input_native_import_run_body_and_directories(library, monkeypatch, denial):
    f, u = library, library.u
    target = _project(u.bob, u.bh)
    source, _path = _input(f, target)
    if denial == "viewer":
        with f.case.database.immediate_session() as unit:
            unit.add(
                ProjectMembership(
                    project_id=target["project_id"], user_id=u.alice_id, role="viewer"
                )
            )
        actor = _actor(f.case, u.alice_id, projects=(target["project_id"],))
    elif denial == "no_run":
        target = _project(u.alice, u.ah, u.workspace_id)
        source, _path = _input(f, target)
        actor = _actor(
            f.case, u.alice_id, frozenset({"app.read", "app.write"}), workspaces=(u.workspace_id,)
        )
    else:
        owner_id = f.case.auth.initial_account().user_id
        actor = _actor(
            f.case, owner_id, FULL_SCOPES | {"app.admin"}, workspaces=(f.homes[owner_id],)
        )
    context = _context(f)
    forbidden = Mock(side_effect=AssertionError("Denied native cleaning reached an effect"))
    context = replace(
        context, _resolve_input=forbidden, _session_dir=forbidden, _operation_dir=forbidden
    )
    original_import = builtins.__import__

    def controlled_import(name, *args, **kwargs):
        if name == "pandrator.logic":
            raise AssertionError("Denied cleaning imported its native implementation")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", controlled_import)
    monkeypatch.setattr(Path, "read_text", forbidden)
    with execution_actor_scope(actor), pytest.raises((AccessDenied, SessionNotFound)):
        clean_source(
            context, _payload(target, source, agent_run_id="foreign-run"), Mock(), threading.Event()
        )
    forbidden.assert_not_called()


def test_bytes_only_dependency_never_selects_source_metadata(library):
    f, u = library, library.u
    origin = _project(u.bob, u.bh, u.workspace_id)
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _input(f, origin)
    _attach(f, target["id"], source)
    with f.case.database.immediate_session() as unit:
        unit.execute(
            text("UPDATE artifacts SET metadata_json = :bad WHERE id = :id"),
            {"bad": "invalid JSON", "id": source.id},
        )
    actor = _actor(f.case, u.alice_id, projects=(target["project_id"],))
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(f.case.database.engine, "before_cursor_execute", capture)
    try:
        with execution_actor_scope(actor):
            result = _load_source(_context(f), target["id"], source.id, "")
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", capture)
    assert result == (source.id, path)
    assert not any("select" in sql and "artifacts.metadata_json" in sql for sql in statements)


@pytest.mark.parametrize("binding", ["wrong_kind", "wrong_target", "forged_lease", "wrong_run"])
def test_cleaning_requires_real_job_and_run_binding(library, binding):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    other = _project(u.alice, u.ah, u.workspace_id)
    source, _path = _input(f, target)
    actor = _actor(f.case, u.alice_id, workspaces=(u.workspace_id,))
    with execution_actor_scope(actor):
        queued = f.services.jobs.enqueue(
            "text.prepare" if binding == "wrong_kind" else "source.clean",
            {},
            session_id=target["id"],
        )
        job = f.services.jobs.claim("native-cleaning-worker", lease_seconds=900)
    assert job is not None and queued.id == job.id
    with f.case.database.immediate_session() as unit:
        run = m.AgentRun(
            session_id=target["id"], source_artifact_id=source.id, job_id=None, status="queued"
        )
        unit.add(run)
        unit.flush()
        run_id = run.id
    with _leased_actor(actor, job):
        forged = None
        if binding == "forged_lease":
            forged = current_job_lease.set(
                {"job_id": m.new_id(), "owner_id": "native-cleaning-worker", "lease_generation": 1}
            )
        try:
            with (
                f.case.database.snapshot_session() as unit,
                pytest.raises((AccessDenied, KeyError)),
            ):
                require_source_cleaning_access(
                    unit,
                    other["id"] if binding == "wrong_target" else target["id"],
                    source.id,
                    agent_run_id=run_id if binding == "wrong_run" else None,
                )
        finally:
            if forged is not None:
                current_job_lease.reset(forged)


@pytest.mark.parametrize("boundary", ["progress", "factory"])
def test_callback_revocation_prevents_extraction_and_output(library, monkeypatch, boundary):
    from pandrator.logic import source_cleaning

    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    origin = _project(u.bob, u.bh, u.workspace_id)
    source, _path = _input(f, origin, suffix=".pdf")
    asset = _attach(f, target["id"], source)
    actor = _actor(f.case, u.alice_id, projects=(target["project_id"],))
    context = _context(f)
    native = Mock(side_effect=AssertionError("Revoked callback reached extraction"))
    monkeypatch.setattr(source_cleaning, "build_source_document", native)
    before = _counts(f)

    def revoke(*_args, **_kwargs):
        _detach_current_source(f, target["id"], asset.id)
        return Mock()

    if boundary == "factory":
        context = replace(context, _fraction_message_callback=revoke)
    with execution_actor_scope(actor), pytest.raises(KeyError):
        clean_source(
            context,
            _payload(target, source),
            revoke if boundary == "progress" else Mock(),
            threading.Event(),
        )
    native.assert_not_called()
    assert _counts(f) == before


def test_swallowed_progress_denial_stops_pipeline_and_cannot_publish(library, monkeypatch):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, _path = _input(f, target)
    actor = _actor(f.case, u.alice_id, workspaces=(u.workspace_id,))
    pipeline = _fake_pipeline(monkeypatch)
    before = _counts(f)
    cancel = threading.Event()

    def progress(value, _detail):
        if value >= 0.45:
            with f.case.database.immediate_session() as unit:
                unit.delete(unit.get_one(WorkspaceMembership, (u.workspace_id, u.alice_id)))
            raise RuntimeError("Generic forwarded progress error")

    def run_pipeline(*_args, **kwargs):
        try:
            kwargs["progress_callback"]("Metadata: LLM turn 1/2...")
        except Exception:
            pass
        assert kwargs["stop_event"].is_set()
        assert not cancel.is_set()
        return pipeline.return_value

    pipeline.side_effect = run_pipeline
    with execution_actor_scope(actor), pytest.raises((AccessDenied, SessionNotFound)):
        clean_source(
            _context(f),
            _payload(target, source, settings={"agentic": True, "phase_names": ["metadata"]}),
            progress,
            cancel,
        )
    assert _counts(f) == before


def test_publication_failure_rolls_back_both_artifacts_run_and_steps(library, monkeypatch):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, _path = _input(f, target)
    actor = _actor(f.case, u.alice_id, workspaces=(u.workspace_id,))
    _fake_pipeline(monkeypatch)
    with f.case.database.immediate_session() as unit:
        run = m.AgentRun(session_id=target["id"], source_artifact_id=source.id, status="queued")
        unit.add(run)
        unit.flush()
        run_id = run.id
    before = _counts(f)

    def fail_step(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lower().startswith("insert into agent_steps"):
            raise RuntimeError("Synthetic phase publication failure")

    event.listen(f.case.database.engine, "before_cursor_execute", fail_step)
    try:
        with (
            execution_actor_scope(actor),
            pytest.raises(RuntimeError, match="Synthetic phase publication"),
        ):
            clean_source(
                _context(f),
                _payload(
                    target,
                    source,
                    agent_run_id=run_id,
                    settings={"agentic": True, "phase_names": ["metadata"]},
                ),
                Mock(),
                threading.Event(),
            )
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", fail_step)
    assert _counts(f) == before
    with f.case.database.snapshot_session() as unit:
        run = unit.get_one(m.AgentRun, run_id)
        assert run.status == "running" and run.result_artifact_id is None


def test_member_cleans_after_creator_departure_with_baseline_bytes_and_edges(library):
    f, u = library, library.u
    origin = _project(u.alice, u.ah, u.workspace_id)
    target = _project(u.alice, u.ah, u.workspace_id)
    revision = _document_revision(f, origin["id"], "Source bytes remain intact.")
    source, path = _input(f, origin, metadata={"revision_id": revision})
    _attach(f, target["id"], source)
    original = path.read_bytes()
    with f.case.database.immediate_session() as unit:
        unit.delete(unit.get_one(WorkspaceMembership, (u.workspace_id, u.alice_id)))
    actor = _actor(f.case, u.bob_id, workspaces=(u.workspace_id,))
    with execution_actor_scope(actor):
        result = clean_source(_context(f), _payload(target, source), Mock(), threading.Event())
    assert path.read_bytes() == original
    assert f.services.paths.managed_path(result["path"]).read_bytes() == original
    with f.case.database.snapshot_session() as unit:
        clean = unit.get_one(m.Artifact, result["artifact_id"])
        baseline = unit.scalar(
            select(m.Artifact).where(
                m.Artifact.session_id == target["id"], m.Artifact.role == "extracted_text"
            )
        )
        assert baseline is not None and clean.role == "clean_text"
        assert f.services.paths.managed_path(baseline.relative_path).read_bytes() == original
        assert unit.get(m.ArtifactEdge, (source.id, baseline.id)) is not None
        assert unit.get(m.ArtifactEdge, (source.id, clean.id)) is not None
        assert unit.get(m.ArtifactEdge, (baseline.id, clean.id)) is not None


def test_user_cancellation_preserves_input_without_publication(library):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _input(f, target)
    original = path.read_bytes()
    before = _counts(f)
    actor = _actor(f.case, u.alice_id, workspaces=(u.workspace_id,))
    cancel = threading.Event()
    cancel.set()
    with execution_actor_scope(actor):
        assert clean_source(_context(f), _payload(target, source), Mock(), cancel) == {}
    assert path.read_bytes() == original
    assert _counts(f) == before


@pytest.mark.parametrize("change", ["membership", "disabled", "connection"])
def test_actual_adapter_records_response_once_before_retry_denial(library, monkeypatch, change):
    from pandrator.logic import llm_handler

    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, _path = _input(f, target)
    p = provider(u.owner, u.oh, workspace=u.workspace_id)
    model(u.owner, u.oh, p, workspace=u.workspace_id, name="native-cleaning-synthetic")
    selected_model = f"custom:{p['id']}/native-cleaning-synthetic"
    actor = _actor(f.case, u.alice_id, workspaces=(u.workspace_id,))
    with execution_actor_scope(actor):
        queued = f.services.jobs.enqueue("source.clean", {}, session_id=target["id"])
        job = f.services.jobs.claim("native-cleaning-worker", lease_seconds=900)
    assert job is not None and job.id == queued.id
    before = _counts(f)
    calls = []

    def native_completion(**kwargs):
        calls.append(kwargs["model"])
        with f.case.database.immediate_session() as unit:
            if change == "membership":
                unit.delete(unit.get_one(WorkspaceMembership, (u.workspace_id, u.alice_id)))
            elif change == "disabled":
                unit.get_one(m.Provider, p["id"]).enabled = False
            else:
                unit.get_one(m.Provider, p["id"]).base_url = "https://changed.invalid/v1"
        return {
            "model": kwargs["model"],
            "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
            "usage": {
                "prompt_tokens": 7,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 3},
            },
            "_hidden_params": {"response_cost": 0.01},
        }

    monkeypatch.setattr(llm_handler, "_get_litellm_clients", lambda: (native_completion, None))
    monkeypatch.setattr(llm_handler, "wait_for_retry", Mock(return_value=True))
    denied = (AccessDenied, SessionNotFound) if change == "membership" else (ValueError,)
    with _leased_actor(actor, job), pytest.raises(denied) as raised:
        clean_source(
            _context(f),
            _payload(
                target,
                source,
                _job_id="untrusted-payload-job",
                settings={
                    "agentic": True,
                    "model_name": selected_model,
                    "phase_names": ["metadata"],
                },
            ),
            Mock(),
            threading.Event(),
        )
    if change != "membership":
        assert str(raised.value) == "The source-cleaning model connection is no longer available."
    assert len(calls) == 1
    after = _counts(f)
    assert after == (before[0], before[1], before[2] + 1)
    with f.case.database.snapshot_session() as unit:
        receipt = unit.scalar(select(m.UsageEvent).where(m.UsageEvent.job_id == job.id))
        assert receipt is not None
        assert (receipt.session_id, receipt.workspace_id, receipt.project_id) == (
            target["id"],
            u.workspace_id,
            target["project_id"],
        )
        assert receipt.provider_key == "openai"
        assert (receipt.input_tokens, receipt.cached_input_tokens, receipt.output_tokens) == (
            7,
            3,
            2,
        )
        assert receipt.cost_usd == 0.01
        assert "messages" not in receipt.raw_usage_json


@pytest.mark.parametrize("binding", ["missing", "kind", "source", "session"])
def test_unleased_run_requires_exact_cleaning_source_and_target(library, binding):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    other = _project(u.alice, u.ah, u.workspace_id)
    source, _path = _input(f, target)
    foreign_source, _foreign_path = _input(f, other)
    actor = _actor(f.case, u.alice_id, workspaces=(u.workspace_id,))
    with f.case.database.immediate_session() as unit:
        run = m.AgentRun(
            session_id=other["id"] if binding == "session" else target["id"],
            source_artifact_id=foreign_source.id if binding == "source" else source.id,
            kind="translation" if binding == "kind" else "source_cleaning",
            status="queued",
        )
        unit.add(run)
        unit.flush()
        run_id = run.id
    with execution_actor_scope(actor), f.case.database.snapshot_session() as unit:
        with pytest.raises(KeyError) as denied:
            require_source_cleaning_access(
                unit,
                target["id"],
                source.id,
                agent_run_id=m.new_id() if binding == "missing" else run_id,
            )
    assert denied.value.args == ("Source-cleaning run not found.",)


@pytest.mark.parametrize("suffix", [".docx", ".mobi"])
def test_converted_scratch_and_clean_output_share_unique_operation_directory(
    library, monkeypatch, suffix
):
    from pandrator.logic import file_handler

    f, u = library, library.u
    target = _project(u.alice, u.ah)
    source, source_path = _input(f, target, suffix=suffix)
    original = source_path.read_bytes()
    context = _context(f)
    allocate = Mock(wraps=context._operation_dir)
    context = replace(context, _operation_dir=allocate)
    converted_paths = []

    def convert(input_path, output_path):
        assert Path(input_path) == source_path
        output = Path(output_path)
        converted_paths.append(output)
        output.write_text("Converted scratch text.", encoding="utf-8")
        return True

    monkeypatch.setattr(file_handler, "convert_doc_to_text", convert)
    with execution_actor_scope(None):
        result = clean_source(context, _payload(target, source), Mock(), threading.Event())
    allocate.assert_called_once_with(target["id"], "clean-source")
    output = f.services.paths.managed_path(result["path"])
    assert converted_paths[0].parent == output.parent
    assert output.name == f"{source_path.stem}_cleaned.txt"
    assert output.read_text("utf-8") == "Converted scratch text."
    assert source_path.read_bytes() == original
