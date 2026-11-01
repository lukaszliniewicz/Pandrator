"""Sound-library metadata and commands honor the live caller workspace."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from pandrator.web.access.contracts import AccessDenied, ProjectAction, WorkspaceNotFound
from pandrator.web.access.sound_library_policy import (
    require_sound_library_headers,
    require_sound_library_workspace,
)
from pandrator.web.accounts.actor_models import JobActorRecord
from pandrator.web.accounts.execution_actor import (
    HostActor,
    UserActor,
    actor_from_principal,
    actor_to_storage,
    execution_actor_scope,
)
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.job_execution import JobLease, current_job_lease
from pandrator.web.models import ApiToken, Job, SoundItem, SoundVariant, utcnow
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.sound_library_commands import (
    discard_in_session,
    save_in_session,
    update_in_session,
)
from pandrator.web.sound_library_queries import (
    get_candidate,
    get_sound,
    list_candidates,
    list_sounds,
)
from tests.test_account_authentication_integration import (
    case as case,
)
from tests.test_account_authentication_integration import (
    current_browser_principal,
)
from tests.test_evidence_access_routes import team
from tests.test_provider_workspace_access import scoped_token
from tests.test_voice_crud_workspace_access import no_network as no_network  # noqa: F401
from tests.test_voice_lifecycle_idempotency import silent_wav


@pytest.fixture
def sound_case(case, monkeypatch):
    users = team(case, monkeypatch)
    services = case.app.extensions["pandrator"]["services"]
    return SimpleNamespace(case=case, users=users, services=services)


def _actor(case, client) -> UserActor:
    actor = actor_from_principal(current_browser_principal(replace(case, client=client)))
    assert type(actor) is UserActor
    return actor


def _personal_workspace(case, user_id: str) -> str:
    with case.database.snapshot_session() as session:
        identifier = session.scalar(
            select(Workspace.id).where(
                Workspace.kind == "personal",
                Workspace.personal_owner_user_id == user_id,
            )
        )
    assert identifier is not None
    return identifier


def _item(case, workspace_id: str, name: str) -> str:
    with case.database.immediate_session() as session:
        row = SoundItem(
            workspace_id=workspace_id,
            name=name,
            tags_json=["synthetic"],
            revision=1,
            archived=False,
        )
        session.add(row)
        session.flush()
        return row.id


def _candidate(
    case,
    workspace_id: str,
    *,
    item_id: str | None = None,
    job_id: str | None = None,
    state: str = "ready",
) -> str:
    services = case.app.extensions["pandrator"]["services"]
    audio_path = services.paths.root / "sound-library-tests" / f"{uuid4()}.wav"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(silent_wav())
    artifact = services.artifacts.register(
        audio_path,
        kind="audio",
        role="sound.original",
        session_id=None,
        workspace_id=workspace_id,
        calculate_hash=True,
        metadata={"operation": "workspace-access-test"},
    )
    with case.database.immediate_session() as session:
        row = SoundVariant(
            workspace_id=workspace_id,
            item_id=item_id,
            job_id=job_id,
            artifact_id=artifact.id,
            state=state,
            provenance_json={"provider": "synthetic", "prompt": "short rain"},
            sample_rate=44100,
            channels=2,
            frame_count=8,
            duration_seconds=0.1,
        )
        session.add(row)
        session.flush()
        return row.id


def _leased_job(case, workspace_id: str, actor: UserActor | None = None) -> JobLease:
    owner_id = "sound-library-test-worker"
    with case.database.immediate_session() as session:
        row = Job(
            kind="sound.library.test",
            workspace_id=workspace_id,
            status="running",
            payload_json={},
            resource_keys_json=[],
            lease_owner=owner_id,
            lease_generation=1,
            lease_expires_at=utcnow() + timedelta(minutes=2),
        )
        session.add(row)
        session.flush()
        if actor is not None:
            stored = actor_to_storage(actor)
            assert stored is not None
            session.add(
                JobActorRecord(
                    job_id=row.id,
                    user_id=stored["user_id"],
                    auth_revision=stored["auth_revision"],
                    context_json=stored["context_json"],
                )
            )
        return {"job_id": row.id, "owner_id": owner_id, "lease_generation": 1}


def _token_actor(user: UserActor, record: ApiToken, scopes: set[str]) -> UserActor:
    return UserActor(
        user_id=user.user_id,
        auth_revision=user.auth_revision,
        subject=record.subject or f"api-token:{record.id}",
        principal_kind="api_token",
        scopes=frozenset(scopes),
        token_id=record.id,
        client_id=record.client_id,
    )


def test_default_reads_are_caller_personal_and_instance_admin_has_no_private_home_wildcard(
    sound_case,
):
    f = sound_case
    alice = _actor(f.case, f.users.alice)
    admin = _actor(f.case, f.users.owner)
    bob_home = _personal_workspace(f.case, f.users.bob_id)
    owner_home = _personal_workspace(f.case, admin.user_id)
    owner_item = _item(f.case, owner_home, "Original owner private sound")

    with execution_actor_scope(alice), f.case.database.snapshot_session() as session:
        page = list_sounds(session)
        assert owner_item not in {row["id"] for row in page["items"]}

    with execution_actor_scope(admin), f.case.database.snapshot_session() as session:
        with pytest.raises(WorkspaceNotFound):
            list_sounds(session, workspace_id=bob_home)


def test_team_member_and_owner_can_read_and_edit_the_explicit_team_library(sound_case):
    f = sound_case
    alice = _actor(f.case, f.users.alice)
    owner = _actor(f.case, f.users.owner)
    item_id = _item(f.case, f.users.workspace_id, "Shared workshop sound")

    for actor in (alice, owner):
        with execution_actor_scope(actor), f.case.database.snapshot_session() as session:
            page = list_sounds(session, workspace_id=f.users.workspace_id)
            assert item_id in {row["id"] for row in page["items"]}

    with execution_actor_scope(alice), f.case.database.immediate_session() as session:
        changed = update_in_session(
            session,
            item_id,
            {"expected_revision": 1, "name": "Shared workshop sound revised"},
            workspace_id=f.users.workspace_id,
        )
    assert changed["name"] == "Shared workshop sound revised"
    assert changed["revision"] == 2


def test_action_validation_scope_denials_and_missing_workspace_precede_private_payloads(
    sound_case,
):
    f = sound_case
    alice = _actor(f.case, f.users.alice)
    item_id = _item(f.case, f.users.workspace_id, "Team sound")
    read_only = replace(alice, scopes=frozenset({"app.read"}))

    with execution_actor_scope(alice), f.case.database.snapshot_session() as session:
        for invalid in ("read", ProjectAction.CANCEL_OWN):
            with pytest.raises(TypeError):
                require_sound_library_workspace(session, cast(ProjectAction, invalid))
        with pytest.raises(WorkspaceNotFound):
            require_sound_library_workspace(
                session,
                ProjectAction.READ,
                workspace_id="missing-workspace-id",
            )

    with execution_actor_scope(read_only), f.case.database.immediate_session() as session:
        with pytest.raises(AccessDenied):
            update_in_session(
                session,
                item_id,
                cast(dict, object()),  # must not be parsed before EDIT authority fails
                workspace_id=f.users.workspace_id,
            )


def test_foreign_scope_and_item_candidate_headers_stop_before_record_bodies(sound_case):
    f = sound_case
    alice = _actor(f.case, f.users.alice)
    bob_home = _personal_workspace(f.case, f.users.bob_id)
    alice_home = _personal_workspace(f.case, f.users.alice_id)
    bob_item = _item(f.case, bob_home, "Bob private sound")
    bob_candidate = _candidate(f.case, bob_home)
    alice_item = _item(f.case, alice_home, "Alice owned sound")
    statements: list[str] = []
    loaded_items: set[str] = set()
    loaded_candidates: set[str] = set()

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(" ".join(statement.lower().split()))

    def capture_item_load(instance, _context):
        loaded_items.add(instance.id)

    def capture_candidate_load(instance, _context):
        loaded_candidates.add(instance.id)

    event.listen(f.case.database.engine, "before_cursor_execute", capture)
    event.listen(SoundItem, "load", capture_item_load)
    event.listen(SoundVariant, "load", capture_candidate_load)
    try:
        with execution_actor_scope(alice), f.case.database.snapshot_session() as session:
            with pytest.raises(WorkspaceNotFound):
                list_sounds(session, workspace_id=bob_home)
        assert not any("from sound_items" in statement for statement in statements)

        statements.clear()
        with execution_actor_scope(alice), f.case.database.snapshot_session() as session:
            with pytest.raises(WorkspaceNotFound):
                get_sound(session, bob_item, workspace_id=alice_home)
        item_queries = [statement for statement in statements if "from sound_items" in statement]
        assert item_queries and all("sound_items.workspace_id =" in statement for statement in item_queries)
        assert bob_item not in loaded_items

        statements.clear()
        with execution_actor_scope(alice), f.case.database.snapshot_session() as session:
            with pytest.raises(WorkspaceNotFound):
                get_candidate(session, bob_candidate, workspace_id=alice_home)
        candidate_queries = [statement for statement in statements if "from sound_variants" in statement]
        assert candidate_queries and all(
            "sound_variants.workspace_id =" in statement for statement in candidate_queries
        )
        assert bob_candidate not in loaded_candidates

        statements.clear()
        with execution_actor_scope(alice), f.case.database.snapshot_session() as session:
            for kwargs in (
                {"item_id": bob_item},
                {"candidate_id": bob_candidate},
            ):
                with pytest.raises(WorkspaceNotFound):
                    require_sound_library_headers(session, alice_home, **kwargs)
        assert any("select sound_items.id from sound_items" in statement for statement in statements)
        assert any("select sound_variants.id from sound_variants" in statement for statement in statements)
        assert not any("select sound_items.id, sound_items.workspace_id" in statement for statement in statements)
        assert not any("select sound_variants.id, sound_variants.workspace_id" in statement for statement in statements)

        statements.clear()
        with execution_actor_scope(alice), f.case.database.immediate_session() as session:
            with pytest.raises(WorkspaceNotFound):
                update_in_session(
                    session,
                    alice_item,
                    {"expected_revision": 1, "preferred_variant_id": bob_candidate},
                    workspace_id=alice_home,
                )
        candidate_queries = [statement for statement in statements if "from sound_variants" in statement]
        assert candidate_queries and all(
            "sound_variants.workspace_id =" in statement for statement in candidate_queries
        )
        assert bob_candidate not in loaded_candidates
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", capture)
        event.remove(SoundItem, "load", capture_item_load)
        event.remove(SoundVariant, "load", capture_candidate_load)


def test_list_and_detail_queries_keep_candidate_projection_and_paging_within_workspace(
    sound_case,
):
    f = sound_case
    alice = _actor(f.case, f.users.alice)
    home = _personal_workspace(f.case, f.users.alice_id)
    item_id = _item(f.case, home, "Alice sound")
    owned_candidate = _candidate(f.case, home, item_id=item_id)
    other_candidate = _candidate(f.case, f.users.workspace_id)

    with execution_actor_scope(alice), f.case.database.snapshot_session() as session:
        sounds = list_sounds(session, workspace_id=home, limit=1)
        candidates = list_candidates(session, workspace_id=home, limit=1)
        detail = get_sound(session, item_id, workspace_id=home, limit=1)
        candidate = get_candidate(session, owned_candidate, workspace_id=home)

    assert [row["id"] for row in sounds["items"]] == [item_id]
    assert sounds["next_cursor"] is None
    assert [row["id"] for row in candidates["items"]] == [owned_candidate]
    assert all(row["id"] != other_candidate for row in candidates["items"])
    assert detail["id"] == item_id
    assert detail["variants"]["items"][0]["id"] == owned_candidate
    assert candidate["id"] == owned_candidate
    assert candidate["provenance"]["provider"] == "synthetic"


def test_workspace_scoped_api_token_needs_live_workspace_grant_and_revocation(sound_case):
    f = sound_case
    alice = _actor(f.case, f.users.alice)
    scopes = {"app.read"}
    ungranted, _ungranted_bearer = scoped_token(
        f.case,
        f.users.alice_id,
        scopes,
    )
    granted, _granted_bearer = scoped_token(
        f.case,
        f.users.alice_id,
        scopes,
        workspace=f.users.workspace_id,
    )
    ungranted_actor = _token_actor(alice, ungranted, scopes)
    granted_actor = _token_actor(alice, granted, scopes)

    with execution_actor_scope(ungranted_actor), f.case.database.snapshot_session() as session:
        with pytest.raises(WorkspaceNotFound):
            list_sounds(session, workspace_id=f.users.workspace_id)
    with execution_actor_scope(granted_actor), f.case.database.snapshot_session() as session:
        assert list_sounds(session, workspace_id=f.users.workspace_id) == {
            "items": [],
            "next_cursor": None,
        }

    with f.case.database.immediate_session() as session:
        token = session.get(ApiToken, granted.id)
        assert token is not None
        token.revoked_at = utcnow()
    with execution_actor_scope(granted_actor), f.case.database.snapshot_session() as session:
        with pytest.raises(AccessDenied):
            list_sounds(session, workspace_id=f.users.workspace_id)


def test_save_personal_team_attach_and_idempotent_reuse_keep_revision_semantics(sound_case):
    f = sound_case
    alice = _actor(f.case, f.users.alice)
    alice_home = _personal_workspace(f.case, f.users.alice_id)
    personal_candidate = _candidate(f.case, alice_home)
    team_candidate = _candidate(f.case, f.users.workspace_id)
    attached_candidate = _candidate(f.case, f.users.workspace_id)

    with execution_actor_scope(alice), f.case.database.immediate_session() as session:
        personal = save_in_session(
            session,
            f.services.paths,
            {"candidate_id": personal_candidate, "name": "Personal rain"},
        )
    assert personal["item"]["id"] == personal["item_id"]
    with f.case.database.snapshot_session() as session:
        item = session.get(SoundItem, personal["item_id"])
        assert item is not None and item.workspace_id == alice_home

    with execution_actor_scope(alice), f.case.database.immediate_session() as session:
        shared = save_in_session(
            session,
            f.services.paths,
            {"candidate_id": team_candidate, "name": "Shared rain"},
            workspace_id=f.users.workspace_id,
        )
    assert shared["item"]["revision"] == 1

    with execution_actor_scope(alice), f.case.database.immediate_session() as session:
        attached = save_in_session(
            session,
            f.services.paths,
            {
                "candidate_id": attached_candidate,
                "item_id": shared["item_id"],
                "expected_revision": 1,
            },
            workspace_id=f.users.workspace_id,
        )
    assert attached["item"]["revision"] == 2

    with execution_actor_scope(alice), f.case.database.immediate_session() as session:
        reused = save_in_session(
            session,
            f.services.paths,
            {"candidate_id": team_candidate, "name": "ignored on reuse"},
            workspace_id=f.users.workspace_id,
        )
    assert reused["reused"] is True
    assert reused["item"]["revision"] == 2


def test_update_preferred_variant_noop_stale_revision_and_foreign_id_semantics(sound_case):
    f = sound_case
    alice = _actor(f.case, f.users.alice)
    item_id = _item(f.case, f.users.workspace_id, "Editable team sound")
    preferred = _candidate(f.case, f.users.workspace_id, item_id=item_id)
    foreign = _candidate(f.case, _personal_workspace(f.case, f.users.bob_id))

    with execution_actor_scope(alice), f.case.database.immediate_session() as session:
        changed = update_in_session(
            session,
            item_id,
            {
                "expected_revision": 1,
                "name": "Edited team sound",
                "preferred_variant_id": preferred,
            },
            workspace_id=f.users.workspace_id,
        )
    assert changed["revision"] == 2
    assert changed["preferred_variant"]["id"] == preferred

    with execution_actor_scope(alice), f.case.database.immediate_session() as session:
        no_op = update_in_session(
            session,
            item_id,
            {"expected_revision": 2, "name": "Edited team sound"},
            workspace_id=f.users.workspace_id,
        )
    assert no_op["revision"] == 2

    with execution_actor_scope(alice), f.case.database.immediate_session() as session:
        with pytest.raises(RevisionConflict):
            update_in_session(
                session,
                item_id,
                {"expected_revision": 1, "name": "stale"},
                workspace_id=f.users.workspace_id,
            )

    with execution_actor_scope(alice), f.case.database.immediate_session() as session:
        with pytest.raises(WorkspaceNotFound):
            update_in_session(
                session,
                item_id,
                {"expected_revision": 2, "preferred_variant_id": foreign},
                workspace_id=f.users.workspace_id,
            )
    with f.case.database.snapshot_session() as session:
        item = session.get(SoundItem, item_id)
        assert item is not None and item.revision == 2 and item.preferred_variant_id == preferred


def test_discard_keeps_workspace_active_job_attached_candidate_and_soft_state_guards(sound_case):
    f = sound_case
    alice = _actor(f.case, f.users.alice)
    home = _personal_workspace(f.case, f.users.alice_id)
    item_id = _item(f.case, home, "Saved candidate owner")
    attached = _candidate(f.case, home, item_id=item_id)
    disposable = _candidate(f.case, home)

    with f.case.database.immediate_session() as session:
        active_job = Job(
            kind="sound.generate",
            workspace_id=home,
            status="queued",
            payload_json={},
            resource_keys_json=[],
        )
        session.add(active_job)
        session.flush()
        active_job_id = active_job.id
    active = _candidate(f.case, home, job_id=active_job_id)

    with execution_actor_scope(alice), f.case.database.immediate_session() as session:
        with pytest.raises(ValueError, match="Saved candidates"):
            discard_in_session(session, attached, workspace_id=home)
        with pytest.raises(ValueError, match="generation job is active"):
            discard_in_session(session, active, workspace_id=home)
        result = discard_in_session(session, disposable, workspace_id=home)
    assert result["status"] == "discarded"
    with f.case.database.snapshot_session() as session:
        assert session.get(SoundVariant, attached).state == "ready"
        assert session.get(SoundVariant, active).state == "ready"
        assert session.get(SoundVariant, disposable).state == "discarded"


def test_archived_workspace_membership_departure_and_account_revocation_are_fresh(sound_case):
    f = sound_case
    alice = _actor(f.case, f.users.alice)
    item_id = _item(f.case, f.users.workspace_id, "Member sound")

    with f.case.database.immediate_session() as session:
        session.get(Workspace, f.users.workspace_id).state = "archived"
    with execution_actor_scope(alice), f.case.database.snapshot_session() as session:
        with pytest.raises(WorkspaceNotFound):
            get_sound(session, item_id, workspace_id=f.users.workspace_id)

    with f.case.database.immediate_session() as session:
        session.get(Workspace, f.users.workspace_id).state = "active"
        membership = session.get(WorkspaceMembership, (f.users.workspace_id, f.users.alice_id))
        assert membership is not None
        session.delete(membership)
    with execution_actor_scope(alice), f.case.database.snapshot_session() as session:
        with pytest.raises(WorkspaceNotFound):
            list_sounds(session, workspace_id=f.users.workspace_id)

    with f.case.database.immediate_session() as session:
        session.add(
            WorkspaceMembership(
                workspace_id=f.users.workspace_id,
                user_id=f.users.alice_id,
                role="member",
            )
        )
        user = session.get(User, f.users.alice_id)
        assert user is not None
        user.is_active = False
    with execution_actor_scope(alice), f.case.database.snapshot_session() as session:
        with pytest.raises(AccessDenied):
            list_sounds(session, workspace_id=f.users.workspace_id)


def test_browser_logout_revokes_ordinary_reads_but_not_captured_leased_read(sound_case):
    f = sound_case
    alice = _actor(f.case, f.users.alice)
    home = _personal_workspace(f.case, f.users.alice_id)
    _item(f.case, home, "Captured actor sound")
    lease = _leased_job(f.case, home, alice)

    response = f.users.alice.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": f.users.ah["X-CSRF-Token"]},
    )
    assert response.status_code == 200

    with execution_actor_scope(alice), f.case.database.snapshot_session() as session:
        with pytest.raises(AccessDenied):
            list_sounds(session)

    token = current_job_lease.set(lease)
    try:
        with execution_actor_scope(alice), f.case.database.snapshot_session() as session:
            assert len(list_sounds(session)["items"]) == 1
    finally:
        current_job_lease.reset(token)


def test_host_and_legacy_actorless_defaults_remain_structural_but_cannot_borrow_job_actor(
    sound_case,
):
    f = sound_case
    owner_home = _personal_workspace(f.case, _actor(f.case, f.users.owner).user_id)
    legacy_item = _item(f.case, owner_home, "Legacy structural sound")
    no_actor_lease = _leased_job(f.case, owner_home)
    saved_actor_lease = _leased_job(f.case, owner_home, _actor(f.case, f.users.alice))

    with execution_actor_scope(None), f.case.database.snapshot_session() as session:
        assert require_sound_library_workspace(session, ProjectAction.READ).id == owner_home
        assert legacy_item in {row["id"] for row in list_sounds(session)["items"]}

    with execution_actor_scope(HostActor("pandrator-cli")), f.case.database.snapshot_session() as session:
        assert (
            require_sound_library_workspace(
                session,
                ProjectAction.EDIT,
                workspace_id=f.users.workspace_id,
            ).id
            == f.users.workspace_id
        )

    token = current_job_lease.set(no_actor_lease)
    try:
        with execution_actor_scope(None), f.case.database.snapshot_session() as session:
            assert require_sound_library_workspace(session, ProjectAction.READ).id == owner_home
    finally:
        current_job_lease.reset(token)

    token = current_job_lease.set(saved_actor_lease)
    try:
        with execution_actor_scope(None), f.case.database.snapshot_session() as session:
            with pytest.raises(AccessDenied):
                require_sound_library_workspace(session, ProjectAction.READ)
    finally:
        current_job_lease.reset(token)
