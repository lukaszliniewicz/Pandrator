"""Fresh authority, finite source reads and atomic text preparation."""

from __future__ import annotations

import builtins
import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text

from pandrator.web import models as m
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.access.session_policy import SessionNotFound
from pandrator.web.access.source_preparation_policy import require_text_preparation_access
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.accounts.execution_actor import (
    HostActor,
    UserActor,
    actor_from_principal,
    execution_actor_scope,
)
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import (
    ApiTokenProjectGrant,
    ApiTokenWorkspaceGrant,
)
from pandrator.web.job_execution import current_job_lease
from pandrator.web.models import new_id
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.workflow_source import (
    SourceWorkflowContext,
    _load_text_preparation_source,
    prepare_text,
)
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_source_library_workspace_http import library as library  # noqa: F401

FULL_SCOPES = frozenset({"app.read", "app.write", "app.run"})


def _project(client, csrf, workspace_id: str | None = None) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": f"Text preparation {uuid4()}",
        "workflow_kind": "audiobook",
        "source_language": "en",
    }
    if workspace_id is not None:
        values["workspace_id"] = workspace_id
    return body(
        client.post("/api/v1/sessions", json=values, headers=headers(csrf, str(uuid4()))),
        201,
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
        "Synthetic text preparation",
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


def _source(
    f: Any,
    record: dict[str, Any],
    *,
    content: str = "Prepared source text.",
    metadata: dict[str, Any] | None = None,
):
    path = f.services.paths.uploads / f"{uuid4()}-cleaned.txt"
    path.write_text(content, encoding="utf-8")
    with execution_actor_scope(HostActor("pandrator-cli")):
        artifact = f.services.artifacts.register(
            path,
            kind="text",
            role="clean_text",
            session_id=record["id"],
            metadata=metadata or {},
        )
    return artifact, path


def _document_revision(f: Any, session_id: str, content: str) -> str:
    with f.case.database.immediate_session() as unit:
        document = m.Document(session_id=session_id, stage="text_source", language="en")
        unit.add(document)
        unit.flush()
        revision = m.DocumentRevision(
            document_id=document.id,
            revision_number=1,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )
        unit.add(revision)
        unit.flush()
        return revision.id


def _attach(f: Any, session_id: str, artifact):
    with execution_actor_scope(HostActor("pandrator-cli")):
        asset = f.services.source_library.ensure_for_artifact(
            artifact.id,
            display_name=Path(artifact.relative_path).name,
            kind="text",
        )
        f.services.source_library.attach(session_id, asset.id)
    return asset


def _context(f: Any) -> SourceWorkflowContext:
    return cast(SourceWorkflowContext, f.services.workflow_handlers._source_workflow_context())


def _row_counts(f: Any) -> tuple[int, ...]:
    models = (m.Artifact, m.GenerationPlan, m.GenerationPlanRevision, m.GenerationSegment)
    with f.case.database.snapshot_session() as unit:
        return tuple(unit.scalar(select(func.count()).select_from(model)) or 0 for model in models)


def _install_preprocessor(monkeypatch, records: list[dict[str, Any]]) -> Mock:
    from pandrator.logic import text_preprocessor

    preprocess = Mock(return_value=records)
    monkeypatch.setattr(text_preprocessor, "preprocess_text", preprocess)
    return preprocess


def _detach_current_source(f: Any, session_id: str, asset_id: str) -> None:
    with f.case.database.immediate_session() as unit:
        attachment = unit.scalar(
            select(m.SessionSource).where(
                m.SessionSource.session_id == session_id,
                m.SessionSource.source_asset_id == asset_id,
                m.SessionSource.is_current.is_(True),
            )
        )
        assert attachment is not None
        attachment.is_current = False


@contextmanager
def _leased_actor(actor: UserActor, job: m.Job) -> Iterator[None]:
    token = current_job_lease.set(
        {
            "job_id": job.id,
            "owner_id": job.lease_owner or "text-preparation-worker",
            "lease_generation": job.lease_generation,
        }
    )
    try:
        with execution_actor_scope(actor):
            yield
    finally:
        current_job_lease.reset(token)


def test_trusted_none_has_no_policy_sql_or_snapshot_and_keeps_callback_identity(
    library, monkeypatch
):
    f, u = library, library.u
    target = _project(u.alice, u.ah)
    source, _source_path = _source(f, target, content="One prepared sentence.")
    context = _context(f)
    resolver = Mock(wraps=context._resolve_input)
    callback = Mock()
    scaled_callback = Mock()
    scale = Mock(return_value=scaled_callback)
    context = replace(context, _resolve_input=resolver, _scaled_progress_callback=scale)
    snapshots = Mock(wraps=f.case.database.snapshot_session)
    monkeypatch.setattr(f.case.database, "snapshot_session", snapshots)
    from pandrator.logic import text_preprocessor

    monkeypatch.setattr(
        text_preprocessor,
        "preprocess_text",
        Mock(return_value=[{"text": "One prepared sentence."}]),
    )

    unit = Mock()
    with execution_actor_scope(None):
        assert require_text_preparation_access(unit, "ignored-session", "ignored-source") is True
        result = prepare_text(
            context,
            {"session_id": target["id"], "source_artifact_id": source.id},
            callback,
            threading.Event(),
        )

    assert unit.mock_calls == []
    snapshots.assert_not_called()
    resolver.assert_called_once_with(source.id)
    scale.assert_called_once_with(callback, 0.1, 0.85)
    assert result["segments"] == 1
    assert set(result) == {
        "artifact_id",
        "path",
        "segments",
        "generation_plan_revision_id",
        "segmentation",
        "segmentation_budget",
    }


@pytest.mark.parametrize("denial", ["foreign_admin", "viewer", "no_run"])
def test_denied_preparation_stops_before_input_path_import_or_operation_directory(
    library, monkeypatch, denial
):
    f, u = library, library.u
    target = _project(u.bob, u.bh)
    source, source_path = _source(f, target)
    if denial == "viewer":
        with f.case.database.immediate_session() as unit:
            unit.add(
                ProjectMembership(
                    project_id=target["project_id"],
                    user_id=u.alice_id,
                    role="viewer",
                )
            )
        actor = _actor(f.case, u.alice_id, projects=(target["project_id"],))
    elif denial == "no_run":
        target = _project(u.alice, u.ah, u.workspace_id)
        source, source_path = _source(f, target)
        actor = _actor(
            f.case,
            u.alice_id,
            frozenset({"app.read", "app.write"}),
            workspaces=(u.workspace_id,),
        )
    else:
        owner_id = f.case.auth.initial_account().user_id
        actor = _actor(
            f.case,
            owner_id,
            FULL_SCOPES | {"app.admin"},
            workspaces=(f.homes[owner_id],),
        )

    context = _context(f)
    resolver = Mock(side_effect=AssertionError("denied preparation resolved its input"))
    operation_dir = Mock(side_effect=AssertionError("denied preparation created a directory"))
    session_record = Mock(side_effect=AssertionError("denied preparation read its session"))
    scale = Mock(side_effect=AssertionError("denied preparation created a callback"))
    context = replace(
        context,
        _resolve_input=resolver,
        _operation_dir=operation_dir,
        _session_record=session_record,
        _scaled_progress_callback=scale,
    )
    original_import = builtins.__import__
    native_imports: list[str] = []

    def deny_preparation_import(name, *args, **kwargs):
        if name in {
            "pandrator.logic.audiobook_chunking",
            "pandrator.logic.text_preprocessor",
        }:
            native_imports.append(name)
            raise AssertionError("Denied preparation reached text processing imports")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_preparation_import)
    read_text = Mock(side_effect=AssertionError("denied preparation read source bytes"))
    monkeypatch.setattr(Path, "read_text", read_text)

    with execution_actor_scope(actor), pytest.raises((AccessDenied, SessionNotFound)):
        prepare_text(
            context,
            {"session_id": target["id"], "source_artifact_id": source.id},
            Mock(),
            threading.Event(),
        )

    resolver.assert_not_called()
    read_text.assert_not_called()
    session_record.assert_not_called()
    scale.assert_not_called()
    operation_dir.assert_not_called()
    assert native_imports == []
    assert source_path.is_file()


