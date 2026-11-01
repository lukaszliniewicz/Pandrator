"""Bounded real-workspace HTTP catalogue and fresh speech-effect fences."""

from __future__ import annotations

import json
import socket
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from pandrator.web import models as m
from pandrator.web import tts_catalogue_service as catalogue_module
from pandrator.web.access.contracts import AccessContext
from pandrator.web.accounts.execution_actor import actor_from_principal
from pandrator.web.manager_proxy import ManagerProxyError
from pandrator.web.speech_catalogue_access import require_speech_access
from pandrator.web.tts_providers import TtsHealth
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_collaboration_authentication import enable, new_account
from tests.test_evidence_access_routes import body, headers, team
from tests.test_project_sharing_routes import login
from tests.test_provider_workspace_access import promote, scoped_token, sql_capture
from tests.test_settings_workspace_access import counts, put

SECRET = "synthetic-speech-secret"
NATIVE_MODEL = "qwen3_tts_1_7b_customvoice_q8_0"


@pytest.fixture(autouse=True)
def local_only(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("Speech catalogue qualification must stay local")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    # The only ADC boundary is external identity discovery, never namespace/policy.
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    def no_adc(**_kwargs):
        raise DefaultCredentialsError("Synthetic fixture has no ADC")

    monkeypatch.setattr(google.auth, "default", no_adc)
    return denied


@pytest.fixture
def catalogue(case, monkeypatch):
    value = case.app.extensions["pandrator"]["services"].tts_catalogue

    def inventory():
        raise ManagerProxyError("not_configured", "Synthetic Manager unavailable")

    monkeypatch.setattr(value.manager_bridge, "inventory", inventory)
    calls = []

    def health(service):
        # A real writer can be acquired here: no catalogue writer spans external I/O.
        with case.database.immediate_session() as session:
            assert session.scalar(select(m.WorkspaceSetting.key).limit(1)) is not None
        calls.append(("health", service["id"]))
        return TtsHealth(True, True)

    def enrich(service, **_kwargs):
        calls.append(("enrich", service["id"]))
        return {**service, "models": ["synthetic-model"]}

    monkeypatch.setattr(value.providers, "health", health)
    monkeypatch.setattr(value.providers, "enrich_catalog", enrich)
    return SimpleNamespace(service=value, calls=calls)


def connection(label, *, identifier="openai", native=False):
    row = {
        "id": identifier,
        "name": label,
        "adapter": "audio_cpp" if native else "openai",
        "api_base": f"https://{label.lower()}.invalid/v1",
        "api_key": SECRET + label,
        "credential_backend": "database",
        "models": [NATIVE_MODEL] if native else ["synthetic-model"],
        "default_model": NATIVE_MODEL if native else "synthetic-model",
        "voices": ["safe-voice"],
        "voice_metadata": {
            "safe-voice": {
                "id": "safe-voice",
                "gender": "female",
                "language": "en",
                "reference_text": "PRIVATE reference",
                "sample_url": "PRIVATE sample",
                "artifact_id": "PRIVATE artifact",
                "path": "PRIVATE path",
            }
        },
        "model_catalog": [
            {
                "id": NATIVE_MODEL if native else "synthetic-model",
                "label": "Safe model",
                "recommended_chunk_characters": 1800,
                "request_parameters": {"private": "PRIVATE configured schema"},
                "options": {"private": "PRIVATE configured options"},
            }
        ],
        "online": True,
        "available": True,
        "health_checked_at": "PRIVATE claim",
    }
    return row


def save(client, csrf, *, workspace=None, label="OWN", records=None, revision=0):
    return body(
        put(
            client,
            csrf,
            "services.tts",
            {"provider_configs": records if records is not None else [connection(label)]},
            workspace=workspace,
            revision=revision,
        )
    )


def change_role(case, members, role):
    with case.database.immediate_session() as session:
        session.get_one(m.WorkspaceMembership, (members.workspace_id, members.alice_id)).role = role


def test_solo_passive_explicit_refresh_and_unknown_model(case, catalogue):
    save(case.client, {"X-CSRF-Token": case.csrf})
    passive = body(case.client.get("/api/v1/services/tts", query_string={"service_id": "openai"}))
    assert passive["services"][0]["api_base"] == "https://own.invalid/v1"
    assert passive["value"]["provider_configs"][0]["name"] == "OWN"
    assert passive["can_manage"] and passive["can_refresh"] and "profiles" in passive
    for method in (case.client.get, case.client.head):
        result = method("/api/v1/services/tts", query_string={"refresh": "true"})
        assert result.status_code == 409
    assert not catalogue.calls
    assert (
        case.client.get(
            "/api/v1/services/tts/audio_cpp", query_string={"model": "missing-model"}
        ).status_code
        == 404
    )
    assert not catalogue.calls
    refreshed = body(
        case.client.post("/api/v1/services/tts/openai/refresh", headers={"X-CSRF-Token": case.csrf})
    )
    assert refreshed["service"]["online"] and catalogue.calls == [
        ("health", "openai"),
        ("enrich", "openai"),
    ]
    assert (
        case.client.post(
            "/api/v1/services/tts/refresh",
            query_string={"session_id": ""},
            headers={"X-CSRF-Token": case.csrf},
        ).status_code
        == 422
    )


def test_noninitial_personal_and_team_approved_metadata(case, catalogue, monkeypatch):
    save(case.client, {"X-CSRF-Token": case.csrf}, label="INITIAL")
    members = team(case, monkeypatch)
    own = save(members.alice, members.ah, label="ALICE")
    wid = members.workspace_id
    save(
        members.owner,
        members.oh,
        workspace=wid,
        records=[
            connection("TEAM"),
            connection("NATIVE", identifier="audio_cpp", native=True),
            {**connection("DISABLED", identifier="disabled_custom"), "enabled": False},
        ],
    )
    body(
        put(
            members.owner,
            members.oh,
            "services.stt",
            {
                "service": "azure_mai_transcribe_2",
                "provider_configs": [
                    {
                        "id": "azure_mai_transcribe_2",
                        "name": "TEAM STT",
                        "api_key": SECRET,
                        "credential_backend": "database",
                        "models": ["mai-transcribe-2"],
                        "language": "en",
                        "options": {"private": "PRIVATE stt"},
                    }
                ],
            },
            workspace=wid,
        )
    )
    actual = body(members.alice.get("/api/v1/services/tts", query_string={"service_id": "openai"}))
    assert (
        actual["workspace_id"] == own["workspace_id"]
        and actual["services"][0]["api_base"] == "https://alice.invalid/v1"
    )
    assert set(actual["manager"]) == {"configured", "available"}
    manager = body(
        members.owner.get(
            "/api/v1/services/tts", query_string={"workspace_id": wid, "service_id": "openai"}
        )
    )
    assert manager["previews"] == [] and set(manager["manager"]) == {"configured", "available"}
    assert "manager_service" not in manager["services"][0]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Approved catalogue touched a private/credential builder")

    monkeypatch.setattr(catalogue_module, "resolve_provider_credentials", forbidden)
    monkeypatch.setattr(catalogue_module, "list_tts_provider_profiles", forbidden)
    monkeypatch.setattr(catalogue.service, "_previews", forbidden)
    monkeypatch.setattr(catalogue.service, "_cached_native_discovery", forbidden)
    monkeypatch.setattr("pandrator.web.stt_providers.provider_credential_status", forbidden)
    before = counts(case)
    for view in ("full", "compact"):
        result = body(
            members.alice.get(
                "/api/v1/services/tts", query_string={"workspace_id": wid, "view": view}
            )
        )
        assert result["value"] == {} and not result["can_manage"] and not result["can_refresh"]
        assert {v["id"] for v in result["services"]} == {"openai", "audio_cpp"}
        text = json.dumps(result)
        for private in (
            SECRET,
            ".invalid",
            "INITIAL",
            "ALICE",
            "PRIVATE",
            "api_base",
            "credential_",
            "previews",
            "profiles",
            "manager_service",
        ):
            assert private not in text
        row = next(v for v in result["services"] if v["id"] == "openai")
        assert row["online"] is None and row["voice_metadata"]["safe-voice"]["gender"] == "female"
        assert "request_parameters" not in text
    detail = body(
        members.alice.get(
            "/api/v1/services/tts/audio_cpp",
            query_string={"workspace_id": wid, "model": NATIVE_MODEL},
        )
    )
    model = detail["service"]["model_catalog"][0]
    assert model["id"] == NATIVE_MODEL and model["recommended_chunk_characters"] == 1800
    assert "PRIVATE" not in json.dumps(model) and model.get("request_parameters")
    stt = body(members.alice.get("/api/v1/services/stt", query_string={"workspace_id": wid}))
    assert stt["profiles"] == [] and stt["value"] == {} and stt["services"][0]["name"] == "TEAM STT"
    assert SECRET not in json.dumps(stt) and "credential_" not in json.dumps(stt)
    assert counts(case) == before and not catalogue.calls


def test_scope_roots_readonly_manager_and_live_denials(case, catalogue, monkeypatch):
    members = team(case, monkeypatch)
    wid = members.workspace_id
    save(members.owner, members.oh, workspace=wid, label="TEAM")
    private = save(members.alice, members.ah, label="ALICE")
    assert (
        members.owner.get(
            "/api/v1/services/tts", query_string={"workspace_id": private["workspace_id"]}
        ).status_code
        == 404
    )
    src = body(
        members.owner.post(
            "/api/v1/sessions",
            json={"name": "Speech root", "workflow_kind": "subtitles", "workspace_id": wid},
            headers=headers(members.oh, "speech-root"),
        ),
        201,
    )
    assert (
        body(members.alice.get("/api/v1/services/tts", query_string={"session_id": src["id"]}))[
            "workspace_id"
        ]
        == wid
    )
    assert (
        members.alice.get(
            "/api/v1/services/tts",
            query_string={"session_id": src["id"], "workspace_id": private["workspace_id"]},
        ).status_code
        == 404
    )
    _, project_only = scoped_token(case, members.alice_id, {"app.read"}, project=src["project_id"])
    assert (
        members.alice.get(
            "/api/v1/services/tts", query_string={"session_id": src["id"]}, headers=project_only
        ).status_code
        == 404
    )
    for invalid in ("", " ", "x" * 81):
        assert (
            members.alice.get(
                "/api/v1/services/tts", query_string={"workspace_id": invalid}
            ).status_code
            == 422
        )
    for route in (
        "/api/v1/services/tts/openai/refresh",
        "/api/v1/services/tts/discover",
        "/api/v1/services/tts/xtts/models",
    ):
        response = (
            members.alice.get(route, query_string={"workspace_id": wid})
            if route.endswith("models")
            else members.alice.post(
                route,
                query_string={"workspace_id": wid},
                json={"base_url": "https://team.invalid/v1"},
                headers=members.ah,
            )
        )
        assert response.status_code in (403, 404), response.get_json()
    promote(case, members)
    _, read = scoped_token(case, members.alice_id, {"app.read"}, workspace=wid)
    config = body(
        members.alice.get(
            "/api/v1/services/tts",
            query_string={"workspace_id": wid, "service_id": "openai"},
            headers=read,
        )
    )
    assert (
        "value" in config
        and config["value"]
        and not config["can_manage"]
        and not config["can_refresh"]
    )
    assert (
        members.alice.post(
            "/api/v1/services/tts/openai/refresh", query_string={"workspace_id": wid}, headers=read
        ).status_code
        == 403
    )
    assert not catalogue.calls


@pytest.mark.parametrize("promotion", [False, True])
def test_passive_return_keeps_original_projection_authority(
    case, catalogue, monkeypatch, promotion
):
    members = team(case, monkeypatch)
    wid = members.workspace_id
    save(members.owner, members.oh, workspace=wid, label="TEAM")
    if not promotion:
        promote(case, members)
    original = catalogue.service.snapshot

    def build(**kwargs):
        result = original(**kwargs)
        change_role(case, members, "admin" if promotion else "member")
        return result

    monkeypatch.setattr(catalogue.service, "snapshot", build)
    result = members.alice.get(
        "/api/v1/services/tts", query_string={"workspace_id": wid, "service_id": "openai"}
    )
    if promotion:
        value = body(result)
        assert value["value"] == {} and not value["can_manage"] and not value["can_refresh"]
    else:
        assert result.status_code == 404


@pytest.mark.parametrize("boundary", ["hydration", "health", "enrichment"])
def test_refresh_current_fences_stop_later_effects_and_cache(
    case, catalogue, monkeypatch, boundary
):
    members = team(case, monkeypatch)
    wid = members.workspace_id
    save(members.owner, members.oh, workspace=wid, label="TEAM")
    promote(case, members)
    body(put(members.owner, members.oh, "defaults.tts", {"language": "en"}, workspace=wid))
    token, bearer = scoped_token(
        case,
        members.alice_id,
        {"app.read", "app.write", "app.run", "app.credentials.write"},
        workspace=wid,
    )
    before = dict(catalogue.service._native_discovery)
    calls = []
    if boundary == "hydration":
        original = catalogue_module.resolve_provider_credentials

        def hydrate(*args, **kwargs):
            values = original(*args, **kwargs)
            assert values[0].resolved_value() == SECRET + "TEAM"
            with case.database.immediate_session() as session:
                session.get_one(m.ApiToken, token.id).scopes_json = [
                    "app.read",
                    "app.write",
                    "app.run",
                ]
            calls.append("hydrated")
            return values

        monkeypatch.setattr(catalogue_module, "resolve_provider_credentials", hydrate)
    else:

        def external(service, **_kwargs):
            calls.append(boundary)
            with case.database.immediate_session() as session:
                if boundary == "health":
                    session.get_one(m.WorkspaceMembership, (wid, members.alice_id)).role = "member"
                else:
                    setting = session.get_one(m.WorkspaceSetting, (wid, "defaults.tts"))
                    setting.revision += 1
            return TtsHealth(True, True) if boundary == "health" else service

        monkeypatch.setattr(
            catalogue.service.providers,
            "health" if boundary == "health" else "enrich_catalog",
            external,
        )
    result = members.alice.post(
        "/api/v1/services/tts/openai/refresh", query_string={"workspace_id": wid}, headers=bearer
    )
    assert result.status_code == {"hydration": 403, "health": 404, "enrichment": 409}[boundary], (
        result.get_json()
    )
    assert dict(catalogue.service._native_discovery) == before
    assert calls == [
        {"hydration": "hydrated", "health": "health", "enrichment": "enrichment"}[boundary]
    ]
    if boundary == "hydration":
        assert not catalogue.calls
    if boundary == "health":
        assert not catalogue.calls


def test_refresh_unfiltered_limit_precedes_hydration_and_probes(case, catalogue, monkeypatch):
    records = [connection(f"SYNTHETIC{i}", identifier=f"custom_{i}") for i in range(21)]
    save(case.client, {"X-CSRF-Token": case.csrf}, records=records)
    calls = []
    monkeypatch.setattr(
        catalogue_module,
        "resolve_provider_credentials",
        lambda *_a, **_k: calls.append("credentials"),
    )
    result = case.client.post("/api/v1/services/tts/refresh", headers={"X-CSRF-Token": case.csrf})
    assert result.status_code == 422, result.get_json()
    assert not calls and not catalogue.calls


def test_stored_discovery_endpoint_and_hydration_revision_fences(case, catalogue, monkeypatch):
    save(case.client, {"X-CSRF-Token": case.csrf}, label="OWN")
    from pandrator.web import credentials

    original = credentials.resolve_provider_credential
    calls = []

    def hydrate(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result.resolved_value() == SECRET + "OWN"
        calls.append("credentials")
        with case.database.immediate_session() as session:
            root = session.get_one(m.WorkspaceSetting, (kwargs["workspace_id"], "services.tts"))
            root.revision += 1
        return result

    monkeypatch.setattr(credentials, "resolve_provider_credential", hydrate)
    monkeypatch.setattr(
        "pandrator.logic.tts_endpoint_discovery.discover_tts_endpoint",
        lambda *_a, **_k: calls.append("discovery") or {"success": True, "models": []},
    )

    def request(endpoint, **extra):
        return case.client.post(
            "/api/v1/services/tts/discover",
            json={"base_url": endpoint, "service_id": "openai", **extra},
            headers={"X-CSRF-Token": case.csrf},
        )

    assert request("https://foreign.invalid/v1").status_code == 422 and not calls
    assert request("https://own.invalid/v1").status_code == 409 and calls == ["credentials"]
    assert body(request("https://explicit.invalid/v1", api_key="synthetic-explicit"))["success"]
    assert calls == ["credentials", "discovery"]


def test_actual_administrator_live_model_control_uses_own_workspace(case, catalogue, monkeypatch):
    save(
        case.client, {"X-CSRF-Token": case.csrf}, records=[connection("INITIAL", identifier="xtts")]
    )
    alice = new_account(case)
    enable(case, monkeypatch)
    ac, ah = login(case, "alice")
    own = save(ac, ah, records=[connection("ALICE", identifier="xtts")])
    calls = []
    monkeypatch.setattr(
        "pandrator.web.service_routes.requests.get",
        lambda url, **_kwargs: (
            calls.append(url) or SimpleNamespace(status_code=200, json=lambda: {"data": []})
        ),
    )
    assert ac.get("/api/v1/services/tts/xtts/models").status_code == 403
    # Actual installation role is changed in the disposable DB; workspace remains personal.
    with case.database.immediate_session() as session:
        session.get_one(m.User, alice.user_id).is_instance_admin = True
    # Login captures the current role/scopes through the genuine browser credential.
    ac, _ah = login(case, "alice")
    body(ac.get("/api/v1/services/tts/xtts/models"))
    assert calls and all("alice.invalid" in url for url in calls)
    assert (
        case.client.get(
            "/api/v1/services/tts/xtts/models", query_string={"workspace_id": own["workspace_id"]}
        ).status_code
        == 404
    )


def test_member_capture_sql_is_fixed_without_private_builders(
    case, catalogue, monkeypatch, tmp_path
):
    members = team(case, monkeypatch)
    wid = members.workspace_id
    save(members.owner, members.oh, workspace=wid, label="TEAM")
    _token, bearer = scoped_token(case, members.alice_id, {"app.read"}, workspace=wid)
    principal = case.auth.resolve_api_token(
        bearer["Authorization"].removeprefix("Bearer "),
        network_zone="loopback",
        target_instance_id=case.app.extensions["pandrator"]["services"]
        .identity.snapshot(observed_origin="http://localhost")
        .instance_id,
    )
    assert principal is not None
    actor = actor_from_principal(principal)
    assert actor is not None
    context = AccessContext(actor)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Member must not resolve credentials or query private cache/previews")

    monkeypatch.setattr(catalogue_module, "resolve_provider_credentials", forbidden)
    monkeypatch.setattr(catalogue.service, "_previews", forbidden)
    monkeypatch.setattr(catalogue.service, "_cached_native_discovery", forbidden)

    def measure():
        with sql_capture(case.database) as statements:
            with case.database.snapshot_session() as session:
                access = require_speech_access(session, context, wid)
                captured = catalogue.service._settings(
                    db_session=session, workspace_id=access.workspace.id
                )
            result, _revision = catalogue.service.snapshot(
                workspace_id=wid, settings_snapshot=captured, approved_only=True
            )
        return statements, result

    single, _ = measure()
    save(
        members.owner,
        members.oh,
        workspace=wid,
        records=[connection(f"TEAM{i}", identifier=f"custom_{i}") for i in range(9)],
        revision=1,
    )
    many, result = measure()
    assert len(single) == len(many) and len(result["services"]) == 9
    for statement, _parameters in many:
        assert statement.lstrip().upper().startswith(("SELECT", "BEGIN")), statement
        assert "stored_credentials" not in statement and "FROM artifacts" not in statement
    summary = {
        "one_service_queries": len(single),
        "nine_service_queries": len(many),
        "queries": [s for s, _p in many],
    }
    (tmp_path / "speech-member-sql.json").write_text(json.dumps(summary, indent=2))
    # Durable evidence contains SQL only, never credential/input values.
    from pathlib import Path

    Path(
        ".local-notes/multi-user-2026-10-23/continued-implementation/workspace-sharing-r3/speech-catalogue-workspace/member-sql.json"
    ).write_text(json.dumps(summary, indent=2))


def test_nonlegacy_personal_and_team_never_inspect_host_adc(case, catalogue, monkeypatch):
    members = team(case, monkeypatch)
    promote(case, members)
    calls = []

    def ambient_adc(**_kwargs):
        calls.append("ambient ADC")
        raise AssertionError("A nonlegacy public namespace must not inspect host ADC")

    def ambient_transport(*_args, **_kwargs):
        calls.append("ADC transport")
        raise AssertionError("A nonlegacy public namespace must not use host ADC transport")

    monkeypatch.setattr("google.auth.default", ambient_adc)
    monkeypatch.setattr("google.auth.transport.requests.Request.__call__", ambient_transport)
    database_key = json.dumps(
        {
            "type": "service_account",
            "project_id": "synthetic-project",
            "client_email": "synthetic@example.invalid",
            "private_key": "SYNTHETIC-NOT-A-REAL-KEY",
            "token_uri": "https://synthetic-token.invalid",
        }
    )
    adc = connection("NONLEGACY", identifier="vertex_ai")
    adc.pop("api_key")
    adc["vertex_project"] = "synthetic-project"
    adc["auth_mode"] = "adc"
    adc["api_base"] = "https://aiplatform.googleapis.com"
    adc["models"] = ["gemini-2.5-flash-preview-tts"]
    adc["default_model"] = "gemini-2.5-flash-preview-tts"
    for workspace in (None, members.workspace_id):
        saved = save(members.alice, members.ah, workspace=workspace, records=[adc])
        with case.database.immediate_session() as session:
            session.add(
                m.Voice(
                    workspace_id=saved["workspace_id"],
                    name="Synthetic designed voice",
                    metadata_json={
                        "providers": {
                            "vertex_ai": {
                                "resource_kind": "designed_voice",
                                "status": "ready",
                                "scope_fingerprint": "synthetic-scope",
                                "voice_id": "voice_synthetic",
                            }
                        }
                    },
                )
            )
        query = {} if workspace is None else {"workspace_id": workspace}
        read = body(members.alice.get("/api/v1/services/tts/vertex_ai", query_string=query))
        assert read["service"]["credential_configured"] is False
        refreshed = body(
            members.alice.post(
                "/api/v1/services/tts/vertex_ai/refresh", query_string=query, headers=members.ah
            )
        )
        assert refreshed["service"]["credential_configured"] is False and not calls
        assert not refreshed["service"]["managed_google_voice_ids"]
        assert catalogue.calls == [("health", "vertex_ai")]
        catalogue.calls.clear()
        # A database key in the same proved namespace remains usable without ADC.
        save(
            members.alice,
            members.ah,
            workspace=workspace,
            records=[{**adc, "api_key": database_key}],
            revision=1,
        )
        seen = []

        def enrich(service, *, api_key="", _seen=seen, **_kwargs):
            _seen.append(api_key)
            return service

        monkeypatch.setattr(catalogue.service.providers, "enrich_catalog", enrich)
        keyed = body(
            members.alice.post(
                "/api/v1/services/tts/vertex_ai/refresh", query_string=query, headers=members.ah
            )
        )
        assert keyed["service"]["credential_configured"] is True
        assert seen == [database_key] and not calls
        catalogue.calls.clear()


@pytest.mark.parametrize("loss", ["scope", "revision"])
def test_legacy_adc_transport_fences_every_call_and_preserves_denial(
    case, catalogue, monkeypatch, loss
):
    adc = connection("LEGACY", identifier="vertex_ai")
    adc["api_base"] = "https://aiplatform.googleapis.com"
    adc.pop("api_key")
    adc["vertex_project"] = "synthetic-project"
    saved = save(case.client, {"X-CSRF-Token": case.csrf}, records=[adc])
    wid = saved["workspace_id"]
    with case.database.immediate_session() as session:
        session.add(
            m.Voice(
                workspace_id=wid,
                name="Synthetic legacy designed voice",
                metadata_json={
                    "providers": {
                        "vertex_ai": {
                            "resource_kind": "designed_voice",
                            "status": "ready",
                            "scope_fingerprint": "synthetic-scope",
                            "voice_id": "voice_synthetic",
                        }
                    }
                },
            )
        )
    token, bearer = scoped_token(
        case,
        case.auth.initial_account().user_id,
        {"app.read", "app.write", "app.run", "app.credentials.write"},
        workspace=wid,
    )
    calls = []

    def transport(_self, *, url, **_kwargs):
        calls.append(url)
        # A real writer is available during the synthetic external request.
        with case.database.immediate_session() as session:
            if loss == "scope":
                session.get_one(m.ApiToken, token.id).scopes_json = [
                    "app.read",
                    "app.write",
                    "app.run",
                ]
            else:
                session.get_one(m.WorkspaceSetting, (wid, "services.tts")).revision += 1
        return SimpleNamespace(status=200, data=b"synthetic ADC response", headers={})

    def default(*, request=None, **_kwargs):
        if request is None:
            calls.append("unfenced ambient default")
            raise AssertionError("Passive saved-voice matching must not retry ambient ADC")
        request(url="https://synthetic-adc.invalid/first")
        request(url="https://synthetic-adc.invalid/second")
        return SimpleNamespace(), "synthetic-project"

    monkeypatch.setattr("google.auth.default", default)
    monkeypatch.setattr("google.auth.transport.requests.Request.__call__", transport)
    # The initial passive ADC lookup uses its rejecting request. Saved-voice
    # matching must not retry without it when no ADC identity was captured.
    passive = body(case.client.get("/api/v1/services/tts/vertex_ai"))
    assert passive["service"]["credential_configured"] is False
    assert not passive["service"]["managed_google_voice_ids"] and not calls
    assert not catalogue.calls
    before = dict(catalogue.service._native_discovery)
    response = case.client.post("/api/v1/services/tts/vertex_ai/refresh", headers=bearer)
    assert response.status_code == (403 if loss == "scope" else 409), response.get_json()
    if loss == "revision":
        assert response.get_json()["error"]["code"] == "catalogue_changed"
    assert calls == ["https://synthetic-adc.invalid/first"]
    assert not catalogue.calls and dict(catalogue.service._native_discovery) == before


@pytest.mark.parametrize("boundary", ["refresh", "pagination"])
def test_transport_closure_real_google_adapter_rechecks_token_and_pages(
    case, catalogue, monkeypatch, boundary
):
    from pandrator.web.tts_providers import TtsProviderRegistry

    # Restore the genuine registry dispatcher/Google adapter; only TCP health,
    # Google identity and actual HTTP transport are synthetic boundaries.
    registry = catalogue.service.providers
    monkeypatch.setattr(
        registry, "enrich_catalog", TtsProviderRegistry.enrich_catalog.__get__(registry)
    )
    adc = connection("NATIVE", identifier="vertex_ai")
    adc.pop("api_key")
    adc.pop("adapter")
    adc.update(
        api_base="https://aiplatform.googleapis.com",
        models=["gemini-3.8-flash-tts"],
        default_model="gemini-3.8-flash-tts",
        vertex_project="synthetic-project",
    )
    saved = save(case.client, {"X-CSRF-Token": case.csrf}, records=[adc])
    wid = saved["workspace_id"]
    token, bearer = scoped_token(
        case,
        case.auth.initial_account().user_id,
        {"app.read", "app.write", "app.run", "app.credentials.write"},
        workspace=wid,
    )
    defaults = []
    refreshes = []
    transports = []
    pages = []

    class Credentials:
        valid = False
        token = ""

        def refresh(self, request):
            refreshes.append(request)
            request(url="https://synthetic-google-token.invalid/token", method="POST")
            self.valid = True
            self.token = "synthetic-access-token"

    credentials = Credentials()

    def default(**kwargs):
        defaults.append(kwargs)
        if boundary == "refresh" and len(defaults) == 2:
            with case.database.immediate_session() as session:
                session.get_one(m.ApiToken, token.id).scopes_json = [
                    "app.read",
                    "app.write",
                    "app.run",
                ]
        return credentials, "synthetic-project"

    def transport(_self, *, url, **_kwargs):
        transports.append(url)
        with case.database.immediate_session() as session:
            assert session.get(m.ApiToken, token.id) is not None
        return SimpleNamespace(status=200, data=b"synthetic", headers={})

    def page(endpoint, **kwargs):
        pages.append((endpoint, kwargs["params"]))
        assert kwargs["params"] == {"pageSize": 1000}
        with case.database.immediate_session() as session:
            session.get_one(m.WorkspaceSetting, (wid, "services.tts")).revision += 1
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "voices": [{"id": "voice_first", "displayName": "Synthetic voice"}],
                "nextPageToken": "second",
            },
        )

    monkeypatch.setattr("google.auth.default", default)
    monkeypatch.setattr("google.auth.transport.requests.Request.__call__", transport)
    monkeypatch.setattr("pandrator.logic.google_tts_catalogue_http.requests.get", page)
    before_native = dict(catalogue.service._native_discovery)
    before_health = dict(catalogue.service._health_observations)
    response = case.client.post("/api/v1/services/tts/vertex_ai/refresh", headers=bearer)
    assert response.status_code == (403 if boundary == "refresh" else 409), response.get_json()
    assert len(defaults) == 2 and all(call.get("request") for call in defaults)
    if boundary == "refresh":
        assert not refreshes and not transports and not pages
    else:
        assert refreshes == [defaults[1]["request"]]
        assert transports == ["https://synthetic-google-token.invalid/token"]
        assert len(pages) == 1
        assert response.get_json()["error"]["code"] == "catalogue_changed"
    assert dict(catalogue.service._native_discovery) == before_native
    assert dict(catalogue.service._health_observations) == before_health


