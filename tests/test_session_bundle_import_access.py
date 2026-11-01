"""Current native bundle import, portable paths and atomic root publication."""

from __future__ import annotations

import hashlib
import json
import threading
import warnings
import zipfile
from contextlib import contextmanager
from datetime import timedelta
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pandrator.web import bundles, workflow_handlers
from pandrator.web import models as m
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.access.session_bundle_policy import (
    BundleImportInput,
    require_bundle_import_execution_access,
)
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.projects.models import Project
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_source_library_workspace_http import library as library  # noqa: F401
from tests.test_source_preparation_workspace_access import (
    FULL_SCOPES,
    _actor,
    _leased_actor,
    _project,
)


def _manifest(contents=None):
    contents = contents or {
        "files/source.txt": b"Source bytes.\n",
        "files/clean.txt": b"Clean bytes.\n",
    }
    artifacts = []
    for index, (name, content) in enumerate(contents.items()):
        artifacts.append(
            {
                "source_id": f"old-{index}",
                "kind": "text",
                "role": "upload" if index == 0 else "clean_text",
                "archive_path": name,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "state": "current",
                "settings_hash": "retained-settings",
                "metadata": {"note": "retained"},
            }
        )
    return {
        "format": "pandrator-session",
        "version": 1,
        "session": {
            "name": "Native import",
            "workflow_kind": "audiobook",
            "source_language": "en",
            "included_stages": [],
        },
        "artifacts": artifacts,
        "artifact_edges": [{"parent": "old-0", "child": "old-1", "relation": "derived_from"}]
        if len(artifacts) > 1
        else [],
    }, contents


def _bundle(f, target, *, manifest=None, contents=None, entries=None):
    if manifest is None:
        manifest, contents = _manifest(contents)
    path = f.services.paths.uploads / f"native-import-{uuid4()}.pandrator-session"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            for name, content in entries if entries is not None else list((contents or {}).items()):
                archive.writestr(name, content)
    with execution_actor_scope(HostActor("pandrator-cli")):
        source = f.services.artifacts.register(
            path,
            kind="bundle",
            role="upload",
            session_id=target["id"],
            metadata={"private_source_metadata": "not required"},
        )
    return source, path


def _counts(f):
    with f.case.database.snapshot_session() as unit:
        return tuple(
            unit.scalar(select(func.count()).select_from(model)) or 0
            for model in (
                m.SessionRecord,
                Project,
                m.Artifact,
                m.ArtifactEdge,
                m.Provider,
                m.ProviderModel,
                m.Voice,
                m.SessionSetting,
            )
        )


def _service(f):
    return bundles.SessionBundleService(f.case.database, f.services.paths)


def _import(f, source_id, progress=None, cancel=None):
    return f.services.workflow_handlers.import_session_bundle(
        {
            "source_artifact_id": source_id,
            "_job_id": "untrusted-reference",
            "workspace_id": "ignored-workspace",
        },
        progress or Mock(),
        cancel or threading.Event(),
    )


def _job(f, actor, source, *, kind="session.bundle.import", selector=None, workspace=None):
    with execution_actor_scope(actor):
        queued = f.services.jobs.enqueue(
            kind,
            {"source_artifact_id": selector or source.id},
            workspace_id=workspace or source.workspace_id,
        )
        claimed = f.services.jobs.claim("bundle-import-worker", lease_seconds=900)
    assert claimed is not None and claimed.id == queued.id
    return claimed


def test_none_policy_and_injected_handler_ports_remain_compatible(library, monkeypatch):
    f = library
    unit = Mock()
    with execution_actor_scope(None):
        assert (
            require_bundle_import_execution_access(unit, "ignored", expected=cast(Any, object()))
            is None
        )
    assert unit.mock_calls == []
    source = f.services.paths.root / "injected.pandrator-session"
    resolver = Mock(return_value=(SimpleNamespace(id="source-id"), source))
    native = Mock()
    native.import_bundle.return_value = {
        "session_id": "new-id",
        "name": "Imported",
        "artifacts": 0,
        "source_bundle_sha256": "digest",
    }
    monkeypatch.setattr(bundles, "SessionBundleService", Mock(return_value=native))
    owner = Mock(return_value=SimpleNamespace(workspace_id="actual-workspace"))
    monkeypatch.setattr(workflow_handlers, "artifact_job_ownership", owner)
    callback = Mock()
    handler = cast(
        WorkflowHandlers,
        SimpleNamespace(database=f.case.database, paths=f.services.paths, _resolve_input=resolver),
    )
    with execution_actor_scope(None):
        result = WorkflowHandlers.import_session_bundle(
            handler,
            {"source_artifact_id": "source-id", "name": "Chosen"},
            callback,
            threading.Event(),
        )
    resolver.assert_called_once_with("source-id")
    assert set(native.import_bundle.call_args.kwargs) == {
        "name",
        "workspace_id",
        "progress_callback",
    }
    assert native.import_bundle.call_args.args == (source,)
    assert native.import_bundle.call_args.kwargs["workspace_id"] == "actual-workspace"
    assert result["session_id"] == "new-id"
    assert [call.args[0] for call in callback.call_args_list] == [0.02, 1.0]