def test_project_only_current_dependency_reads_bytes_without_source_metadata(library):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    origin = _project(u.bob, u.bh, u.workspace_id)
    source, source_path = _source(
        f,
        origin,
        content="Dependency transcript.",
        metadata={"speech_markup": {"1": "poison"}, "revision_id": "hidden"},
    )
    _attach(f, target["id"], source)
    with f.case.database.immediate_session() as unit:
        unit.execute(
            text("UPDATE artifacts SET metadata_json = :value WHERE id = :id"),
            {"value": "not valid JSON and must stay unread", "id": source.id},
        )
    actor = _actor(f.case, u.alice_id, projects=(target["project_id"],))
    context = _context(f)
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(f.case.database.engine, "before_cursor_execute", capture)
    try:
        with execution_actor_scope(actor):
            source_id, authorized_path, metadata = _load_text_preparation_source(
                context,
                target["id"],
                source.id,
            )
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", capture)

    assert source_id == source.id
    assert authorized_path == source_path
    assert authorized_path.read_text(encoding="utf-8") == "Dependency transcript."
    assert metadata == {}
    assert not any(
        "select" in statement and "artifacts.metadata_json" in statement for statement in statements
    )


def test_host_input_proof_still_requires_target_workspace_and_current_state(library):
    f, u = library, library.u
    target = _project(u.alice, u.ah)
    source, _source_path = _source(f, target)
    foreign_session = _project(u.bob, u.bh)
    foreign_source, _foreign_path = _source(f, foreign_session)

    with execution_actor_scope(HostActor("pandrator-cli")):
        with f.case.database.snapshot_session() as unit:
            assert require_text_preparation_access(unit, target["id"], source.id) is True
            with pytest.raises(KeyError) as foreign:
                require_text_preparation_access(unit, target["id"], foreign_source.id)
        assert foreign.value.args == ("Source artifact not found.",)

        with f.case.database.immediate_session() as unit:
            unit.get_one(m.Artifact, source.id).state = "deleted"
        with f.case.database.snapshot_session() as unit, pytest.raises(KeyError) as deleted:
            require_text_preparation_access(unit, target["id"], source.id)
    assert deleted.value.args == ("Source artifact not found.",)


