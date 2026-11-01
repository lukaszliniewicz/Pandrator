"""Fresh authority, finite input reads and atomic source ingress publication."""

from __future__ import annotations

import builtins
import shutil
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Iterator, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text

from pandrator.web import models as m
from pandrator.web.access.artifact_visibility import resource_visibility_clause
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.access.session_policy import SessionNotFound
from pandrator.web.access.source_ingress_policy import require_source_ingress_access
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.accounts.execution_actor import (
    HostActor,
    UserActor,
    actor_from_principal,
    execution_actor_scope,
)
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.job_execution import current_job_lease
from pandrator.web.models import new_id
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.source_library import SourceLibraryService
from pandrator.web.workflow_source import (
    SourceWorkflowContext,
    _load_reusable_source,
    download_source_url,
    reuse_source,
)
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_source_library_workspace_http import library as library  # noqa: F401

FULL_SCOPES = frozenset({"app.read", "app.write", "app.run"})


def project(client, csrf, workspace=None):
    values = {"name": f"Source ingress {uuid4()}", "workflow_kind": "subtitles"}
    if workspace is not None:
        values["workspace_id"] = workspace
    return body(
        client.post("/api/v1/sessions", json=values, headers=headers(csrf, str(uuid4()))), 201
    )


def _actor(
    case: Any,
    user_id: str,
    scopes: frozenset[str] = FULL_SCOPES,
    *,
    workspaces: tuple[str, ...] = (),
    projects: tuple[str, ...] = (),
) -> UserActor:
    _record, raw = case.auth.create_api_token(
        "Synthetic source ingress",
        user_id=user_id,
        scopes=scopes,
        resource_grants=CredentialResources(
            workspace_ids=frozenset(workspaces),
            project_ids=frozenset(projects),
        ),
    )
    principal = case.auth.resolve_api_token(
        raw,
        network_zone="loopback",
        target_instance_id=None,
    )
    assert principal is not None
    actor = actor_from_principal(principal)
    assert type(actor) is UserActor
    return actor


def _workflow_context(
    f: Any,
    record: dict[str, Any],
    *,
    resolver=None,
    session_dir: Path | None = None,
    validate_url=None,
) -> SourceWorkflowContext:
    with f.case.database.snapshot_session() as unit:
        stored = unit.get_one(m.SessionRecord, record["id"])
        default_dir = f.services.paths.sessions / stored.storage_key
    return cast(
        SourceWorkflowContext,
        SimpleNamespace(
            database=f.case.database,
            paths=f.services.paths,
            artifacts=f.services.artifacts,
            _resolve_input=resolver or Mock(side_effect=AssertionError("unexpected input lookup")),
            _session_dir=lambda _session_id: session_dir or default_dir,
            _validate_download_url=validate_url or (lambda value: value),
        ),
    )


def _registered_source(
    f: Any,
    record: dict[str, Any],
    *,
    filename: str = "source.txt",
    data: bytes = b"Original reusable source bytes.",
):
    path = f.services.paths.uploads / f"{uuid4()}-{filename}"
    path.write_bytes(data)
    with execution_actor_scope(HostActor("pandrator-cli")):
        artifact = f.services.artifacts.register(
            path,
            kind=path.suffix.lower().lstrip(".") or "text",
            role="upload",
            session_id=record["id"],
            metadata={"original_filename": filename},
        )
        asset = f.services.source_library.ensure_for_artifact(
            artifact.id,
            display_name=filename,
        )
        f.services.source_library.attach(record["id"], asset.id)
    return artifact, asset, path


def _row_counts(f: Any) -> tuple[int, ...]:
    models = (m.Artifact, m.SourceRecord, m.SourceAsset, m.SessionSource)
    with f.case.database.snapshot_session() as unit:
        return tuple(unit.scalar(select(func.count()).select_from(model)) or 0 for model in models)


