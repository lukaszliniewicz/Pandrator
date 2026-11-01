"""Synthetic speech effects keep namespace identity separate from host authority."""

from __future__ import annotations

import socket
import threading
from typing import Any

import google.auth
import pytest
import requests.sessions
from sqlalchemy import select

from pandrator.logic import google_voice_scope, tts_handler
from pandrator.logic.tts_io_policy import current_tts_io_policy
from pandrator.web import google_voice_design, workflow_handlers, workflow_voice
from pandrator.web.access.contracts import AccessContext, AccessDenied
from pandrator.web.access.workspace_policy import WorkspaceAction
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.accounts.models import User
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.credential_identity import credential_namespace
from pandrator.web.credentials import ResolvedCredential
from pandrator.web.generation_provider_preflight import generation_provider_preflight
from pandrator.web.models import Voice
from pandrator.web.speech_catalogue_access import CatalogueChanged, SpeechFence
from pandrator.web.tts_catalogue_service import TtsCatalogueService
from tests import test_generation_execution_policy as generation
from tests import test_generation_voice_workspace as voice_fixtures
from tests.test_generation_execution_policy import template as template  # noqa: F401
from tests.test_google_voice_design import MODELS, _new_queued_voice

generation_case = generation.case
voice_case = voice_fixtures.case

POLICY_CHANGED = "The host credential policy changed during execution."
HOST = HostActor("pandrator-cli")
SCOPE = {"fingerprint": "synthetic-scope", "project": "synthetic-project"}


class BoundaryReached(Exception):
    """Stop a synthetic effect before audio, auth or provider execution."""


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Host credential speech cases must remain synthetic and offline")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(requests.sessions.Session, "request", denied)
    monkeypatch.setattr(google.auth, "default", denied)


def _demote(database: Any) -> None:
    with database.immediate_session() as session:
        owner = session.scalar(select(User).where(User.legacy_subject == "owner"))
        assert owner is not None
        owner.is_instance_admin = False


def test_generation_expected_proof_fences_capability_with_stable_namespace(
    generation_case: Any,
) -> None:
    f = generation_case
    workspace = f.personal[f.users["owner"]]
    root = f.services.sessions.create("Synthetic host root", workspace_id=workspace)
    payload = {"generation_run_id": generation._run(f, root)}
    before = generation._call(f, HOST, None, payload)
    assert before.legacy_host_access and before.host_credentials_allowed
    _demote(f.database)
    after = generation._call(f, HOST, None, payload)
    assert after.legacy_host_access and not after.host_credentials_allowed
    with pytest.raises(AccessDenied):
        generation._call(f, HOST, None, payload, expected=before)


@pytest.mark.parametrize("captured", [True, None])
def test_speech_fence_checks_current_capability_only_when_captured(
    generation_case: Any, captured: bool | None
) -> None:
    f = generation_case
    actor = generation.credential(f, "owner").actor
    with f.database.snapshot_session() as session:
        assert (
            session.get(ApiTokenWorkspaceGrant, (actor.token_id, f.personal[f.users["owner"]]))
            is not None
        )
    fence = SpeechFence(
        f.database,
        AccessContext(actor),
        f.personal[f.users["owner"]],
        WorkspaceAction.READ,
        expected_host_credentials=captured,
    )
    fence()
    _demote(f.database)
    if captured is True:
        with pytest.raises(CatalogueChanged):
            fence()
    else:
        fence()


def test_explicit_preflight_does_not_claim_adc_or_environment_project_after_demotion(
    voice_case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    module = importlib.import_module("pandrator.web.generation_provider_preflight")
    _demote(voice_case.database)
    seen: list[str] = []

    def resolve(*_args: Any, **options: Any) -> ResolvedCredential:
        seen.append(options["fallback_environment_variable"])
        return ResolvedCredential()

    monkeypatch.setattr(module, "resolve_provider_credential", resolve)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "synthetic-host-project")
    result = generation_provider_preflight(
        voice_case.database,
        {
            "tts": {
                "service": "vertex_ai",
                "provider_configs": [
                    {
                        "id": "vertex_ai",
                        "api_key_env": "SYNTHETIC_HOST_KEY",
                    }
                ],
            }
        },
        workspace_id=voice_case.owners[2],
    )
    assert seen == [""]
    assert result["credential_origin"] == "unconfigured"
    assert result["credential_configured"] is False
    assert result["project_resolution"] == "unavailable" and result["project"] == ""


