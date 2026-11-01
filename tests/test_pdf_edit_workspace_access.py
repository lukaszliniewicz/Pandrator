"""Current PDF target/source authority and recoverable pair publication."""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import uuid4

import pymupdf as fitz
import pytest
from sqlalchemy import event, func, select

from pandrator.web import models as m
from pandrator.web import pdf_editor, pdf_publication
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.access.pdf_edit_policy import PdfEditInput, require_pdf_edit_access
from pandrator.web.access.session_policy import SessionNotFound
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.job_execution import current_job_lease
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_source_library_workspace_http import library as library  # noqa: F401
from tests.test_source_preparation_workspace_access import (
    FULL_SCOPES,
    _actor,
    _attach,
    _detach_current_source,
    _leased_actor,
    _project,
)

RUN_SCOPES = frozenset({"app.read", "app.run"})


def _source(f, target):
    path = f.services.paths.uploads / f"authority-{uuid4()}.pdf"
    with fitz.open() as document:
        for index in range(3):
            page = document.new_page(width=200, height=250)
            page.insert_text((25, 40), f"Source page {index}")
        document.save(path)
    with execution_actor_scope(HostActor("pandrator-cli")):
        artifact = f.services.artifacts.register(
            path,
            kind="source",
            role="upload",
            session_id=target["id"],
            metadata={"private_origin": "never hydrate this for a selected dependency"},
        )
    return artifact, path


def _rows(f):
    with f.case.database.snapshot_session() as unit:
        return tuple(
            unit.scalar(select(func.count()).select_from(model)) or 0
            for model in (m.Artifact, m.ArtifactEdge)
        )


