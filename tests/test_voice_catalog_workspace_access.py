"""Actual modern voice catalogue scope, writer fences and exact receipt namespaces."""

from __future__ import annotations

import socket

import pytest
from sqlalchemy import func, select

from pandrator.web import models as m
from pandrator.web import tts_catalogue_service as tts_module
from pandrator.web import voice_catalog_routes as routes
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_collaboration_authentication import enable, new_account
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import promote, scoped_token
from tests.test_settings_workspace_access import put
from tests.test_speech_catalogue_workspace_access import catalogue as catalogue  # noqa: F401


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Voice catalogue qualification is local only")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    def no_adc(**_kwargs):
        raise DefaultCredentialsError("Synthetic fixture has no ambient ADC")

    monkeypatch.setattr(google.auth, "default", no_adc)


def settings(client, csrf, workspace=None, *, label="same", revision=0):
    return body(
        put(
            client,
            csrf,
            "services.tts",
            {
                "provider_configs": [
                    {
                        "id": "openai",
                        "api_base": f"https://{label}.invalid/v1",
                        "api_key": "synthetic-voice-key",
                        "credential_backend": "database",
                        "models": ["same-model"],
                        "default_model": "same-model",
                        "voices": ["alpha", "beta"],
                    }
                ]
            },
            workspace=workspace,
            revision=revision,
        )
    )


