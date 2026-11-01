"""Direct private owner/lease/effect proofs; no app, transport fixtures or native work."""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import delete, select, text

from pandrator.logic.dubbing import cloud_stt
from pandrator.web import models as m
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.access.private_transcription_execution_policy import (
    require_private_transcription_execution,
)
from pandrator.web.accounts.auth_models import BrowserAuthSession
from pandrator.web.accounts.commands import AccountActor, AccountCommands
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.accounts.execution_actor import (
    ExecutionActor,
    HostActor,
    UserActor,
    execution_actor_scope,
)
from pandrator.web.accounts.models import Workspace, WorkspaceMembership
from pandrator.web.accounts.private_work_models import QuickTranscriptionOwner
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.auth import AuthService
from pandrator.web.credentials import upsert_credential
from pandrator.web.database import Database
from pandrator.web.job_execution import JobLease, current_job_lease
from pandrator.web.jobs import JobQueue
from pandrator.web.quick_transcription_execution import prepare_private_transcription_execution
from pandrator.web.sessions import SessionService
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def private(tmp_path: Path):
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    auth = AuthService(database)
    initial = auth.initial_account()
    commands = AccountCommands(database)
    admin = AccountActor(initial.user_id, initial.auth_revision, frozenset({"app.admin"}))
    alice = commands.create_user(admin, login_name="private-alice", display_name="Alice")
    bob = commands.create_user(admin, login_name="private-bob", display_name="Bob")
    f = SimpleNamespace(
        paths=paths, database=database, auth=auth, initial=initial, alice=alice, bob=bob
    )
    yield f
    database.dispose()


def _identity(f, user_id, *, token=False):
    with f.database.snapshot_session() as unit:
        home = unit.scalar(select(Workspace.id).where(Workspace.personal_owner_user_id == user_id))
    if token:
        record, raw = f.auth.create_api_token(
            "Private run",
            user_id=user_id,
            scopes=frozenset({"app.run"}),
            resource_grants=CredentialResources(workspace_ids=frozenset({home})),
        )
        actor = UserActor(
            user_id, 1, str(record.subject), "api_token", frozenset({"app.run"}), token_id=record.id
        )
        return actor, home, raw
    issued = f.auth.browser_sessions.issue(
        user_id,
        1,
        subject="Private owner",
        principal_kind="owner_session",
        scopes=frozenset({"app.run"}),
    )
    return (
        UserActor(
            user_id,
            1,
            "Private owner",
            "owner_session",
            frozenset({"app.run"}),
            browser_session_id=issued.record_id,
        ),
        home,
        issued.raw_token,
    )


def _work(f, *, user_id=None, token=False, captured=True):
    actor, home, raw = _identity(f, user_id or f.alice.user_id, token=token)
    value = b"owned source bytes"
    with f.database.immediate_session() as unit:
        record = m.QuickTranscription(
            owner_subject="attribution-is-not-authority",
            source_suffix=".wav",
            size_bytes=len(value),
            sha256=hashlib.sha256(value).hexdigest(),
            uploaded_bytes=len(value),
            next_chunk_index=1,
            format="txt",
            settings_json={"stt_engine": "whisper", "stt_language": "en"},
            state="uploading",
            expires_at=m.utcnow() + timedelta(hours=1),
        )
        unit.add(record)
        unit.flush()
        unit.add(
            QuickTranscriptionOwner(
                transcription_id=record.id, user_id=actor.user_id, workspace_id=home
            )
        )
        identifier = record.id
    directory = f.paths.root / "quick-transcriptions" / identifier
    directory.mkdir(parents=True)
    source = directory / "source.wav"
    source.write_bytes(value)
    with execution_actor_scope(actor if captured else None):
        job = JobQueue(f.database).enqueue(
            "transcription.quick", {"transcription_id": identifier}, workspace_id=home
        )
    with f.database.immediate_session() as unit:
        record = unit.get(m.QuickTranscription, identifier)
        assert record is not None
        record.job_id = job.id
        record.state = "submitted"
    job = JobQueue(f.database).claim(f"private-worker-{identifier}", lease_seconds=900)
    assert job is not None
    return SimpleNamespace(
        actor=actor,
        home=home,
        raw=raw,
        job=job,
        identifier=identifier,
        source=source,
        payload={
            "transcription_id": identifier,
            "_job_id": job.id,
            "_lease_generation": job.lease_generation,
        },
    )


