"""Queued delivery contexts preserve input privacy and committed output paths."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import delete, event, select, update

from pandrator.runtime import DataPaths
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.access.delivery_execution_policy import capture_delivery_execution
from pandrator.web.accounts.execution_actor import execution_actor_scope
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.artifacts import ArtifactService
from pandrator.web.delivery_runtime import DeliveryRuntime
from pandrator.web.models import Artifact, SessionRecord
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.sound_mix_claims import _validate_claim
from pandrator.web.sound_mix_snapshot import PreparedSoundMix
from pandrator.web.workflow_output_context import OutputWorkflowContext, publish_output
from tests.test_delivery_execution_policy import _job
from tests.test_generation_execution_policy import _lease, _lease_scope, credential
from tests.test_generation_execution_policy import case as case
from tests.test_generation_execution_policy import no_network as no_network
from tests.test_generation_execution_policy import template as template


@pytest.fixture
def runtime_case(case, tmp_path):
    paths = DataPaths(tmp_path / "files")
    paths.uploads.mkdir(parents=True)
    artifacts = ArtifactService(case.database, paths)
    source = paths.uploads / "own-source.wav"
    source.write_bytes(b"Synthetic source bytes")
    artifact = artifacts.register(source, kind="audio", session_id=case.a.id)
    actor = credential(case, "member").actor
    job, payload = _job(case, actor)

    def session_record(sid):
        with case.database.snapshot_session() as session:
            value = session.get_one(SessionRecord, sid)
            session.expunge(value)
        return value

    context = OutputWorkflowContext(
        case.database,
        paths,
        artifacts,
        artifacts.resolve,
        lambda _sid: paths.sessions / case.a.storage_key,
        session_record,
        lambda _sid, _kind: paths.temporary,
    )
    return SimpleNamespace(
        f=case,
        paths=paths,
        artifacts=artifacts,
        artifact=artifact,
        actor=actor,
        job=job,
        payload=payload,
        context=context,
    )


@contextmanager
def scoped(f):
    with execution_actor_scope(f.actor), _lease_scope(_lease(f.job)):
        with f.f.database.snapshot_session() as session:
            proof = capture_delivery_execution(session, f.payload, entry="export")
        with DeliveryRuntime(f.context, proof, threading.Event()) as context:
            yield context


def render(context, f):
    artifact, path = context._resolve_input(f.artifact.id)
    output = context._session_dir(f.f.a.id) / "exports" / "audio" / "result.wav"
    output.parent.mkdir(parents=True)
    output.write_bytes(path.read_bytes())
    return artifact, output


def test_member_publication_survives_private_staging_cleanup(runtime_case):
    f = runtime_case
    with scoped(f) as context:
        source, rendered = render(context, f)
        private = rendered.parents[2]
        assert private.name.startswith(".delivery-")
        exported = publish_output(
            context,
            rendered,
            kind="export",
            role="export",
            session_id=f.f.a.id,
            parent_ids=[source.id],
        )
        final = f.paths.managed_path(exported.relative_path)
        assert ".delivery-" not in exported.relative_path
        assert final.read_bytes() == b"Synthetic source bytes"
    assert not private.exists() and final.is_file()
    with f.f.database.snapshot_session() as session:
        stored = session.get_one(Artifact, exported.id)
        assert (stored.workspace_id, stored.project_id, stored.session_id) == (
            f.f.team,
            f.f.a.project_id,
            f.f.a.id,
        )


def test_foreign_input_denies_before_full_artifact_body(runtime_case):
    f = runtime_case
    foreign = f.f.services.sessions.create(
        "Private source", workspace_id=f.f.personal[f.f.users["outsider"]]
    )
    path = f.paths.uploads / "foreign.wav"
    path.write_bytes(b"Private other-account content")
    private = f.artifacts.register(path, kind="audio", session_id=foreign.id)
    statements = []
    with scoped(f) as context:

        def capture(_c, _cu, sql, _p, _co, _e):
            statements.append(sql)

        event.listen(f.f.database.engine, "before_cursor_execute", capture)
        try:
            with pytest.raises(AccessDenied):
                context._resolve_input(private.id)
        finally:
            event.remove(f.f.database.engine, "before_cursor_execute", capture)
    assert not any("metadata_json" in sql or "artifacts.relative_path" in sql for sql in statements)
    assert path.read_bytes() == b"Private other-account content"


@pytest.mark.parametrize("change", ["membership", "content"])
def test_late_input_or_role_loss_keeps_originals_and_publishes_nothing(runtime_case, change):
    f = runtime_case
    with scoped(f) as context:
        source, rendered = render(context, f)
        private = rendered.parents[2]
        with f.f.database.immediate_session() as session:
            if change == "membership":
                session.execute(
                    delete(WorkspaceMembership).where(
                        WorkspaceMembership.workspace_id == f.f.team,
                        WorkspaceMembership.user_id == f.actor.user_id,
                    )
                )
            else:
                session.execute(
                    update(Artifact).where(Artifact.id == source.id).values(content_hash="b" * 64)
                )
        with pytest.raises(AccessDenied if change == "membership" else RevisionConflict):
            publish_output(
                context,
                rendered,
                kind="export",
                role="export",
                session_id=f.f.a.id,
                parent_ids=[source.id],
            )
    assert not private.exists()
    assert f.paths.managed_path(f.artifact.relative_path).read_bytes() == b"Synthetic source bytes"
    with f.f.database.snapshot_session() as session:
        assert not session.scalars(select(Artifact.id).where(Artifact.role == "export")).all()


def test_runtime_io_rechecks_headers_without_delivery_body_reads(runtime_case):
    f = runtime_case
    statements = []
    with scoped(f) as context:
        context._resolve_input(f.artifact.id)

        def capture(_c, _cu, sql, _p, _co, _e):
            statements.append(sql)

        event.listen(f.f.database.engine, "before_cursor_execute", capture)
        try:
            assert context._before_io is not None
            for _ in range(3):
                context._before_io()
        finally:
            event.remove(f.f.database.engine, "before_cursor_execute", capture)
    assert statements and not any("payload_json" in sql for sql in statements)


def test_inline_sound_claim_reads_headers_without_large_delivery_payload(runtime_case):
    f = runtime_case
    statements = []
    prepared = cast(PreparedSoundMix, SimpleNamespace(session_id=f.f.a.id))

    def capture(_c, _cu, sql, _p, _co, _e):
        statements.append(sql)

    with scoped(f) as context:
        event.listen(f.f.database.engine, "before_cursor_execute", capture)
        try:
            with f.f.database.snapshot_session() as session:
                claimed = _validate_claim(
                    session,
                    prepared,
                    threading.Event(),
                    authorization_guard=context._authorization_guard,
                )
        finally:
            event.remove(f.f.database.engine, "before_cursor_execute", capture)
    assert claimed == f.job.id
    assert statements and not any("payload_json" in sql for sql in statements)
