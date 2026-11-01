"""Fresh native archive export authority, consistent streaming and publication."""

from __future__ import annotations

import json
import threading
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from pandrator.web import bundles
from pandrator.web import models as m
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.access.session_bundle_policy import (
    BundleExportInput,
    require_bundle_export_access,
)
from pandrator.web.access.session_policy import SessionNotFound
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import (
    ApiTokenProjectGrant,
    ApiTokenWorkspaceGrant,
)
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_source_library_workspace_http import library as library  # noqa: F401
from tests.test_source_preparation_workspace_access import (
    FULL_SCOPES,
    _actor,
    _attach,
    _leased_actor,
    _project,
)

RUN_SCOPES = frozenset({"app.read", "app.run"})


def _source(f: Any, target: dict[str, Any], *, metadata=None, content=b"Bundle source bytes.\n"):
    path = f.services.paths.uploads / f"native-bundle-{uuid4()}.txt"
    path.write_bytes(content)
    with execution_actor_scope(HostActor("pandrator-cli")):
        artifact = f.services.artifacts.register(
            path, kind="source", role="upload", session_id=target["id"], metadata=metadata
        )
    return artifact, path


def _count(f):
    with f.case.database.snapshot_session() as unit:
        return (
            unit.scalar(
                select(func.count())
                .select_from(m.Artifact)
                .where(m.Artifact.role == "session_bundle")
            )
            or 0
        )


def _export(f, target, progress=None, cancel=None, **extra):
    return f.services.workflow_handlers.export_session_bundle(
        {"session_id": target["id"], **extra},
        progress or Mock(),
        cancel or threading.Event(),
    )


def _archive(path):
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        content = {
            item["source_id"]: archive.read(item["archive_path"]) for item in manifest["artifacts"]
        }
    return manifest, content


def test_none_policy_and_legacy_handler_keep_injected_ports(library, monkeypatch):
    f = library
    unit = Mock()
    with execution_actor_scope(None):
        require_bundle_export_access(unit, "not-consulted", inputs=cast(Any, object()))
    assert unit.mock_calls == []
    callback = Mock()
    native = Mock()
    actual = f.services.paths.root / "legacy-native-result.pandrator-session"
    native.export_bundle.return_value = {
        "path": str(actual),
        "sha256": "synthetic",
        "artifacts": 0,
        "version": 1,
    }
    monkeypatch.setattr(bundles, "SessionBundleService", Mock(return_value=native))
    register = Mock(return_value=SimpleNamespace(id="registered-bundle"))
    record = Mock(return_value=SimpleNamespace(storage_key="legacy-key"))
    directory = Mock(return_value=f.services.paths.root)
    handler = SimpleNamespace(
        database=f.case.database,
        paths=f.services.paths,
        artifacts=SimpleNamespace(register=register),
        _session_record=record,
        _session_dir=directory,
    )
    cancel = threading.Event()
    with execution_actor_scope(None):
        result = WorkflowHandlers.export_session_bundle(
            cast(WorkflowHandlers, handler),
            {"session_id": "legacy-target", "include_sources": False},
            callback,
            cancel,
        )
    record.assert_called_once_with("legacy-target")
    directory.assert_called_once_with("legacy-target")
    assert set(native.export_bundle.call_args.kwargs) == {"include_sources", "progress_callback"}
    assert native.export_bundle.call_args.kwargs["include_sources"] is False
    register.assert_called_once_with(
        actual, kind="bundle", role="session_bundle", session_id="legacy-target"
    )
    assert result["artifact_id"] == "registered-bundle"
    assert [call.args[0] for call in callback.call_args_list] == [0.02, 0.97, 1.0]