def no_named_effects(catalogue, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Named catalogue reached a private/seed/provider/Manager boundary")

    monkeypatch.setattr(routes, "ensure_bundled_voice", forbidden)
    for name in ("resolve_provider_credentials", "list_tts_provider_profiles"):
        monkeypatch.setattr(tts_module, name, forbidden)
    for name in (
        "_previews",
        "_cached_native_discovery",
        "_merge_remembered_google_voices",
        "_apply_health_observations",
    ):
        monkeypatch.setattr(catalogue.service, name, forbidden)
    monkeypatch.setattr(catalogue.service.manager_bridge, "inventory", forbidden)
    monkeypatch.setattr(catalogue.service.providers, "health", forbidden)
    monkeypatch.setattr(catalogue.service.providers, "enrich_catalog", forbidden)
    monkeypatch.setattr("google.auth.default", forbidden)


def collection(client, csrf, name, *, wid=None, key="voice-collection-create"):
    return client.post(
        "/api/v1/voice-collections",
        query_string={} if wid is None else {"workspace_id": wid},
        json={"name": name},
        headers=headers(csrf, key),
    )


def provider_ref():
    return {"kind": "provider", "service_id": "openai", "model": "same-model", "voice": "alpha"}


def annotate(client, csrf, wid, *, key="voice-provider-annotation", revision=0, changes=None):
    return client.patch(
        "/api/v1/voice-catalog/metadata",
        query_string={"workspace_id": wid},
        json={
            "reference": provider_ref(),
            "expected_revision": revision,
            "changes": changes or {"name": "Annotated"},
        },
        headers=headers(csrf, key),
    )


def test_named_homes_team_and_configured_manager_reads_never_seed_or_inspect(
    case, catalogue, monkeypatch
):
    members = team(case, monkeypatch)
    promote(case, members)
    own = settings(members.alice, members.ah, label="alice")
    bob = settings(members.bob, members.bh, label="bob")
    settings(members.owner, members.oh, members.workspace_id, label="team")
    for wid, name in (
        (own["workspace_id"], "Alice only"),
        (bob["workspace_id"], "Bob only"),
        (members.workspace_id, "Team only"),
    ):
        with case.database.immediate_session() as session:
            session.add(m.Voice(workspace_id=wid, name=name))
    ac = body(collection(members.alice, members.ah, "Alice collection"), 201)
    bc = body(collection(members.bob, members.bh, "Bob collection"), 201)
    tc = body(
        collection(members.alice, members.ah, "Team collection", wid=members.workspace_id), 201
    )
    monkeypatch.setattr(
        type(catalogue.service.manager_bridge), "configured", property(lambda _self: True)
    )
    no_named_effects(catalogue, monkeypatch)
    for client, wid, name, cid in (
        (members.alice, own["workspace_id"], "Alice only", ac["id"]),
        (members.bob, bob["workspace_id"], "Bob only", bc["id"]),
        (members.bob, members.workspace_id, "Team only", tc["id"]),
    ):
        query = {} if wid != members.workspace_id else {"workspace_id": wid}
        result = body(
            client.get("/api/v1/voice-catalog", query_string={**query, "kind": "managed"})
        )
        assert result["workspace_id"] == wid and result["can_edit"]
        assert [row["name"] for row in result["items"]] == [name]
        assert [row["id"] for row in result["collections"]] == [cid]
        caps = body(
            client.get(
                "/api/v1/voice-catalog/capabilities", query_string={**query, "service_id": "openai"}
            )
        )
        assert (
            caps["workspace_id"] == wid
            and caps["can_edit"]
            and caps["models"][0]["model"] == "same-model"
        )
        listed = body(client.get("/api/v1/voice-collections", query_string=query))
        assert listed["workspace_id"] == wid and listed["items"][0]["id"] == cid
    _, readonly = scoped_token(case, members.bob_id, {"app.read"}, workspace=members.workspace_id)
    assert (
        body(
            members.bob.get(
                "/api/v1/voice-catalog",
                query_string={"workspace_id": members.workspace_id},
                headers=readonly,
            )
        )["can_edit"]
        is False
    )
    assert not catalogue.calls


def test_foreign_roots_collection_headers_project_viewer_and_removed_member_are_opaque(
    case, catalogue, monkeypatch
):
    members = team(case, monkeypatch)
    home = settings(members.alice, members.ah)["workspace_id"]
    foreign = body(collection(members.alice, members.ah, "Private"), 201)
    private_session = body(
        members.owner.post(
            "/api/v1/sessions",
            json={"name": "Private root", "workflow_kind": "subtitles"},
            headers=headers(members.oh, "voice-private-root"),
        ),
        201,
    )
    viewers = body(members.owner.get(f"/api/v1/projects/{private_session['project_id']}/viewers"))
    body(
        members.owner.put(
            f"/api/v1/projects/{private_session['project_id']}/viewers/{members.alice_id}",
            json={"expected_revision": viewers["revision"]},
            headers=members.oh,
        )
    )
    assert members.alice.get(f"/api/v1/sessions/{private_session['id']}").status_code == 200
    no_named_effects(catalogue, monkeypatch)

    def protected(*_args, **_kwargs):
        raise AssertionError("Unauthorized root/header reached protected data")

    monkeypatch.setattr(routes, "voice_payloads", protected)
    assert (
        members.owner.get("/api/v1/voice-catalog", query_string={"workspace_id": home}).status_code
        == 404
    )
    assert (
        members.alice.get(
            "/api/v1/voice-catalog", query_string={"session_id": private_session["id"]}
        ).status_code
        == 404
    )
    assert (
        members.bob.get(
            "/api/v1/voice-catalog",
            query_string={"workspace_id": members.workspace_id, "collection_id": foreign["id"]},
        ).status_code
        == 404
    )
    assert (
        members.bob.patch(
            f"/api/v1/voice-collections/{foreign['id']}",
            query_string={"workspace_id": members.workspace_id},
            json={"expected_revision": 1, "name": "Must not change"},
            headers=headers(members.bh, "foreign-update"),
        ).status_code
        == 404
    )
    assert (
        members.owner.get(
            "/api/v1/voice-catalog/capabilities", query_string={"workspace_id": home}
        ).status_code
        == 404
    )
    for root in ("", " ", "x" * 81):
        assert (
            members.alice.get(
                "/api/v1/voice-catalog", query_string={"workspace_id": root}
            ).status_code
            == 422
        )
    assert (
        members.alice.post(
            "/api/v1/voice-collections",
            query_string={"session_id": ""},
            json={"name": "bad"},
            headers=headers(members.ah, "bad-session-scope"),
        ).status_code
        == 422
    )
    with case.database.immediate_session() as session:
        session.delete(
            session.get_one(m.WorkspaceMembership, (members.workspace_id, members.bob_id))
        )
    assert (
        members.bob.get(
            "/api/v1/voice-collections", query_string={"workspace_id": members.workspace_id}
        ).status_code
        == 404
    )


def test_cursor_binds_actual_home_workspace_and_session_without_changing_selectors(
    case, catalogue, monkeypatch
):
    members = team(case, monkeypatch)
    home = settings(members.alice, members.ah)["workspace_id"]
    settings(members.owner, members.oh, members.workspace_id)
    no_named_effects(catalogue, monkeypatch)
    one = body(
        members.alice.get("/api/v1/voice-catalog", query_string={"kind": "provider", "limit": 1})
    )
    same = body(
        members.alice.get(
            "/api/v1/voice-catalog",
            query_string={"workspace_id": members.workspace_id, "kind": "provider", "limit": 1},
        )
    )
    assert one["next_cursor"] and same["catalog_revision"] == one["catalog_revision"]
    assert (
        members.alice.get(
            "/api/v1/voice-catalog",
            query_string={
                "workspace_id": members.workspace_id,
                "kind": "provider",
                "limit": 1,
                "cursor": one["next_cursor"],
            },
        ).status_code
        == 422
    )
    assert (
        members.alice.get(
            "/api/v1/voice-catalog",
            query_string={
                "workspace_id": home,
                "kind": "provider",
                "limit": 1,
                "cursor": one["next_cursor"],
            },
        ).status_code
        == 200
    )
    sessions = [
        body(
            members.alice.post(
                "/api/v1/sessions",
                json={
                    "name": f"Root {index}",
                    "workflow_kind": "subtitles",
                    "workspace_id": members.workspace_id,
                },
                headers=headers(members.ah, f"voice-root-{index}"),
            ),
            201,
        )
        for index in range(2)
    ]
    scoped = body(
        members.alice.get(
            "/api/v1/voice-catalog",
            query_string={"session_id": sessions[0]["id"], "kind": "provider", "limit": 1},
        )
    )
    assert (
        members.alice.get(
            "/api/v1/voice-catalog",
            query_string={
                "session_id": sessions[1]["id"],
                "kind": "provider",
                "limit": 1,
                "cursor": scoped["next_cursor"],
            },
        ).status_code
        == 422
    )
    assert (
        members.alice.get(
            "/api/v1/voice-catalog",
            query_string={"session_id": sessions[0]["id"], "workspace_id": home},
        ).status_code
        == 404
    )
    _, project_only = scoped_token(
        case, members.alice_id, {"app.read"}, project=sessions[0]["project_id"]
    )
    assert (
        members.alice.get(
            "/api/v1/voice-catalog",
            query_string={"session_id": sessions[0]["id"]},
            headers=project_only,
        ).status_code
        == 404
    )


def test_member_writes_revision_retry_and_scoped_managed_evidence_fail_atomically(
    case, catalogue, monkeypatch
):
    members = team(case, monkeypatch)
    wid = members.workspace_id
    foreign_home = settings(members.owner, members.oh)["workspace_id"]
    settings(members.owner, members.oh, wid)
    with case.database.immediate_session() as session:
        voice = m.Voice(workspace_id=foreign_home, name="Foreign voice")
        session.add(voice)
        session.flush()
        foreign_voice = voice.id
    services = case.app.extensions["pandrator"]["services"]
    evidence_path = services.paths.artifacts / "foreign-voice-evidence.wav"
    evidence_path.write_bytes(b"synthetic local audio evidence")
    evidence = services.artifacts.register(
        evidence_path, kind="audio", role="tts_voice_preview", workspace_id=foreign_home
    )
    no_named_effects(catalogue, monkeypatch)
    created = body(collection(members.alice, members.ah, "Team", wid=wid), 201)
    assert created["can_edit"] and created["workspace_id"] == wid
    target = f"/api/v1/voice-collections/{created['id']}"
    payload = {"expected_revision": 1, "add_members": [provider_ref()]}
    first = body(
        members.alice.patch(
            target,
            query_string={"workspace_id": wid},
            json=payload,
            headers=headers(members.ah, "provider-addition"),
        )
    )
    assert first["revision"] == 2 and first["member_count"] == 1
    settings(members.owner, members.oh, wid, revision=1, label="changed")
    retry = members.alice.patch(
        target,
        query_string={"workspace_id": wid},
        json=payload,
        headers=headers(members.ah, "provider-addition"),
    )
    assert body(retry) == first and retry.headers["Idempotency-Replayed"] == "true"
    assert (
        members.alice.patch(
            target,
            query_string={"workspace_id": wid},
            json={"expected_revision": 1, "name": "stale"},
            headers=headers(members.ah, "stale-update"),
        ).status_code
        == 409
    )
    bad = {
        "expected_revision": 2,
        "name": "Must rollback",
        "add_members": [{"kind": "managed", "voice_id": foreign_voice}],
    }
    assert (
        members.alice.patch(
            target,
            query_string={"workspace_id": wid},
            json=bad,
            headers=headers(members.ah, "foreign-member"),
        ).status_code
        == 422
    )
    changed = body(annotate(members.alice, members.ah, wid))
    assert changed["revision"] == 1 and changed["workspace_id"] == wid
    bad_evidence = {
        "profile": {
            "evidence": {
                "pitch": {
                    "source": "audition_review",
                    "status": "reviewed",
                    "artifact_id": evidence.id,
                }
            }
        },
        "name": "Must rollback",
    }
    assert (
        annotate(
            members.alice, members.ah, wid, key="bad-evidence", revision=1, changes=bad_evidence
        ).status_code
        == 422
    )
    _, read = scoped_token(case, members.alice_id, {"app.read"}, workspace=wid)
    assert (
        members.alice.post(
            "/api/v1/voice-collections",
            query_string={"workspace_id": wid},
            json={"name": "No write"},
            headers=read,
        ).status_code
        == 403
    )
    rows = body(members.bob.get("/api/v1/voice-collections", query_string={"workspace_id": wid}))[
        "items"
    ]
    assert rows[0]["name"] == "Team" and rows[0]["revision"] == 2
    assert body(annotate(members.alice, members.ah, wid))["name"] == "Annotated"


def test_same_subject_and_key_are_isolated_by_real_user_and_workspace(case, catalogue, monkeypatch):
    members = team(case, monkeypatch)
    wid = members.workspace_id
    no_named_effects(catalogue, monkeypatch)
    alice, ah = scoped_token(case, members.alice_id, {"app.read", "app.write"}, workspace=wid)
    bob, bh = scoped_token(case, members.bob_id, {"app.read", "app.write"}, workspace=wid)
    with case.database.immediate_session() as session:
        for token in (alice, bob):
            session.get_one(m.ApiToken, token.id).subject = "identical-principal-label"
    a = body(collection(members.alice, ah, "Alice command", wid=wid, key="same-subject-key"), 201)
    b = body(collection(members.bob, bh, "Bob command", wid=wid, key="same-subject-key"), 201)
    assert a["id"] != b["id"]
    t = body(
        collection(members.alice, members.ah, "Team second", wid=wid, key="same-workspace-key"), 201
    )
    h = body(collection(members.alice, members.ah, "Home second", key="same-workspace-key"), 201)
    assert t["workspace_id"] != h["workspace_id"]
    with case.database.immediate_session() as session:
        session.get_one(m.ApiToken, alice.id).scopes_json = ["app.read"]
    assert (
        collection(members.alice, ah, "Alice command", wid=wid, key="same-subject-key").status_code
        == 403
    )
    with case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (wid, members.bob_id)))
    assert (
        collection(members.bob, bh, "Bob command", wid=wid, key="same-subject-key").status_code
        == 404
    )