def test_ordinary_source_markup_is_retained_and_uses_current_generation_controls(
    library, monkeypatch
):
    from pandrator.logic import speech_markup
    from pandrator.web.generation_controls import save_generation_controls

    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    xml = '<segment id="1"><dialogue><speaker ref="c-speaker">Hello.</speaker></dialogue></segment>'
    source_revision_id = _document_revision(f, target["id"], "Hello.")
    source, _source_path = _source(
        f,
        target,
        content="Hello.",
        metadata={
            "revision_id": source_revision_id,
            "speech_markup": {"1": xml},
        },
    )
    with execution_actor_scope(HostActor("pandrator-cli")):
        with f.case.database.session() as unit:
            save_generation_controls(
                unit,
                target["id"],
                expected_revision=0,
                characters=[
                    {
                        "id": "c-speaker",
                        "display_name": "Speaker",
                        "voice_category": "female",
                    }
                ],
            )

    actor = _actor(f.case, u.alice_id, projects=(target["project_id"],))
    context = _context(f)
    store = Mock(wraps=context._store_generation_plan)
    context = replace(context, _store_generation_plan=store)
    parse = Mock(wraps=speech_markup.parse_speech_markup)
    monkeypatch.setattr(speech_markup, "parse_speech_markup", parse)

    with execution_actor_scope(actor):
        result = prepare_text(
            context,
            {"session_id": target["id"], "source_artifact_id": source.id},
            Mock(),
            threading.Event(),
        )

    assert result["segments"] == 1
    assert result["segmentation"]["policy_applied"] is False
    prepared = json.loads(f.services.paths.managed_path(result["path"]).read_text("utf-8"))
    assert prepared[0]["original_sentence"] == "Hello."
    assert '<speaker ref="c-speaker">' in prepared[0]["speech_plan"]["speech_xml"]
    assert any(
        call.kwargs.get("characters", [{}])[0].get("id") == "c-speaker"
        for call in parse.call_args_list
    )
    assert store.call_args.kwargs["source_revision_id"] == source_revision_id
    assert store.call_args.kwargs["db_session"] is not None


