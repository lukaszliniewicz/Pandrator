"""Helper-unit host policy checks; SQL role setup is not account-command acceptance."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, call

import pytest
from sqlalchemy import event, update

from pandrator.runtime import DataPaths
from pandrator.web import credentials
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import User, Workspace
from pandrator.web.credential_identity import credential_namespace
from pandrator.web.database import Database
from pandrator.web.models import StoredCredential, WorkspaceSetting
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class HostPolicyCase:
    database: Database
    paths: DataPaths
    owner_id: str
    workspaces: dict[str, str]

    def retire_owner(self, state: str) -> None:
        # Deliberately isolated helper setup: disabled keeps its admin flag so
        # the two necessary conditions are tested independently.
        with self.database.immediate_session() as session:
            session.execute(
                update(User)
                .where(User.id == self.owner_id)
                .values(is_active=state != "disabled", is_instance_admin=state != "demoted")
            )

    def put(self, workspace: str, key: str, value: str) -> None:
        with self.database.session() as session:
            credentials.upsert_credential(
                session, key, "Synthetic", value, workspace_id=self.workspaces[workspace]
            )


@pytest.fixture
def host_case(tmp_path: Path) -> Iterator[HostPolicyCase]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    with database.immediate_session() as session:
        original = initial_personal_workspace(session)
        assert original.personal_owner_user_id is not None
        owner_id = original.personal_owner_user_id
        other_user = User(display_name="Synthetic other owner")
        session.add(other_user)
        session.flush()
        other = Workspace(
            kind="personal", name="Other personal", personal_owner_user_id=other_user.id
        )
        team = Workspace(kind="team", name="Synthetic team")
        session.add_all([other, team])
        session.flush()
        workspaces = {"original": original.id, "other": other.id, "team": team.id}
    try:
        yield HostPolicyCase(database, paths, owner_id, workspaces)
    finally:
        database.dispose()


@pytest.mark.parametrize("state", ("demoted", "disabled"))
def test_namespace_role_projection_is_fresh_with_unchanged_select_count(
    host_case: HostPolicyCase, state: str
) -> None:
    case = host_case
    statements: list[str] = []

    def capture(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: Any,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    with case.database.session() as session:
        cached_owner = session.get_one(User, case.owner_id)
        assert cached_owner.is_active and cached_owner.is_instance_admin
        event.listen(case.database.engine, "before_cursor_execute", capture)
        try:
            before = credential_namespace(session, case.workspaces["original"])
            initial_selects = list(statements)
            session.execute(
                update(User)
                .where(User.id == case.owner_id)
                .values(is_active=state != "disabled", is_instance_admin=state != "demoted")
                .execution_options(synchronize_session=False)
            )
            statements.clear()
            after = credential_namespace(session, case.workspaces["original"])
        finally:
            event.remove(case.database.engine, "before_cursor_execute", capture)
        assert cached_owner.is_active and cached_owner.is_instance_admin
    assert before.workspace_id == after.workspace_id == case.workspaces["original"]
    assert before.legacy_host_access is after.legacy_host_access is True
    assert before.host_credentials_allowed is True
    assert after.host_credentials_allowed is False
    assert before != after
    assert statements == initial_selects
    assert len(statements) == 2  # Existing workspace check plus the joined original-owner read.
    joined = [query.lower() for query in statements if "join users" in query.lower()]
    assert len(joined) == 1
    assert "users.is_active" in joined[0] and "users.is_instance_admin" in joined[0]


@pytest.mark.parametrize("state", ("demoted", "disabled"))
def test_database_credentials_and_rebinding_survive_original_owner_retirement(
    host_case: HostPolicyCase, state: str
) -> None:
    case = host_case
    for workspace in case.workspaces:
        case.put(workspace, "existing", f"synthetic-{workspace}-before")
    case.retire_owner(state)
    for workspace, workspace_id in case.workspaces.items():
        with case.database.session() as session:
            namespace = credential_namespace(session, workspace_id)
            assert namespace.legacy_host_access is (workspace == "original")
            assert namespace.host_credentials_allowed is False
            configured = credentials.configure_credential_reference(
                session,
                case.database,
                case.paths,
                key="replacement",
                label="Synthetic replacement",
                current_reference="db:existing",
                secret_value=f"synthetic-{workspace}-after",
                delete_previous=True,
                workspace_id=workspace_id,
            )
            assert configured.reference == "db:replacement"
            credentials.set_auxiliary_reference(
                session, "jina", configured.reference, workspace_id=workspace_id
            )
        resolved = credentials.resolve_secret_reference(
            case.database, case.paths, configured.reference, workspace_id=workspace_id
        )
        assert resolved.value == f"synthetic-{workspace}-after"
        with case.database.snapshot_session() as session:
            assert session.get(StoredCredential, (workspace_id, "existing")) is None
            assert credentials.auxiliary_reference_map(session, workspace_id=workspace_id) == {
                "jina": "db:replacement"
            }


@pytest.mark.parametrize("state", ("demoted", "disabled"))
@pytest.mark.parametrize(
    ("backend", "reference", "locator"),
    (
        ("environment", "env:PANDRATOR_SYNTHETIC_HOST", "PANDRATOR_SYNTHETIC_HOST"),
        ("keyring", "keyring:Pandrator/synthetic-host", ""),
        (
            "file",
            "file-path:/synthetic/never-read-host-secret",
            "/synthetic/never-read-host-secret",
        ),
    ),
)
def test_retired_owner_external_guards_precede_backend_effects(
    host_case: HostPolicyCase,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    backend: str,
    reference: str,
    locator: str,
) -> None:
    case = host_case
    case.put("original", "existing", "synthetic-retained-database-value")
    case.retire_owner(state)
    workspace_id = case.workspaces["original"]
    external = Mock(side_effect=AssertionError("Synthetic external effect attempted"))
    environment = MagicMock(wraps={})
    environment.get.side_effect = AssertionError("Synthetic host environment read attempted")
    monkeypatch.setattr(credentials.os, "environ", environment)
    monkeypatch.setattr(credentials, "_load_keyring", external)
    monkeypatch.setattr(credentials, "_read_secret_file", external)
    with case.database.session() as session:
        with pytest.raises(ValueError, match="Host credential bindings"):
            credentials.configure_credential_reference(
                session,
                case.database,
                case.paths,
                key="existing",
                label="Synthetic",
                backend=backend,
                locator=locator,
                secret_value="synthetic-external-submission",
                workspace_id=workspace_id,
            )
        with pytest.raises(ValueError, match="Host credential bindings"):
            credentials.delete_managed_reference(
                session, reference, preserve_shared=False, workspace_id=workspace_id
            )
        with pytest.raises(ValueError, match="Host credential bindings"):
            credentials.configure_credential_reference(
                session,
                case.database,
                case.paths,
                key="existing",
                label="Synthetic",
                current_reference=reference,
                secret_value="synthetic-forbidden-replacement",
                delete_previous=True,
                workspace_id=workspace_id,
            )
    with pytest.raises(ValueError, match="Host credential bindings"):
        credentials.resolve_secret_reference(
            case.database, case.paths, reference, workspace_id=workspace_id
        )
    with monkeypatch.context() as batch_patch:
        projection = Mock(side_effect=AssertionError("Premature secret batch projection"))
        batch_patch.setattr(credentials, "_project_database_credentials", projection)
        with pytest.raises(ValueError, match="Host credential bindings"):
            credentials.resolve_provider_credentials(
                case.database,
                case.paths,
                [("openai", "db:existing", "", False), ("openai", reference, "", False)],
                workspace_id=workspace_id,
            )
        projection.assert_not_called()
    with case.database.session() as session:
        with pytest.raises(ValueError, match="Host credential bindings"):
            credentials.set_auxiliary_reference(
                session, "jina", reference, workspace_id=workspace_id
            )
    for prepare, service_id in (
        (credentials.prepare_tts_settings_for_storage, "openai"),
        (credentials.prepare_stt_settings_for_storage, "azure_mai_transcribe_1_5"),
    ):
        with case.database.session() as session:
            with pytest.raises(ValueError, match="Host credential bindings"):
                prepare(
                    session,
                    case.database,
                    case.paths,
                    {"provider_configs": [{"id": service_id, "secret_ref": reference}]},
                    {},
                    workspace_id=workspace_id,
                )
    with case.database.snapshot_session() as session:
        row = session.get_one(StoredCredential, (workspace_id, "existing"))
        assert row.secret_value == "synthetic-retained-database-value" and row.revision == 1
        assert (
            session.get(WorkspaceSetting, (workspace_id, credentials.AUXILIARY_REFERENCES_SETTING))
            is None
        )
    external.assert_not_called()
    environment.get.assert_not_called()


@pytest.mark.parametrize("state", ("demoted", "disabled"))
@pytest.mark.parametrize("reference", ("", "db:missing"))
def test_missing_database_value_has_no_host_fallback_after_retirement(
    host_case: HostPolicyCase, monkeypatch: pytest.MonkeyPatch, state: str, reference: str
) -> None:
    case = host_case
    case.put("original", "retained", "synthetic-persisted-secret")
    case.retire_owner(state)
    environment = MagicMock(wraps={"PANDRATOR_SYNTHETIC_HOST": "synthetic-ambient-secret"})
    environment.get.side_effect = AssertionError("Ambient fallback was read")
    monkeypatch.setattr(credentials.os, "environ", environment)
    resolved = credentials.resolve_secret_reference(
        case.database,
        case.paths,
        reference,
        fallback_environment_variable="PANDRATOR_SYNTHETIC_HOST",
        workspace_id=case.workspaces["original"],
    )
    assert not resolved.configured and resolved.resolved_value() == ""
    assert resolved.environment_variable == ""
    # Raw-dict legacy preload compatibility survives even though host effects do not.
    loaded = credentials.resolve_secret_reference(
        case.database,
        case.paths,
        "db:retained",
        preloaded_database_credentials={"retained": "synthetic-legacy-preload"},
        workspace_id=case.workspaces["original"],
    )
    assert loaded.value == "synthetic-legacy-preload"
    persisted = credentials.resolve_secret_reference(
        case.database, case.paths, "db:retained", workspace_id=case.workspaces["original"]
    )
    assert persisted.value == "synthetic-persisted-secret"
    environment.get.assert_not_called()


@pytest.mark.parametrize("state", ("demoted", "disabled"))
def test_hydration_clears_host_environment_without_changing_legacy_manager_path(
    host_case: HostPolicyCase, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    case = host_case
    workspace_id = case.workspaces["original"]
    monkeypatch.setattr(credentials.os, "environ", {})
    bridge = Mock(configured=True)
    bridge.managed_service.return_value = {
        "endpoint": "http://synthetic-manager.invalid/xtts",
        "health": {"state": "healthy"},
    }
    tts_settings = {
        "service": "XTTS",
        "provider_configs": [
            {"id": "xtts", "connection_mode": "managed_local", "api_key_env": "SYNTHETIC_XTTS"},
            {"id": "openai", "api_key_env": "SYNTHETIC_UNSELECTED"},
        ],
    }
    stt_settings = {
        "stt_engine": "azure_mai_transcribe_2",
        "provider_configs": [{"id": "azure_mai_transcribe_2", "api_key_env": "SYNTHETIC_STT"}],
    }
    before = credentials.hydrate_tts_settings(
        case.database, case.paths, tts_settings, workspace_id=workspace_id, manager_bridge=bridge
    )
    assert before["provider_configs"][0]["api_key_env"] == "SYNTHETIC_XTTS"
    case.retire_owner(state)
    after = credentials.hydrate_tts_settings(
        case.database, case.paths, tts_settings, workspace_id=workspace_id, manager_bridge=bridge
    )
    assert after["provider_configs"][0]["api_key_env"] == ""
    assert after["provider_configs"][0]["connection_mode"] == "managed_local"
    assert after["provider_configs"][0]["api_base"] == before["provider_configs"][0]["api_base"]
    assert after["provider_configs"][1] == tts_settings["provider_configs"][1]
    assert after["service"] == "XTTS"
    assert bridge.managed_service.call_args_list == [call("tts.xtts"), call("tts.xtts")]
    hydrated_stt = credentials.hydrate_stt_settings(
        case.database, case.paths, stt_settings, workspace_id=workspace_id
    )
    assert hydrated_stt["stt_engine"] == stt_settings["stt_engine"]
    assert hydrated_stt["provider_configs"][0]["api_key_env"] == ""
    assert tts_settings["provider_configs"][0]["api_key_env"] == "SYNTHETIC_XTTS"
    assert stt_settings["provider_configs"][0]["api_key_env"] == "SYNTHETIC_STT"


@pytest.mark.parametrize("state", ("demoted", "disabled"))
def test_redactor_denies_retired_owner_file_and_keyring_but_keeps_database_and_scoped_values(
    host_case: HostPolicyCase, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    case = host_case
    case.put("original", "redaction", "synthetic-original-db-secret")
    case.put("team", "redaction", "synthetic-team-db-secret")
    references = {"file-path:/synthetic/never-read", "keyring:Pandrator/synthetic-redaction"}
    with case.database.session() as session:
        session.add(
            WorkspaceSetting(
                workspace_id=case.workspaces["original"],
                key="synthetic.host.references",
                value_json={"references": sorted(references)},
                revision=1,
            )
        )
    case.retire_owner(state)
    external = Mock(side_effect=AssertionError("Redactor attempted an external backend"))
    monkeypatch.setattr(credentials, "_load_keyring", external)
    monkeypatch.setattr(credentials, "_read_secret_file", external)
    monkeypatch.setattr(credentials.os, "environ", {"OPENAI_API_KEY": "synthetic-known-env-secret"})
    resolver = Mock(wraps=credentials.resolve_secret_reference)
    monkeypatch.setattr(credentials, "resolve_secret_reference", resolver)
    redactor = credentials.SecretRedactor(case.database, case.paths)
    values = set(redactor.values())
    assert {
        "synthetic-original-db-secret",
        "synthetic-team-db-secret",
        "synthetic-known-env-secret",
    } <= values
    assert {call.args[2] for call in resolver.call_args_list} == references
    assert all(
        call.kwargs["workspace_id"] == case.workspaces["original"]
        for call in resolver.call_args_list
    )
    external.assert_not_called()
    with redactor.scoped_values(["synthetic-scoped-secret"]):
        text = redactor.redact(" ".join([*sorted(values), "synthetic-scoped-secret"]))
        assert all(secret not in text for secret in (*values, "synthetic-scoped-secret"))