@pytest.mark.parametrize("loss", ["membership", "scope", "revision"])
def test_preparation_loss_blocks_actual_writer_and_leaves_no_receipt(
    case, catalogue, monkeypatch, loss
):
    members = team(case, monkeypatch)
    wid = members.workspace_id
    settings(members.owner, members.oh, wid)
    token, bearer = scoped_token(case, members.alice_id, {"app.read", "app.write"}, workspace=wid)
    no_named_effects(catalogue, monkeypatch)
    original = catalogue.service.snapshot

    def prepared(**kwargs):
        result = original(**kwargs)
        with case.database.immediate_session() as session:
            if loss == "membership":
                session.delete(session.get_one(m.WorkspaceMembership, (wid, members.alice_id)))
            elif loss == "scope":
                session.get_one(m.ApiToken, token.id).scopes_json = ["app.read"]
            else:
                session.get_one(m.WorkspaceSetting, (wid, "services.tts")).revision += 1
        return result

    monkeypatch.setattr(catalogue.service, "snapshot", prepared)
    result = annotate(members.alice, bearer, wid)
    assert result.status_code == {"membership": 404, "scope": 403, "revision": 409}[loss], (
        result.get_json()
    )
    with case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(m.VoiceCatalogOverride)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(m.ApiIdempotency)
                .where(m.ApiIdempotency.operation_id == "updateCatalogVoiceMetadata")
            )
            == 0
        )