def test_transport_closure_opaque_workspace_key_cannot_inspect_ambient_project(
    case, catalogue, monkeypatch
):
    from pandrator.web.credentials import (
        database_reference,
        tts_service_credential_key,
        upsert_credential,
    )

    members = team(case, monkeypatch)
    home = body(members.alice.get("/api/v1/defaults/tts"))["workspace_id"]
    credential_key = tts_service_credential_key("vertex_ai")
    # Represents an existing opaque stored credential. Current JSON submission
    # validation remains real and unchanged; the namespace reader must handle it.
    with case.database.immediate_session() as session:
        upsert_credential(
            session, credential_key, "Synthetic opaque key", "opaque-fixture-key", workspace_id=home
        )
        session.add(
            m.Voice(
                workspace_id=home,
                name="Synthetic opaque voice",
                metadata_json={
                    "providers": {
                        "vertex_ai": {
                            "resource_kind": "designed_voice",
                            "status": "ready",
                            "voice_id": "voice_opaque",
                            "scope_fingerprint": "synthetic",
                        }
                    }
                },
            )
        )
    vertex = connection("OPAQUE", identifier="vertex_ai")
    vertex.pop("api_key")
    vertex.update(
        api_base="https://aiplatform.googleapis.com", secret_ref=database_reference(credential_key)
    )
    save(members.alice, members.ah, records=[vertex])
    calls = []

    def ambient(**_kwargs):
        calls.append("ambient project lookup")
        raise AssertionError("Opaque nonlegacy key must not borrow host project identity")

    monkeypatch.setattr("google.auth.default", ambient)
    value = body(members.alice.get("/api/v1/services/tts/vertex_ai"))
    assert value["workspace_id"] == home and value["service"]["credential_configured"]
    assert value["service"]["managed_google_voice_ids"] == []
    assert not calls and not catalogue.calls