@pytest.mark.parametrize("leased", [False, True])
def test_run_without_edit_exports_with_actual_lease_and_no_io_in_snapshot(
    library, monkeypatch, leased
):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _source(f, target)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, projects=(target["project_id"],))
    original_snapshot = f.case.database.snapshot_session
    original_hash = bundles.sha256_file
    active = 0

    @contextmanager
    def tracked_snapshot():
        nonlocal active
        with original_snapshot() as unit:
            active += 1
            try:
                yield unit
            finally:
                active -= 1

    def outside_hash(candidate):
        assert active == 0
        return original_hash(candidate)

    monkeypatch.setattr(f.case.database, "snapshot_session", tracked_snapshot)
    monkeypatch.setattr(bundles, "sha256_file", outside_hash)
    job: m.Job | None = None
    with execution_actor_scope(actor):
        if leased:
            queued = f.services.jobs.enqueue("session.bundle.export", {}, session_id=target["id"])
            job = f.services.jobs.claim("session-export-worker", lease_seconds=900)
            assert job is not None and job.id == queued.id
    scope = _leased_actor(actor, job) if job is not None else execution_actor_scope(actor)
    with scope:
        result = _export(f, target, _job_id="untrusted-job-reference")
    manifest, content = _archive(result["path"])
    assert content[source.id] == path.read_bytes()
    assert manifest["version"] == 1
    with original_snapshot() as unit:
        artifact = unit.get_one(m.Artifact, result["artifact_id"])
        assert artifact.content_hash == result["sha256"]
        assert artifact.session_id == target["id"]


@pytest.mark.parametrize("denial", ["foreign_admin", "reader", "wrong_kind"])
def test_denial_precedes_session_body_and_native_output(library, monkeypatch, denial):
    f, u = library, library.u
    target = _project(u.alice, u.ah, None if denial == "foreign_admin" else u.workspace_id)
    _source(f, target)
    job: m.Job | None = None
    valid: m.Job | None = None
    if denial == "foreign_admin":
        owner = f.case.auth.initial_account().user_id
        actor = _actor(f.case, owner, FULL_SCOPES | {"app.admin"}, workspaces=(f.homes[owner],))
    elif denial == "reader":
        actor = _actor(f.case, u.alice_id, frozenset({"app.read"}), workspaces=(u.workspace_id,))
    else:
        actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
        with execution_actor_scope(actor):
            wrong = f.services.jobs.enqueue("text.prepare", {}, session_id=target["id"])
            valid = f.services.jobs.enqueue("session.bundle.export", {}, session_id=target["id"])
            job = f.services.jobs.claim("session-export-worker", lease_seconds=900)
        assert job is not None and job.id == wrong.id
    original_get = Session.get

    def deny_body(unit, entity, *args, **kwargs):
        if entity is m.SessionRecord:
            raise AssertionError("Denied export hydrated session body")
        return original_get(unit, entity, *args, **kwargs)

    monkeypatch.setattr(Session, "get", deny_body)
    native = Mock(side_effect=AssertionError("Denied export opened ZIP output"))
    monkeypatch.setattr(zipfile, "ZipFile", native)
    scope = _leased_actor(actor, job) if job is not None else execution_actor_scope(actor)
    with scope, pytest.raises((AccessDenied, SessionNotFound)):
        _export(f, target, _job_id=valid.id if valid is not None else "forged-job")
    native.assert_not_called()


def test_byte_only_attached_source_omits_origin_metadata_but_keeps_bytes(library, monkeypatch):
    f, u = library, library.u
    origin = _project(u.bob, u.bh, u.workspace_id)
    target = _project(u.alice, u.ah, u.workspace_id)
    hidden, hidden_path = _source(
        f, origin, metadata={"origin_private": "never project this value"}
    )
    _attach(f, target["id"], hidden)
    visible, visible_path = _source(
        f, target, metadata={"public_note": "retained", "api_key": "synthetic-export-secret"}
    )
    with f.case.database.immediate_session() as unit:
        unit.get_one(m.Artifact, hidden.id).settings_hash = "origin-settings-private"
        unit.get_one(m.Artifact, visible.id).settings_hash = "visible-settings"
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, projects=(target["project_id"],))
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(f.case.database.engine, "before_cursor_execute", capture)
    try:
        with execution_actor_scope(actor):
            result = _export(f, target)
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", capture)
    manifest, contents = _archive(result["path"])
    rows = {item["source_id"]: item for item in manifest["artifacts"]}
    assert contents == {hidden.id: hidden_path.read_bytes(), visible.id: visible_path.read_bytes()}
    assert rows[hidden.id]["metadata"] == {} and rows[hidden.id]["settings_hash"] is None
    assert rows[visible.id]["metadata"]["public_note"] == "retained"
    assert "synthetic-export-secret" not in json.dumps(manifest)
    assert rows[visible.id]["settings_hash"] == "visible-settings"
    assert any(
        "case when" in sql and "artifacts.metadata_json" in sql and "artifacts.settings_hash" in sql
        for sql in statements
    )
    assert all("origin_private" not in item for item in [json.dumps(rows[hidden.id])])