@contextmanager
def _bound(work, *, actor: ExecutionActor | None = None, use_original=True, lease=True):
    value: JobLease = {
        "job_id": work.job.id,
        "owner_id": work.job.lease_owner,
        "lease_generation": work.job.lease_generation,
    }
    marker = current_job_lease.set(value if lease else None)
    try:
        with execution_actor_scope(work.actor if use_original else actor):
            yield
    finally:
        current_job_lease.reset(marker)


def _prepare(f, work):
    return prepare_private_transcription_execution(
        f.database, f.paths, work.payload, threading.Event()
    )


@pytest.mark.parametrize(
    "mode", ["solo", "named", "run_token", "browser_logout", "browser_expiry", "api_expiry"]
)
def test_actual_owner_durable_run_only_and_natural_logout_expiry(private, mode):
    f = private
    work = _work(
        f,
        user_id=f.initial.user_id if mode == "solo" else None,
        token=mode in {"run_token", "api_expiry"},
    )
    if mode == "browser_logout":
        f.auth.browser_sessions.revoke(work.raw)
    elif mode == "browser_expiry":
        with f.database.immediate_session() as unit:
            row = unit.get(BrowserAuthSession, work.actor.browser_session_id)
            assert row is not None
            row.expires_at = m.utcnow() - timedelta(seconds=1)
            row.idle_expires_at = m.utcnow() - timedelta(seconds=1)
    elif mode == "api_expiry":
        with f.database.immediate_session() as unit:
            row = unit.get(m.ApiToken, work.actor.token_id)
            assert row is not None
            row.expires_at = m.utcnow() - timedelta(seconds=1)
    with _bound(work):
        operation = _prepare(f, work)
        assert operation is not None
        operation.check_source()
        assert operation.header.owner_user_id == work.actor.user_id
        assert operation.header.workspace_id == work.home
        assert operation.runtime_settings(operation.settings) == operation.settings
        assert operation.settings == {"stt_engine": "whisper", "stt_language": "en"}


@pytest.mark.parametrize(
    "loss", ["account", "revision", "token", "scope", "workspace_grant", "membership"]
)
def test_explicit_revocation_stops_effect_and_latches_original(private, loss):
    f = private
    work = _work(f, token=loss in {"token", "scope", "workspace_grant"})
    with _bound(work):
        operation = _prepare(f, work)
        assert operation is not None
        with f.database.immediate_session() as unit:
            if loss == "account":
                unit.execute(
                    text("UPDATE users SET is_active = 0 WHERE id = :id"),
                    {"id": work.actor.user_id},
                )
            elif loss == "revision":
                unit.execute(
                    text(
                        "UPDATE user_credentials SET auth_revision = auth_revision + 1 WHERE user_id = :id"
                    ),
                    {"id": work.actor.user_id},
                )
            elif loss == "token":
                row = unit.get(m.ApiToken, work.actor.token_id)
                assert row is not None
                row.revoked_at = m.utcnow()
            elif loss == "scope":
                row = unit.get(m.ApiToken, work.actor.token_id)
                assert row is not None
                row.scopes_json = []
            elif loss == "workspace_grant":
                unit.execute(
                    delete(ApiTokenWorkspaceGrant).where(
                        ApiTokenWorkspaceGrant.token_id == work.actor.token_id
                    )
                )
            else:
                unit.execute(
                    delete(WorkspaceMembership).where(
                        WorkspaceMembership.workspace_id == work.home,
                        WorkspaceMembership.user_id == work.actor.user_id,
                    )
                )
        with pytest.raises((AccessDenied, PermissionError)) as denied:
            operation.before_io()
        with pytest.raises(type(denied.value)) as repeated:
            operation.raise_if_failed()
        assert repeated.value is denied.value


