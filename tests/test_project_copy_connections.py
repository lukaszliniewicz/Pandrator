"""Destination-approved LLM copy bindings through real admission and worker paths."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import event, func, select

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import actor_from_principal, execution_actor_scope
from pandrator.web.jobs import Worker
from pandrator.web.projects import copy_connections as connections
from pandrator.web.projects import copy_jobs as domain
from pandrator.web.projects.copy_contracts import ProjectCopyError
from pandrator.web.provider_settings import build_llm_settings
from tests.test_project_copy_jobs import _none_published
from tests.test_project_copy_jobs import copy_case as copy_case  # noqa: F401
from tests.test_project_copy_jobs import no_network as no_network  # noqa: F401
from tests.test_project_copy_jobs import unbound_job_case as _unbound_job_case  # noqa: F401
from tests.test_project_copy_snapshot import bundle_case as bundle_case  # noqa: F401

job_case = _unbound_job_case


def provider(
    case: Any,
    workspace: str,
    *,
    key="openai",
    options=None,
    name="Approved model",
    model="team-model",
) -> tuple[str, str]:
    with case.database.immediate_session() as session:
        item = m.Provider(
            workspace_id=workspace,
            kind="llm",
            provider_key=key,
            label=f"{name} {model}",
            enabled=True,
            secret_ref="db:copy-model-key",
            base_url="https://models.invalid/v1",
            options_json=options or {},
        )
        session.add(item)
        session.flush()
        row = m.ProviderModel(
            provider_id=item.id,
            model_id=model,
            is_active=True,
            is_default=True,
            input_modalities_json=["text"],
            output_modalities_json=["text"],
        )
        session.add(row)
        existing = session.scalar(
            select(m.StoredCredential.key).where(
                m.StoredCredential.workspace_id == workspace,
                m.StoredCredential.key == "copy-model-key",
            )
        )
        if existing is None:
            session.add(
                m.StoredCredential(
                    workspace_id=workspace,
                    key="copy-model-key",
                    label="copy-model-key",
                    secret_value="synthetic-destination-key",
                )
            )
        session.flush()
        return item.id, row.id


def choose(
    case: Any, *, destination=None, model_ids=None
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    destination = destination or case.destination
    initial = case.copies.preview(case.owner_context, case.source.project_id, destination)
    assert initial["connection_requirements"] and initial["connection_options"]
    option = next(
        (
            item
            for item in initial["connection_options"]
            if model_ids is None or (item["provider_id"], item["provider_model_id"]) == model_ids
        )
    )
    choices = [
        {
            "requirement_id": item["id"],
            "provider_id": option["provider_id"],
            "provider_model_id": option["provider_model_id"],
        }
        for item in initial["connection_requirements"]
    ]
    return case.copies.preview(
        case.owner_context, case.source.project_id, destination, destination_connections=choices
    ), choices


def request(
    case: Any, preview: dict[str, Any], choices, *, destination=None, key="destination-connections"
):
    return case.copies.request(
        case.principal,
        case.source.project_id,
        destination or case.destination,
        expected_fingerprint=preview["fingerprint"],
        destination_connections=choices,
        idempotency_key=key,
    )


def run(case: Any):
    # Keep durable dispatch, with a single disposable attempt for terminal assertions.
    with case.database.immediate_session() as session:
        for job in session.scalars(select(m.Job).where(m.Job.status == "queued")):
            job.max_attempts = 1
    handlers = case.app.extensions["pandrator"]["workflow_handlers"].handlers()
    worker = Worker(case.services.jobs, "destination-model-copy", handlers)
    assert worker.run_once()


def model_settings(case: Any, value: str) -> None:
    with case.database.immediate_session() as session:
        for record in session.scalars(
            select(m.SessionRecord).where(m.SessionRecord.project_id == case.source.project_id)
        ):
            for section in ("correction", "translation"):
                current = session.get(m.SessionSetting, (record.id, section))
                if current is None:
                    session.add(
                        m.SessionSetting(
                            session_id=record.id,
                            section=section,
                            value_json={
                                "model_name": value,
                                "instructions": "Keep saved editorial parameters.",
                            },
                            revision=1,
                        )
                    )
                else:
                    current.value_json = {**current.value_json, "model_name": value}
                    current.revision += 1


def test_registered_copy_uses_destination_model_after_source_provider_departure(job_case):
    case = job_case
    source_ids = provider(
        case, case.source_workspace, options={"is_custom": True}, model="private-source"
    )
    destination_ids = provider(case, case.destination)
    model_settings(case, f"custom:{source_ids[0]}/private-source")
    preview, choices = choose(case, model_ids=destination_ids)
    assert preview["ready"]
    assert all(item["session_names"] for item in preview["connection_requirements"])
    with case.database.snapshot_session() as session:
        before = tuple(
            session.scalar(select(func.count()).select_from(table))
            for table in (m.Provider, m.ProviderModel, m.StoredCredential)
        )
    job, status = request(case, preview, choices)
    assert status == 202
    replay, _status = request(case, preview, list(reversed(choices)))
    assert replay["id"] == job["id"]
    run(case)
    result = case.copies.get(case.owner_context, job["id"])
    assert result["state"] == "succeeded", result
    copied = result["result"]["source_session_id"]
    with case.database.snapshot_session() as session:
        counts = tuple(
            session.scalar(select(func.count()).select_from(table))
            for table in (m.Provider, m.ProviderModel, m.StoredCredential)
        )
        assert counts == before
        settings = session.get_one(m.SessionSetting, (copied, "translation")).value_json
        assert settings["model_name"] == f"custom:{destination_ids[0]}/team-model"
        stored = session.get_one(m.Job, job["id"])
        frozen = domain._decode_snapshot(
            stored.payload_json["snapshot_zlib_b64"], stored.payload_json["snapshot_sha256"]
        )
        assert source_ids[0] not in frozen.state_json and "private-source" not in frozen.state_json
        assert frozen.destination_connection_options_json == "[]"
    with case.database.immediate_session() as session:
        session.delete(session.get_one(m.Provider, source_ids[0]))
        session.get_one(m.User, case.principal.user_id).is_active = False
    with execution_actor_scope(actor_from_principal(case.member)):
        hydrated, selected = build_llm_settings(
            case.database,
            case.services.paths,
            session_id=copied,
            requested_model=settings["model_name"],
        )
    assert selected == settings["model_name"] and hydrated.provider_binding_id == destination_ids[0]
    assert [item["id"] for item in hydrated.provider_configs] == [destination_ids[0]]


@pytest.mark.parametrize("custom", [False, True])
def test_initial_home_selector_matches_real_legacy_builder(job_case, custom):
    case = job_case
    ids = provider(
        case,
        case.source_workspace,
        key="openai",
        options={
            "is_custom": custom,
            **({"provider_id": "approved-home-custom"} if custom else {}),
        },
    )
    preview, choices = choose(case, destination=case.source_workspace, model_ids=ids)
    job, _status = request(case, preview, choices, destination=case.source_workspace)
    run(case)
    result = case.copies.get(case.owner_context, job["id"])
    assert result["state"] == "succeeded", result
    copied = result["result"]["source_session_id"]
    with case.database.snapshot_session() as session:
        selector = session.get_one(m.SessionSetting, (copied, "correction")).value_json[
            "model_name"
        ]
    expected = "custom:approved-home-custom/team-model" if custom else "openai/team-model"
    assert selector == expected
    hydrated, selected = build_llm_settings(
        case.database, case.services.paths, session_id=copied, requested_model=selector
    )
    assert selected == selector
    assert any(
        item["id"] == ("approved-home-custom" if custom else "openai")
        for item in hydrated.provider_configs
    )


@pytest.mark.parametrize(
    "mutation", ["disabled", "provider_revision", "model_revision", "inactive"]
)
def test_destination_revocation_fails_before_staging_or_inserts(job_case, monkeypatch, mutation):
    case = job_case
    ids = provider(case, case.destination)
    preview, choices = choose(case, model_ids=ids)
    job, _status = request(case, preview, choices)
    with case.database.immediate_session() as session:
        p, model = session.get_one(m.Provider, ids[0]), session.get_one(m.ProviderModel, ids[1])
        if mutation == "disabled":
            p.enabled = False
        elif mutation == "provider_revision":
            p.revision += 1
        elif mutation == "model_revision":
            model.revision += 1
        elif mutation == "inactive":
            model.is_active, model.is_default = False, False
    monkeypatch.setattr(
        domain,
        "stage_project_files",
        lambda *_a, **_k: pytest.fail("revoked destination reached files"),
    )
    run(case)
    result = case.copies.get(case.owner_context, job["id"])
    assert (
        result["state"] == "failed" and result["error"]["code"] == "copy_connection_unavailable"
    ), result
    _none_published(case, job["id"])


def test_final_writer_rechecks_after_staging_and_rolls_back(job_case, monkeypatch):
    case = job_case
    ids = provider(case, case.destination)
    preview, choices = choose(case, model_ids=ids)
    job, _status = request(case, preview, choices)
    original = domain.stage_project_files

    def stage(*args, **kwargs):
        staged = original(*args, **kwargs)
        with case.database.immediate_session() as session:
            session.get_one(m.ProviderModel, ids[1]).revision += 1
        return staged

    monkeypatch.setattr(domain, "stage_project_files", stage)
    run(case)
    result = case.copies.get(case.owner_context, job["id"])
    assert (
        result["state"] == "failed" and result["error"]["code"] == "copy_connection_unavailable"
    ), result
    _none_published(case, job["id"])


def test_preview_is_no_secret_no_network_and_transient_options_are_not_enveloped(job_case):
    case = job_case
    ids = provider(case, case.destination)
    statements = []

    def query(_conn, _cursor, sql, params, _ctx, _many):
        statements.append((sql, params))

    event.listen(case.database.engine, "before_cursor_execute", query)
    try:
        with patch(
            "pandrator.web.provider_settings.resolve_provider_credential",
            side_effect=AssertionError("credential resolution"),
        ):
            preview, choices = choose(case, model_ids=ids)
            job, _status = request(case, preview, choices)
    finally:
        event.remove(case.database.engine, "before_cursor_execute", query)
    provider_reads = [
        sql for sql, _params in statements if "FROM providers" in sql or "JOIN providers" in sql
    ]
    assert provider_reads and all(
        "secret_ref" not in sql and "providers.options_json" not in sql for sql in provider_reads
    )
    with case.database.snapshot_session() as session:
        record = session.get_one(m.Job, job["id"])
        assert (
            "connection_options" not in record.payload_json
            and "destination_connections" not in record.payload_json
        )
        snap = domain._decode_snapshot(
            record.payload_json["snapshot_zlib_b64"], record.payload_json["snapshot_sha256"]
        )
        assert snap.destination_connection_options_json == "[]"
        assert json.loads(snap.state_json)["destination_connections"]


def test_no_requirement_control_has_no_new_catalogue_or_namespace_queries(job_case, monkeypatch):
    case = job_case
    with case.database.immediate_session() as session:
        for record in session.scalars(
            select(m.SessionRecord).where(m.SessionRecord.project_id == case.source.project_id)
        ):
            record.included_stages_json = []
        for outcome in session.scalars(select(m.OutcomePlan)):
            value = deepcopy(outcome.value_json)
            value["transformations"] = {key: False for key in value.get("transformations", {})}
            outcome.value_json = value
    monkeypatch.setattr(
        connections, "destination_options", lambda *_a, **_k: pytest.fail("unneeded model metadata")
    )
    preview = case.copies.preview(case.owner_context, case.source.project_id, case.destination)
    assert (
        preview["ready"]
        and preview["connection_requirements"] == preview["connection_options"] == []
    )
    job, _status = request(case, preview, [])
    replay, _status = request(case, preview, None)
    assert job["id"] == replay["id"]
    run(case)
    assert case.copies.get(case.owner_context, job["id"])["state"] == "succeeded"


def test_unbound_private_selectors_unknown_connections_and_unused_choices_remain_blocked(job_case):
    case = job_case
    ids = provider(case, case.destination)
    with case.database.immediate_session() as session:
        section = session.get(m.SessionSetting, (case.source.id, "correction"))
        session.merge(
            m.SessionSetting(
                session_id=case.source.id,
                section="correction",
                revision=1,
                value_json={
                    **(section.value_json if section else {}),
                    "stt_api_base": "https://private.invalid",
                    "web_research_model_name": "custom:source/research",
                    "other_model": "custom:source/unknown",
                },
            )
        )
    preview, choices = choose(case, model_ids=ids)
    assert not preview["ready"]
    with pytest.raises(ProjectCopyError) as error:
        request(case, preview, choices)
    assert error.value.code == "copy_not_ready"
    with pytest.raises(ProjectCopyError):
        case.copies.preview(
            case.owner_context,
            case.source.project_id,
            case.destination,
            destination_connections=[
                {"requirement_id": "f" * 64, "provider_id": ids[0], "provider_model_id": ids[1]}
            ],
        )


def test_foreign_destination_ids_and_changed_choices_are_not_reused(job_case):
    from pandrator.web.idempotency import IdempotencyConflict

    case = job_case
    first = provider(case, case.destination, model="first-model")
    second = provider(case, case.destination, model="second-model")
    foreign = provider(case, case.source_workspace, model="source-only-model")
    preview, choices = choose(case, model_ids=first)
    foreign_choices = [
        {**item, "provider_id": foreign[0], "provider_model_id": foreign[1]} for item in choices
    ]
    with pytest.raises(ProjectCopyError) as error:
        case.copies.preview(
            case.owner_context,
            case.source.project_id,
            case.destination,
            destination_connections=foreign_choices,
        )
    assert error.value.code == "copy_connection_unavailable"
    changed, changed_choices = choose(case, model_ids=second)
    assert changed["fingerprint"] != preview["fingerprint"]
    request(case, preview, choices)
    with pytest.raises(IdempotencyConflict):
        request(case, changed, changed_choices)


def test_revision_change_before_recapture_uses_existing_preview_stale(job_case):
    case = job_case
    ids = provider(case, case.destination)
    preview, choices = choose(case, model_ids=ids)
    with case.database.immediate_session() as session:
        session.get_one(m.ProviderModel, ids[1]).revision += 1
    with pytest.raises(ProjectCopyError) as error:
        request(case, preview, choices)
    assert error.value.code == "copy_preview_stale"
    with case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(m.Job)) == 0


def test_revision_change_after_capture_fails_before_enqueue(job_case, monkeypatch):
    case = job_case
    ids = provider(case, case.destination)
    preview, choices = choose(case, model_ids=ids)
    original = domain._encode_snapshot

    def encode(snapshot):
        result = original(snapshot)
        with case.database.immediate_session() as session:
            session.get_one(m.Provider, ids[0]).revision += 1
        return result

    monkeypatch.setattr(domain, "_encode_snapshot", encode)
    with pytest.raises(ProjectCopyError) as error:
        request(case, preview, choices)
    assert error.value.code == "copy_connection_unavailable"
    with case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(m.Job)) == 0


def test_initial_catalogue_excludes_ambiguous_or_unrepresentable_aliases(job_case):
    case = job_case
    allowed = provider(
        case,
        case.source_workspace,
        options={"is_custom": True, "provider_id": "allowed-alias"},
        model="allowed-model",
    )
    duplicate1 = provider(
        case,
        case.source_workspace,
        options={"is_custom": True, "provider_id": "duplicate-alias"},
        model="duplicate-one",
    )
    duplicate2 = provider(
        case,
        case.source_workspace,
        options={"is_custom": True, "provider_id": "duplicate-alias"},
        model="duplicate-two",
    )
    shadow = provider(
        case,
        case.source_workspace,
        options={"is_custom": True, "provider_id": "openai"},
        model="shadowed-model",
    )
    slugged = provider(
        case,
        case.source_workspace,
        options={"is_custom": True, "provider_id": "Not A Runtime ID"},
        model="slugged-model",
    )
    preview = case.copies.preview(case.owner_context, case.source.project_id, case.source_workspace)
    available = {
        (item["provider_id"], item["provider_model_id"]) for item in preview["connection_options"]
    }
    assert allowed in available
    assert not {duplicate1, duplicate2, shadow, slugged} & available


def test_noninitial_catalogue_uses_runtime_keys_modalities_and_bound(job_case, monkeypatch):
    case = job_case
    allowed = provider(case, case.destination)
    unsupported = provider(case, case.destination, key="not-supported", model="unsupported")
    nontext = provider(case, case.destination, model="audio-only")
    with case.database.immediate_session() as session:
        session.get_one(m.ProviderModel, nontext[1]).input_modalities_json = ["audio"]
    preview = case.copies.preview(case.owner_context, case.source.project_id, case.destination)
    assert {
        (item["provider_id"], item["provider_model_id"]) for item in preview["connection_options"]
    } == {allowed}
    monkeypatch.setattr(connections, "MAX_OPTIONS", 1)
    queries = []

    def query(_conn, _cursor, sql, params, _ctx, _many):
        if "FROM providers" in sql and "JOIN provider_models" in sql:
            queries.append((sql, params))

    event.listen(case.database.engine, "before_cursor_execute", query)
    try:
        # Invalid modality/key rows still count toward the bounded header input.
        with pytest.raises(ProjectCopyError) as error:
            case.copies.preview(case.owner_context, case.source.project_id, case.destination)
    finally:
        event.remove(case.database.engine, "before_cursor_execute", query)
    assert error.value.code == "copy_limit_exceeded"
    assert len(queries) == 1
    assert "LIMIT ? OFFSET ?" in queries[0][0] and queries[0][1][-2:] == (2, 0)
    assert unsupported != nontext


def test_initial_builtin_at_alias_cannot_redirect_destination_connection(job_case):
    from pandrator.logic.llm_handler import _resolve_model_request_details

    case = job_case
    bad_model = "chosen@other-runtime"
    builtin = provider(case, case.source_workspace, model=bad_model)
    provider(
        case,
        case.source_workspace,
        options={"is_custom": True, "provider_id": "other-runtime"},
        model="custom-alternative",
    )
    legacy_alias = "openai/" + bad_model
    hydrated, selected = build_llm_settings(
        case.database,
        case.services.paths,
        workspace_id=case.source_workspace,
        requested_model=legacy_alias,
    )
    details = _resolve_model_request_details(selected, hydrated)
    # The existing runtime treats the builtin model suffix as a provider override.
    assert details["provider_config"]["id"] == "other-runtime"
    preview = case.copies.preview(case.owner_context, case.source.project_id, case.source_workspace)
    assert not any(
        item["provider_model_id"] == builtin[1] for item in preview["connection_options"]
    )
    choices = [
        {"requirement_id": item["id"], "provider_id": builtin[0], "provider_model_id": builtin[1]}
        for item in preview["connection_requirements"]
    ]
    with pytest.raises(ProjectCopyError) as error:
        case.copies.preview(
            case.owner_context,
            case.source.project_id,
            case.source_workspace,
            destination_connections=choices,
        )
    assert error.value.code == "copy_connection_unavailable"
    with case.database.snapshot_session() as session:
        binding = {
            "provider_id": builtin[0],
            "provider_model_id": builtin[1],
            "selector": legacy_alias,
            "provider_revision": session.get_one(m.Provider, builtin[0]).revision,
            "model_revision": session.get_one(m.ProviderModel, builtin[1]).revision,
        }
        with pytest.raises(ProjectCopyError) as error:
            connections.validate_current_connections(session, case.source_workspace, [binding])
    assert error.value.code == "copy_connection_unavailable"


@pytest.mark.parametrize("initial", [True, False], ids=["legacy-custom", "noninitial-custom"])
def test_explicit_custom_at_model_remains_literal(job_case, initial):
    from pandrator.logic.llm_handler import _resolve_model_request_details

    case = job_case
    destination = case.source_workspace if initial else case.destination
    ids = provider(
        case,
        destination,
        options={"is_custom": True, "provider_id": "literal-runtime"},
        model="chosen@other-runtime",
    )
    preview, choices = choose(case, destination=destination, model_ids=ids)
    assert preview["ready"]
    job, _status = request(case, preview, choices, destination=destination)
    run(case)
    result = case.copies.get(case.owner_context, job["id"])
    assert result["state"] == "succeeded", result
    runtime = "literal-runtime" if initial else ids[0]
    selector = f"custom:{runtime}/chosen@other-runtime"
    hydrated, selected = build_llm_settings(
        case.database,
        case.services.paths,
        session_id=result["result"]["source_session_id"],
        requested_model=selector,
    )
    assert selected == selector
    details = _resolve_model_request_details(selected, hydrated)
    assert details["provider_config"]["id"] == runtime
    assert details["model"].endswith("chosen@other-runtime")
