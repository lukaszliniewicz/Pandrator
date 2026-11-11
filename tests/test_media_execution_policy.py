"""Current actor and lease checks for media source policy."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Iterator, cast

import pytest
from sqlalchemy import delete, text, update

from pandrator.web.access.contracts import AccessDenied
from pandrator.web.access.media_execution_policy import (
    MediaSourceSnapshot,
    require_media_execution_artifact,
)
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.accounts.execution_actor import (
    ExecutionActor,
    HostActor,
    actor_from_principal,
    execution_actor_scope,
)
from pandrator.web.accounts.models import User
from pandrator.web.accounts.resource_grant_models import (
    AutomationClientProjectGrant,
)
from pandrator.web.job_execution import current_job_lease
from pandrator.web.model_base import new_id, utcnow
from pandrator.web.models import (
    ApiToken,
    Artifact,
    AutomationClient,
    Job,
    JobActorRecord,
)
from pandrator.web.projects.models import ProjectMembership
from tests.test_job_access_routes import SCOPES, grant_workspace_content_access
from tests.test_job_access_routes import (
    case as media_case,  # noqa: F401 - pytest fixture registration
)
from tests.test_job_access_routes import credential as media_credential
from tests.test_job_access_routes import no_network as no_network


@pytest.fixture
def case(request: pytest.FixtureRequest) -> Any:
    return request.getfixturevalue("media_case")


@contextmanager
def _lease_scope(value: object | None) -> Iterator[None]:
    token = current_job_lease.set(cast(Any, value))
    try:
        yield
    finally:
        current_job_lease.reset(token)


def _artifact(
    case: Any,
    *,
    workspace_id: str | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
) -> str:
    identifier = new_id()
    row = Artifact(
        id=identifier,
        workspace_id=workspace_id or case.team,
        project_id=project_id if project_id is not None else case.a.project_id,
        session_id=session_id if session_id is not None else case.a.id,
        kind="source.media",
        role="source",
        relative_path=f"media-policy/{identifier}.bin",
        content_hash="source-hash-v1",
        size_bytes=64,
        state="current",
        metadata_json={},
    )
    with case.database.immediate_session() as session:
        session.add(row)
        session.flush()
    return identifier


def _rootless_artifact(case: Any, workspace_id: str) -> str:
    identifier = new_id()
    row = Artifact(
        id=identifier,
        workspace_id=workspace_id,
        project_id=None,
        session_id=None,
        kind="source.media",
        role="source",
        relative_path=f"media-policy/{identifier}.bin",
        content_hash="source-hash-v1",
        size_bytes=64,
        state="current",
        metadata_json={},
    )
    with case.database.immediate_session() as session:
        session.add(row)
        session.flush()
    return identifier


def _enqueue_job(
    case: Any,
    actor: ExecutionActor | None,
    kind: str,
    source_id: str,
    *,
    workspace_id: str,
    project_id: str | None,
    session_id: str | None,
    controls: dict[str, Any] | None = None,
) -> Job:
    payload = {"source_artifact_id": source_id, **(controls or {})}
    with execution_actor_scope(actor):
        return case.services.jobs.enqueue(
            kind,
            payload,
            workspace_id=workspace_id,
            project_id=project_id,
            session_id=session_id,
        )


def _claim(case: Any, queued: Job, worker_id: str = "media-policy-worker") -> Job:
    claimed = case.services.jobs.claim(worker_id, lease_seconds=300)
    assert claimed is not None and claimed.id == queued.id
    assert claimed.lease_owner == worker_id
    return claimed


def _complete(case: Any, claimed: Job) -> None:
    assert claimed.lease_owner is not None
    case.services.jobs.complete(
        claimed.id,
        claimed.lease_owner,
        {},
        lease_generation=claimed.lease_generation,
    )


def _handler_payload(queued: Job, claimed: Job) -> dict[str, Any]:
    return {
        **dict(queued.payload_json),
        "_job_id": claimed.id,
        "_lease_generation": claimed.lease_generation,
    }


def _call_leased(
    case: Any,
    actor: ExecutionActor | None,
    queued: Job,
    claimed: Job,
    *,
    kind: str | None = None,
    overrides: dict[str, Any] | None = None,
    expected_source: MediaSourceSnapshot | None = None,
) -> Artifact:
    payload = _handler_payload(queued, claimed)
    if overrides:
        payload.update(overrides)
    lease = {
        "job_id": claimed.id,
        "owner_id": claimed.lease_owner,
        "lease_generation": claimed.lease_generation,
    }
    with execution_actor_scope(actor), _lease_scope(lease):
        with case.database.snapshot_session() as session:
            return require_media_execution_artifact(
                session,
                payload,
                kind or queued.kind,
                expected_source=expected_source,
            )


def _client_credential(case: Any) -> tuple[Any, str]:
    client_id = new_id()
    with case.database.immediate_session() as session:
        session.add(
            AutomationClient(
                id=client_id,
                name="Synthetic media policy client",
                subject=client_id,
                redirect_uris_json=["http://localhost/callback"],
                allowed_scopes_json=sorted(SCOPES),
                target_instance_id="synthetic",
                canonical_origin="http://localhost",
                created_by="media-policy-test",
                user_id=case.users["editor"],
                user_auth_revision=1,
            )
        )
        session.flush()
        session.add(
            AutomationClientProjectGrant(
                client_id=client_id,
                project_id=case.a.project_id,
            )
        )
    token, raw = case.auth.create_api_token(
        "Synthetic media policy client token",
        user_id=case.users["editor"],
        auth_revision=1,
        scopes=SCOPES,
        client_id=client_id,
        resource_grants=CredentialResources(project_ids=frozenset({case.a.project_id})),
    )
    principal = case.auth.resolve_api_token(raw, network_zone="loopback", target_instance_id=None)
    assert principal is not None
    actor = actor_from_principal(principal)
    assert actor is not None and actor.client_id == client_id
    return SimpleNamespace(actor=actor, token=token), client_id


def test_leased_project_editor_and_actual_workspace_owner_can_read(case: Any):
    grant_workspace_content_access(case, "editor")
    source_id = _artifact(case)
    for name in ("editor", "owner"):
        caller = media_credential(case, name)
        queued = _enqueue_job(
            case,
            caller.actor,
            "audio.preview",
            source_id,
            workspace_id=case.team,
            project_id=case.a.project_id,
            session_id=case.a.id,
        )
        claimed = _claim(case, queued, f"media-{name}-worker")
        artifact = _call_leased(case, caller.actor, queued, claimed)
        assert artifact.id == source_id
        _complete(case, claimed)


@pytest.mark.parametrize("name", ("viewer", "outsider"))
def test_viewer_and_instance_admin_without_current_project_role_are_denied(case: Any, name: str):
    scopes = SCOPES | {"app.admin"} if name == "outsider" else SCOPES
    caller = media_credential(case, name, scopes=scopes)
    source_id = _artifact(case)
    queued = _enqueue_job(
        case,
        caller.actor,
        "audio.preview",
        source_id,
        workspace_id=case.team,
        project_id=case.a.project_id,
        session_id=case.a.id,
    )
    claimed = _claim(case, queued, f"media-{name}-worker")

    with pytest.raises(AccessDenied):
        _call_leased(case, caller.actor, queued, claimed)


def test_noninitial_personal_workspace_owner_can_read_rootless_source(case: Any):
    caller = media_credential(case, "editor", grant_team=False)
    workspace_id = case.personal[case.users["editor"]]
    source_id = _rootless_artifact(case, workspace_id)
    queued = _enqueue_job(
        case,
        caller.actor,
        "audio.preview",
        source_id,
        workspace_id=workspace_id,
        project_id=None,
        session_id=None,
    )
    claimed = _claim(case, queued)

    assert _call_leased(case, caller.actor, queued, claimed).id == source_id


def test_no_lease_user_invocation_uses_strict_current_read_and_run_scopes(case: Any):
    grant_workspace_content_access(case, "editor")
    source_id = _artifact(case)
    allowed = media_credential(case, "editor")
    with execution_actor_scope(allowed.actor), _lease_scope(None):
        with case.database.snapshot_session() as session:
            assert (
                require_media_execution_artifact(
                    session,
                    {"source_artifact_id": source_id, "_job_id": "historical-id"},
                    "audio.preview",
                ).id
                == source_id
            )

    missing_run = media_credential(case, "editor", scopes=frozenset({"app.read"}))
    with execution_actor_scope(missing_run.actor), _lease_scope(None):
        with case.database.snapshot_session() as session, pytest.raises(AccessDenied):
            require_media_execution_artifact(
                session,
                {"source_artifact_id": source_id},
                "audio.preview",
            )


@pytest.mark.parametrize(
    "revocation", ("membership", "client-ceiling", "account", "epoch", "token")
)
def test_leased_user_rechecks_current_role_credential_and_account_state(case: Any, revocation: str):
    if revocation == "client-ceiling":
        caller, client_id = _client_credential(case)
    else:
        caller = media_credential(case, "editor")
        client_id = None
    source_id = _artifact(case)
    queued = _enqueue_job(
        case,
        caller.actor,
        "audio.preview",
        source_id,
        workspace_id=case.team,
        project_id=case.a.project_id,
        session_id=case.a.id,
    )
    claimed = _claim(case, queued)

    with case.database.immediate_session() as session:
        if revocation == "membership":
            session.execute(
                delete(ProjectMembership).where(
                    ProjectMembership.project_id == case.a.project_id,
                    ProjectMembership.user_id == case.users["editor"],
                )
            )
        elif revocation == "client-ceiling":
            session.execute(
                delete(AutomationClientProjectGrant).where(
                    AutomationClientProjectGrant.client_id == client_id
                )
            )
            session.add(
                AutomationClientProjectGrant(
                    client_id=client_id,
                    project_id=case.b.project_id,
                )
            )
        elif revocation == "account":
            session.execute(
                update(User).where(User.id == case.users["editor"]).values(is_active=False)
            )
        elif revocation == "epoch":
            session.execute(
                update(UserCredential)
                .where(UserCredential.user_id == case.users["editor"])
                .values(auth_revision=2)
            )
        elif revocation == "token":
            session.execute(
                update(ApiToken).where(ApiToken.id == caller.token.id).values(revoked_at=utcnow())
            )

    with pytest.raises(AccessDenied):
        _call_leased(case, caller.actor, queued, claimed)


def test_expired_api_token_remains_valid_only_under_its_active_job_lease(case: Any):
    grant_workspace_content_access(case, "editor")
    caller = media_credential(case, "editor")
    source_id = _artifact(case)
    queued = _enqueue_job(
        case,
        caller.actor,
        "audio.preview",
        source_id,
        workspace_id=case.team,
        project_id=case.a.project_id,
        session_id=case.a.id,
    )
    claimed = _claim(case, queued)
    with case.database.immediate_session() as session:
        session.execute(
            update(ApiToken)
            .where(ApiToken.id == caller.token.id)
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )

    assert _call_leased(case, caller.actor, queued, claimed).id == source_id
    with execution_actor_scope(caller.actor), _lease_scope(None):
        with case.database.snapshot_session() as session, pytest.raises(AccessDenied):
            require_media_execution_artifact(
                session,
                _handler_payload(queued, claimed),
                "audio.preview",
            )


def test_source_snapshot_compares_all_seven_identity_fields(case: Any):
    caller = media_credential(case, "editor")
    source_id = _artifact(case)
    queued = _enqueue_job(
        case,
        caller.actor,
        "audio.preview",
        source_id,
        workspace_id=case.team,
        project_id=case.a.project_id,
        session_id=case.a.id,
    )
    claimed = _claim(case, queued)
    with case.database.snapshot_session() as session:
        source = session.get(Artifact, source_id)
        assert source is not None
        expected = MediaSourceSnapshot.from_artifact(source)
    with pytest.raises(FrozenInstanceError):
        expected.relative_path = "changed.bin"

    wrong_snapshots = (
        replace(expected, artifact_id=new_id()),
        replace(expected, workspace_id=case.personal[case.users["editor"]]),
        replace(expected, project_id=None),
        replace(expected, session_id=None),
        replace(expected, relative_path="other.bin"),
        replace(expected, content_hash="other-hash"),
        replace(expected, size_bytes=(expected.size_bytes or 0) + 1),
    )
    for wrong in wrong_snapshots:
        with pytest.raises(AccessDenied):
            _call_leased(case, caller.actor, queued, claimed, expected_source=wrong)


def test_stale_artifact_identity_map_is_refreshed_before_snapshot_comparison(case: Any):
    caller = media_credential(case, "editor")
    source_id = _artifact(case)
    queued = _enqueue_job(
        case,
        caller.actor,
        "audio.preview",
        source_id,
        workspace_id=case.team,
        project_id=case.a.project_id,
        session_id=case.a.id,
    )
    claimed = _claim(case, queued)
    lease = {
        "job_id": claimed.id,
        "owner_id": claimed.lease_owner,
        "lease_generation": claimed.lease_generation,
    }
    payload = _handler_payload(queued, claimed)

    with execution_actor_scope(caller.actor), _lease_scope(lease):
        with case.database.immediate_session() as session:
            cached = session.get(Artifact, source_id)
            assert cached is not None
            expected = MediaSourceSnapshot.from_artifact(cached)
            session.execute(
                update(Artifact)
                .where(Artifact.id == source_id)
                .values(
                    relative_path="media-policy/changed.bin",
                    content_hash="source-hash-v2",
                    size_bytes=65,
                ),
                execution_options={"synchronize_session": False},
            )
            with pytest.raises(AccessDenied):
                require_media_execution_artifact(
                    session,
                    payload,
                    "audio.preview",
                    expected_source=expected,
                )


def test_source_id_and_control_values_must_match_stored_job_payload(case: Any):
    caller = media_credential(case, "editor")
    source_id = _artifact(case)
    alternate_source_id = _artifact(case)
    queued = _enqueue_job(
        case,
        caller.actor,
        "video.preview",
        source_id,
        workspace_id=case.team,
        project_id=case.a.project_id,
        session_id=case.a.id,
        controls={"source_content_hash": "source-hash-v1", "preview_version": 1},
    )
    claimed = _claim(case, queued)

    for changed in (
        {"source_artifact_id": alternate_source_id},
        {"source_content_hash": "foreign-hash"},
        {"preview_version": True},
    ):
        with pytest.raises(AccessDenied):
            _call_leased(case, caller.actor, queued, claimed, overrides=changed)


def test_waveform_controls_are_checked_with_exact_types_and_values(case: Any):
    grant_workspace_content_access(case, "editor")
    caller = media_credential(case, "editor")
    source_id = _artifact(case)
    queued = _enqueue_job(
        case,
        caller.actor,
        "audio.waveform",
        source_id,
        workspace_id=case.team,
        project_id=case.a.project_id,
        session_id=case.a.id,
        controls={"max_points": 1600, "start_ms": 250, "end_ms": 1000},
    )
    claimed = _claim(case, queued)

    assert _call_leased(case, caller.actor, queued, claimed).id == source_id
    for changed in ({"max_points": True}, {"start_ms": 251}, {"end_ms": None}):
        with pytest.raises(AccessDenied):
            _call_leased(case, caller.actor, queued, claimed, overrides=changed)


def test_wrong_kind_job_id_and_job_roots_are_denied_before_payload_json(case: Any):
    caller = media_credential(case, "editor")
    source_id = _artifact(case)
    queued = _enqueue_job(
        case,
        caller.actor,
        "audio.preview",
        source_id,
        workspace_id=case.team,
        project_id=case.b.project_id,
        session_id=case.b.id,
    )
    claimed = _claim(case, queued)
    with case.database.immediate_session() as session:
        session.execute(
            text("UPDATE jobs SET payload_json = :payload WHERE id = :job_id"),
            {"payload": "{invalid-json", "job_id": queued.id},
        )

    with pytest.raises(AccessDenied):
        _call_leased(case, caller.actor, queued, claimed)
    with pytest.raises(AccessDenied):
        _call_leased(
            case,
            caller.actor,
            queued,
            claimed,
            kind="video.preview",
        )
    with pytest.raises(AccessDenied):
        _call_leased(
            case,
            caller.actor,
            queued,
            claimed,
            overrides={"_job_id": new_id()},
        )


def test_no_autoflush_and_caller_snapshot_are_preserved(case: Any):
    grant_workspace_content_access(case, "editor")
    caller = media_credential(case, "editor")
    source_id = _artifact(case)
    queued = _enqueue_job(
        case,
        caller.actor,
        "audio.preview",
        source_id,
        workspace_id=case.team,
        project_id=case.a.project_id,
        session_id=case.a.id,
    )
    claimed = _claim(case, queued)
    lease = {
        "job_id": claimed.id,
        "owner_id": claimed.lease_owner,
        "lease_generation": claimed.lease_generation,
    }
    pending_id = new_id()
    pending = Artifact(
        id=pending_id,
        workspace_id=case.team,
        project_id=case.a.project_id,
        session_id=case.a.id,
        kind="source.media",
        role="source",
        relative_path=f"media-policy/{pending_id}.bin",
        metadata_json={},
    )
    with execution_actor_scope(caller.actor), _lease_scope(lease):
        with case.database.snapshot_session() as session:
            session.add(pending)
            result = require_media_execution_artifact(
                session,
                _handler_payload(queued, claimed),
                "audio.preview",
            )
            assert result.id == source_id
            assert pending in session.new


def test_host_execution_lease_and_trusted_direct_host_calls_remain_supported(case: Any):
    host = HostActor("pandrator-cli")
    source_id = _artifact(case)
    queued = _enqueue_job(
        case,
        host,
        "video.preview",
        source_id,
        workspace_id=case.team,
        project_id=case.a.project_id,
        session_id=case.a.id,
    )
    claimed = _claim(case, queued)
    payload = _handler_payload(queued, claimed)
    payload.pop("source_content_hash", None)
    payload.pop("preview_version", None)
    with (
        execution_actor_scope(host),
        _lease_scope(
            {
                "job_id": claimed.id,
                "owner_id": claimed.lease_owner,
                "lease_generation": claimed.lease_generation,
            }
        ),
    ):
        with case.database.snapshot_session() as session:
            assert (
                require_media_execution_artifact(session, payload, "video.preview").id == source_id
            )

    with execution_actor_scope(host), _lease_scope(None):
        with case.database.snapshot_session() as session:
            assert (
                require_media_execution_artifact(
                    session,
                    {"source_artifact_id": source_id, "_job_id": "historical-job-id"},
                    "audio.preview",
                ).id
                == source_id
            )


def test_trusted_direct_no_actor_uses_actual_noninitial_workspace_ownership(case: Any):
    workspace_id = case.personal[case.users["editor"]]
    source_id = _rootless_artifact(case, workspace_id)
    with execution_actor_scope(None), _lease_scope(None):
        with case.database.snapshot_session() as session:
            assert (
                require_media_execution_artifact(
                    session,
                    {"source_artifact_id": source_id, "_job_id": "legacy-direct-id"},
                    "audio.preview",
                ).id
                == source_id
            )


def test_unexpired_unauthored_worker_lease_is_legacy_compatible_but_attribution_blocks_fallback(
    case: Any,
):
    source_id = _artifact(case)
    legacy_job = _enqueue_job(
        case,
        None,
        "audio.waveform",
        source_id,
        workspace_id=case.team,
        project_id=case.a.project_id,
        session_id=case.a.id,
    )
    legacy_claim = _claim(case, legacy_job)
    request_payload = _handler_payload(legacy_job, legacy_claim)
    request_payload.update(start_ms=None, end_ms=None, max_points=None)
    with (
        execution_actor_scope(None),
        _lease_scope(
            {
                "job_id": legacy_claim.id,
                "owner_id": legacy_claim.lease_owner,
                "lease_generation": legacy_claim.lease_generation,
            }
        ),
    ):
        with case.database.snapshot_session() as session:
            assert (
                require_media_execution_artifact(session, request_payload, "audio.waveform").id
                == source_id
            )
    _complete(case, legacy_claim)

    caller = media_credential(case, "editor")
    attributed_job = _enqueue_job(
        case,
        caller.actor,
        "audio.preview",
        source_id,
        workspace_id=case.team,
        project_id=case.a.project_id,
        session_id=case.a.id,
    )
    attributed_claim = _claim(case, attributed_job, "media-attributed-worker")
    with (
        execution_actor_scope(None),
        _lease_scope(
            {
                "job_id": attributed_claim.id,
                "owner_id": attributed_claim.lease_owner,
                "lease_generation": attributed_claim.lease_generation,
            }
        ),
    ):
        with case.database.snapshot_session() as session, pytest.raises(AccessDenied):
            assert session.get(JobActorRecord, attributed_claim.id) is not None
            require_media_execution_artifact(
                session,
                _handler_payload(attributed_job, attributed_claim),
                "audio.preview",
            )


@pytest.mark.parametrize("lease_change", ("extra-key", "generation-bool", "expired"))
def test_legacy_lease_shape_generation_and_expiry_are_strict(case: Any, lease_change: str):
    source_id = _artifact(case)
    queued = _enqueue_job(
        case,
        None,
        "audio.preview",
        source_id,
        workspace_id=case.team,
        project_id=case.a.project_id,
        session_id=case.a.id,
    )
    claimed = _claim(case, queued)
    lease: dict[str, Any] = {
        "job_id": claimed.id,
        "owner_id": claimed.lease_owner,
        "lease_generation": claimed.lease_generation,
    }
    if lease_change == "extra-key":
        lease["unexpected"] = True
    elif lease_change == "generation-bool":
        lease["lease_generation"] = True
    else:
        with case.database.immediate_session() as session:
            session.execute(
                update(Job)
                .where(Job.id == claimed.id)
                .values(lease_expires_at=utcnow() - timedelta(seconds=1))
            )
            session.execute(
                text("UPDATE jobs SET payload_json = :payload WHERE id = :job_id"),
                {"payload": "{invalid-json", "job_id": claimed.id},
            )

    with (
        execution_actor_scope(None),
        _lease_scope(lease),
        pytest.raises(AccessDenied),
    ):
        with case.database.snapshot_session() as session:
            require_media_execution_artifact(
                session,
                _handler_payload(queued, claimed),
                "audio.preview",
            )


def test_malformed_actor_cannot_fall_back_to_legacy_lease(case: Any):
    source_id = _artifact(case)
    queued = _enqueue_job(
        case,
        None,
        "audio.preview",
        source_id,
        workspace_id=case.team,
        project_id=case.a.project_id,
        session_id=case.a.id,
    )
    claimed = _claim(case, queued)

    with (
        execution_actor_scope(cast(ExecutionActor, object())),
        _lease_scope(
            {
                "job_id": claimed.id,
                "owner_id": claimed.lease_owner,
                "lease_generation": claimed.lease_generation,
            }
        ),
        pytest.raises(AccessDenied),
    ):
        with case.database.snapshot_session() as session:
            require_media_execution_artifact(
                session,
                _handler_payload(queued, claimed),
                "audio.preview",
            )