@pytest.mark.parametrize(
    "wrong",
    [
        "foreign",
        "kind",
        "root",
        "selector",
        "owner",
        "generation",
        "expiry",
        "lost_actor",
        "lost_both",
        "host",
    ],
)
def test_wrong_private_owner_job_roots_selectors_or_context_never_read_settings(
    private, monkeypatch, wrong
):
    f = private
    work = _work(f)
    payload = dict(work.payload)
    selected = work
    if wrong in {"foreign", "kind", "root", "selector", "host"}:
        if wrong == "foreign":
            selected = _work(f, user_id=f.bob.user_id)
            payload = dict(selected.payload)
            actor = work.actor
        else:
            actor = HostActor("pandrator-cli") if wrong == "host" else work.actor
            kind = "noop" if wrong in {"kind", "root"} else "transcription.quick"
            record = None
            if wrong == "root":
                record = SessionService(f.database).create(
                    "Actual rooted sibling", workspace_id=work.home
                )
            dispatch = {"transcription_id": m.new_id() if wrong == "selector" else work.identifier}
            with execution_actor_scope(actor):
                if wrong == "root":
                    assert record is not None
                    JobQueue(f.database).enqueue(kind, dispatch, session_id=record.id)
                else:
                    JobQueue(f.database).enqueue(kind, dispatch, workspace_id=work.home)
            job = JobQueue(f.database).claim("different-private-worker", lease_seconds=900)
            assert job is not None
            selected = SimpleNamespace(**{**vars(work), "job": job})
            payload.update(_job_id=job.id, _lease_generation=job.lease_generation)
    else:
        actor = None if wrong in {"lost_actor", "lost_both"} else work.actor
        with f.database.immediate_session() as unit:
            row = unit.get(m.Job, work.job.id)
            assert row is not None
            if wrong == "owner":
                row.lease_owner = "replacement"
            elif wrong == "generation":
                row.lease_generation += 1
            elif wrong == "expiry":
                row.lease_expires_at = m.utcnow() - timedelta(seconds=1)
    with f.database.immediate_session() as unit:
        unit.execute(
            text("UPDATE quick_transcriptions SET settings_json = :bad"), {"bad": "{private-poison"}
        )
    with (
        _bound(selected, actor=actor, use_original=False, lease=wrong != "lost_both"),
        pytest.raises((AccessDenied, PermissionError, KeyError)),
    ):
        prepare_private_transcription_execution(f.database, f.paths, payload, threading.Event())


@pytest.mark.parametrize("change", ["bytes", "size", "symlink"])
def test_source_integrity_uses_proved_private_path_and_exact_hash(private, change):
    f = private
    work = _work(f)
    with _bound(work):
        operation = _prepare(f, work)
        assert operation is not None
        if change == "bytes":
            work.source.write_bytes(b"different source!!")
        elif change == "size":
            work.source.write_bytes(b"x")
        else:
            other = work.source.parent / "other.wav"
            other.write_bytes(work.source.read_bytes())
            work.source.unlink()
            work.source.symlink_to(other)
        with pytest.raises(
            ValueError, match="temporary transcription source changed or is unavailable"
        ):
            operation.check_source()


def test_structural_input_cleanup_survives_role_loss_but_not_replacement_and_result_retains(
    private,
):
    f = private
    work = _work(f)
    with _bound(work):
        operation = _prepare(f, work)
        assert operation is not None
        with f.database.immediate_session() as unit:
            unit.execute(
                text("UPDATE users SET is_active = 0 WHERE id = :id"), {"id": work.actor.user_id}
            )
        with f.database.snapshot_session() as unit:
            assert operation.may_cleanup_inputs(unit)
        with f.database.immediate_session() as unit:
            row = unit.get(m.Job, work.job.id)
            assert row is not None
            row.lease_generation += 1
            row.lease_owner = "replacement"
            record = unit.get(m.QuickTranscription, work.identifier)
            assert record is not None
            record.state = "finished"
        with f.database.snapshot_session() as unit:
            assert not operation.may_cleanup_inputs(unit)
        with execution_actor_scope(None), f.database.snapshot_session() as unit:
            assert operation.result_is_committed(unit)