@pytest.mark.parametrize("leased", [False, True])
def test_native_import_remaps_bytes_edges_and_actual_user_creator(library, leased):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _bundle(f, target)
    original = path.read_bytes()
    actor = _actor(f.case, u.alice_id, FULL_SCOPES, workspaces=(u.workspace_id,))
    before = _counts(f)
    scope = _leased_actor(actor, _job(f, actor, source)) if leased else execution_actor_scope(actor)
    with scope:
        result = _import(f, source.id)
    assert path.read_bytes() == original
    assert result["source_bundle_sha256"] == hashlib.sha256(original).hexdigest()
    assert result["artifacts"] == 2
    assert _counts(f) == tuple(
        value + delta for value, delta in zip(before, (1, 1, 2, 1, 0, 0, 0, 0), strict=True)
    )
    with f.case.database.snapshot_session() as unit:
        record = unit.get_one(m.SessionRecord, result["session_id"])
        project = unit.get_one(Project, record.project_id)
        assert project.created_by_user_id == u.alice_id and project.workspace_id == u.workspace_id
        assert record.interface_mode == "advanced"
        artifacts = list(unit.scalars(select(m.Artifact).where(m.Artifact.session_id == record.id)))
        mapped = {item.metadata_json["bundle_source_artifact_id"]: item for item in artifacts}
        assert set(mapped) == {"old-0", "old-1"}
        assert all(
            item.id not in mapped
            and item.workspace_id == u.workspace_id
            and item.settings_hash == "retained-settings"
            for item in artifacts
        )
        assert unit.get(m.ArtifactEdge, (mapped["old-0"].id, mapped["old-1"].id)) is not None
        assert (
            f.services.paths.managed_path(mapped["old-0"].relative_path).read_bytes()
            == b"Source bytes.\n"
        )
        assert (
            f.services.paths.managed_path(mapped["old-1"].relative_path).read_bytes()
            == b"Clean bytes.\n"
        )


@pytest.mark.parametrize(
    "denial",
    ["wrong_kind", "wrong_selector", "wrong_workspace", "foreign_source", "narrowed_credential"],
)
def test_execution_denial_precedes_source_body_and_archive_reads(library, monkeypatch, denial):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, _path = _bundle(f, target)
    actor = (
        _actor(f.case, u.alice_id, FULL_SCOPES, projects=(target["project_id"],))
        if denial == "narrowed_credential"
        else _actor(f.case, u.alice_id, FULL_SCOPES, workspaces=(u.workspace_id,))
    )
    job = None
    if denial in {"wrong_kind", "wrong_selector", "wrong_workspace"}:
        job = _job(
            f,
            actor,
            source,
            kind="session.bundle.export" if denial == "wrong_kind" else "session.bundle.import",
            selector="another-source" if denial == "wrong_selector" else None,
            workspace=f.homes[u.alice_id] if denial == "wrong_workspace" else None,
        )
    if denial == "foreign_source":
        foreign = _project(u.bob, u.bh)
        source, _path = _bundle(f, foreign)
    original_get = Session.get

    def guard_body(unit, entity, *args, **kwargs):
        if entity is m.Artifact:
            raise AssertionError("Denied import hydrated source body")
        return original_get(unit, entity, *args, **kwargs)

    monkeypatch.setattr(Session, "get", guard_body)
    archive = Mock(side_effect=AssertionError("Denied import opened source ZIP"))
    monkeypatch.setattr(zipfile, "ZipFile", archive)
    scope = _leased_actor(actor, job) if job is not None else execution_actor_scope(actor)
    with scope, pytest.raises((AccessDenied, KeyError)):
        _import(f, source.id)
    archive.assert_not_called()


