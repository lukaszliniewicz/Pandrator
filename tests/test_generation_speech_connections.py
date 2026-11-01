"""Passive, same-session fences for authorized workspace speech connections."""

from __future__ import annotations

import hashlib
import json
import os
import socket
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from types import SimpleNamespace
from typing import Any, Iterator, cast

import pytest
import requests.sessions
from sqlalchemy import delete, event, select, update
from sqlalchemy.orm import Session

from pandrator.logic import tts_handler
from pandrator.web import credentials
from pandrator.web.accounts.models import Workspace
from pandrator.web.database import Database
from pandrator.web.generation_speech_connections import (
    GenerationSpeechConnection,
    capture_generation_speech_connection,
    require_generation_speech_connection,
)
from pandrator.web.models import StoredCredential, WorkspaceSetting
from pandrator.web.tts_catalogue_service import TtsCatalogueService
from tests.web_test_support import prepare_web_test_data_root

MODEL = "gemini-3.8-flash-tts"
NATIVE_MODEL = "qwen3_tts_1_7b_base_q8_0"
SECRET = "synthetic-generation-speech-secret"
CHOOSE = "Choose a speech connection configured for this workspace."
CHANGED = (
    "The speech connection or credential changed. "
    "Review the workspace connection and submit a new run."
)