def _install_fake_ytdlp(
    monkeypatch,
    *,
    filename: str = "synthetic-url.mp4",
    data: bytes = b"URL source bytes.",
) -> list[Path]:
    module = cast(Any, ModuleType("yt_dlp"))
    utils = cast(Any, ModuleType("yt_dlp.utils"))
    output_paths: list[Path] = []

    class DownloadError(Exception):
        pass

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, *, download):
            assert download is True
            output_path = Path(self.options["outtmpl"]).parent / filename
            for hook in self.options["progress_hooks"]:
                hook({"status": "downloading", "downloaded_bytes": 5, "total_bytes": 10})
                hook({"status": "finished"})
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(data)
            output_paths.append(output_path)
            return {"id": "synthetic", "ext": output_path.suffix.lstrip(".")}

        def prepare_filename(self, _information):
            return str(output_paths[-1])

    utils.DownloadError = DownloadError
    module.YoutubeDL = FakeYoutubeDL
    module.__path__ = []
    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    monkeypatch.setitem(sys.modules, "yt_dlp.utils", utils)
    return output_paths


@contextmanager
def _leased_actor(actor: UserActor, job: m.Job) -> Iterator[None]:
    token = current_job_lease.set(
        {
            "job_id": job.id,
            "owner_id": job.lease_owner or "source-ingress-worker",
            "lease_generation": job.lease_generation,
        }
    )
    try:
        with execution_actor_scope(actor):
            yield
    finally:
        current_job_lease.reset(token)


def test_trusted_none_validates_job_kind_uses_injected_loader_and_skips_new_snapshots(
    library, monkeypatch
):
    f, u = library, library.u
    target = project(u.bob, u.bh)
    source, source_path = f.services.artifacts.resolve(f.private.artifact_id)
    resolver = Mock(return_value=(source, source_path))
    context = _workflow_context(f, target, resolver=resolver)
    monkeypatch.setattr(
        f.case.database,
        "snapshot_session",
        lambda: (_ for _ in ()).throw(AssertionError("trusted ingress opened a new snapshot")),
    )

    unit = Mock()
    with execution_actor_scope(None):
        require_source_ingress_access(unit, target["id"], "source.reuse", artifact_id="input")
        with pytest.raises(TypeError):
            require_source_ingress_access(unit, target["id"], "source.other")
        result = reuse_source(
            context,
            {"session_id": target["id"], "artifact_id": source.id},
            Mock(),
            threading.Event(),
        )
    resolver.assert_called_once_with(source.id)
    assert unit.mock_calls == []
    assert result["filename"] == source_path.name


@pytest.mark.parametrize("principal_kind", ["viewer", "read_only_member", "foreign_admin"])
@pytest.mark.parametrize("operation", ["url", "reuse"])
def test_denied_ingress_stops_before_url_resolution_input_lookup_or_destination(
    library, monkeypatch, principal_kind, operation
):
    f, u = library, library.u
    if principal_kind == "read_only_member":
        target = project(u.alice, u.ah, u.workspace_id)
        actor = _actor(
            f.case,
            u.alice_id,
            frozenset({"app.read"}),
            workspaces=(u.workspace_id,),
        )
    elif principal_kind == "viewer":
        target = project(u.bob, u.bh)
        with f.case.database.immediate_session() as unit:
            unit.add(
                ProjectMembership(
                    project_id=target["project_id"],
                    user_id=u.alice_id,
                    role="viewer",
                )
            )
        actor = _actor(
            f.case,
            u.alice_id,
            projects=(target["project_id"],),
        )
    else:
        target = project(u.bob, u.bh)
        owner_id = f.case.auth.initial_account().user_id
        actor = _actor(
            f.case,
            owner_id,
            FULL_SCOPES | {"app.admin"},
            workspaces=(f.homes[owner_id],),
        )

    session_dir = f.services.paths.sessions / f"denied-{uuid4()}"
    resolver = Mock(side_effect=AssertionError("denied reuse read its input"))
    url_validator = Mock(side_effect=AssertionError("denied URL reached DNS validation"))
    context = _workflow_context(
        f,
        target,
        resolver=resolver,
        session_dir=session_dir,
        validate_url=url_validator,
    )

    original_import = builtins.__import__

    def deny_ytdlp_import(name, *args, **kwargs):
        if name == "yt_dlp" or name.startswith("yt_dlp."):
            raise AssertionError("Denied URL ingress imported yt-dlp")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_ytdlp_import)
    with execution_actor_scope(actor), pytest.raises((AccessDenied, KeyError)):
        if operation == "url":
            download_source_url(
                context,
                {"session_id": target["id"], "url": "https://example.invalid/video"},
                Mock(),
                threading.Event(),
            )
        else:
            reuse_source(
                context,
                {"session_id": target["id"], "artifact_id": "private-input"},
                Mock(),
                threading.Event(),
            )
    resolver.assert_not_called()
    url_validator.assert_not_called()
    assert not session_dir.exists()


