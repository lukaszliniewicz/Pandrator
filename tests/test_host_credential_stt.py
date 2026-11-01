"""Cloud STT host isolation helpers; SQL role setup is not account-command acceptance."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from pandrator.logic.dubbing import cloud_stt
from pandrator.web import credentials
from pandrator.web.credential_identity import credential_namespace
from tests.test_host_credential_policy import HostPolicyCase
from tests.test_host_credential_policy import host_case as host_case


def _forbidden_environment() -> MagicMock:
    environment = MagicMock(wraps={"AZURE_SPEECH_KEY": "synthetic-hostile-azure-key"})
    environment.get.side_effect = AssertionError("Cloud STT attempted a host environment read")
    return environment


@pytest.mark.parametrize("state", ("demoted", "disabled"))
@pytest.mark.parametrize(
    "records", (None, [], [{"id": "unselected"}], [{"id": "azure_mai_transcribe_2"}])
)
def test_retired_original_hydration_denies_native_environment_before_any_early_return(
    host_case: HostPolicyCase,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    records: list[dict[str, Any]] | None,
) -> None:
    case = host_case
    settings: dict[str, Any] = {
        "stt_engine": "azure_mai_transcribe_2",
        "stt_allow_environment_credentials": True,
    }
    if records is not None:
        settings["provider_configs"] = records
    case.retire_owner(state)
    with case.database.snapshot_session() as session:
        namespace = credential_namespace(session, case.workspaces["original"])
    assert namespace.legacy_host_access and not namespace.host_credentials_allowed
    environment = _forbidden_environment()
    with monkeypatch.context() as scoped:
        scoped.setattr(cloud_stt.os, "environ", environment)
        hydrated = credentials.hydrate_stt_settings(
            case.database, case.paths, settings, workspace_id=case.workspaces["original"]
        )
        profile = cloud_stt._profile_for(hydrated)
        # The real native profile still restores the builtin locator. The
        # transient policy must stop _api_key from consulting that locator.
        assert profile["api_key_env"] == "AZURE_SPEECH_KEY"
        with pytest.raises(
            cloud_stt.CloudSTTConfigurationError,
            match="Configure a database credential for this workspace before transcription",
        ):
            cloud_stt._api_key(hydrated, profile)
    environment.get.assert_not_called()
    assert hydrated["stt_allow_environment_credentials"] is False
    assert settings["stt_allow_environment_credentials"] is True


@pytest.mark.parametrize("state", ("demoted", "disabled"))
@pytest.mark.parametrize("engine", ("azure_mai_transcribe_1_5", "azure_mai_transcribe_2"))
def test_retired_original_database_key_survives_native_profile_merge_without_host_read(
    host_case: HostPolicyCase, monkeypatch: pytest.MonkeyPatch, state: str, engine: str
) -> None:
    case = host_case
    key = credentials.stt_service_credential_key(engine)
    case.put("original", key, "synthetic-database-azure-key")
    case.retire_owner(state)
    settings = {
        "stt_engine": engine,
        "provider_configs": [{"id": engine, "secret_ref": credentials.database_reference(key)}],
    }
    environment = _forbidden_environment()
    with monkeypatch.context() as scoped:
        scoped.setattr(cloud_stt.os, "environ", environment)
        hydrated = credentials.hydrate_stt_settings(
            case.database, case.paths, settings, workspace_id=case.workspaces["original"]
        )
        profile = cloud_stt._profile_for(hydrated)
        resolved_key = cloud_stt._api_key(hydrated, profile)
    assert hydrated["stt_allow_environment_credentials"] is False
    assert resolved_key == "synthetic-database-azure-key"
    assert profile["api_key"] == "synthetic-database-azure-key"
    environment.get.assert_not_called()
    assert "stt_allow_environment_credentials" not in settings
    assert "api_key" not in settings["provider_configs"][0]


@pytest.mark.parametrize("flag", (None, True, False))
def test_active_admin_hydration_and_unhydrated_native_keep_existing_environment_policy(
    host_case: HostPolicyCase, monkeypatch: pytest.MonkeyPatch, flag: bool | None
) -> None:
    case = host_case
    settings: dict[str, Any] = {"stt_engine": "azure_mai_transcribe_2"}
    if flag is not None:
        settings["stt_allow_environment_credentials"] = flag
    hydrated = credentials.hydrate_stt_settings(
        case.database, case.paths, settings, workspace_id=case.workspaces["original"]
    )
    assert hydrated == settings
    with monkeypatch.context() as scoped:
        scoped.setattr(cloud_stt.os, "environ", {"AZURE_SPEECH_KEY": "synthetic-legacy-azure-key"})
        for runtime in (settings, hydrated):
            profile = cloud_stt._profile_for(runtime)
            if flag is False:
                with pytest.raises(
                    cloud_stt.CloudSTTConfigurationError, match="Configure a database credential"
                ):
                    cloud_stt._api_key(runtime, profile)
            else:
                assert cloud_stt._api_key(runtime, profile) == "synthetic-legacy-azure-key"


@pytest.mark.parametrize("source", ("stt_api_key", "cloud_stt_api_key", "api_key", "profile"))
def test_false_environment_policy_preserves_explicit_private_credential_priority(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    settings: dict[str, Any] = {
        "stt_engine": "azure_mai_transcribe_2",
        "stt_allow_environment_credentials": False,
    }
    if source == "profile":
        settings["provider_configs"] = [
            {"id": "azure_mai_transcribe_2", "api_key": "synthetic-explicit-private-key"}
        ]
    else:
        settings[source] = "synthetic-explicit-private-key"
    environment = _forbidden_environment()
    with monkeypatch.context() as scoped:
        scoped.setattr(cloud_stt.os, "environ", environment)
        profile = cloud_stt._profile_for(settings)
        assert cloud_stt._api_key(settings, profile) == "synthetic-explicit-private-key"
    environment.get.assert_not_called()