def test_final_writer_allows_only_captured_finished_and_refreshes_ttl(private):
    f = private
    work = _work(f)
    with _bound(work):
        operation = _prepare(f, work)
        assert operation is not None
        with f.database.immediate_session() as unit:
            operation.check_in_session(unit)
            row = unit.get(m.QuickTranscription, work.identifier)
            assert row is not None
            row.state = "finished"
            row.expires_at = m.utcnow() + timedelta(hours=2)
            operation.check_in_session(unit, allow_finished=True)
        with f.database.snapshot_session() as unit:
            assert operation.result_is_committed(unit)
            with pytest.raises(AccessDenied):
                require_private_transcription_execution(unit, work.payload, allow_finished=True)
        with pytest.raises(KeyError):
            _prepare(f, work)
    legacy = _work(f, captured=False)
    with f.database.immediate_session() as unit:
        unit.execute(
            text("UPDATE quick_transcriptions SET settings_json = :bad WHERE id = :id"),
            {"bad": "{uncaptured-private", "id": legacy.identifier},
        )
    with _bound(legacy, actor=None, use_original=False):
        assert _prepare(f, legacy) is None


@pytest.mark.parametrize("change", ["key", "connection", "environment"])
def test_cloud_freezes_effective_scoped_identity_and_never_uses_named_host_env(
    private, monkeypatch, change
):
    from pandrator.web import quick_transcription_execution as execution

    f = private
    work = _work(f)
    engine = "azure_mai_transcribe_1_5"
    profile = {
        "id": engine,
        "enabled": True,
        "api_base": "https://private-one.cognitiveservices.azure.com",
        "secret_ref": "db:private.azure",
        "label": "First label",
    }
    with f.database.immediate_session() as unit:
        if change != "environment":
            upsert_credential(
                unit, "private.azure", "Synthetic", "SCOPED_SECRET", workspace_id=work.home
            )
        else:
            profile.pop("secret_ref")
        unit.add(
            m.WorkspaceSetting(
                workspace_id=work.home,
                key="services.stt",
                value_json={"provider_configs": [profile]},
            )
        )
    settings = {
        "stt_engine": engine,
        "stt_language": "en",
        "stt_api_key": "HOST_FLAT_SENTINEL",
        "stt_api_base": "https://host.cognitiveservices.azure.com",
    }
    builtin = cloud_stt.get_stt_provider_profile(engine)
    assert builtin is not None
    monkeypatch.setenv(builtin["api_key_env"], "HOST_ENV_SENTINEL")
    post = Mock(return_value=object())
    monkeypatch.setattr(execution.requests, "post", post)
    with _bound(work):
        operation = _prepare(f, work)
        assert operation is not None
        if change == "environment":
            with pytest.raises(
                ValueError, match="selected transcription service is no longer available"
            ):
                operation.runtime_settings(settings)
            post.assert_not_called()
            return
        runtime = operation.runtime_settings(settings)
        assert "stt_api_key" not in runtime and "stt_api_base" not in runtime
        url = cloud_stt._api_base(runtime, cloud_stt._profile_for(runtime))
        operation.cloud_request(
            url, timeout=7, files={"synthetic": "preserve"}, headers={"key": "preserve"}
        )
        assert post.call_args.kwargs == {
            "timeout": 7,
            "files": {"synthetic": "preserve"},
            "headers": {"key": "preserve"},
        }
        with f.database.immediate_session() as unit:
            row = unit.get(m.WorkspaceSetting, (work.home, "services.stt"))
            assert row is not None
            row.value_json = {"provider_configs": [{**profile, "label": "Cosmetic label"}]}
        operation.cloud_request(url)
        assert post.call_count == 2
        with f.database.immediate_session() as unit:
            if change == "key":
                upsert_credential(
                    unit, "private.azure", "Synthetic", "ROTATED_SECRET", workspace_id=work.home
                )
            else:
                row = unit.get(m.WorkspaceSetting, (work.home, "services.stt"))
                assert row is not None
                row.value_json = {
                    "provider_configs": [
                        {**profile, "api_base": "https://private-two.cognitiveservices.azure.com"}
                    ]
                }
        with pytest.raises(
            ValueError, match="selected transcription service is no longer available"
        ) as denied:
            operation.cloud_request(url)
        with pytest.raises(ValueError) as latched:
            operation.raise_if_failed()
        assert latched.value is denied.value
        assert post.call_count == 2
        assert operation.settings == {"stt_engine": "whisper", "stt_language": "en"}