class NoProviderIo:
    def capabilities(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Connection leaf requested provider capabilities")

    def health(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Connection leaf requested provider health")

    def enrich_catalog(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Connection leaf performed provider discovery")


class NoManagerIo:
    configured_error: BaseException | None = None

    @property
    def configured(self) -> bool:
        if self.configured_error is not None:
            raise self.configured_error
        return False

    def inventory(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Connection leaf performed Manager discovery")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Speech connection qualification must stay offline")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)


def _save_setting(case: Any, key: str, value: Any, *, workspace_id: str | None = None) -> None:
    workspace_id = workspace_id or case.workspace_id
    with case.database.immediate_session() as session:
        row = session.get(WorkspaceSetting, (workspace_id, key))
        if row is None:
            session.add(WorkspaceSetting(workspace_id=workspace_id, key=key, value_json=value))
        else:
            row.value_json = deepcopy(value)
            row.revision += 1


def _profile(service_id: str = "gemini") -> dict[str, Any]:
    native = service_id == "audio_cpp"
    custom = service_id == "custom-speech"
    model = NATIVE_MODEL if native else "custom-model" if custom else MODEL
    return {
        "id": service_id,
        "enabled": True,
        "models": [model],
        "default_model": model,
        "api_base": "https://speech.invalid/v1",
        "connection_mode": "external",
        "secret_ref": "" if native else "db:tts:custom_speech" if custom else "db:shared:gemini",
        "settings": {"transport_option": "initial"},
        **({"provider": "openai", "adapter": "openai_compatible"} if custom else {}),
    }


@pytest.fixture
def case(tmp_path: Any) -> Iterator[Any]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    with database.immediate_session() as session:
        legacy = session.scalar(select(Workspace.id).where(Workspace.kind == "personal"))
        assert legacy is not None
        for workspace_id in ("connection-alpha", "connection-beta"):
            session.add(Workspace(id=workspace_id, kind="team", name=workspace_id))
        session.flush()
        for workspace_id in ("connection-alpha", "connection-beta"):
            for key in ("shared:gemini", "tts:custom_speech"):
                session.add(
                    StoredCredential(
                        workspace_id=workspace_id,
                        key=key,
                        label="Synthetic credential",
                        secret_value=SECRET
                        if workspace_id == "connection-alpha"
                        else "synthetic-foreign-secret",
                    )
                )
    manager = NoManagerIo()
    catalogue = TtsCatalogueService(
        database, paths, NoProviderIo(), manager_bridge=cast(Any, manager)
    )
    result = SimpleNamespace(
        database=database,
        paths=paths,
        catalogue=catalogue,
        manager=manager,
        workspace_id="connection-alpha",
        foreign_id="connection-beta",
        legacy_id=legacy,
    )
    _save_setting(result, "services.tts", {"provider_configs": [_profile()]})
    try:
        yield result
    finally:
        database.dispose()


def _settings(case: Any, service_id: str = "gemini", model: str | None = MODEL) -> dict[str, Any]:
    with case.database.snapshot_session() as session:
        settings = case.catalogue.preview_settings(
            service_id,
            model=model,
            voice=None,
            language=None,
            workspace_id=case.workspace_id,
            db_session=session,
        )
        assert type(settings) is dict
        return settings


def _capture(
    case: Any, settings: Any = None, *, workspace_id: str | None = None
) -> GenerationSpeechConnection:
    settings = _settings(case) if settings is None else settings
    with case.database.snapshot_session() as session:
        return capture_generation_speech_connection(
            session,
            case.catalogue,
            settings,
            workspace_id=workspace_id or case.workspace_id,
        )


def _require(case: Any, proof: Any) -> None:
    with case.database.snapshot_session() as session:
        require_generation_speech_connection(session, case.catalogue, proof)


def _modify_profile(case: Any, **changes: Any) -> None:
    with case.database.snapshot_session() as session:
        value = deepcopy(
            session.get_one(WorkspaceSetting, (case.workspace_id, "services.tts")).value_json
        )
    value["provider_configs"][0].update(changes)
    _save_setting(case, "services.tts", value)


def test_current_gemini_connection_is_frozen_and_secret_free(case: Any):
    settings = _settings(case)
    before = deepcopy(settings)
    proof = _capture(case, settings)
    assert (
        proof.workspace_id == case.workspace_id
        and proof.service_id == "gemini"
        and proof.model == MODEL
    )
    assert proof.credential_fingerprint == hashlib.sha256(SECRET.encode()).hexdigest()
    assert len(proof.connection_fingerprint) == 64
    assert (
        SECRET not in repr(proof)
        and "db:" not in repr(proof)
        and "speech.invalid" not in repr(proof)
    )
    assert settings == before
    assert not hasattr(proof, "__dict__")
    with pytest.raises(FrozenInstanceError):
        cast(Any, proof).model = "changed"
    _require(case, proof)


@pytest.mark.parametrize("selection", ["xtts", "model", "default"])
def test_actual_model_precedence_and_transport_normalization(case: Any, selection: str):
    settings = _settings(case)
    settings.pop("xtts_model")
    settings.pop("model")
    if selection == "xtts":
        settings.update(xtts_model="gemini/models/" + MODEL, model="unapproved-model")
    elif selection == "model":
        settings["model"] = "models/" + MODEL
    assert _capture(case, settings).model == MODEL


def test_legacy_service_alias_and_default_xtts_selection_use_the_frozen_profile(case: Any):
    record = {"id": "xtts", "enabled": True, "api_base": "https://local-speech.invalid/v1"}
    _save_setting(case, "services.tts", {"provider_configs": [record]})
    settings: dict[str, Any] = {"provider_configs": [record], "tts_service": "XTTS"}
    profile = tts_handler.get_service_config(settings, "XTTS")
    assert profile is not None
    proof = _capture(case, settings)
    assert proof.service_id == "xtts" and proof.model == profile["default_model"]
    del settings["tts_service"]
    assert _capture(case, settings) == proof


@pytest.mark.parametrize(
    "service_id,model", [("audio_cpp", NATIVE_MODEL), ("custom-speech", "custom-model")]
)
def test_configured_native_and_standard_custom_connections(case: Any, service_id: str, model: str):
    _save_setting(case, "services.tts", {"provider_configs": [_profile(service_id)]})
    settings = _settings(case, service_id, model)
    proof = _capture(case, settings)
    assert proof.service_id == service_id.replace("-", "_") and proof.model == model
    if service_id == "audio_cpp":
        assert proof.credential_fingerprint == hashlib.sha256(b"").hexdigest()
    _require(case, proof)


def test_native_request_id_is_not_aliased_to_an_approved_model(case: Any):
    _save_setting(case, "services.tts", {"provider_configs": [_profile("audio_cpp")]})
    settings = _settings(case, "audio_cpp", NATIVE_MODEL)
    settings["xtts_model"] = "models/" + NATIVE_MODEL
    with pytest.raises(ValueError) as error:
        _capture(case, settings)
    assert str(error.value) == CHOOSE


@pytest.mark.parametrize("service", ["Custom", "OpenAI-Compatible", "openai_compatible"])
def test_custom_alias_requires_the_selected_string_id(case: Any, service: str):
    _save_setting(case, "services.tts", {"provider_configs": [_profile("custom-speech")]})
    settings = _settings(case, "custom-speech", "custom-model")
    settings["service"] = service
    assert _capture(case, settings).service_id == "custom_speech"
    settings["openai_audio_endpoint"] = {
        "id": "custom-speech",
        "api_base": "https://private.invalid",
    }
    with pytest.raises(ValueError) as error:
        _capture(case, settings)
    assert str(error.value) == CHOOSE


@pytest.mark.parametrize("change", ["disabled", "removed", "model_removed"])
def test_current_approval_and_model_loss_deny_safely(case: Any, change: str):
    extra = "gemini-workspace-only-model"
    _modify_profile(case, models=[extra], default_model=extra)
    settings = _settings(case, model=extra)
    proof = _capture(case, settings)
    if change == "disabled":
        _modify_profile(case, enabled=False)
    elif change == "removed":
        _save_setting(case, "services.tts", {"provider_configs": []})
    else:
        _modify_profile(case, models=[MODEL], default_model=MODEL)
    for operation in (lambda: _capture(case, settings), lambda: _require(case, proof)):
        with pytest.raises(ValueError) as error:
            operation()
        assert str(error.value) == CHOOSE


@pytest.mark.parametrize(
    "changes",
    [
        {"api_base": "https://changed.invalid/v1"},
        {"secret_ref": "db:another-private-key"},
        {"adapter": "generic_json"},
        {"vertex_project": "changed-private-project"},
        {"speech_path": "/changed/private/speech"},
        {"profile_id": "changed-profile"},
        {"settings": {"transport_option": "changed"}},
        {"request_defaults": {"format": "changed"}},
    ],
)
def test_material_selected_transport_changes_require_resubmission(
    case: Any, changes: dict[str, Any]
):
    settings = _settings(case)
    proof = _capture(case, settings)
    _modify_profile(case, **changes)
    for operation in (lambda: _capture(case, settings), lambda: _require(case, proof)):
        with pytest.raises(ValueError) as error:
            operation()
        assert str(error.value) == CHANGED
        assert SECRET not in str(error.value) and "private" not in str(error.value)


@pytest.mark.parametrize("change", ["rotation", "deletion"])
def test_selected_current_database_credential_changes_deny(case: Any, change: str):
    proof = _capture(case)
    with case.database.immediate_session() as session:
        predicate = (
            StoredCredential.workspace_id == case.workspace_id,
            StoredCredential.key == "shared:gemini",
        )
        if change == "rotation":
            session.execute(
                update(StoredCredential)
                .where(*predicate)
                .values(secret_value="synthetic-rotated-secret")
            )
        else:
            session.execute(delete(StoredCredential).where(*predicate))
    with pytest.raises(ValueError) as error:
        _require(case, proof)
    assert str(error.value) == CHANGED


def test_foreign_same_key_cannot_supply_or_change_selected_credential(case: Any):
    proof = _capture(case)
    with case.database.immediate_session() as session:
        session.execute(
            update(StoredCredential)
            .where(StoredCredential.workspace_id == case.foreign_id)
            .values(secret_value="foreign-rotated")
        )
    _require(case, proof)
    with case.database.immediate_session() as session:
        session.execute(
            delete(StoredCredential).where(StoredCredential.workspace_id == case.workspace_id)
        )
    with pytest.raises(ValueError) as error:
        _require(case, proof)
    assert str(error.value) == CHANGED


@pytest.mark.parametrize(
    "reference",
    [
        "env:SYNTHETIC_SPEECH_KEY",
        "keyring:private/key",
        "file-path:/private/key",
        "file:private-key",
    ],
)
def test_nonlegacy_external_references_deny_before_io(
    case: Any, monkeypatch: pytest.MonkeyPatch, reference: str
):
    def blocked(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("External credential IO must not run")

    monkeypatch.setattr(credentials, "_load_keyring", blocked)
    monkeypatch.setattr(credentials, "_read_secret_file", blocked)
    _modify_profile(case, secret_ref=reference)
    settings = _settings(case)
    with pytest.raises(ValueError) as error:
        _capture(case, settings)
    assert str(error.value) == CHOOSE


@pytest.mark.parametrize("service_id", ["audio_cpp", "vertex_ai"])
def test_missing_database_key_never_uses_environment_or_adc(
    case: Any,
    monkeypatch: pytest.MonkeyPatch,
    service_id: str,
):
    import google.auth

    record = _profile(service_id)
    record.update(secret_ref="db:absent-key", api_key_env="SYNTHETIC_SPEECH_KEY")
    _save_setting(case, "services.tts", {"provider_configs": [record]})
    model = NATIVE_MODEL if service_id == "audio_cpp" else MODEL
    settings = _settings(case, service_id, model)
    real_get = type(os.environ).get

    def guarded_environment(environment: Any, key: Any, default: Any = None) -> Any:
        if key in {
            "SYNTHETIC_SPEECH_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
        }:
            raise AssertionError("Speech credential environment must not be read")
        return real_get(environment, key, default)

    def blocked_adc(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("ADC discovery must not run")

    monkeypatch.setattr(type(os.environ), "get", guarded_environment)
    monkeypatch.setattr(google.auth, "default", blocked_adc)
    proof = _capture(case, settings)
    _require(case, proof)
    assert proof.credential_fingerprint == hashlib.sha256(b"").hexdigest()


def test_default_model_catalogue_and_unrelated_provider_changes_do_not_pin_revisions(case: Any):
    proof = _capture(case)
    _save_setting(
        case,
        "defaults.tts",
        {"service": "unrelated", "model": "different", "voice": "different", "speed": 1.9},
    )
    _modify_profile(
        case,
        name="Changed label",
        models=[MODEL, "added-model"],
        default_model="added-model",
        voices=["changed-voice"],
        default_voice="changed-voice",
        pricing={MODEL: {"input": 99}},
    )
    with case.database.snapshot_session() as session:
        value = deepcopy(
            session.get_one(WorkspaceSetting, (case.workspace_id, "services.tts")).value_json
        )
    value["provider_configs"].append(
        {"id": "openai", "api_base": "https://unrelated.invalid", "models": ["unrelated-model"]}
    )
    _save_setting(case, "services.tts", value)
    _require(case, proof)


@pytest.mark.parametrize("location", ["top", "profile", "nested_profile"])
def test_raw_inline_api_key_is_not_a_frozen_connection(case: Any, location: str):
    settings = _settings(case)
    if location == "top":
        settings["api_key"] = SECRET
    elif location == "profile":
        settings["provider_configs"][0]["api_key"] = SECRET
    else:
        settings["provider_configs"][0]["settings"]["api_key"] = SECRET
    with pytest.raises(ValueError) as error:
        _capture(case, settings)
    assert str(error.value) == CHOOSE and SECRET not in str(error.value)


def test_current_raw_inline_key_and_inactive_namespace_deny(case: Any):
    proof = _capture(case)
    _modify_profile(case, api_key=SECRET)
    with pytest.raises(ValueError) as error:
        _require(case, proof)
    assert str(error.value) == CHOOSE and SECRET not in str(error.value)
    with case.database.immediate_session() as session:
        session.execute(
            update(Workspace).where(Workspace.id == case.workspace_id).values(state="archived")
        )
    with pytest.raises(ValueError) as error:
        _require(case, proof)
    assert str(error.value) == CHOOSE


def test_absent_and_null_optional_parsed_transport_fields_are_equivalent(case: Any):
    proof = _capture(case)
    _modify_profile(
        case, profile_id=None, vertex_project=None, vertex_location=None, speech_path=None
    )
    _require(case, proof)


@pytest.mark.parametrize("value", [object(), ("tuple",), float("nan"), {1: "integer key"}])
def test_malformed_frozen_connection_json_has_no_string_coercion(case: Any, value: Any):
    settings = _settings(case)
    settings["provider_configs"][0]["settings"]["invalid"] = value
    with pytest.raises(ValueError) as error:
        _capture(case, settings)
    assert str(error.value) == CHOOSE


@pytest.mark.parametrize(
    "field,value",
    [
        ("workspace_id", ""),
        ("service_id", "Gemini"),
        ("model", " padded "),
        ("connection_fingerprint", "bad"),
        ("credential_fingerprint", True),
    ],
)
def test_expected_proof_requires_exact_fields(case: Any, field: str, value: Any):
    proof = _capture(case)
    with pytest.raises(ValueError) as error:
        _require(case, replace(proof, **{field: value}))
    assert str(error.value) == CHOOSE


def test_invalid_expected_type_and_legacy_namespace_deny(case: Any):
    proof = _capture(case)
    for malformed in (None, asdict(proof)):
        with pytest.raises(ValueError) as error:
            _require(case, malformed)
        assert str(error.value) == CHOOSE
    with pytest.raises(ValueError) as error:
        _capture(case, _settings(case), workspace_id=case.legacy_id)
    assert str(error.value) == CHOOSE
    with pytest.raises(ValueError) as error:
        _require(case, replace(proof, workspace_id=case.legacy_id))
    assert str(error.value) == CHOOSE


def test_same_session_persisted_credentials_ignore_stale_and_pending_objects(case: Any):
    settings = _settings(case)
    with case.database.engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        with Session(bind=connection) as session:
            row = session.get_one(StoredCredential, (case.workspace_id, "shared:gemini"))
            session.execute(
                update(StoredCredential)
                .where(
                    StoredCredential.workspace_id == case.workspace_id,
                    StoredCredential.key == "shared:gemini",
                )
                .values(secret_value="persisted-rotated")
                .execution_options(synchronize_session=False)
            )
            assert row.secret_value == SECRET
            statements: list[str] = []
            event.listen(
                connection,
                "before_cursor_execute",
                lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
            )
            proof = capture_generation_speech_connection(
                session, case.catalogue, settings, workspace_id=case.workspace_id
            )
            assert proof.credential_fingerprint == hashlib.sha256(b"persisted-rotated").hexdigest()
            row.secret_value = "unflushed-pending"
            require_generation_speech_connection(session, case.catalogue, proof)
            assert row in session.dirty and row.secret_value == "unflushed-pending"
            assert connection.in_transaction()
            assert statements and all(
                statement.lstrip().upper().startswith("SELECT") for statement in statements
            )
        connection.rollback()


@pytest.mark.parametrize("failure", [AssertionError("guard"), OSError("guard")])
def test_external_io_guard_errors_are_not_swallowed(case: Any, failure: BaseException):
    settings = _settings(case)
    case.manager.configured_error = failure
    with pytest.raises(type(failure), match="guard"):
        _capture(case, settings)


def test_serialized_proof_contains_only_identifiers_and_fingerprints(case: Any):
    proof = _capture(case)
    encoded = json.dumps(asdict(proof))
    assert SECRET not in encoded and "db:" not in encoded and "speech.invalid" not in encoded
    assert set(asdict(proof)) == {
        "workspace_id",
        "service_id",
        "model",
        "connection_fingerprint",
        "credential_fingerprint",
    }