def test_passive_capabilities_solo_full_detail_and_compact_need_no_refresh(case, catalogue):
    from copy import deepcopy

    from pandrator.logic.speech_capabilities import decorate_service_capabilities
    from pandrator.logic.tts_provider_profiles import AUDIO_CPP_MODEL_CATALOG

    canonical = next(row for row in AUDIO_CPP_MODEL_CATALOG if row["id"] == NATIVE_MODEL)
    expected = {
        "id": "audio_cpp",
        "adapter": "audio_cpp",
        "models": [NATIVE_MODEL],
        "model_catalog": [deepcopy(canonical)],
    }
    decorate_service_capabilities(expected)
    save(
        case.client,
        {"X-CSRF-Token": case.csrf},
        records=[connection("NATIVE", identifier="audio_cpp", native=True)],
    )
    full = body(case.client.get("/api/v1/services/tts", query_string={"service_id": "audio_cpp"}))
    service = full["services"][0]
    assert (
        service["expressive_capabilities"][NATIVE_MODEL]
        == expected["expressive_capabilities"][NATIVE_MODEL]
    )
    detail = body(
        case.client.get("/api/v1/services/tts/audio_cpp", query_string={"model": NATIVE_MODEL})
    )["service"]
    assert detail["expressive_capabilities"] == expected["expressive_capabilities"]
    assert detail["generation_prompt_models"] == expected["generation_prompt_models"]
    assert detail["expressive_capabilities"][NATIVE_MODEL]["instructions"] != "none"
    assert detail["model_catalog"][0]["recommended_chunk_characters"] == 1800
    compact = body(
        case.client.get(
            "/api/v1/services/tts", query_string={"view": "compact", "service_id": "audio_cpp"}
        )
    )["services"][0]
    assert NATIVE_MODEL in compact["generation_prompt_models"]
    assert "expressive_capabilities" not in compact
    assert all("expressive_capabilities" not in row for row in compact["model_catalog"])
    assert not catalogue.calls