@pytest.mark.parametrize("binding", ["wrong_kind", "wrong_target", "forged_lease"])
def test_preparation_lease_must_bind_real_job_kind_and_target(library, binding):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    other_target = _project(u.alice, u.ah, u.workspace_id)
    source, _source_path = _source(f, target)
    actor = _actor(f.case, u.alice_id, workspaces=(u.workspace_id,))
    queued_kind = "source.reuse" if binding == "wrong_kind" else "text.prepare"
    with execution_actor_scope(actor):
        queued = f.services.jobs.enqueue(
            queued_kind,
            {"synthetic": True},
            session_id=target["id"],
        )
        claimed = f.services.jobs.claim("text-preparation-worker", lease_seconds=900)
    assert claimed is not None and claimed.id == queued.id

    with _leased_actor(actor, claimed):
        if binding == "forged_lease":
            forged = current_job_lease.set(
                {
                    "job_id": new_id(),
                    "owner_id": "text-preparation-worker",
                    "lease_generation": 1,
                }
            )
            try:
                with f.case.database.snapshot_session() as unit, pytest.raises(AccessDenied):
                    require_text_preparation_access(unit, target["id"], source.id)
            finally:
                current_job_lease.reset(forged)
        elif binding == "wrong_kind":
            with f.case.database.snapshot_session() as unit, pytest.raises(AccessDenied):
                require_text_preparation_access(unit, target["id"], source.id)
        else:
            with f.case.database.snapshot_session() as unit, pytest.raises(AccessDenied):
                require_text_preparation_access(unit, other_target["id"], source.id)


def test_progress_callback_revocation_stops_preprocessor_and_publication(library, monkeypatch):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    origin = _project(u.bob, u.bh, u.workspace_id)
    source, source_path = _source(f, origin, content="Dependency narration.")
    asset = _attach(f, target["id"], source)
    actor = _actor(f.case, u.alice_id, projects=(target["project_id"],))
    context = _context(f)
    resolver = Mock(side_effect=AssertionError("present actor used the legacy resolver"))
    operation_dir = Mock(side_effect=AssertionError("revoked preparation created output"))
    store = Mock(wraps=context._store_generation_plan)
    context = replace(
        context,
        _resolve_input=resolver,
        _operation_dir=operation_dir,
        _store_generation_plan=store,
    )
    preprocess = _install_preprocessor(monkeypatch, [{"text": "Dependency narration."}])
    before = _row_counts(f)

    def detach_after_progress(_value, _detail):
        _detach_current_source(f, target["id"], asset.id)

    with execution_actor_scope(actor), pytest.raises(KeyError) as denied:
        prepare_text(
            context,
            {"session_id": target["id"], "source_artifact_id": source.id},
            detach_after_progress,
            threading.Event(),
        )

    assert denied.value.args == ("Source artifact not found.",)
    resolver.assert_not_called()
    operation_dir.assert_not_called()
    preprocess.assert_not_called()
    store.assert_not_called()
    assert _row_counts(f) == before
    assert source_path.read_text(encoding="utf-8") == "Dependency narration."


