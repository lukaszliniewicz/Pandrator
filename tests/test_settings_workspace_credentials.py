from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy import select, update

from pandrator.runtime import DataPaths
from pandrator.web import credentials
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import User, Workspace
from pandrator.web.database import Database
from pandrator.web.models import (
    AppSetting,
    SessionRecord,
    StoredCredential,
    WorkspaceSetting,
    WorkspaceSettingHistory,
)
from pandrator.web.projects.identity import create_session_project
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class SettingsCredentialCase:
    database: Database
    paths: DataPaths
    workspaces: dict[str, str]
    sessions: dict[str, str]

    def put(self, owner: str, key: str, value: str) -> None:
        with self.database.session() as session:
            credentials.upsert_credential(
                session,
                key,
                "Synthetic",
                value,
                workspace_id=self.workspaces[owner],
            )


@pytest.fixture
def settings_credential_case(tmp_path: Path):
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    workspaces: dict[str, str] = {}
    sessions: dict[str, str] = {}
    with database.session() as session:
        personal = initial_personal_workspace(session)
        workspaces["personal"] = personal.id
        for owner in ("alpha", "beta"):
            workspace = Workspace(kind="team", name=f"Workspace {owner}")
            session.add(workspace)
            session.flush([workspace])
            workspaces[owner] = workspace.id
            record = SessionRecord(
                name=f"Session {owner}",
                storage_key=f"settings-credentials-{owner}",
                workflow_kind="subtitles",
                source_language="en",
            )
            create_session_project(session, record, workspace_id=workspace.id)
            sessions[owner] = record.id
    try:
        yield SettingsCredentialCase(database, paths, workspaces, sessions)
    finally:
        database.dispose()


def _tts_value(service_id: str, **fields: Any) -> dict[str, Any]:
    return {"provider_configs": [{"id": service_id, **fields}]}


def _stt_value(*records: dict[str, Any]) -> dict[str, Any]:
    return {"provider_configs": list(records)}


def test_tts_prepare_and_clear_are_isolated_for_same_credential_id(
    settings_credential_case: SettingsCredentialCase,
) -> None:
    case = settings_credential_case
    prepared: dict[str, dict[str, Any]] = {}
    for owner in ("alpha", "beta"):
        with case.database.session() as session:
            prepared[owner] = credentials.prepare_tts_settings_for_storage(
                session,
                case.database,
                case.paths,
                _tts_value("openai", api_key=f"synthetic-{owner}-tts"),
                {},
                workspace_id=case.workspaces[owner],
            )
    assert prepared["alpha"]["provider_configs"][0]["secret_ref"] == "db:shared:openai"
    assert prepared["beta"]["provider_configs"][0]["secret_ref"] == "db:shared:openai"

    with case.database.session() as session:
        cleared = credentials.prepare_tts_settings_for_storage(
            session,
            case.database,
            case.paths,
            _tts_value("openai", clear_api_key=True),
            prepared["alpha"],
            workspace_id=case.workspaces["alpha"],
        )
    assert "secret_ref" not in cleared["provider_configs"][0]
    for owner, expected in (("alpha", ""), ("beta", "synthetic-beta-tts")):
        resolved = credentials.resolve_secret_reference(
            case.database,
            case.paths,
            "db:shared:openai",
            workspace_id=case.workspaces[owner],
        )
        assert resolved.value == expected