def test_passive_capabilities_member_selected_detail_uses_only_canonical_models(
    case, catalogue, monkeypatch
):
    from copy import deepcopy

    from pandrator.logic.speech_capabilities import decorate_service_capabilities
    from pandrator.logic.tts_provider_profiles import AUDIO_CPP_MODEL_CATALOG
    from pandrator.web import speech_catalogue_projection

    members = team(case, monkeypatch)
    canonical = {row["id"]: row for row in AUDIO_CPP_MODEL_CATALOG}
    other = next(identifier for identifier in canonical if identifier != NATIVE_MODEL)
    unknown = "unknown-custom-native"
    raw = connection("NATIVE", identifier="audio_cpp", native=True)
    raw.update(
        models=[NATIVE_MODEL, other, unknown],
        expressive_capabilities={NATIVE_MODEL: {"PRIVATE": "configured claim"}},
        generation_prompt_models=[unknown],
    )
    raw["model_catalog"] = [
        {
            "id": identifier,
            "family": "PRIVATE family",
            "voice_mode": "PRIVATE mode",
            "recommended_chunk_characters": 1800,
            "expressive_capabilities": {"PRIVATE": "configured capability"},
            "request_parameters": {"PRIVATE": "configured request schema"},
        }
        for identifier in (NATIVE_MODEL, other, unknown)
    ]
    save(members.owner, members.oh, workspace=members.workspace_id, records=[raw])
    query = {"workspace_id": members.workspace_id}
    for view in ("full", "compact"):
        collection = body(
            members.alice.get("/api/v1/services/tts", query_string={**query, "view": view})
        )
        assert "expressive_capabilities" not in json.dumps(collection)
    calls = []

    def derive(context):
        calls.append((list(context["models"]), [row["id"] for row in context["model_catalog"]]))
        decorate_service_capabilities(context)

    monkeypatch.setattr(speech_catalogue_projection, "decorate_service_capabilities", derive)
    expected = {
        "id": "audio_cpp",
        "adapter": "audio_cpp",
        "models": [NATIVE_MODEL],
        "model_catalog": [deepcopy(canonical[NATIVE_MODEL])],
    }
    decorate_service_capabilities(expected)
    detail = body(
        members.alice.get(
            "/api/v1/services/tts/audio_cpp", query_string={**query, "model": NATIVE_MODEL}
        )
    )["service"]
    assert detail["expressive_capabilities"] == expected["expressive_capabilities"]
    assert detail["generation_prompt_models"] == expected["generation_prompt_models"]
    assert set(detail["expressive_capabilities"]) == {NATIVE_MODEL}
    assert "PRIVATE" not in json.dumps(detail["expressive_capabilities"])
    assert detail["model_catalog"][0]["recommended_chunk_characters"] == 1800
    assert calls == [([NATIVE_MODEL], [NATIVE_MODEL])]
    custom = body(
        members.alice.get(
            "/api/v1/services/tts/audio_cpp", query_string={**query, "model": unknown}
        )
    )["service"]
    assert "expressive_capabilities" not in custom and "generation_prompt_models" not in custom
    assert "request_parameters" not in custom["model_catalog"][0]
    mixed = body(
        members.alice.get(
            "/api/v1/services/tts/audio_cpp",
            query_string={**query, "models": f"{NATIVE_MODEL},{unknown}"},
        )
    )["service"]
    assert set(mixed["expressive_capabilities"]) == {NATIVE_MODEL}
    assert set(mixed["generation_prompt_models"]) <= {NATIVE_MODEL}
    assert calls == [([NATIVE_MODEL], [NATIVE_MODEL]), ([NATIVE_MODEL], [NATIVE_MODEL])]
    assert not catalogue.calls