@pytest.mark.parametrize("revocation", ["membership", "resource", "metadata"])
def test_callback_revocation_rejects_publication_and_preserves_old_output(library, revocation):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    origin = _project(u.bob, u.bh, u.workspace_id)
    source, source_path = _source(f, origin)
    _attach(f, target["id"], source)
    actor = _actor(
        f.case,
        u.alice_id,
        RUN_SCOPES,
        projects=(target["project_id"],),
        workspaces=(u.workspace_id,) if revocation == "metadata" else (),
    )
    with execution_actor_scope(actor):
        prior = _export(f, target)
    old_path = Path(prior["path"])
    old_bytes, original = old_path.read_bytes(), source_path.read_bytes()
    before = _count(f)
    revoked = False

    def progress(_value, detail):
        nonlocal revoked
        if revoked or detail != "Writing session bundle manifest":
            return
        revoked = True
        with f.case.database.immediate_session() as unit:
            if revocation == "membership":
                unit.delete(unit.get_one(WorkspaceMembership, (u.workspace_id, u.alice_id)))
            elif revocation == "resource":
                unit.delete(
                    unit.get_one(ApiTokenProjectGrant, (actor.token_id, target["project_id"]))
                )
            else:
                unit.delete(unit.get_one(ApiTokenWorkspaceGrant, (actor.token_id, u.workspace_id)))
        if revocation == "metadata":
            with f.case.database.snapshot_session() as unit:
                require_bundle_export_access(
                    unit,
                    target["id"],
                    inputs=(
                        BundleExportInput(
                            source.id, source.relative_path, source.content_hash, False
                        ),
                    ),
                )

    with execution_actor_scope(actor), pytest.raises((AccessDenied, SessionNotFound, KeyError)):
        _export(f, target, progress)
    assert revoked and _count(f) == before
    assert old_path.read_bytes() == old_bytes and source_path.read_bytes() == original
    assert list(old_path.parent.parent.glob("session-bundle-*/*.tmp")) == []
    assert list(old_path.parent.parent.glob("session-bundle-*/*.pandrator-session")) == [old_path]


def test_source_mutation_after_initial_hash_cannot_publish_inconsistent_archive(library):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _source(f, target)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    before = _count(f)

    def mutate(_value, detail):
        if detail == "Verified session artifact 1 of 1":
            path.write_bytes(b"Changed after hashing.\n")

    with (
        execution_actor_scope(actor),
        pytest.raises(ValueError, match="Bundle artifact changed during export"),
    ):
        _export(f, target, mutate)
    assert _count(f) == before
    with f.case.database.snapshot_session() as unit:
        record = unit.get_one(m.SessionRecord, target["id"])
        session_dir = f.services.paths.sessions / record.storage_key
    assert list(session_dir.glob("stage-runs/session-bundle-*/*.pandrator-session")) == []
    with f.case.database.snapshot_session() as unit:
        assert unit.get(m.Artifact, source.id) is not None


def test_repeated_authenticated_outputs_are_unique_and_old_archive_is_immutable(library):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    _source(f, target)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    with execution_actor_scope(actor):
        first = _export(f, target)
        original = Path(first["path"]).read_bytes()
        second = _export(f, target)
    assert first["path"] != second["path"] and first["artifact_id"] != second["artifact_id"]
    assert Path(first["path"]).read_bytes() == original