def test_stt_sibling_retention_is_local_to_the_owning_workspace(
    settings_credential_case: SettingsCredentialCase,
) -> None:
    case = settings_credential_case
    with case.database.session() as session:
        alpha = credentials.prepare_stt_settings_for_storage(
            session,
            case.database,
            case.paths,
            _stt_value(
                {"id": "azure_mai_transcribe_1_5", "api_key": "synthetic-alpha-stt"},
                {"id": "azure_mai_transcribe_2"},
            ),
            {},
            workspace_id=case.workspaces["alpha"],
        )
    with case.database.session() as session:
        beta = credentials.prepare_stt_settings_for_storage(
            session,
            case.database,
            case.paths,
            _stt_value({"id": "azure_mai_transcribe_1_5", "api_key": "synthetic-beta-stt"}),
            {},
            workspace_id=case.workspaces["beta"],
        )

    with case.database.session() as session:
        alpha_after_clear = credentials.prepare_stt_settings_for_storage(
            session,
            case.database,
            case.paths,
            _stt_value(
                {"id": "azure_mai_transcribe_1_5", "clear_api_key": True},
                {"id": "azure_mai_transcribe_2"},
            ),
            alpha,
            workspace_id=case.workspaces["alpha"],
        )
    key = "db:stt:azure_mai_transcribe_1_5"
    assert (
        credentials.resolve_secret_reference(
            case.database, case.paths, key, workspace_id=case.workspaces["alpha"]
        ).value
        == "synthetic-alpha-stt"
    )
    assert (
        credentials.resolve_secret_reference(
            case.database, case.paths, key, workspace_id=case.workspaces["beta"]
        ).value
        == "synthetic-beta-stt"
    )

    with case.database.session() as session:
        credentials.prepare_stt_settings_for_storage(
            session,
            case.database,
            case.paths,
            _stt_value(),
            alpha_after_clear,
            workspace_id=case.workspaces["alpha"],
        )
    assert not credentials.resolve_secret_reference(
        case.database, case.paths, key, workspace_id=case.workspaces["alpha"]
    ).configured
    assert (
        credentials.resolve_secret_reference(
            case.database, case.paths, key, workspace_id=case.workspaces["beta"]
        ).value
        == "synthetic-beta-stt"
    )
    assert beta["provider_configs"][0]["secret_ref"] == key


@pytest.mark.parametrize("kind", ("tts", "stt"))
def test_failed_settings_batch_rolls_back_earlier_keys_and_caller_writes(
    settings_credential_case: SettingsCredentialCase, kind: str
) -> None:
    case = settings_credential_case
    workspace_id = case.workspaces["alpha"]
    key = "shared:openai" if kind == "tts" else "stt:azure_mai_transcribe_1_5"
    with case.database.session() as session:
        session.add(AppSetting(key="rollback-synthetic-marker", value_json={}, revision=1))
        with pytest.raises(ValueError):
            if kind == "tts":
                credentials.prepare_tts_settings_for_storage(
                    session,
                    case.database,
                    case.paths,
                    {
                        "provider_configs": [
                            {"id": "openai", "api_key": "synthetic-first-write"},
                            {
                                "id": "gemini",
                                "api_key": "synthetic-second-write",
                                "credential_backend": "unsupported",
                            },
                        ]
                    },
                    {},
                    workspace_id=workspace_id,
                )
            else:
                credentials.prepare_stt_settings_for_storage(
                    session,
                    case.database,
                    case.paths,
                    _stt_value(
                        {
                            "id": "azure_mai_transcribe_1_5",
                            "api_key": "synthetic-first-write",
                        },
                        {"id": "unsupported_stt", "api_key": "synthetic-second-write"},
                    ),
                    {},
                    workspace_id=workspace_id,
                )
    with case.database.snapshot_session() as session:
        assert session.get(StoredCredential, (workspace_id, key)) is None
        assert session.get(AppSetting, "rollback-synthetic-marker") is None