def test_summary_payload_get_is_safe_for_manager_and_member_and_bounded(
    case, catalogue, monkeypatch
):
    from pathlib import Path

    from pandrator.logic.tts_provider_profiles import AUDIO_CPP_MODEL_CATALOG
    from pandrator.web.speech_catalogue_projection import summary_tts_payload

    save(case.client, {"X-CSRF-Token": case.csrf}, label="INITIAL")
    members = team(case, monkeypatch)
    promote(case, members)
    home = connection("ALICE", identifier="openai")
    home["models"] = ["alice-home-model"]
    home["default_model"] = "alice-home-model"
    save(members.alice, members.ah, records=[home])
    native = connection("TEAM", identifier="audio_cpp", native=True)
    native.pop("model_catalog")
    native["models"] = [row["id"] for row in AUDIO_CPP_MODEL_CATALOG]
    native["voices"] = [f"voice_{index}" for index in range(40)]
    save(members.owner, members.oh, workspace=members.workspace_id, records=[native])

    def forbidden(*_args, **_kwargs):
        raise AssertionError(
            "Summary GET must stop before private credential/cache/provider builders"
        )

    for name in ("resolve_provider_credentials", "list_tts_provider_profiles"):
        monkeypatch.setattr(catalogue_module, name, forbidden)
    for name in (
        "_previews",
        "_cached_native_discovery",
        "_apply_health_observations",
        "_merge_remembered_google_voices",
    ):
        monkeypatch.setattr(catalogue.service, name, forbidden)
    monkeypatch.setattr("google.auth.default", forbidden)
    measured = {}
    for label, client, manage, query in (
        ("personal_manager", members.alice, True, {}),
        ("team_manager", members.alice, True, {"workspace_id": members.workspace_id}),
        ("team_member", members.bob, False, {"workspace_id": members.workspace_id}),
    ):
        response = client.get("/api/v1/services/tts", query_string={**query, "view": "summary"})
        value = body(response)
        assert value["view"] == "summary" and value["can_manage"] is manage
        assert value["can_refresh"] is manage
        assert not {"value", "default_value", "profiles", "previews", "manager"} & value.keys()
        text = json.dumps(value)
        assert SECRET not in text and ".invalid" not in text and "credential_" not in text
        assert (
            "voice_metadata" not in text
            and "language_support" not in text
            and "request_parameters" not in text
        )
        measured[label] = len(response.data)
        assert len(response.data) < 128 * 1024
        if query:
            assert value["workspace_id"] == members.workspace_id
            row = value["services"][0]
            assert row["id"] == "audio_cpp" and len(row["models"]) == len(AUDIO_CPP_MODEL_CATALOG)
            assert row["voice_count"] == 40 and len(row["voices"]) == 20
            assert all(
                set(model) <= {"id", "label", "family", "voice_mode", "loaded"}
                for model in row["model_catalog"]
            )
        else:
            assert value["workspace_id"] != members.workspace_id
            assert value["services"][0]["models"] == ["alice-home-model"]
    assert not catalogue.calls
    assert (
        members.alice.get(
            "/api/v1/services/tts/audio_cpp",
            query_string={"view": "summary", "workspace_id": members.workspace_id},
        ).status_code
        == 422
    )
    assert (
        members.alice.post(
            "/api/v1/services/tts/audio_cpp/refresh",
            query_string={"view": "summary", "workspace_id": members.workspace_id},
            headers=members.ah,
        ).status_code
        == 422
    )
    assert (
        members.alice.get(
            "/api/v1/services/tts", query_string={"view": "summary", "refresh": "true"}
        ).status_code
        == 409
    )
    assert (
        members.alice.get(
            "/api/v1/services/tts", query_string={"view": "summary", "service_id": "missing"}
        ).status_code
        == 404
    )
    # Scalar provenance only, even if an already-approved producer grows a map.
    projected = summary_tts_payload(
        {
            "services": [
                {
                    "id": "synthetic",
                    "voices": ["a", "b"],
                    "live_voices": ["b", "c"],
                    "catalogue": {
                        "source": "configured",
                        "complete": False,
                        "extension": {"private": True},
                        "error": {"nested": "drop"},
                    },
                    "model_catalog": [
                        {"id": "m", "loaded": True, "language_support": {"private": True}}
                    ],
                }
            ]
        }
    )
    row = projected["services"][0]
    assert row["voice_count"] == 3 and row["voices"] == ["a", "b", "c"]
    assert row["catalogue"] == {"source": "configured", "complete": False}
    assert row["model_catalog"] == [{"id": "m", "loaded": True}]
    measured["canonical_model_count"] = len(AUDIO_CPP_MODEL_CATALOG)
    Path(
        ".local-notes/multi-user-2026-10-23/continued-implementation/workspace-sharing-r3/speech-catalogue-workspace/summary-payload/bytes.json"
    ).write_text(json.dumps(measured, indent=2))