def test_project_only_current_dependency_loads_only_authorized_header_and_bytes(library):
    f, u = library, library.u
    target = project(u.alice, u.ah, u.workspace_id)
    origin = project(u.bob, u.bh, u.workspace_id)
    source, asset, source_path = _registered_source(
        f,
        origin,
        filename="private-origin.txt",
        data=b"Current source dependency bytes.",
    )
    with execution_actor_scope(HostActor("pandrator-cli")):
        f.services.source_library.attach(target["id"], asset.id)
    with f.case.database.immediate_session() as unit:
        unit.execute(
            text("UPDATE artifacts SET metadata_json = :value WHERE id = :id"),
            {"value": "not valid JSON and must stay unread", "id": source.id},
        )

    actor = _actor(
        f.case,
        u.alice_id,
        projects=(target["project_id"],),
    )
    context = _workflow_context(f, target)
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(f.case.database.engine, "before_cursor_execute", capture)
    try:
        with execution_actor_scope(actor):
            header, authorized_path = _load_reusable_source(
                context,
                target["id"],
                source.id,
            )
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", capture)

    assert header.id == source.id
    assert authorized_path.read_bytes() == b"Current source dependency bytes."
    assert "metadata_json" not in header.__dict__
    assert "settings_json" not in header.__dict__
    assert not any("artifacts.metadata_json" in sql for sql in statements)
    assert source_path.read_bytes() == b"Current source dependency bytes."


def test_project_only_dependency_detached_by_progress_callback_denies_before_copy(library):
    f, u = library, library.u
    target = project(u.alice, u.ah, u.workspace_id)
    origin = project(u.bob, u.bh, u.workspace_id)
    source, asset, source_path = _registered_source(
        f,
        origin,
        filename="current-dependency.txt",
        data=b"Current dependency bytes.",
    )
    with execution_actor_scope(HostActor("pandrator-cli")):
        f.services.source_library.attach(target["id"], asset.id)

    actor = _actor(
        f.case,
        u.alice_id,
        projects=(target["project_id"],),
    )
    context = _workflow_context(f, target)
    with f.case.database.snapshot_session() as unit:
        attachment_id = unit.scalar(
            select(m.SessionSource.id).where(
                m.SessionSource.session_id == target["id"],
                m.SessionSource.source_asset_id == asset.id,
                m.SessionSource.is_current.is_(True),
            )
        )
    assert attachment_id is not None

    before = _row_counts(f)
    copied = Mock(wraps=shutil.copy2)

    def detach_after_progress(_value, _detail):
        with f.case.database.immediate_session() as unit:
            unit.get_one(m.SessionSource, attachment_id).is_current = False

    with execution_actor_scope(actor), pytest.raises(KeyError) as denied:
        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr("pandrator.web.workflow_source.shutil.copy2", copied)
            reuse_source(
                context,
                {"session_id": target["id"], "artifact_id": source.id},
                detach_after_progress,
                threading.Event(),
            )

    assert denied.value.args == ("Source artifact not found.",)
    copied.assert_not_called()
    assert _row_counts(f) == before
    assert source_path.read_bytes() == b"Current dependency bytes."