def test_metadata_grant_narrowing_at_progress_denies_markup_publication(library, monkeypatch):
    from pandrator.logic import speech_markup

    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    origin = _project(u.bob, u.bh, u.workspace_id)
    source_revision_id = _document_revision(f, origin["id"], "Annotated dependency.")
    source, source_path = _source(
        f,
        origin,
        content="Annotated dependency.",
        metadata={
            "revision_id": source_revision_id,
            "speech_markup": {"1": "must not be parsed after metadata grant loss"},
        },
    )
    _attach(f, target["id"], source)
    actor = _actor(f.case, u.alice_id, workspaces=(u.workspace_id,))
    context = _context(f)
    operation_dir = Mock(side_effect=AssertionError("denied metadata created output"))
    context = replace(context, _operation_dir=operation_dir)
    parse = Mock(side_effect=AssertionError("denied metadata reached markup parsing"))
    monkeypatch.setattr(speech_markup, "parse_speech_markup", parse)
    before = _row_counts(f)

    def narrow_workspace_grant(_value, _detail):
        with f.case.database.immediate_session() as unit:
            unit.delete(
                unit.get_one(
                    ApiTokenWorkspaceGrant,
                    (actor.token_id, u.workspace_id),
                )
            )
            unit.add(
                ApiTokenProjectGrant(
                    token_id=actor.token_id,
                    project_id=target["project_id"],
                )
            )
        with f.case.database.snapshot_session() as unit:
            assert (
                require_text_preparation_access(
                    unit,
                    target["id"],
                    source.id,
                )
                is False
            )
            with pytest.raises(KeyError) as metadata_denied:
                require_text_preparation_access(
                    unit,
                    target["id"],
                    source.id,
                    require_metadata=True,
                )
        assert metadata_denied.value.args == ("Source artifact not found.",)

    with execution_actor_scope(actor), pytest.raises(KeyError) as denied:
        prepare_text(
            context,
            {"session_id": target["id"], "source_artifact_id": source.id},
            narrow_workspace_grant,
            threading.Event(),
        )

    assert denied.value.args == ("Source artifact not found.",)
    operation_dir.assert_not_called()
    parse.assert_not_called()
    assert _row_counts(f) == before
    assert source_path.read_text(encoding="utf-8") == "Annotated dependency."


def test_input_revocation_during_hash_prevents_artifact_and_plan_publication(library, monkeypatch):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    origin = _project(u.bob, u.bh, u.workspace_id)
    source, source_path = _source(f, origin, content="Hash-bound dependency.")
    asset = _attach(f, target["id"], source)
    actor = _actor(f.case, u.alice_id, projects=(target["project_id"],))
    context = _context(f)
    store = Mock(wraps=context._store_generation_plan)
    context = replace(context, _store_generation_plan=store)
    _install_preprocessor(monkeypatch, [{"text": "Prepared dependency."}])
    before = _row_counts(f)
    original_prepare = context.artifacts.prepare_registration

    def hash_then_detach(path, **kwargs):
        prepared = original_prepare(path, **kwargs)
        _detach_current_source(f, target["id"], asset.id)
        return prepared

    monkeypatch.setattr(context.artifacts, "prepare_registration", hash_then_detach)

    with execution_actor_scope(actor), pytest.raises(KeyError) as denied:
        prepare_text(
            context,
            {"session_id": target["id"], "source_artifact_id": source.id},
            Mock(),
            threading.Event(),
        )

    assert denied.value.args == ("Source artifact not found.",)
    assert _row_counts(f) == before
    store.assert_not_called()
    assert source_path.read_text(encoding="utf-8") == "Hash-bound dependency."
    with f.case.database.snapshot_session() as unit:
        record = unit.get_one(m.SessionRecord, target["id"])
        session_dir = f.services.paths.sessions / record.storage_key
    staged = list(session_dir.glob("stage-runs/prepare-text-*/prepared_narration.json"))
    assert len(staged) == 1 and staged[0].is_file()