def test_user_cannot_import_an_unregistered_file_or_override_scoped_source(library):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _bundle(f, target)
    actor = _actor(f.case, u.alice_id, FULL_SCOPES, workspaces=(u.workspace_id,))
    with execution_actor_scope(actor):
        with pytest.raises(AccessDenied):
            _service(f).import_bundle(path)
        with pytest.raises(AccessDenied):
            _service(f).import_bundle(
                None, source_artifact_id=source.id, workspace_id=f.homes[u.alice_id]
            )
        with pytest.raises(AccessDenied):
            _service(f).import_bundle(
                path.with_name("other.pandrator-session"), source_artifact_id=source.id
            )


@pytest.mark.parametrize(
    "boundary", ["progress_membership", "preparation_credential", "writer_lease"]
)
def test_current_authority_loss_rolls_back_every_root_and_owned_target(
    library, monkeypatch, boundary
):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _bundle(f, target)
    original = path.read_bytes()
    actor = _actor(f.case, u.alice_id, FULL_SCOPES, workspaces=(u.workspace_id,))
    job = _job(f, actor, source)
    service = _service(f)
    before = _counts(f)
    directories = set(f.services.paths.sessions.iterdir())
    changed = False

    def revoke():
        nonlocal changed
        if changed:
            return
        changed = True
        with f.case.database.immediate_session() as unit:
            if boundary == "progress_membership":
                unit.delete(unit.get_one(WorkspaceMembership, (u.workspace_id, u.alice_id)))
            else:
                unit.delete(unit.get_one(ApiTokenWorkspaceGrant, (actor.token_id, u.workspace_id)))

    def progress(value, _detail):
        if boundary == "progress_membership" and _detail == "Verified bundled artifact 2 of 2":
            revoke()

    if boundary == "preparation_credential":
        original_prepare = service.artifacts.prepare_registration

        def prepare_then_revoke(path, **kwargs):
            prepared = original_prepare(path, **kwargs)
            revoke()
            return prepared

        monkeypatch.setattr(service.artifacts, "prepare_registration", prepare_then_revoke)
    if boundary == "writer_lease":
        original_writer = f.case.database.immediate_session

        @contextmanager
        def expire_then_write():
            nonlocal changed
            with original_writer() as unit:
                unit.get_one(m.Job, job.id).lease_expires_at = m.utcnow() - timedelta(seconds=10)
            changed = True
            with original_writer() as unit:
                yield unit

        monkeypatch.setattr(f.case.database, "immediate_session", expire_then_write)
    with _leased_actor(actor, job), pytest.raises((AccessDenied, KeyError)):
        service.import_bundle(None, source_artifact_id=source.id, progress_callback=progress)
    assert changed and _counts(f) == before
    assert path.read_bytes() == original
    assert set(f.services.paths.sessions.iterdir()) == directories
    assert list(f.services.paths.temporary.glob("pandrator-bundle-*")) == []


def test_registration_failure_after_partial_writes_rolls_back_entire_import(library, monkeypatch):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _bundle(f, target)
    original = path.read_bytes()
    actor = _actor(f.case, u.alice_id, FULL_SCOPES, workspaces=(u.workspace_id,))
    service = _service(f)
    register = service.artifacts.register_in_session
    before = _counts(f)
    directories = set(f.services.paths.sessions.iterdir())
    calls = 0

    def register_then_fail(unit, path, **kwargs):
        nonlocal calls
        artifact = register(unit, path, **kwargs)
        calls += 1
        if calls == 2:
            raise RuntimeError("Synthetic partial import failure")
        return artifact

    monkeypatch.setattr(service.artifacts, "register_in_session", register_then_fail)
    with (
        execution_actor_scope(actor),
        pytest.raises(RuntimeError, match="Synthetic partial import failure"),
    ):
        service.import_bundle(None, source_artifact_id=source.id)
    assert calls == 2 and _counts(f) == before
    assert path.read_bytes() == original and set(f.services.paths.sessions.iterdir()) == directories