def test_hydration_uses_typed_session_owner_and_rejects_workspace_mismatch(
    settings_credential_case: SettingsCredentialCase,
) -> None:
    case = settings_credential_case
    for owner in ("alpha", "beta"):
        case.put(owner, "stt:azure_mai_transcribe_1_5", f"synthetic-{owner}-stt")
        case.put(owner, "shared:openai", f"synthetic-{owner}-tts")
    stt_settings = _stt_value(
        {"id": "azure_mai_transcribe_1_5", "secret_ref": "db:stt:azure_mai_transcribe_1_5"}
    )
    stt_settings["stt_engine"] = "azure_mai_transcribe_1_5"
    tts_settings = {
        "service": "openai",
        "provider_configs": [
            {
                "id": "openai",
                "connection_mode": "external",
                "secret_ref": "db:shared:openai",
                "api_key_env": "OPENAI_API_KEY",
            }
        ],
    }
    hydrated_stt = credentials.hydrate_stt_settings(
        case.database,
        case.paths,
        stt_settings,
        session_id=case.sessions["alpha"],
    )
    hydrated_tts = credentials.hydrate_tts_settings(
        case.database,
        case.paths,
        tts_settings,
        session_id=case.sessions["alpha"],
    )
    assert hydrated_stt["provider_configs"][0]["api_key"] == "synthetic-alpha-stt"
    assert hydrated_tts["provider_configs"][0]["api_key"] == "synthetic-alpha-tts"
    with pytest.raises(ValueError, match="workspace"):
        credentials.hydrate_stt_settings(
            case.database,
            case.paths,
            stt_settings,
            session_id=case.sessions["alpha"],
            workspace_id=case.workspaces["beta"],
        )


class _ForbiddenEnvironment(dict[str, str]):
    def get(self, key: str, default: Any = None) -> Any:
        raise AssertionError(f"host environment read attempted for {key}")

    def __getitem__(self, key: str) -> str:
        raise AssertionError(f"host environment read attempted for {key}")