def test_readable_foreign_workspace_input_is_hidden_from_reuse_loader(library):
    f, u = library, library.u
    target = project(u.alice, u.ah, u.workspace_id)
    owner_id = f.case.auth.initial_account().user_id
    source, source_path = f.services.artifacts.resolve(f.original.artifact_id)
    actor = _actor(
        f.case,
        owner_id,
        workspaces=(u.workspace_id, f.homes[owner_id]),
    )
    context = _workflow_context(f, target)

    with execution_actor_scope(actor), f.case.database.snapshot_session() as unit:
        assert (
            unit.scalar(
                select(m.Artifact.id).where(
                    m.Artifact.id == source.id,
                    resource_visibility_clause(m.Artifact, actor),
                )
            )
            == source.id
        )
    with execution_actor_scope(actor), pytest.raises(KeyError) as hidden:
        _load_reusable_source(context, target["id"], source.id)
    assert hidden.value.args == ("Source artifact not found.",)
    assert source_path.is_file()


@pytest.mark.parametrize("binding", ["job_kind", "target_session", "forged_lease"])
def test_worker_lease_must_be_real_and_bound_to_source_kind_and_target(library, binding):
    f, u = library, library.u
    target = project(u.alice, u.ah, u.workspace_id)
    other_target = project(u.alice, u.ah, u.workspace_id)
    actor = _actor(f.case, u.alice_id, workspaces=(u.workspace_id,))

    with execution_actor_scope(actor):
        job = f.services.jobs.enqueue(
            "source.reuse",
            {"synthetic": True},
            session_id=target["id"],
        )
        claimed = f.services.jobs.claim("source-ingress-worker", lease_seconds=900)
    assert claimed is not None and claimed.id == job.id

    with _leased_actor(actor, claimed):
        if binding == "forged_lease":
            forged = current_job_lease.set(
                {
                    "job_id": new_id(),
                    "owner_id": "source-ingress-worker",
                    "lease_generation": 1,
                }
            )
            try:
                with f.case.database.snapshot_session() as unit, pytest.raises(AccessDenied):
                    require_source_ingress_access(unit, target["id"], "source.reuse")
            finally:
                current_job_lease.reset(forged)
        elif binding == "job_kind":
            with f.case.database.snapshot_session() as unit, pytest.raises(AccessDenied):
                require_source_ingress_access(unit, target["id"], "source.download_url")
        else:
            with f.case.database.snapshot_session() as unit, pytest.raises(AccessDenied):
                require_source_ingress_access(unit, other_target["id"], "source.reuse")


def test_progress_callback_revocation_prevents_native_copy(library):
    f, u = library, library.u
    target = project(u.alice, u.ah, u.workspace_id)
    source, source_path = f.services.artifacts.resolve(f.shared.artifact_id)
    actor = _actor(f.case, u.bob_id, workspaces=(u.workspace_id,))
    context = _workflow_context(f, target)
    copied = Mock(wraps=shutil.copy2)

    def revoke_after_progress(_value, _detail):
        with f.case.database.immediate_session() as unit:
            unit.delete(unit.get_one(WorkspaceMembership, (u.workspace_id, u.bob_id)))

    with execution_actor_scope(actor), pytest.raises(SessionNotFound):
        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr("pandrator.web.workflow_source.shutil.copy2", copied)
            reuse_source(
                context,
                {"session_id": target["id"], "artifact_id": source.id},
                revoke_after_progress,
                threading.Event(),
            )
    copied.assert_not_called()
    assert source_path.is_file()