def test_cancel_during_streaming_returns_empty_without_rows_or_target(library, monkeypatch):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _bundle(f, target, contents={"files/large.txt": b"a" * (1024 * 1024 + 1)})
    original = path.read_bytes()
    actor = _actor(f.case, u.alice_id, FULL_SCOPES, workspaces=(u.workspace_id,))
    before = _counts(f)
    cancel = threading.Event()
    original_read = zipfile.ZipExtFile.read
    copied = []

    def read_then_cancel(handle, size=-1):
        chunk = original_read(handle, size)
        if handle.name != "manifest.json":
            copied.append(len(chunk))
            cancel.set()
        return chunk

    monkeypatch.setattr(zipfile.ZipExtFile, "read", read_then_cancel)
    with execution_actor_scope(actor):
        assert _import(f, source.id, cancel=cancel) == {}
    assert copied == [1024 * 1024] and _counts(f) == before
    assert path.read_bytes() == original
    assert list(f.services.paths.temporary.glob("pandrator-bundle-*")) == []


def test_postcommit_progress_failure_preserves_committed_output_after_revocation(library):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _bundle(f, target)
    original = path.read_bytes()
    actor = _actor(f.case, u.alice_id, FULL_SCOPES, workspaces=(u.workspace_id,))
    before = _counts(f)

    def fail_after_commit(value, _detail):
        if value == 1:
            with f.case.database.immediate_session() as unit:
                unit.delete(unit.get_one(WorkspaceMembership, (u.workspace_id, u.alice_id)))
            raise RuntimeError("Synthetic postcommit progress failure")

    with (
        execution_actor_scope(actor),
        pytest.raises(RuntimeError, match="Synthetic postcommit progress failure"),
    ):
        _service(f).import_bundle(
            None, source_artifact_id=source.id, progress_callback=fail_after_commit
        )
    assert _counts(f) == tuple(
        value + delta for value, delta in zip(before, (1, 1, 2, 1, 0, 0, 0, 0), strict=True)
    )
    with f.case.database.snapshot_session() as unit:
        artifacts = list(
            unit.scalars(
                select(m.Artifact).where(
                    m.Artifact.id != source.id,
                    m.Artifact.metadata_json["bundle_source_artifact_id"]
                    .as_string()
                    .in_(["old-0", "old-1"]),
                )
            )
        )
        assert len(artifacts) == 2
        assert all(
            f.services.paths.managed_path(item.relative_path).is_file() for item in artifacts
        )
    assert path.read_bytes() == original


def test_portable_member_rules_and_leading_dot_compatibility():
    invalid = [
        "",
        "/absolute",
        "../up",
        "files\\escape",
        "C:drive",
        "files/NUL.txt",
        "files/COM¹.log",
        "files/LPT².txt",
        "files/CONIN$.x",
        "files/con .txt",
        "files/dot.",
        "files/space ",
        "files/question?",
        "files/control\x00x",
        "files/control\x1fx",
    ]
    for name in invalid:
        with pytest.raises(ValueError, match="Unsafe bundle member"):
            bundles.SessionBundleService._safe_member(name)
    for name in ("files/.hidden", "files/book.txt", "files/COM10.txt", "files/LPT0.txt"):
        assert bundles.SessionBundleService._safe_member(name) == PurePosixPath(name)


@pytest.mark.parametrize(
    "fault", ["unsafe", "source_ids", "raw_members", "canonical_members", "case_members"]
)
def test_unsafe_or_ambiguous_archives_never_create_roots(library, fault):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    manifest, contents = _manifest()
    entries = list(contents.items())
    if fault == "unsafe":
        manifest["artifacts"][0]["archive_path"] = "files\\escape.txt"
        entries[0] = ("files\\escape.txt", entries[0][1])
    elif fault == "source_ids":
        manifest["artifacts"][1]["source_id"] = manifest["artifacts"][0]["source_id"]
    elif fault == "raw_members":
        entries.append(entries[0])
    elif fault == "canonical_members":
        entries.append(("files//source.txt", entries[0][1]))
    else:
        entries.extend([("files/A.txt", b"a"), ("files/a.txt", b"b")])
    source, path = _bundle(f, target, manifest=manifest, entries=entries)
    original = path.read_bytes()
    before = _counts(f)
    actor = _actor(f.case, u.alice_id, FULL_SCOPES, workspaces=(u.workspace_id,))
    with execution_actor_scope(actor), pytest.raises(ValueError):
        _import(f, source.id)
    assert _counts(f) == before and path.read_bytes() == original
    assert list(f.services.paths.temporary.glob("pandrator-bundle-*")) == []