def test_legacy_seed_eligibility_and_old_exact_receipt_survive_enablement(
    case, catalogue, monkeypatch
):
    real_seed = routes.ensure_bundled_voice
    seeded = []

    def seed(*args, **kwargs):
        seeded.append(kwargs["workspace_id"])
        return real_seed(*args, **kwargs)

    monkeypatch.setattr(routes, "ensure_bundled_voice", seed)
    body(case.client.get("/api/v1/voice-catalog/capabilities"))
    body(case.client.get("/api/v1/voice-collections"))
    created = body(
        collection(case.client, {"X-CSRF-Token": case.csrf}, "Legacy", key="legacy-exact-retry"),
        201,
    )
    assert not seeded
    _, readonly = scoped_token(
        case, case.auth.initial_account().user_id, {"app.read"}, workspace=created["workspace_id"]
    )
    body(case.client.get("/api/v1/voice-catalog", headers=readonly))
    assert not seeded
    body(case.client.get("/api/v1/voice-catalog"))
    assert seeded == [created["workspace_id"]]
    with case.database.immediate_session() as session:
        receipt = session.scalar(
            select(m.ApiIdempotency).where(m.ApiIdempotency.idempotency_key == "legacy-exact-retry")
        )
        receipt.response_json = {
            k: v for k, v in receipt.response_json.items() if k not in {"workspace_id", "can_edit"}
        }
    new_account(case)
    enable(case, monkeypatch)
    # Keep the same genuine credential/subject; a fresh named login is a
    # different principal namespace, not an exact retry of this browser command.
    replay = collection(
        case.client, {"X-CSRF-Token": case.csrf}, "Legacy", key="legacy-exact-retry"
    )
    assert (
        body(replay, 201)["id"] == created["id"]
        and replay.headers["Idempotency-Replayed"] == "true"
    )
    assert "workspace_id" not in body(replay, 201)
    body(case.client.get("/api/v1/voice-catalog"))
    assert seeded == [created["workspace_id"]]