def test_cancellation_during_copy_returns_empty_and_removes_unpublished_output(
    library, monkeypatch
):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    _artifact, source_path = _source(f, target, content=b"large enough" * 150000)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    cancel = threading.Event()
    before = _count(f)

    original_open = Path.open
    copied = []

    def opening(candidate, *args, **kwargs):
        handle = original_open(candidate, *args, **kwargs)
        if candidate != source_path:
            return handle

        @contextmanager
        def canceling_handle():
            with handle:

                def read(size):
                    chunk = handle.read(size)
                    copied.append(len(chunk))
                    cancel.set()
                    return chunk

                yield SimpleNamespace(read=read)

        return canceling_handle()

    def cancel_during_copy(_value, detail):
        if detail == "Writing session bundle manifest":
            monkeypatch.setattr(Path, "open", opening)

    with execution_actor_scope(actor):
        assert _export(f, target, cancel_during_copy, cancel) == {}
    assert copied == [1024 * 1024]
    assert _count(f) == before
    with f.case.database.snapshot_session() as unit:
        record = unit.get_one(m.SessionRecord, target["id"])
        directory = f.services.paths.sessions / record.storage_key
    assert list(directory.glob("stage-runs/session-bundle-*")) == []


def test_injected_registration_failure_rolls_back_row_and_removes_new_file(library, monkeypatch):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    _source(f, target)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    artifacts = f.services.workflow_handlers.artifacts
    original_register = artifacts.register_in_session
    outputs = []
    before = _count(f)

    def fail_after_registration(unit, path, **kwargs):
        outputs.append(path)
        original_register(unit, path, **kwargs)
        raise RuntimeError("Synthetic registration failure")

    monkeypatch.setattr(artifacts, "register_in_session", fail_after_registration)
    with (
        execution_actor_scope(actor),
        pytest.raises(RuntimeError, match="Synthetic registration failure"),
    ):
        _export(f, target)
    assert _count(f) == before and len(outputs) == 1 and not outputs[0].exists()
    assert not outputs[0].parent.exists()


def test_callback_failure_after_committed_registration_keeps_valid_output(library):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    _source(f, target)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    published = []

    def commit_then_fail(path, _digest, _identities):
        artifact = f.services.artifacts.register(
            path, kind="bundle", role="session_bundle", session_id=target["id"]
        )
        published.append((artifact.id, path))
        raise RuntimeError("Synthetic callback failure after commit")

    with (
        execution_actor_scope(actor),
        pytest.raises(RuntimeError, match="Synthetic callback failure after commit"),
    ):
        bundles.SessionBundleService(f.case.database, f.services.paths).export_bundle(
            target["id"], None, publish_callback=commit_then_fail
        )
    assert len(published) == 1 and published[0][1].is_file()
    with f.case.database.snapshot_session() as unit:
        assert unit.get(m.Artifact, published[0][0]) is not None


def test_trusted_explicit_registered_destination_is_never_reused_even_when_missing(library):
    f, u = library, library.u
    target = _project(u.alice, u.ah)
    _source(f, target)
    destination = f.services.paths.root / f"retained-{uuid4()}.pandrator-session"
    destination.write_bytes(b"Retained ordinary archive bytes")
    with execution_actor_scope(HostActor("pandrator-cli")):
        f.services.artifacts.register(
            destination, kind="bundle", role="session_bundle", session_id=target["id"]
        )
    service = bundles.SessionBundleService(f.case.database, f.services.paths)
    with execution_actor_scope(None):
        first = service.export_bundle(target["id"], destination)
        assert destination.read_bytes() == b"Retained ordinary archive bytes"
        destination.unlink()
        second = service.export_bundle(target["id"], destination)
    assert Path(first["path"]) != destination and Path(second["path"]) not in {
        destination,
        Path(first["path"]),
    }


def test_bare_host_can_export_retained_rows_while_user_cannot(library):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    _source(f, target)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    with f.case.database.immediate_session() as unit:
        unit.get_one(m.SessionRecord, target["id"]).trashed_at = m.utcnow()
        unit.get_one(m.Workspace, u.workspace_id).state = "archived"
    service = bundles.SessionBundleService(f.case.database, f.services.paths)
    with execution_actor_scope(actor), pytest.raises((AccessDenied, SessionNotFound)):
        service.export_bundle(target["id"], None)
    with execution_actor_scope(HostActor("pandrator-cli")):
        result = service.export_bundle(
            target["id"], f.services.paths.root / f"host-{uuid4()}.pandrator-session"
        )
    assert Path(result["path"]).is_file()