def test_writer_rechecks_revoked_member_before_atomic_publication(library, monkeypatch):
    f, u = library, library.u
    target = project(u.alice, u.ah, u.workspace_id)
    source, source_path = f.services.artifacts.resolve(f.shared.artifact_id)
    actor = _actor(f.case, u.bob_id, workspaces=(u.workspace_id,))
    context = _workflow_context(f, target)
    before = _row_counts(f)
    prepare = f.services.artifacts.prepare_registration

    def prepare_then_revoke(path):
        prepared = prepare(path)
        with f.case.database.immediate_session() as unit:
            unit.delete(unit.get_one(WorkspaceMembership, (u.workspace_id, u.bob_id)))
        return prepared

    monkeypatch.setattr(f.services.artifacts, "prepare_registration", prepare_then_revoke)
    with execution_actor_scope(actor), pytest.raises(SessionNotFound):
        reuse_source(
            context,
            {"session_id": target["id"], "artifact_id": source.id},
            Mock(),
            threading.Event(),
        )

    assert _row_counts(f) == before
    assert source_path.read_bytes() == b"Team source"
    output_paths = list(
        (context._session_dir(target["id"]) / "sources").glob(f"*/{source.id}-{source_path.name}")
    )
    assert len(output_paths) == 1 and output_paths[0].is_file()


def test_url_hook_preserves_typed_denial_when_downloader_wraps_callback_error(library, monkeypatch):
    f, u = library, library.u
    target = project(u.alice, u.ah, u.workspace_id)
    actor = _actor(f.case, u.alice_id, workspaces=(u.workspace_id,))
    context = _workflow_context(f, target)
    outputs = _install_fake_ytdlp(monkeypatch)
    module = cast(Any, sys.modules["yt_dlp"])
    download_error = cast(Any, sys.modules["yt_dlp.utils"]).DownloadError

    class WrappingDownloader(module.YoutubeDL):
        def extract_info(self, *args, **kwargs):
            try:
                return super().extract_info(*args, **kwargs)
            except Exception as error:
                raise download_error("Downloader wrapped callback failure") from error

    monkeypatch.setattr(module, "YoutubeDL", WrappingDownloader)
    before = _row_counts(f)

    def revoke_during_download(value, _detail):
        if value >= 0.05:
            with f.case.database.immediate_session() as unit:
                unit.delete(unit.get_one(WorkspaceMembership, (u.workspace_id, u.alice_id)))

    with execution_actor_scope(actor), pytest.raises(SessionNotFound):
        download_source_url(
            context,
            {"session_id": target["id"], "url": "https://example.invalid/synthetic"},
            revoke_during_download,
            threading.Event(),
        )
    assert outputs == []
    assert _row_counts(f) == before


def test_publication_failure_rolls_back_artifact_record_asset_and_attachment(library, monkeypatch):
    f, u = library, library.u
    target = project(u.bob, u.bh)
    source, source_path = f.services.artifacts.resolve(f.private.artifact_id)
    context = _workflow_context(
        f,
        target,
        resolver=Mock(return_value=(source, source_path)),
    )
    before = _row_counts(f)

    def fail_attach(*_args, **_kwargs):
        raise RuntimeError("synthetic source attachment failure")

    monkeypatch.setattr(SourceLibraryService, "attach_in_session", fail_attach)
    with (
        execution_actor_scope(None),
        pytest.raises(
            RuntimeError,
            match="synthetic source attachment failure",
        ),
    ):
        reuse_source(
            context,
            {"session_id": target["id"], "artifact_id": source.id},
            Mock(),
            threading.Event(),
        )

    assert _row_counts(f) == before
    assert source_path.read_bytes() == b"Bob private source"