def test_noninitial_hydration_denies_host_references_and_clears_ambient_fallback(
    settings_credential_case: SettingsCredentialCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = settings_credential_case
    file_read = Mock(side_effect=AssertionError("secret file read attempted"))
    keyring_load = Mock(side_effect=AssertionError("keyring read attempted"))
    with monkeypatch.context() as context:
        context.setattr(credentials.os, "environ", _ForbiddenEnvironment())
        context.setattr(credentials, "_read_secret_file", file_read)
        context.setattr(credentials, "_load_keyring", keyring_load)
        for reference in (
            "env:SYNTHETIC_HOST_KEY",
            "file-path:/tmp/pandrator-synthetic-secret",
            "keyring:Pandrator/synthetic-host-key",
        ):
            with pytest.raises(ValueError, match="Host credential bindings"):
                credentials.resolve_secret_reference(
                    case.database,
                    case.paths,
                    reference,
                    workspace_id=case.workspaces["alpha"],
                )
        stt_settings = _stt_value(
            {
                "id": "azure_mai_transcribe_1_5",
                "secret_ref": "db:missing-synthetic-key",
                "api_key_env": "AZURE_SPEECH_KEY",
            }
        )
        stt_settings["stt_engine"] = "azure_mai_transcribe_1_5"
        hydrated = credentials.hydrate_stt_settings(
            case.database,
            case.paths,
            stt_settings,
            session_id=case.sessions["alpha"],
        )
    assert hydrated["provider_configs"][0]["api_key_env"] == ""
    assert "api_key" not in hydrated["provider_configs"][0]
    file_read.assert_not_called()
    keyring_load.assert_not_called()


@pytest.mark.parametrize("kind", ("tts", "stt"))
@pytest.mark.parametrize("reference_source", ("submitted", "retained"))
@pytest.mark.parametrize(
    "reference",
    (
        "env:SYNTHETIC_HOST_KEY",
        "file-path:/tmp/pandrator-synthetic-secret",
        "keyring:Pandrator/synthetic-host-key",
    ),
)
def test_noninitial_prepare_rejects_direct_host_references_atomically(
    settings_credential_case: SettingsCredentialCase,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    reference_source: str,
    reference: str,
) -> None:
    case = settings_credential_case
    workspace_id = case.workspaces["alpha"]
    service_id = "openai" if kind == "tts" else "azure_mai_transcribe_1_5"
    submitted_record = {"id": service_id}
    previous_record = {"id": service_id}
    if reference_source == "submitted":
        submitted_record["secret_ref"] = reference
    else:
        previous_record["secret_ref"] = reference
    submitted = (
        _tts_value(
            service_id, **{key: value for key, value in submitted_record.items() if key != "id"}
        )
        if kind == "tts"
        else _stt_value(submitted_record)
    )
    previous = (
        _tts_value(
            service_id, **{key: value for key, value in previous_record.items() if key != "id"}
        )
        if kind == "tts"
        else _stt_value(previous_record)
    )
    marker = "noninitial-host-reference-rollback"
    configure = Mock(side_effect=AssertionError("credential configuration attempted"))
    file_read = Mock(side_effect=AssertionError("secret file read attempted"))
    keyring_load = Mock(side_effect=AssertionError("keyring read attempted"))
    monkeypatch.setattr(credentials, "configure_credential_reference", configure)
    monkeypatch.setattr(credentials, "_read_secret_file", file_read)
    monkeypatch.setattr(credentials, "_load_keyring", keyring_load)
    with monkeypatch.context() as scoped:
        scoped.setattr(credentials.os, "environ", _ForbiddenEnvironment())
        with case.database.session() as session:
            session.add(AppSetting(key=marker, value_json={}, revision=1))
            with pytest.raises(ValueError, match="Host credential bindings"):
                prepare = (
                    credentials.prepare_tts_settings_for_storage
                    if kind == "tts"
                    else credentials.prepare_stt_settings_for_storage
                )
                prepare(
                    session,
                    case.database,
                    case.paths,
                    submitted,
                    previous,
                    workspace_id=workspace_id,
                )

    with case.database.snapshot_session() as session:
        assert session.get(AppSetting, marker) is None
        assert (
            session.scalar(
                select(StoredCredential.key).where(StoredCredential.workspace_id == workspace_id)
            )
            is None
        )
    configure.assert_not_called()
    file_read.assert_not_called()
    keyring_load.assert_not_called()


@pytest.mark.parametrize(
    "reference",
    (
        "env:SYNTHETIC_HOST_KEY",
        "file-path:/tmp/pandrator-synthetic-secret",
        "keyring:Pandrator/synthetic-host-key",
    ),
)
def test_noninitial_auxiliary_setter_rejects_host_references_before_mutation(
    settings_credential_case: SettingsCredentialCase,
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
) -> None:
    case = settings_credential_case
    workspace_id = case.workspaces["alpha"]
    marker = "auxiliary-host-reference-rollback"
    configure = Mock(side_effect=AssertionError("credential configuration attempted"))
    file_read = Mock(side_effect=AssertionError("secret file read attempted"))
    keyring_load = Mock(side_effect=AssertionError("keyring read attempted"))
    monkeypatch.setattr(credentials, "configure_credential_reference", configure)
    monkeypatch.setattr(credentials, "_read_secret_file", file_read)
    monkeypatch.setattr(credentials, "_load_keyring", keyring_load)
    with monkeypatch.context() as scoped:
        scoped.setattr(credentials.os, "environ", _ForbiddenEnvironment())
        with case.database.session() as session:
            session.add(AppSetting(key=marker, value_json={}, revision=1))
            with pytest.raises(ValueError, match="Host credential bindings"):
                credentials.set_auxiliary_reference(
                    session, "jina", reference, workspace_id=workspace_id
                )

    with case.database.snapshot_session() as session:
        assert session.get(AppSetting, marker) is None
        assert (
            session.get(
                WorkspaceSetting,
                (workspace_id, credentials.AUXILIARY_REFERENCES_SETTING),
            )
            is None
        )
        assert (
            session.scalar(
                select(StoredCredential.key).where(StoredCredential.workspace_id == workspace_id)
            )
            is None
        )
    configure.assert_not_called()
    file_read.assert_not_called()
    keyring_load.assert_not_called()


def test_noninitial_managed_local_paths_are_gated_before_manager_io(
    settings_credential_case: SettingsCredentialCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = settings_credential_case

    def fail_manager_construction() -> Any:
        raise AssertionError("Manager bridge construction attempted")

    from pandrator.web import manager_proxy

    monkeypatch.setattr(manager_proxy, "LocalManagerProxy", fail_manager_construction)
    with pytest.raises(ValueError, match="host-approved binding"):
        credentials.hydrate_tts_settings(
            case.database,
            case.paths,
            {"service": "XTTS"},
            session_id=case.sessions["alpha"],
        )
    external = credentials.hydrate_tts_settings(
        case.database,
        case.paths,
        {
            "service": "XTTS",
            "provider_configs": [{"id": "xtts", "connection_mode": "external"}],
        },
        session_id=case.sessions["alpha"],
    )
    assert external["provider_configs"][0]["connection_mode"] == "external"


def test_auxiliary_maps_are_scoped_versioned_and_keep_other_profile_keys(
    settings_credential_case: SettingsCredentialCase,
) -> None:
    case = settings_credential_case
    with case.database.session() as session:
        session.add(
            WorkspaceSetting(
                workspace_id=case.workspaces["alpha"],
                key=credentials.AUXILIARY_REFERENCES_SETTING,
                value_json={"jina": "db:alpha-jina", "unknown": "db:raw-prior"},
                revision=1,
            )
        )
    with case.database.session() as session:
        credentials.set_auxiliary_reference(
            session,
            "jina",
            "db:alpha-jina-v2",
            workspace_id=case.workspaces["alpha"],
        )
        credentials.set_auxiliary_reference(
            session,
            "deepl",
            "db:alpha-deepl",
            workspace_id=case.workspaces["alpha"],
        )
    with case.database.session() as session:
        credentials.set_auxiliary_reference(
            session, "jina", "db:beta-jina", workspace_id=case.workspaces["beta"]
        )
    with case.database.snapshot_session() as session:
        alpha_map = credentials.auxiliary_reference_map(
            session, workspace_id=case.workspaces["alpha"]
        )
        beta_map = credentials.auxiliary_reference_map(
            session, workspace_id=case.workspaces["beta"]
        )
        alpha_record = session.get(
            WorkspaceSetting,
            (case.workspaces["alpha"], credentials.AUXILIARY_REFERENCES_SETTING),
        )
        alpha_history = list(
            session.scalars(
                select(WorkspaceSettingHistory)
                .where(
                    WorkspaceSettingHistory.workspace_id == case.workspaces["alpha"],
                    WorkspaceSettingHistory.key == credentials.AUXILIARY_REFERENCES_SETTING,
                )
                .order_by(WorkspaceSettingHistory.revision)
            ).all()
        )
    assert alpha_map == {
        "jina": "db:alpha-jina-v2",
        "deepl": "db:alpha-deepl",
        "unknown": "db:raw-prior",
    }
    assert beta_map == {"jina": "db:beta-jina"}
    assert alpha_record is not None
    assert alpha_record.revision == 3
    assert [item.revision for item in alpha_history] == [1, 2]
    assert [item.value_json for item in alpha_history] == [
        {"jina": "db:alpha-jina", "unknown": "db:raw-prior"},
        {
            "jina": "db:alpha-jina-v2",
            "unknown": "db:raw-prior",
        },
    ]
    with case.database.snapshot_session() as session:
        beta_history = list(
            session.scalars(
                select(WorkspaceSettingHistory).where(
                    WorkspaceSettingHistory.workspace_id == case.workspaces["beta"],
                    WorkspaceSettingHistory.key == credentials.AUXILIARY_REFERENCES_SETTING,
                )
            ).all()
        )
    assert beta_history == []


def test_auxiliary_map_reads_fresh_after_cached_state_and_serializes_concurrent_writes(
    settings_credential_case: SettingsCredentialCase,
) -> None:
    case = settings_credential_case
    workspace_id = case.workspaces["alpha"]
    with case.database.session() as session:
        credentials.set_auxiliary_reference(
            session, "jina", "db:initial", workspace_id=workspace_id
        )
    with case.database.session() as stale_session:
        cached = credentials.workspace_setting_in_workspace(
            stale_session, workspace_id, credentials.AUXILIARY_REFERENCES_SETTING
        )
        assert cached is not None
        assert cached.value_json == {"jina": "db:initial"}
        stale_session.rollback()
        with case.database.session() as writer:
            credentials.set_auxiliary_reference(
                writer, "jina", "db:concurrent-update", workspace_id=workspace_id
            )
        credentials.set_auxiliary_reference(
            stale_session, "deepl", "db:stale-session-addition", workspace_id=workspace_id
        )
        fresh_map = credentials.auxiliary_reference_map(stale_session, workspace_id=workspace_id)
        assert fresh_map == {
            "jina": "db:concurrent-update",
            "deepl": "db:stale-session-addition",
        }

    barrier = threading.Barrier(2)

    def add_reference(credential_id: str) -> None:
        barrier.wait(timeout=10)
        with case.database.session() as session:
            credentials.set_auxiliary_reference(
                session,
                credential_id,
                f"db:concurrent-{credential_id}",
                workspace_id=workspace_id,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(add_reference, ("jina", "deepl")))
    with case.database.snapshot_session() as session:
        final_map = credentials.auxiliary_reference_map(session, workspace_id=workspace_id)
    assert final_map == {
        "jina": "db:concurrent-jina",
        "deepl": "db:concurrent-deepl",
    }


def test_auxiliary_resolution_uses_selected_profile_reference_and_typed_owner(
    settings_credential_case: SettingsCredentialCase,
) -> None:
    case = settings_credential_case
    case.put("alpha", "synthetic-picked-deepl", "synthetic-alpha-deepl")
    case.put("beta", "synthetic-picked-deepl", "synthetic-beta-deepl")
    case.put("beta", credentials.auxiliary_credential_key("deepl"), "synthetic-beta-default")
    with case.database.session() as session:
        credentials.set_auxiliary_reference(
            session,
            "deepl",
            "db:synthetic-picked-deepl",
            workspace_id=case.workspaces["alpha"],
        )
    alpha = credentials.resolve_auxiliary_credential(
        case.database, case.paths, "deepl", session_id=case.sessions["alpha"]
    )
    beta = credentials.resolve_auxiliary_credential(
        case.database,
        case.paths,
        "deepl",
        session_id=case.sessions["beta"],
    )
    assert alpha.value == "synthetic-alpha-deepl"
    assert beta.value == "synthetic-beta-default"
    with pytest.raises(ValueError, match="workspace"):
        credentials.resolve_auxiliary_credential(
            case.database,
            case.paths,
            "deepl",
            session_id=case.sessions["alpha"],
            workspace_id=case.workspaces["beta"],
        )
    with pytest.raises(ValueError, match="Unsupported auxiliary"):
        credentials.resolve_auxiliary_credential(case.database, case.paths, "synthetic-unknown")


def test_auxiliary_profile_status_uses_each_workspace_reference_map(
    settings_credential_case: SettingsCredentialCase,
) -> None:
    case = settings_credential_case
    case.put("beta", "synthetic-beta-jina", "synthetic-beta-jina-value")
    with case.database.session() as session:
        credentials.set_auxiliary_reference(
            session,
            "jina",
            "db:synthetic-missing-jina",
            workspace_id=case.workspaces["alpha"],
        )
        credentials.set_auxiliary_reference(
            session,
            "jina",
            "db:synthetic-beta-jina",
            workspace_id=case.workspaces["beta"],
        )
    alpha_profiles = credentials.auxiliary_profiles(
        case.database, case.paths, workspace_id=case.workspaces["alpha"]
    )
    beta_profiles = credentials.auxiliary_profiles(
        case.database, case.paths, workspace_id=case.workspaces["beta"]
    )
    alpha_jina = next(item for item in alpha_profiles if item["id"] == "jina")
    beta_jina = next(item for item in beta_profiles if item["id"] == "jina")
    assert alpha_jina["secret_ref"] == "db:synthetic-missing-jina"
    assert alpha_jina["credential_configured"] is False
    assert beta_jina["secret_ref"] == "db:synthetic-beta-jina"
    assert beta_jina["credential_configured"] is True


def test_auxiliary_set_failure_rolls_back_caller_changes(
    settings_credential_case: SettingsCredentialCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = settings_credential_case
    workspace_id = case.workspaces["alpha"]
    original = credentials.credential_namespace
    admissions = 0

    def archive_after_admission(session: Any, selected_workspace_id: str | None = None):
        nonlocal admissions
        namespace = original(session, selected_workspace_id)
        admissions += 1
        if admissions == 1:
            with case.database.immediate_session() as other:
                other.execute(
                    update(Workspace).where(Workspace.id == workspace_id).values(state="archived")
                )
        return namespace

    monkeypatch.setattr(credentials, "credential_namespace", archive_after_admission)
    with case.database.session() as session:
        session.add(AppSetting(key="auxiliary-rollback-marker", value_json={}, revision=1))
        with pytest.raises(ValueError, match="archived|inactive"):
            credentials.set_auxiliary_reference(
                session, "jina", "db:synthetic-reference", workspace_id=workspace_id
            )
    with case.database.snapshot_session() as session:
        assert session.get(AppSetting, "auxiliary-rollback-marker") is None
        assert (
            session.get(
                WorkspaceSetting,
                (workspace_id, credentials.AUXILIARY_REFERENCES_SETTING),
            )
            is None
        )


def test_redactor_keeps_all_database_values_without_resolving_foreign_host_references(
    settings_credential_case: SettingsCredentialCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = settings_credential_case
    case.put("beta", "redactor-team-db", "synthetic-team-db-secret")
    with case.database.session() as session:
        session.add_all(
            [
                WorkspaceSetting(
                    workspace_id=case.workspaces["personal"],
                    key="synthetic.initial.references",
                    value_json={"credential": "env:SYNTHETIC_PERSONAL_HOST"},
                    revision=1,
                ),
                WorkspaceSetting(
                    workspace_id=case.workspaces["beta"],
                    key="synthetic.team.references",
                    value_json={"credential": "env:SYNTHETIC_TEAM_HOST"},
                    revision=1,
                ),
            ]
        )
    monkeypatch.setattr(
        credentials.os,
        "environ",
        {
            "SYNTHETIC_PERSONAL_HOST": "synthetic-personal-host-secret",
            "SYNTHETIC_TEAM_HOST": "synthetic-team-host-secret",
        },
    )
    resolver = Mock(wraps=credentials.resolve_secret_reference)
    monkeypatch.setattr(credentials, "resolve_secret_reference", resolver)
    redactor = credentials.SecretRedactor(case.database, case.paths)
    values = set(redactor.values())
    assert "synthetic-team-db-secret" in values
    assert "synthetic-personal-host-secret" in values
    assert "synthetic-team-host-secret" not in values
    assert any(call.args[2] == "env:SYNTHETIC_PERSONAL_HOST" for call in resolver.call_args_list)
    assert not any(call.args[2] == "env:SYNTHETIC_TEAM_HOST" for call in resolver.call_args_list)


def test_redactor_keeps_team_database_values_when_initial_user_is_disabled(
    settings_credential_case: SettingsCredentialCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = settings_credential_case
    case.put("beta", "disabled-owner-redactor", "synthetic-active-team-secret")
    monkeypatch.setattr(credentials.os, "environ", {})
    with case.database.session() as session:
        session.execute(
            update(User)
            .where(User.legacy_subject == "owner")
            .values(is_active=False, is_instance_admin=False)
        )
        team = session.get(Workspace, case.workspaces["beta"])
        assert team is not None
        assert team.state == "active"

    values = set(credentials.SecretRedactor(case.database, case.paths).values())
    assert "synthetic-active-team-secret" in values
