"""Credential namespaces separate database values and ambient host authority."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy import event, select, update

from pandrator.runtime import DataPaths
from pandrator.web import credentials
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.credential_identity import CredentialNamespace, credential_namespace
from pandrator.web.database import Database
from pandrator.web.models import AppSetting, StoredCredential, User, Workspace
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class CredentialCase:
    database: Database
    paths: DataPaths
    workspaces: dict[str, str]

    def put(self, workspace: str, key: str, value: str, label: str = "Synthetic") -> None:
        with self.database.session() as session:
            credentials.upsert_credential(
                session, key, label, value, workspace_id=self.workspaces[workspace]
            )

    def resolve(
        self, workspace: str | None, key: str, **options: Any
    ) -> credentials.ResolvedCredential:
        return credentials.resolve_secret_reference(
            self.database,
            self.paths,
            credentials.database_reference(key),
            workspace_id=self.workspaces[workspace] if workspace else None,
            **options,
        )


@pytest.fixture
def credential_case(tmp_path: Path) -> Iterator[CredentialCase]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    with database.immediate_session() as session:
        workspaces = {"personal": initial_personal_workspace(session).id}
        for name in ("alpha", "beta"):
            workspace = Workspace(kind="team", name=name)
            session.add(workspace)
            session.flush()
            workspaces[name] = workspace.id
    try:
        yield CredentialCase(database, paths, workspaces)
    finally:
        database.dispose()


def test_same_logical_reference_selects_only_the_requested_namespace(
    credential_case: CredentialCase,
) -> None:
    case = credential_case
    for name in case.workspaces:
        case.put(name, "same-key", f"synthetic-{name}-value")
    assert case.resolve(None, "same-key").value == "synthetic-personal-value"
    for name in case.workspaces:
        assert case.resolve(name, "same-key").value == f"synthetic-{name}-value"
    with case.database.session() as session:
        assert credentials.delete_credential(
            session, "same-key", workspace_id=case.workspaces["alpha"]
        )
        assert not credentials.delete_credential(
            session, "same-key", workspace_id=case.workspaces["alpha"]
        )
    assert not case.resolve("alpha", "same-key").configured
    assert case.resolve("beta", "same-key").configured
    assert case.resolve(None, "same-key").configured


def test_atomic_upsert_preserves_creation_time_and_counts_revisions(
    credential_case: CredentialCase,
) -> None:
    case = credential_case
    case.put("alpha", "revision-key", " synthetic-first ", "Original label")
    with case.database.session() as session:
        original = session.get_one(StoredCredential, (case.workspaces["alpha"], "revision-key"))
        created = original.created_at
        assert original.revision == 1 and original.secret_value == "synthetic-first"
        changed = credentials.upsert_credential(
            session, "revision-key", "", "synthetic-second", workspace_id=case.workspaces["alpha"]
        )
        assert changed.revision == 2 and changed.label == "Original label"
        assert changed.created_at == created and changed.secret_value == "synthetic-second"
    barrier = threading.Barrier(2)

    def write(index: int) -> None:
        barrier.wait(timeout=10)
        case.put("alpha", "concurrent-key", f"synthetic-writer-{index}")

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(write, (1, 2)))
    with case.database.snapshot_session() as session:
        row = session.get_one(StoredCredential, (case.workspaces["alpha"], "concurrent-key"))
        assert row.revision == 2
        assert row.secret_value in {"synthetic-writer-1", "synthetic-writer-2"}


@pytest.mark.parametrize("action", ("upsert", "delete"))
def test_archive_during_writer_admission_rolls_back_even_when_caught(
    credential_case: CredentialCase, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    case = credential_case
    case.put("alpha", "racing-key", "synthetic-before")
    original = credentials.credential_namespace
    admissions = 0

    def archive_after_admission(
        session: Any, workspace_id: str | None = None
    ) -> CredentialNamespace:
        nonlocal admissions
        namespace = original(session, workspace_id)
        admissions += 1
        if admissions == 1:
            with case.database.immediate_session() as other:
                other.execute(
                    update(Workspace)
                    .where(Workspace.id == case.workspaces["alpha"])
                    .values(state="archived")
                )
        return namespace

    monkeypatch.setattr(credentials, "credential_namespace", archive_after_admission)
    with case.database.session() as session:
        session.add(AppSetting(key="rollback-credential-marker", value_json={}, revision=1))
        with pytest.raises(ValueError, match="archived|inactive"):
            if action == "upsert":
                credentials.upsert_credential(
                    session,
                    "racing-key",
                    "Replacement",
                    "synthetic-after",
                    workspace_id=case.workspaces["alpha"],
                )
            else:
                credentials.delete_credential(
                    session, "racing-key", workspace_id=case.workspaces["alpha"]
                )
    with case.database.snapshot_session() as session:
        row = session.get_one(StoredCredential, (case.workspaces["alpha"], "racing-key"))
        assert row.secret_value == "synthetic-before" and row.revision == 1
        assert session.get(AppSetting, "rollback-credential-marker") is None
        assert session.get_one(Workspace, case.workspaces["alpha"]).state == "archived"


def test_missing_and_archived_namespaces_never_inherit_solo_credentials(
    credential_case: CredentialCase,
) -> None:
    case = credential_case
    case.put("personal", "solo-only", "synthetic-solo-only")
    assert not case.resolve("alpha", "solo-only").configured
    with pytest.raises(ValueError, match="missing"):
        credentials.resolve_secret_reference(
            case.database, case.paths, "db:solo-only", workspace_id="missing-workspace"
        )
    with case.database.immediate_session() as session:
        session.execute(
            update(Workspace)
            .where(Workspace.id == case.workspaces["alpha"])
            .values(state="archived")
        )
    with pytest.raises(ValueError, match="archived|inactive"):
        case.resolve("alpha", "solo-only")


def test_explicit_workspace_does_not_require_the_original_user_to_remain_active(
    credential_case: CredentialCase,
) -> None:
    case = credential_case
    case.put("alpha", "active-team", "synthetic-team-value")
    with case.database.immediate_session() as session:
        session.execute(update(User).where(User.legacy_subject == "owner").values(is_active=False))
    assert case.resolve("alpha", "active-team").configured
    with pytest.raises(RuntimeError, match="owner user"):
        case.resolve(None, "active-team")


def test_shared_provider_batch_precedence_and_deletion_are_workspace_local(
    credential_case: CredentialCase,
) -> None:
    case = credential_case
    shared = credentials.shared_provider_credential_key("openai")
    for name in ("personal", "alpha", "beta"):
        case.put(name, shared, f"synthetic-shared-{name}")
        case.put(name, "direct-openai", f"synthetic-direct-{name}")
    inputs = [
        ("openai", "db:direct-openai", "", True),
        ("openai", "db:direct-openai", "", False),
        ("openai", "db:absent", "PANDRATOR_SYNTHETIC_KEY", False),
    ]
    resolved = credentials.resolve_provider_credentials(
        case.database, case.paths, inputs, workspace_id=case.workspaces["alpha"]
    )
    assert [item.resolved_value() for item in resolved] == [
        "synthetic-shared-alpha",
        "synthetic-direct-alpha",
        "",
    ]
    with case.database.session() as session:
        assert not credentials.delete_managed_reference(
            session, credentials.database_reference(shared), workspace_id=case.workspaces["alpha"]
        )
        assert credentials.delete_managed_reference(
            session,
            credentials.database_reference(shared),
            preserve_shared=False,
            workspace_id=case.workspaces["alpha"],
        )
    assert case.resolve("beta", shared).value == "synthetic-shared-beta"
    assert case.resolve(None, shared).value == "synthetic-shared-personal"


@pytest.mark.parametrize("workspace", (None, "alpha"))
def test_batch_namespace_validation_has_fixed_cost_and_one_secret_projection(
    credential_case: CredentialCase, workspace: str | None
) -> None:
    case = credential_case
    case.put(workspace or "personal", "batch-key", "synthetic-batch-value")

    def measure(size: int) -> list[str]:
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

        event.listen(case.database.engine, "before_cursor_execute", capture)
        try:
            resolved = credentials.resolve_provider_credentials(
                case.database,
                case.paths,
                [("openai", "db:batch-key", "", False)] * size,
                workspace_id=case.workspaces[workspace] if workspace else None,
            )
        finally:
            event.remove(case.database.engine, "before_cursor_execute", capture)
        assert len(resolved) == size
        assert all(value.value == "synthetic-batch-value" for value in resolved)
        assert sum("from stored_credentials" in query.lower() for query in statements) == 1
        return statements

    single, batch = measure(1), measure(20)
    assert len(single) == len(batch)
    # One read of the secret projection plus the fixed current-SQL namespace reads.
    assert len(batch) <= (5 if workspace is None else 3)


def test_batch_rejects_a_later_host_reference_before_any_secret_projection(
    credential_case: CredentialCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = credential_case
    projection = Mock(side_effect=AssertionError("Forbidden batch must not load secrets"))
    monkeypatch.setattr(credentials, "_project_database_credentials", projection)
    with pytest.raises(ValueError, match="Host credential bindings"):
        credentials.resolve_provider_credentials(
            case.database,
            case.paths,
            [
                ("openai", "db:synthetic", "", False),
                ("anthropic", "env:PANDRATOR_SYNTHETIC_KEY", "", False),
            ],
            workspace_id=case.workspaces["alpha"],
        )
    assert not projection.called


def test_tagged_preloads_are_immutable_and_must_match_the_selected_namespace(
    credential_case: CredentialCase,
) -> None:
    case = credential_case
    case.put("alpha", "cached-key", "synthetic-cached-value")
    loaded = credentials._load_database_credentials(
        case.database, {"cached-key"}, workspace_id=case.workspaces["alpha"]
    )
    assert isinstance(loaded, credentials.ScopedCredentialValues)
    assert "synthetic-cached-value" not in repr(loaded)
    with pytest.raises(TypeError):
        cast(Any, loaded.values)["cached-key"] = "changed"
    assert case.resolve("alpha", "cached-key", preloaded_database_credentials=loaded).configured
    for target, cached in (
        ("beta", loaded),
        ("alpha", {"cached-key": "synthetic-untagged-value"}),
    ):
        with pytest.raises(ValueError, match="preload"):
            case.resolve(target, "cached-key", preloaded_database_credentials=cached)
    assert case.resolve(
        None, "legacy-cached", preloaded_database_credentials={"legacy-cached": "synthetic-legacy"}
    ).configured
    with case.database.immediate_session() as session:
        session.execute(
            update(Workspace)
            .where(Workspace.id == case.workspaces["alpha"])
            .values(state="archived")
        )
    with pytest.raises(ValueError, match="archived|inactive"):
        case.resolve("alpha", "cached-key", preloaded_database_credentials=loaded)


@pytest.mark.parametrize(
    ("reference", "backend"),
    (
        ("env:PANDRATOR_SYNTHETIC_KEY", "environment"),
        ("file:fixture", "file"),
        ("file-path:/synthetic/credential", "file"),
        ("keyring:synthetic/fixture", "keyring"),
    ),
)
def test_other_workspaces_reject_host_references_before_any_external_io(
    credential_case: CredentialCase,
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
    backend: str,
) -> None:
    case = credential_case
    backend_calls = Mock(side_effect=AssertionError("Host backend must not be accessed"))
    environment = MagicMock(wraps={})
    environment.get.side_effect = AssertionError("Host environment must not be accessed")
    monkeypatch.setattr(credentials.os, "environ", environment)
    monkeypatch.setattr(credentials, "_load_keyring", backend_calls)
    monkeypatch.setattr(credentials, "_read_secret_file", backend_calls)
    monkeypatch.setattr(Path, "is_file", backend_calls)
    with pytest.raises(ValueError, match="Host credential bindings"):
        credentials.resolve_secret_reference(
            case.database, case.paths, reference, workspace_id=case.workspaces["alpha"]
        )
    with case.database.session() as session:
        with pytest.raises(ValueError, match="Host credential bindings"):
            credentials.configure_credential_reference(
                session,
                case.database,
                case.paths,
                key="fixture",
                label="Synthetic",
                backend=backend,
                locator="PANDRATOR_SYNTHETIC_KEY",
                workspace_id=case.workspaces["alpha"],
            )
        with pytest.raises(ValueError, match="Host credential bindings"):
            credentials.delete_managed_reference(
                session, reference, preserve_shared=False, workspace_id=case.workspaces["alpha"]
            )
    assert not backend_calls.called and not environment.get.called


@pytest.mark.parametrize("reference", ("", "db:missing"))
def test_noninitial_missing_reference_suppresses_ambient_fallback(
    credential_case: CredentialCase, monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    case = credential_case
    environment = MagicMock(wraps={})
    environment.get.side_effect = AssertionError("Ambient fallback must not be accessed")
    monkeypatch.setattr(credentials.os, "environ", environment)
    resolved = credentials.resolve_secret_reference(
        case.database,
        case.paths,
        reference,
        fallback_environment_variable="PANDRATOR_SYNTHETIC_KEY",
        workspace_id=case.workspaces["alpha"],
    )
    assert (
        not resolved.configured
        and not resolved.environment_variable
        and not resolved.resolved_value()
    )
    assert not environment.get.called


@pytest.mark.parametrize("backend", ("environment", "file-path", "file", "keyring"))
def test_initial_workspace_retains_explicit_host_backends(
    credential_case: CredentialCase, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    case = credential_case
    synthetic = "synthetic-host-fixture"
    monkeypatch.setattr(credentials.os, "environ", {"PANDRATOR_SYNTHETIC_KEY": synthetic})
    keyring = Mock()
    keyring.get_password.return_value = synthetic
    monkeypatch.setattr(credentials, "_load_keyring", lambda: (keyring, "synthetic"))
    monkeypatch.setattr(credentials, "_read_secret_file", lambda _path: synthetic)
    case.paths.secrets_file.write_text(json.dumps({"fixture": synthetic}), encoding="utf-8")
    case.paths.secrets_file.chmod(0o600)
    references = {
        "environment": "env:PANDRATOR_SYNTHETIC_KEY",
        "file-path": "file-path:/synthetic/fixture",
        "file": "file:fixture",
        "keyring": "keyring:synthetic/fixture",
    }
    resolved = credentials.resolve_secret_reference(case.database, case.paths, references[backend])
    assert resolved.configured and resolved.resolved_value() == synthetic


def test_database_configuration_is_write_only_and_trusted_redaction_covers_all_namespaces(
    credential_case: CredentialCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = credential_case
    monkeypatch.setattr(credentials.os, "environ", {})
    with case.database.session() as session:
        result = credentials.configure_credential_reference(
            session,
            case.database,
            case.paths,
            key="configured",
            label="Synthetic",
            secret_value="synthetic-alpha-redaction",
            workspace_id=case.workspaces["alpha"],
        )
        assert result.reference == "db:configured" and "synthetic-alpha-redaction" not in repr(
            result
        )
        assert credential_namespace(session, case.workspaces["alpha"]).legacy_host_access is False
    case.put("beta", "configured", "synthetic-beta-redaction")
    case.put("personal", "configured", "synthetic-solo-redaction")
    status = credentials.credential_status(
        case.database, case.paths, result.reference, workspace_id=case.workspaces["alpha"]
    )
    assert status == {"credential_configured": True, "credential_source": "database"}
    redactor = credentials.SecretRedactor(case.database, case.paths)
    values = redactor.values()
    assert {
        "synthetic-alpha-redaction",
        "synthetic-beta-redaction",
        "synthetic-solo-redaction",
    } <= set(values)
    diagnostic = credentials.redact_secret_text(" ".join(values), values)
    assert not any(value in diagnostic for value in values)
    with case.database.snapshot_session() as session:
        assert len(session.scalars(select(StoredCredential)).all()) == 3


@pytest.mark.parametrize("existing", (False, True))
def test_forbidden_previous_host_cleanup_is_rejected_before_storing_database_value(
    credential_case: CredentialCase, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    case = credential_case
    if existing:
        case.put("alpha", "replacement-key", "synthetic-before")
    upsert = Mock(wraps=credentials.upsert_credential)
    monkeypatch.setattr(credentials, "upsert_credential", upsert)
    with case.database.session() as session:
        with pytest.raises(ValueError, match="Host credential bindings"):
            credentials.configure_credential_reference(
                session,
                case.database,
                case.paths,
                key="replacement-key",
                label="Replacement",
                current_reference="env:PANDRATOR_SYNTHETIC_KEY",
                secret_value="synthetic-after",
                delete_previous=True,
                workspace_id=case.workspaces["alpha"],
            )
    assert not upsert.called
    with case.database.snapshot_session() as session:
        record = session.get(StoredCredential, (case.workspaces["alpha"], "replacement-key"))
        if existing:
            assert record is not None and record.secret_value == "synthetic-before"
            assert record.revision == 1
        else:
            assert record is None


def test_cleanup_failure_after_database_replacement_rolls_back_the_caller_transaction(
    credential_case: CredentialCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = credential_case
    case.put("alpha", "replacement-key", "synthetic-before")
    case.put("alpha", "old-key", "synthetic-old")
    cleanup = Mock(side_effect=RuntimeError("Synthetic cleanup failure"))
    monkeypatch.setattr(credentials, "delete_managed_reference", cleanup)
    with case.database.session() as session:
        session.add(AppSetting(key="rollback-configuration-marker", value_json={}, revision=1))
        with pytest.raises(RuntimeError, match="Synthetic cleanup failure"):
            credentials.configure_credential_reference(
                session,
                case.database,
                case.paths,
                key="replacement-key",
                label="Replacement",
                current_reference="db:old-key",
                secret_value="synthetic-after",
                delete_previous=True,
                workspace_id=case.workspaces["alpha"],
            )
    assert cleanup.call_count == 1
    with case.database.snapshot_session() as session:
        record = session.get_one(StoredCredential, (case.workspaces["alpha"], "replacement-key"))
        assert record.secret_value == "synthetic-before" and record.revision == 1
        assert (
            session.get_one(StoredCredential, (case.workspaces["alpha"], "old-key")).revision == 1
        )
        assert session.get(AppSetting, "rollback-configuration-marker") is None