def test_generation_plan_failure_rolls_back_artifact_and_plan_rows(library, monkeypatch):
    f, u = library, library.u
    target = _project(u.alice, u.ah)
    source, source_path = _source(f, target, content="Rollback narration.")
    context = _context(f)
    store = context._store_generation_plan
    _install_preprocessor(monkeypatch, [{"text": "Replacement narration."}])
    before = _row_counts(f)

    def store_then_fail(
        session_id,
        records,
        *,
        settings,
        source_revision_id=None,
        source_artifact_id=None,
        db_session=None,
    ):
        assert db_session is not None
        store(
            session_id,
            records,
            settings=settings,
            source_revision_id=source_revision_id,
            source_artifact_id=source_artifact_id,
            db_session=db_session,
        )
        raise RuntimeError("synthetic generation plan write failure")

    context = replace(context, _store_generation_plan=store_then_fail)
    with (
        execution_actor_scope(None),
        pytest.raises(
            RuntimeError,
            match="synthetic generation plan write failure",
        ),
    ):
        prepare_text(
            context,
            {"session_id": target["id"], "source_artifact_id": source.id},
            Mock(),
            threading.Event(),
        )

    assert _row_counts(f) == before
    assert source_path.read_text(encoding="utf-8") == "Rollback narration."
    with f.case.database.snapshot_session() as unit:
        record = unit.get_one(m.SessionRecord, target["id"])
        session_dir = f.services.paths.sessions / record.storage_key
    staged = list(session_dir.glob("stage-runs/prepare-text-*/prepared_narration.json"))
    assert len(staged) == 1 and staged[0].is_file()


def test_workspace_member_prepares_after_creator_departure_with_source_provenance(
    library, monkeypatch
):
    f, u = library, library.u
    origin = _project(u.alice, u.ah, u.workspace_id)
    target = _project(u.alice, u.ah, u.workspace_id)
    source_revision_id = _document_revision(
        f,
        origin["id"],
        "Creator-owned source bytes.",
    )
    source, source_path = _source(
        f,
        origin,
        content="Creator-owned source bytes.",
        metadata={"revision_id": source_revision_id},
    )
    _attach(f, target["id"], source)
    with f.case.database.immediate_session() as unit:
        unit.delete(unit.get_one(WorkspaceMembership, (u.workspace_id, u.alice_id)))

    actor = _actor(f.case, u.bob_id, workspaces=(u.workspace_id,))
    context = _context(f)
    records = [{"text": "Prepared narration."}]
    _install_preprocessor(monkeypatch, records)
    progress: list[tuple[float, str | None]] = []

    with execution_actor_scope(actor):
        result = prepare_text(
            context,
            {
                "session_id": target["id"],
                "source_artifact_id": source.id,
                "settings": {"test_setting": "preserved"},
            },
            lambda value, detail=None: progress.append((value, detail)),
            threading.Event(),
        )

    assert set(result) == {
        "artifact_id",
        "path",
        "segments",
        "generation_plan_revision_id",
        "segmentation",
        "segmentation_budget",
    }
    assert result["segments"] == 1
    assert result["path"]
    assert progress[-1] == (1.0, "Narration segments ready")
    output_path = f.services.paths.managed_path(result["path"])
    output = json.loads(output_path.read_text(encoding="utf-8"))
    assert output == records
    assert source_path.read_text(encoding="utf-8") == "Creator-owned source bytes."

    with f.case.database.snapshot_session() as unit:
        artifact = unit.get_one(m.Artifact, result["artifact_id"])
        edge = unit.get(m.ArtifactEdge, (source.id, artifact.id))
        plan = unit.scalar(
            select(m.GenerationPlan).where(m.GenerationPlan.session_id == target["id"])
        )
        revision = unit.get_one(m.GenerationPlanRevision, result["generation_plan_revision_id"])
        assert plan is not None
        assert plan.active_revision_id == revision.id
        assert revision.source_revision_id == source_revision_id
        assert artifact.role == "prepared_text" and artifact.kind == "json"
        assert artifact.session_id == target["id"]
        assert artifact.relative_path == result["path"]
        assert revision.settings_json["test_setting"] == "preserved"
        assert "_audiobook_chunk_budget" in revision.settings_json
        assert artifact.settings_hash
        assert artifact.metadata_json["segment_count"] == 1
        assert artifact.metadata_json["segmentation"] == result["segmentation"]
        assert edge is not None