def _files(f):
    return {
        path
        for root in (
            f.services.paths.sessions,
            f.services.paths.artifacts,
            f.services.paths.uploads,
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def _payload(target, source, **extra):
    return {
        "session_id": target["id"],
        "source_artifact_id": source.id,
        "plan": {"deleted_pages": [1]},
        **extra,
    }


def _edit(f, target, source, progress=None, cancel=None, **extra):
    return f.services.workflow_handlers.apply_pdf_edits(
        _payload(target, source, **extra), progress or Mock(), cancel or threading.Event()
    )


def _job(f, actor, target, source, *, kind="pdf.apply_edits", queued_target=None, selector=None):
    with execution_actor_scope(actor):
        queued = f.services.jobs.enqueue(
            kind,
            _payload(target, source, **({"source_artifact_id": selector} if selector else {})),
            session_id=(queued_target or target)["id"],
        )
        claimed = f.services.jobs.claim("pdf-authority-worker", lease_seconds=900)
    assert claimed is not None and claimed.id == queued.id
    return claimed


@pytest.mark.parametrize("host", [False, True])
def test_trusted_policy_fast_path_and_legacy_injected_handler_ports(library, monkeypatch, host):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _source(f, target)
    actor = HostActor("pandrator-cli") if host else None
    unit = Mock()
    with execution_actor_scope(actor):
        assert require_pdf_edit_access(unit, None, "ignored", expected=cast(Any, object())) is None
    assert unit.mock_calls == []
    output_dir = f.services.paths.sessions / f"injected-{uuid4()}"
    output_dir.mkdir()
    resolver = Mock(return_value=(source, path))
    directory = Mock(return_value=output_dir)
    snapshot = Mock(wraps=f.case.database.snapshot_session)
    monkeypatch.setattr(f.case.database, "snapshot_session", snapshot)
    handler = cast(
        WorkflowHandlers,
        SimpleNamespace(
            database=f.case.database,
            paths=f.services.paths,
            artifacts=f.services.artifacts,
            _resolve_input=resolver,
            _session_dir=directory,
        ),
    )
    native = Mock(
        return_value=(
            output_dir / "staged.pdf",
            output_dir / "staged.json",
            {"output": {"page_count": 2}},
        )
    )
    publish = Mock(return_value=(SimpleNamespace(id="pdf-id"), SimpleNamespace(id="manifest-id")))
    monkeypatch.setattr(pdf_editor, "apply_pdf_edit_plan", native)
    monkeypatch.setattr(pdf_publication, "publish_pdf_edit", publish)
    callback, cancel = Mock(), threading.Event()
    with execution_actor_scope(actor):
        result = WorkflowHandlers.apply_pdf_edits(
            handler,
            {
                "source_artifact_id": source.id,
                "plan": {},
                "_job_id": "legacy-job",
                "_lease_generation": 7,
            },
            callback,
            cancel,
        )
    resolver.assert_called_once_with(source.id)
    directory.assert_called_once_with(target["id"])
    if not host:
        snapshot.assert_not_called()
    assert native.call_args.kwargs == {"parent_artifact_id": source.id}
    assert "_expected_input" not in publish.call_args.kwargs
    assert publish.call_args.kwargs["job_id"] == "legacy-job"
    assert publish.call_args.kwargs["lease_generation"] == 7
    assert publish.call_args.kwargs["cancel_event"] is cancel
    assert result == {
        "artifact_id": "pdf-id",
        "manifest_artifact_id": "manifest-id",
        "page_count": 2,
    }
    assert [call.args[0] for call in callback.call_args_list] == [0.1, 0.85, 1.0]


@pytest.mark.parametrize("leased", [False, True])
def test_member_read_run_without_write_publishes_native_pair_and_source_edges(library, leased):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _source(f, target)
    original = path.read_bytes()
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    scope = (
        _leased_actor(actor, _job(f, actor, target, source))
        if leased
        else execution_actor_scope(actor)
    )
    before = _rows(f)
    with scope:
        result = _edit(f, target, source, _job_id="forged-payload-job", _lease_generation=-77)
    assert _rows(f) == (before[0] + 2, before[1] + 3)
    assert result["page_count"] == 2 and path.read_bytes() == original
    with f.case.database.snapshot_session() as unit:
        pdf = unit.get_one(m.Artifact, result["artifact_id"])
        manifest = unit.get_one(m.Artifact, result["manifest_artifact_id"])
        assert (pdf.role, manifest.role) == ("pdf_edited", "provenance")
        assert pdf.workspace_id == manifest.workspace_id == u.workspace_id
        assert pdf.id != source.id and pdf.session_id == manifest.session_id == target["id"]
        assert unit.get(m.ArtifactEdge, (source.id, pdf.id)) is not None
        assert unit.get(m.ArtifactEdge, (source.id, manifest.id)) is not None
        assert unit.get(m.ArtifactEdge, (pdf.id, manifest.id)) is not None
        pdf_path = f.services.paths.managed_path(pdf.relative_path)
        manifest_path = f.services.paths.managed_path(manifest.relative_path)
    with fitz.open(pdf_path) as document:
        assert document.page_count == 2
    provenance = json.loads(manifest_path.read_text("utf-8"))
    assert provenance["source"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert provenance["output"]["path"] == str(pdf_path)


@pytest.mark.parametrize("denial", ["viewer", "foreign_admin", "narrowed", "missing_target"])
def test_denial_precedes_legacy_resolver_source_body_and_native_pdf(library, monkeypatch, denial):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, _path = _source(f, target)
    if denial == "viewer":
        target = _project(u.bob, u.bh)
        source, _path = _source(f, target)
        with f.case.database.immediate_session() as unit:
            unit.add(
                ProjectMembership(
                    project_id=target["project_id"], user_id=u.alice_id, role="viewer"
                )
            )
        actor = _actor(f.case, u.alice_id, RUN_SCOPES, projects=(target["project_id"],))
    elif denial == "foreign_admin":
        owner = f.case.auth.initial_account().user_id
        actor = _actor(f.case, owner, FULL_SCOPES | {"app.admin"}, workspaces=(f.homes[owner],))
    elif denial == "narrowed":
        other = _project(u.alice, u.ah, u.workspace_id)
        actor = _actor(f.case, u.alice_id, RUN_SCOPES, projects=(other["project_id"],))
    else:
        actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    forbidden = Mock(side_effect=AssertionError("Denied edit reached old source/native port"))
    monkeypatch.setattr(f.services.workflow_handlers, "_resolve_input", forbidden)
    monkeypatch.setattr(pdf_editor, "apply_pdf_edit_plan", forbidden)
    values = _payload(target, source)
    if denial == "missing_target":
        values.pop("session_id")
    with execution_actor_scope(actor), pytest.raises((AccessDenied, SessionNotFound)):
        f.services.workflow_handlers.apply_pdf_edits(values, Mock(), threading.Event())
    forbidden.assert_not_called()


@pytest.mark.parametrize("binding", ["kind", "source_selector", "root", "forged_lease"])
def test_exact_actual_job_kind_selectors_root_and_lease_are_required(library, binding):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    other = _project(u.alice, u.ah, u.workspace_id)
    source, _path = _source(f, target)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    job = _job(
        f,
        actor,
        target,
        source,
        kind="text.prepare" if binding == "kind" else "pdf.apply_edits",
        queued_target=other if binding == "root" else None,
        selector="another-source" if binding == "source_selector" else None,
    )
    with _leased_actor(actor, job):
        token = (
            current_job_lease.set(
                {"job_id": m.new_id(), "owner_id": "forged-owner", "lease_generation": 1}
            )
            if binding == "forged_lease"
            else None
        )
        try:
            with f.case.database.snapshot_session() as unit, pytest.raises(AccessDenied):
                require_pdf_edit_access(unit, target["id"], source.id)
        finally:
            if token is not None:
                current_job_lease.reset(token)


def test_current_selected_source_exports_bytes_without_loading_origin_artifact(library):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    origin = _project(u.bob, u.bh, u.workspace_id)
    source, path = _source(f, origin)
    asset = _attach(f, target["id"], source)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, projects=(target["project_id"],))
    loaded = []

    def source_loaded(artifact, _context):
        if artifact.id == source.id:
            loaded.append(artifact.id)
            raise AssertionError("Selected source ORM body was hydrated")

    event.listen(m.Artifact, "load", source_loaded)
    try:
        with execution_actor_scope(actor):
            result = _edit(f, target, source)
    finally:
        event.remove(m.Artifact, "load", source_loaded)
    assert result["page_count"] == 2 and loaded == []
    assert path.is_file()
    _detach_current_source(f, target["id"], asset.id)
    before = _rows(f)
    with execution_actor_scope(actor), pytest.raises(KeyError, match="Source artifact not found"):
        _edit(f, target, source)
    assert _rows(f) == before


@pytest.mark.parametrize("boundary", [0.1, 0.85])
def test_membership_loss_in_progress_stops_native_or_publication(library, monkeypatch, boundary):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _source(f, target)
    original, files, before = path.read_bytes(), _files(f), _rows(f)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    native = Mock(wraps=pdf_editor.apply_pdf_edit_plan)
    monkeypatch.setattr(pdf_editor, "apply_pdf_edit_plan", native)

    def revoke(value, _detail):
        if value == boundary:
            with f.case.database.immediate_session() as unit:
                unit.delete(unit.get_one(WorkspaceMembership, (u.workspace_id, u.alice_id)))

    with execution_actor_scope(actor), pytest.raises((AccessDenied, SessionNotFound)):
        _edit(f, target, source, revoke)
    assert native.call_count == (0 if boundary == 0.1 else 1)
    assert _rows(f) == before and _files(f) == files and path.read_bytes() == original


@pytest.mark.parametrize("loss", ["lease", "credential"])
def test_final_writer_rechecks_current_lease_and_credential_before_moves(
    library, monkeypatch, loss
):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _source(f, target)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    job = _job(f, actor, target, source)
    before, files, original = _rows(f), _files(f), path.read_bytes()
    original_writer = f.case.database.immediate_session

    @contextmanager
    def revoke_then_write():
        with original_writer() as unit:
            if loss == "lease":
                unit.get_one(m.Job, job.id).lease_expires_at = m.utcnow() - timedelta(seconds=10)
            else:
                unit.delete(unit.get_one(ApiTokenWorkspaceGrant, (actor.token_id, u.workspace_id)))
        with original_writer() as unit:
            yield unit

    monkeypatch.setattr(f.case.database, "immediate_session", revoke_then_write)
    with _leased_actor(actor, job), pytest.raises((AccessDenied, SessionNotFound)):
        _edit(f, target, source)
    assert _rows(f) == before and _files(f) == files and path.read_bytes() == original


def test_partial_registration_fault_rolls_back_pair_edges_and_files(library, monkeypatch):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _source(f, target)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    before, files, original = _rows(f), _files(f), path.read_bytes()
    artifacts = f.services.workflow_handlers.artifacts
    register = artifacts.register_in_session
    calls = 0

    def register_then_fail(unit, path, **kwargs):
        nonlocal calls
        artifact = register(unit, path, **kwargs)
        calls += 1
        if calls == 2:
            raise RuntimeError("Synthetic PDF registration failure")
        return artifact

    monkeypatch.setattr(artifacts, "register_in_session", register_then_fail)
    with (
        execution_actor_scope(actor),
        pytest.raises(RuntimeError, match="Synthetic PDF registration failure"),
    ):
        _edit(f, target, source)
    assert (
        calls == 2 and _rows(f) == before and _files(f) == files and path.read_bytes() == original
    )


@pytest.mark.parametrize("cancelled", [False, True])
def test_cancellation_only_swallows_interruptions_with_real_set_event(
    library, monkeypatch, cancelled
):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _source(f, target)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    cancel = threading.Event()
    before, files, original = _rows(f), _files(f), path.read_bytes()
    native = pdf_editor.apply_pdf_edit_plan

    def interrupt(source, destination, plan, **kwargs):
        if not cancelled:
            raise InterruptedError("Synthetic unrelated interruption")
        result = native(source, destination, plan, **kwargs)
        cancel.set()
        return result

    monkeypatch.setattr(pdf_editor, "apply_pdf_edit_plan", interrupt)
    with execution_actor_scope(actor):
        if cancelled:
            assert _edit(f, target, source, cancel=cancel) == {}
        else:
            with pytest.raises(InterruptedError, match="Synthetic unrelated interruption"):
                _edit(f, target, source, cancel=cancel)
    assert _rows(f) == before and _files(f) == files and path.read_bytes() == original


@pytest.mark.parametrize("fault", ["progress", "context_after_commit"])
def test_committed_pair_survives_late_callback_and_uncertain_context_failure(
    library, monkeypatch, fault
):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _source(f, target)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    before, original = _rows(f), path.read_bytes()

    def late_progress(value, _detail):
        if fault == "progress" and value == 1.0:
            with f.case.database.immediate_session() as unit:
                unit.delete(unit.get_one(WorkspaceMembership, (u.workspace_id, u.alice_id)))
            raise RuntimeError("Synthetic late PDF failure")

    if fault == "context_after_commit":
        writer = f.case.database.immediate_session

        @contextmanager
        def commit_then_raise():
            with writer() as unit:
                yield unit
            raise RuntimeError("Synthetic late PDF failure")

        monkeypatch.setattr(f.case.database, "immediate_session", commit_then_raise)
    with (
        execution_actor_scope(actor),
        pytest.raises(RuntimeError, match="Synthetic late PDF failure"),
    ):
        _edit(f, target, source, late_progress)
    assert _rows(f) == (before[0] + 2, before[1] + 3) and path.read_bytes() == original
    with f.case.database.snapshot_session() as unit:
        pair = list(
            unit.scalars(
                select(m.Artifact).where(
                    m.Artifact.session_id == target["id"],
                    m.Artifact.role.in_(["pdf_edited", "provenance"]),
                )
            )
        )
        assert len(pair) == 2
        assert all(f.services.paths.managed_path(item.relative_path).is_file() for item in pair)


def test_user_publisher_requires_exact_captured_input_before_rendered_preparation(
    library, monkeypatch
):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _source(f, target)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    artifacts = f.services.workflow_handlers.artifacts
    prepare = Mock(side_effect=AssertionError("Missing captured authority prepared a file"))
    monkeypatch.setattr(artifacts, "prepare_registration", prepare)
    for expected in (None, object()):
        with execution_actor_scope(actor), pytest.raises(AccessDenied):
            pdf_publication.publish_pdf_edit(
                f.case.database,
                f.services.paths,
                artifacts,
                path,
                path.with_suffix(".json"),
                {},
                path.with_name("edited.pdf"),
                source_artifact_id=source.id,
                session_id=target["id"],
                cancel_event=threading.Event(),
                _expected_input=cast(Any, expected),
            )
    prepare.assert_not_called()


def test_changed_native_source_digest_rejects_before_publication(library, monkeypatch):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _source(f, target)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    before, files, original = _rows(f), _files(f), path.read_bytes()
    native = pdf_editor.apply_pdf_edit_plan

    def wrong_source_digest(*args, **kwargs):
        destination, manifest, provenance = native(*args, **kwargs)
        provenance["source"]["sha256"] = "f" * 64
        return destination, manifest, provenance

    monkeypatch.setattr(pdf_editor, "apply_pdf_edit_plan", wrong_source_digest)
    with (
        execution_actor_scope(actor),
        pytest.raises(ValueError, match="The PDF source changed before publication"),
    ):
        _edit(f, target, source)
    assert _rows(f) == before and _files(f) == files and path.read_bytes() == original


def test_expected_input_rejects_changed_header_after_rendering(library):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _source(f, target)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    with execution_actor_scope(actor), f.case.database.snapshot_session() as unit:
        captured = require_pdf_edit_access(unit, target["id"], source.id)
        assert type(captured) is PdfEditInput
        with pytest.raises(KeyError, match="Source artifact not found"):
            require_pdf_edit_access(
                unit, target["id"], source.id, expected=replace(captured, relative_path="different")
            )
    before, files = _rows(f), _files(f)

    def change(value, _detail):
        if value == 0.85:
            with f.case.database.immediate_session() as unit:
                unit.get_one(m.Artifact, source.id).content_hash = "f" * 64

    with execution_actor_scope(actor), pytest.raises(KeyError, match="Source artifact not found"):
        _edit(f, target, source, change)
    assert _rows(f) == before and _files(f) == files and path.is_file()