def test_member_can_download_and_reuse_after_creator_departure_with_provenance(
    library, monkeypatch
):
    f, u = library, library.u
    origin = project(u.alice, u.ah, u.workspace_id)
    target = project(u.alice, u.ah, u.workspace_id)
    original, _asset, original_path = _registered_source(
        f,
        origin,
        filename="creator-source.txt",
        data=b"Bytes retained after creator departure.",
    )
    with f.case.database.immediate_session() as unit:
        unit.delete(unit.get_one(WorkspaceMembership, (u.workspace_id, u.alice_id)))

    actor = _actor(f.case, u.bob_id, workspaces=(u.workspace_id,))
    context = _workflow_context(f, target)
    url_outputs = _install_fake_ytdlp(monkeypatch)
    progress: list[tuple[float, str | None]] = []

    with execution_actor_scope(actor):
        downloaded = download_source_url(
            context,
            {"session_id": target["id"], "url": "https://example.invalid/synthetic"},
            lambda value, detail=None: progress.append((value, detail)),
            threading.Event(),
        )
        reused = reuse_source(
            context,
            {"session_id": target["id"], "artifact_id": original.id},
            lambda value, detail=None: progress.append((value, detail)),
            threading.Event(),
        )
        reused_again = reuse_source(
            context,
            {"session_id": target["id"], "artifact_id": original.id},
            lambda value, detail=None: progress.append((value, detail)),
            threading.Event(),
        )

    url_output = url_outputs[0]
    reused_paths = list(
        (context._session_dir(target["id"]) / "sources").glob(
            f"*/{original.id}-{original_path.name}"
        )
    )
    assert len(reused_paths) == 2
    assert len({path.parent for path in reused_paths}) == 2
    assert all(
        path.read_bytes() == b"Bytes retained after creator departure." for path in reused_paths
    )
    assert url_output.parent not in {path.parent for path in reused_paths}
    assert url_output.parent.parent == context._session_dir(target["id"]) / "sources"
    assert all(
        path.parent.parent == context._session_dir(target["id"]) / "sources"
        for path in reused_paths
    )
    assert url_output.read_bytes() == b"URL source bytes."
    assert original_path.read_bytes() == b"Bytes retained after creator departure."
    assert progress[-1] == (1.0, "Reusable source ready")
    assert set(downloaded) == {"artifact_id", "filename"}
    assert downloaded["filename"] == "synthetic-url.mp4"
    assert set(reused) == {"artifact_id", "filename"}
    assert reused["filename"] == original_path.name
    assert set(reused_again) == {"artifact_id", "filename"}
    assert reused_again["filename"] == original_path.name

    with f.case.database.snapshot_session() as unit:
        downloaded_artifact = unit.get_one(m.Artifact, downloaded["artifact_id"])
        reused_artifact = unit.get_one(m.Artifact, reused["artifact_id"])
        reused_again_artifact = unit.get_one(m.Artifact, reused_again["artifact_id"])
        edge = unit.get(m.ArtifactEdge, (original.id, reused_artifact.id))
        edge_again = unit.get(m.ArtifactEdge, (original.id, reused_again_artifact.id))
        downloaded_record = unit.scalar(
            select(m.SourceRecord).where(m.SourceRecord.artifact_id == downloaded_artifact.id)
        )
        reused_record = unit.scalar(
            select(m.SourceRecord).where(m.SourceRecord.artifact_id == reused_artifact.id)
        )
        reused_asset = unit.scalar(
            select(m.SourceAsset).where(m.SourceAsset.artifact_id == reused_artifact.id)
        )
        assert reused_asset is not None
        reused_again_asset = unit.scalar(
            select(m.SourceAsset).where(m.SourceAsset.artifact_id == reused_again_artifact.id)
        )
        assert reused_again_asset is not None
        attachment = unit.scalar(
            select(m.SessionSource).where(
                m.SessionSource.session_id == target["id"],
                m.SessionSource.source_asset_id == reused_again_asset.id,
                m.SessionSource.is_current.is_(True),
            )
        )
        assert downloaded_artifact.metadata_json == {
            "original_filename": "synthetic-url.mp4",
            "source_url": "https://example.invalid/synthetic",
            "downloader": "yt-dlp",
        }
        assert downloaded_record is not None
        assert downloaded_record.metadata_json == {
            "url": "https://example.invalid/synthetic",
            "downloader": "yt-dlp",
        }
        assert reused_artifact.metadata_json == {
            "original_filename": original_path.name,
            "reused_from": original.id,
        }
        assert reused_again_artifact.relative_path != reused_artifact.relative_path
        assert reused_record is not None
        assert reused_record.metadata_json == {"reused_from": original.id}
        assert edge_again is not None and edge_again.relation == "derived_from"
        assert edge is not None and edge.relation == "derived_from"
        assert attachment is not None