@pytest.mark.parametrize("fault", ["size", "sha256", "source_archive"])
def test_checksum_failures_publish_no_session(library, fault):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    manifest, contents = _manifest()
    if fault == "size":
        manifest["artifacts"][0]["size_bytes"] += 1
    elif fault == "sha256":
        manifest["artifacts"][0]["sha256"] = "0" * 64
    source, path = _bundle(f, target, manifest=manifest, contents=contents)
    actor = _actor(f.case, u.alice_id, FULL_SCOPES, workspaces=(u.workspace_id,))
    before = _counts(f)

    def mutate(value, _detail):
        if fault == "source_archive" and value == 0.92:
            with path.open("ab") as handle:
                handle.write(b"source archive changed")

    with (
        execution_actor_scope(actor),
        pytest.raises(ValueError, match="Bundle checksum validation failed"),
    ):
        _service(f).import_bundle(None, source_artifact_id=source.id, progress_callback=mutate)
    assert _counts(f) == before


@pytest.mark.parametrize("guard", ["manifest", "disk"])
def test_small_mocked_resource_limits_reject_before_extraction(library, monkeypatch, guard):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, _path = _bundle(f, target)
    actor = _actor(f.case, u.alice_id, FULL_SCOPES, workspaces=(u.workspace_id,))
    before = _counts(f)
    if guard == "manifest":
        monkeypatch.setattr(bundles, "_MAX_BUNDLE_MANIFEST_BYTES", 8)
        expected = "Session bundle manifest is too large"
    else:
        monkeypatch.setattr(
            bundles.shutil, "disk_usage", Mock(return_value=SimpleNamespace(free=0))
        )
        expected = "Session bundle exceeds available temporary storage"
    with execution_actor_scope(actor), pytest.raises(ValueError, match=expected):
        _import(f, source.id)
    assert _counts(f) == before
    assert list(f.services.paths.temporary.glob("pandrator-bundle-*")) == []


def test_invalid_container_and_field_shapes_fail_before_root_creation(library):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    actor = _actor(f.case, u.alice_id, FULL_SCOPES, workspaces=(u.workspace_id,))
    for field, value in (("session", []), ("artifacts", {}), ("artifact_edges", [None])):
        manifest, contents = _manifest()
        manifest[field] = value
        source, _path = _bundle(f, target, manifest=manifest, contents=contents)
        before = _counts(f)
        with (
            execution_actor_scope(actor),
            pytest.raises(ValueError, match="Invalid session bundle manifest"),
        ):
            _import(f, source.id)
        assert _counts(f) == before


def test_preparation_header_change_rejects_captured_input_identity(library, monkeypatch):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _bundle(f, target)
    original = path.read_bytes()
    actor = _actor(f.case, u.alice_id, FULL_SCOPES, workspaces=(u.workspace_id,))
    service = _service(f)
    prepare = service.artifacts.prepare_registration
    before = _counts(f)
    changed = False

    def change_header(candidate, **kwargs):
        nonlocal changed
        prepared = prepare(candidate, **kwargs)
        if not changed:
            changed = True
            with f.case.database.immediate_session() as unit:
                unit.get_one(m.Artifact, source.id).content_hash = "f" * 64
        return prepared

    monkeypatch.setattr(service.artifacts, "prepare_registration", change_header)
    with execution_actor_scope(actor), pytest.raises(KeyError, match="Bundle artifact not found"):
        service.import_bundle(None, source_artifact_id=source.id)
    assert changed and _counts(f) == before and path.read_bytes() == original
    with execution_actor_scope(None):
        assert (
            require_bundle_import_execution_access(
                Mock(), source.id, expected=BundleImportInput("ignored", "ignored", "ignored", None)
            )
            is None
        )


def test_none_and_bare_host_explicit_file_keep_legacy_default_workspace(library):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    _source, path = _bundle(f, target)
    original = path.read_bytes()
    initial_owner = f.case.auth.initial_account().user_id
    for actor in (None, HostActor("pandrator-cli")):
        with execution_actor_scope(actor):
            result = _service(f).import_bundle(path)
        with f.case.database.snapshot_session() as unit:
            record = unit.get_one(m.SessionRecord, result["session_id"])
            project = unit.get_one(Project, record.project_id)
            assert project.workspace_id == f.homes[initial_owner]
        assert path.read_bytes() == original