def test_summary_payload_post_refreshes_only_selection_then_projects(case, catalogue, monkeypatch):
    save(
        case.client,
        {"X-CSRF-Token": case.csrf},
        records=[connection("POST"), connection("OTHER", identifier="audio_cpp", native=True)],
    )

    def enrich(service, *, api_key=""):
        assert api_key == SECRET + "POST"
        catalogue.calls.append(("enrich", service["id"]))
        return {
            **service,
            "voices": [f"voice_{i}" for i in range(40)],
            "live_voices": [f"voice_{i}" for i in range(30, 60)],
            "voice_metadata": {
                "PRIVATE": {
                    "path": "/private/sample",
                    "sample_url": "https://private.invalid/sample",
                }
            },
            "model_catalog": [
                {
                    "id": "synthetic-model",
                    "request_parameters": {"PRIVATE": "schema"},
                    "language_support": {"languages": ["en"]},
                }
            ],
        }

    monkeypatch.setattr(catalogue.service.providers, "enrich_catalog", enrich)
    response = case.client.post(
        "/api/v1/services/tts/refresh",
        query_string={"view": "summary", "service_id": "openai"},
        headers={"X-CSRF-Token": case.csrf},
    )
    result = body(response)
    assert result["view"] == "summary" and result["can_manage"] and result["can_refresh"]
    assert catalogue.calls == [("health", "openai"), ("enrich", "openai")]
    assert len(result["services"]) == 1
    row = result["services"][0]
    assert row["id"] == "openai" and len(row["voices"]) == 20 and row["voice_count"] == 60
    text = json.dumps(result)
    assert not {"value", "default_value", "manager", "profiles", "previews"} & result.keys()
    assert (
        SECRET not in text
        and "PRIVATE" not in text
        and "api_base" not in text
        and "credential_" not in text
    )
    assert row["online"] is True and row["available"] is True
    assert isinstance(row["health_checked_at"], str) and len(response.data) < 128 * 1024
    assert row["catalogue"]["source"] == "runtime" and row["catalogue"]["complete"] is True
    assert set(row["catalogue"]) == {"source", "scope", "service_id", "checked_at", "complete"}
    assert isinstance(row["catalogue"]["checked_at"], str)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Summary GET must not reuse private refresh health")

    monkeypatch.setattr(catalogue.service, "_apply_health_observations", forbidden)
    monkeypatch.setattr(catalogue.service, "_cached_native_discovery", forbidden)
    passive = body(
        case.client.get(
            "/api/v1/services/tts", query_string={"view": "summary", "service_id": "openai"}
        )
    )["services"][0]
    assert passive["online"] is None and passive["available"] is None
    assert passive["catalogue"]["source"] == "configured"
    assert catalogue.calls == [("health", "openai"), ("enrich", "openai")]