def test_legacy_solo_visible_builtin_choice_can_be_collected_and_annotated(
    case, catalogue, monkeypatch
):
    real_seed = routes.ensure_bundled_voice
    seeds = []

    def seed(*args, **kwargs):
        seeds.append(kwargs["workspace_id"])
        return real_seed(*args, **kwargs)

    monkeypatch.setattr(routes, "ensure_bundled_voice", seed)
    with case.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(m.WorkspaceSetting)
                .where(m.WorkspaceSetting.key == "services.tts")
            )
            == 0
        )
    visible = body(
        case.client.get("/api/v1/voice-catalog", query_string={"kind": "provider", "limit": 1})
    )
    assert visible["items"] and visible["items"][0]["reference"]["kind"] == "provider"
    reference = visible["items"][0]["reference"]
    seeded_before = list(seeds)
    csrf = {"X-CSRF-Token": case.csrf}
    created = body(
        collection(case.client, csrf, "Visible solo choices", key="visible-builtin-collection"), 201
    )
    added = body(
        case.client.patch(
            f"/api/v1/voice-collections/{created['id']}",
            json={"expected_revision": 1, "add_members": [reference]},
            headers=headers(csrf, "visible-builtin-add"),
        )
    )
    assert added["member_count"] == 1 and added["members"][0]["reference"] == reference
    annotated = body(
        case.client.patch(
            "/api/v1/voice-catalog/metadata",
            json={
                "reference": reference,
                "expected_revision": 0,
                "changes": {"name": "Visible built-in annotation"},
            },
            headers=headers(csrf, "visible-builtin-annotate"),
        )
    )
    assert (
        annotated["reference"] == reference and annotated["name"] == "Visible built-in annotation"
    )
    assert seeds == seeded_before and not catalogue.calls