def test_google_configured_service_keeps_legacy_selection_without_environment_fallback(
    voice_case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _demote(voice_case.database)
    seen: list[str] = []

    def resolve(*_args: Any, **options: Any) -> ResolvedCredential:
        seen.append(options["fallback_environment_variable"])
        return ResolvedCredential()

    monkeypatch.setattr(google_voice_design, "resolve_provider_credential", resolve)
    service, credential = google_voice_design.configured_google_design_service(
        voice_case.database, voice_case.paths, "vertex_ai", workspace_id=voice_case.owners[2]
    )
    assert service["id"] == "vertex_ai" and credential == ""
    assert seen == [""]
    with voice_case.database.snapshot_session() as session:
        namespace = credential_namespace(session, voice_case.owners[2])
        assert namespace.legacy_host_access and not namespace.host_credentials_allowed


@pytest.mark.parametrize("revoke", [False, True])
def test_preview_native_boundary_gets_false_or_reproves_before_host_effect(
    voice_case: Any, monkeypatch: pytest.MonkeyPatch, revoke: bool
) -> None:
    f = voice_case
    if not revoke:
        _demote(f.database)
    settings = {
        "service": "Silero",
        "provider_configs": [
            {
                "id": "silero",
                "api_key_env": "SYNTHETIC_PREVIEW_KEY",
            }
        ],
    }
    monkeypatch.setattr(workflow_handlers, "hydrate_tts_settings", lambda *_args, **_kw: settings)
    monkeypatch.setattr(f.handlers, "prepare_audio_cpp_voice_reference", lambda value, **_kw: value)
    monkeypatch.setattr(f.handlers, "_ensure_qwen_cloned_voice", lambda *_args, **_kw: None)
    effects: list[str] = []
    environment_reads: list[str] = []
    original_getenv = workflow_handlers.os.getenv

    def getenv(name: str, default: Any = None) -> Any:
        if name == "SYNTHETIC_PREVIEW_KEY":
            environment_reads.append(name)
            return "synthetic-host-key"
        return original_getenv(name, default)

    monkeypatch.setattr(workflow_handlers.os, "getenv", getenv)

    def synthesize(*_args: Any, **_kwargs: Any) -> Any:
        policy = current_tts_io_policy()
        assert policy is not None
        assert policy.allow_environment_credentials is revoke
        assert policy.allow_host_adc is revoke
        if revoke:
            _demote(f.database)
        reads_before_reproof = list(environment_reads)
        try:
            policy.before_io()
        finally:
            assert environment_reads == reads_before_reproof
        effects.append("synthetic native effect")
        raise BoundaryReached("synthetic boundary reached")

    monkeypatch.setattr(f.handlers.tts_providers, "synthesize", synthesize)
    with execution_actor_scope(HOST):
        # The preview wrapper deliberately redacts and wraps adapter failures.
        with pytest.raises(
            RuntimeError, match=POLICY_CHANGED if revoke else "synthetic boundary reached"
        ):
            f.handlers.preview_tts_voice(
                {"text": "Synthetic preview.", "settings": settings, "workspace_id": f.owners[2]},
                lambda *_args: None,
                threading.Event(),
            )
    assert effects == ([] if revoke else ["synthetic native effect"])
    assert bool(environment_reads) is revoke


@pytest.mark.parametrize("operation", ["publish_voice", "unpublish_voice"])
@pytest.mark.parametrize("revoke", [False, True])
def test_voice_effect_boundary_gets_false_or_reproves_before_dispatch(
    voice_case: Any, monkeypatch: pytest.MonkeyPatch, operation: str, revoke: bool
) -> None:
    f = voice_case
    if not revoke:
        _demote(f.database)
    config = {
        "id": "xtts",
        "adapter": "xtts",
        "api_base": "http://synthetic.invalid",
        "supports_voice_deletion": True,
    }
    monkeypatch.setattr(workflow_voice, "hydrate_tts_settings", lambda *_args, **_kw: {})
    monkeypatch.setattr(tts_handler, "get_service_config", lambda *_args: config)
    if operation == "unpublish_voice":
        with f.database.immediate_session() as session:
            voice = session.get_one(Voice, f.voices[2])
            voice.metadata_json = {
                "providers": {
                    "xtts": {
                        "voice_id": "synthetic-provider-voice",
                        "managed_by": "pandrator",
                        "resource_kind": "uploaded_reference",
                        "endpoint_fingerprint": workflow_voice._provider_endpoint_fingerprint(
                            config["api_base"]
                        ),
                    }
                }
            }
    effects: list[str] = []

    def provider(
        *_args: Any, before_io: Any, allow_environment_credentials: bool, **_kw: Any
    ) -> Any:
        assert allow_environment_credentials is revoke
        if revoke:
            _demote(f.database)
        before_io()
        effects.append("synthetic provider effect")
        raise BoundaryReached("synthetic boundary reached")

    monkeypatch.setattr(
        f.handlers.tts_providers,
        "upload_voice" if operation == "publish_voice" else "delete_voice",
        provider,
    )
    with (
        execution_actor_scope(HOST),
        pytest.raises(
            ValueError if revoke else BoundaryReached, match=POLICY_CHANGED if revoke else None
        ),
    ):
        getattr(f.handlers, operation)(
            {"workspace_id": f.owners[2], "voice_id": f.voices[2], "service_id": "xtts"},
            lambda *_args: None,
            threading.Event(),
        )
    assert effects == ([] if revoke else ["synthetic provider effect"])


@pytest.mark.parametrize("revoke", [False, True])
def test_google_design_auth_boundaries_use_current_capability_and_reprove(
    voice_case: Any, monkeypatch: pytest.MonkeyPatch, revoke: bool
) -> None:
    f = voice_case
    voice_id, payload = _new_queued_voice(
        f.database, service_id="vertex_ai", model=MODELS[0], job_id="synthetic-design", scope=SCOPE
    )
    if not revoke:
        _demote(f.database)
    assert voice_id == payload["voice_id"]
    service = {"id": "vertex_ai", "models": list(MODELS)}
    monkeypatch.setattr(
        google_voice_design, "configured_google_design_service", lambda *_args, **_kw: (service, "")
    )
    seen: list[str] = []

    def scope(*_args: Any, allow_host_adc: bool, before_io: Any, **_kw: Any) -> dict[str, str]:
        assert allow_host_adc is revoke
        before_io()
        seen.append("scope")
        return SCOPE

    def auth(*_args: Any, allow_host_adc: bool, before_io: Any, **_kw: Any) -> Any:
        assert allow_host_adc is revoke
        if revoke:
            _demote(f.database)
        before_io()
        seen.append("synthetic ADC effect")
        raise BoundaryReached("synthetic boundary reached")

    monkeypatch.setattr(google_voice_design, "_scope_for_payload", scope)
    monkeypatch.setattr(google_voice_design, "_creation_endpoint_and_headers", auth)
    with (
        execution_actor_scope(HOST),
        pytest.raises(
            ValueError if revoke else BoundaryReached, match=POLICY_CHANGED if revoke else None
        ),
    ):
        google_voice_design.design_google_voice(
            f.handlers._voice_workflow_context(), payload, lambda *_args: None, threading.Event()
        )
    assert seen == (["scope"] if revoke else ["scope", "synthetic ADC effect"])


@pytest.mark.parametrize(
    "host_allowed,captured_public", [(False, False), (True, False), (True, True)]
)
def test_remembered_google_scope_receives_capability_and_callback(
    voice_case: Any, monkeypatch: pytest.MonkeyPatch, host_allowed: bool, captured_public: bool
) -> None:
    f = voice_case
    with f.database.immediate_session() as session:
        voice = session.get_one(Voice, f.voices[2])
        voice.metadata_json = {
            "providers": {
                "gemini": {
                    "resource_kind": "designed_voice",
                    "status": "ready",
                    "voice_id": "voice_synthetic",
                    "scope_fingerprint": SCOPE["fingerprint"],
                }
            }
        }
    calls: list[str] = []

    def fence() -> None:
        calls.append("before")

    def scope(*_args: Any, allow_host_adc: bool, before_io: Any) -> dict[str, str]:
        assert allow_host_adc is (host_allowed and not captured_public)
        assert before_io is fence
        before_io()
        return SCOPE

    monkeypatch.setattr(google_voice_scope, "google_voice_scope", scope)
    # This helper needs only database access; avoid constructing a manager or providers.
    catalogue = object.__new__(TtsCatalogueService)
    catalogue.database = f.database
    services = [{"id": "gemini", "models": [MODELS[0]]}]
    catalogue._merge_remembered_google_voices(
        services,
        [ResolvedCredential(value="synthetic-key", source="database")],
        workspace_id=f.owners[2],
        allow_host_adc=host_allowed,
        captured_public=captured_public,
        before_io=fence,
    )
    assert calls == ["before"]
    assert services[0]["managed_google_voice_ids"] == ["voice_synthetic"]