@pytest.mark.parametrize("loss", ["scope", "revision"])
def test_summary_payload_post_keeps_hydration_and_publication_fences(
    case, catalogue, monkeypatch, loss
):
    saved = save(case.client, {"X-CSRF-Token": case.csrf}, label="FENCED")
    wid = saved["workspace_id"]
    token, bearer = scoped_token(
        case,
        case.auth.initial_account().user_id,
        {"app.read", "app.write", "app.run", "app.credentials.write"},
        workspace=wid,
    )
    if loss == "scope":
        original = catalogue_module.resolve_provider_credentials

        def hydrate(*args, **kwargs):
            credentials = original(*args, **kwargs)
            assert credentials[0].resolved_value() == SECRET + "FENCED"
            with case.database.immediate_session() as session:
                session.get_one(m.ApiToken, token.id).scopes_json = [
                    "app.read",
                    "app.write",
                    "app.run",
                ]
            return credentials

        monkeypatch.setattr(catalogue_module, "resolve_provider_credentials", hydrate)
    else:

        def health(service):
            catalogue.calls.append(("health", service["id"]))
            with case.database.immediate_session() as session:
                session.get_one(m.WorkspaceSetting, (wid, "services.tts")).revision += 1
            return TtsHealth(True, True)

        monkeypatch.setattr(catalogue.service.providers, "health", health)
    before = (
        dict(catalogue.service._native_discovery),
        dict(catalogue.service._health_observations),
    )
    response = case.client.post(
        "/api/v1/services/tts/refresh",
        query_string={"view": "summary", "service_id": "openai"},
        headers=bearer,
    )
    assert response.status_code == (403 if loss == "scope" else 409), response.get_json()
    assert catalogue.calls == ([] if loss == "scope" else [("health", "openai")])
    assert (
        dict(catalogue.service._native_discovery),
        dict(catalogue.service._health_observations),
    ) == before
