"""Disposable non-HTTP tests for the account-bound project-link foundation."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import select

from pandrator.web.access.contracts import AccessContext
from pandrator.web.accounts.auth_contracts import ALL_SCOPES
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.execution_actor import UserActor
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.audit import AuditService
from pandrator.web.auth import AuthService
from pandrator.web.credentials import SecretRedactor
from pandrator.web.database import Database
from pandrator.web.model_base import Base, new_id
from pandrator.web.models import (
    Artifact,
    Document,
    DocumentRevision,
    SessionRecord,
    SessionStageSelection,
)
from pandrator.web.projects.models import Project
from pandrator.web.public_link_http import request_allowed
from pandrator.web.public_links.contracts import PublicLinkError
from pandrator.web.public_links.service import PublicLinkService

VERIFY_ROOT = (
    Path(__file__).resolve().parents[1]
    / ".local-notes/public-access-sharing-2026-11-01/verification/resources"
)


@dataclass
class Case:
    database: Database
    service: PublicLinkService
    admin: AccessContext
    outsider: AccessContext
    project_id: str
    other_project_id: str
    workspace_id: str
    source_id: str
    translation_id: str


@pytest.fixture(scope="session")
def schema_template() -> Iterator[Path]:
    VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=VERIFY_ROOT) as directory:
        path = Path(directory) / "schema.sqlite3"
        database = Database(path)
        Base.metadata.create_all(database.engine)
        database.engine.dispose()
        yield path


@pytest.fixture
def case(schema_template: Path) -> Iterator[Case]:
    VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=VERIFY_ROOT) as directory:
        path = Path(directory) / "test.sqlite3"
        shutil.copyfile(schema_template, path)
        db = Database(path)
        with db.immediate_session() as session:
            owner = User(display_name="Owner", is_instance_admin=True)
            outsider = User(display_name="Other admin", is_instance_admin=True)
            session.add_all([owner, outsider])
            session.flush()
            session.add_all(
                [
                    UserCredential(user_id=owner.id, login_name="owner", auth_revision=1),
                    UserCredential(user_id=outsider.id, login_name="other", auth_revision=1),
                ]
            )
            workspace = Workspace(kind="team", name="Team")
            session.add(workspace)
            session.flush()
            session.add(
                WorkspaceMembership(workspace_id=workspace.id, user_id=owner.id, role="owner")
            )
            projects = [Project(workspace_id=workspace.id, name=name) for name in ("Main", "Other")]
            session.add_all(projects)
            session.flush()
            sources = [SessionRecord(project_id=project.id, name="Root") for project in projects]
            session.add_all(sources)
            session.flush()
            for project, source in zip(projects, sources, strict=True):
                project.source_session_id = source.id
            language = SessionRecord(project_id=projects[0].id, name="Polish", target_language="pl")
            session.add(language)
            session.flush()
            document = Document(session_id=language.id, stage="translation", language="pl")
            session.add(document)
            session.flush()
            revision = DocumentRevision(
                document_id=document.id, revision_number=1, content_hash="c" * 64
            )
            session.add(revision)
            session.flush()
            document.active_revision_id = revision.id
            artifact = Artifact(
                workspace_id=workspace.id,
                project_id=projects[0].id,
                session_id=language.id,
                kind="subtitles",
                role="translation",
                relative_path="synthetic.srt",
                content_hash="a" * 64,
                metadata_json={
                    "document_id": document.id,
                    "revision_id": revision.id,
                    "revision_number": 1,
                },
            )
            session.add(artifact)
            session.flush()
            session.add(
                SessionStageSelection(
                    session_id=language.id, stage_key="translate", artifact_id=artifact.id
                )
            )
            ids = (
                owner.id,
                outsider.id,
                projects[0].id,
                projects[1].id,
                workspace.id,
                sources[0].id,
                artifact.id,
            )
        auth = AuthService(db)
        contexts = []
        for user in ids[:2]:
            issued = auth.browser_sessions.issue(
                user, 1, subject=f"test:{user}", principal_kind="owner_session", scopes=ALL_SCOPES
            )
            contexts.append(
                AccessContext(
                    UserActor(
                        user,
                        1,
                        f"test:{user}",
                        "owner_session",
                        ALL_SCOPES,
                        browser_session_id=issued.record_id,
                    )
                )
            )
        service = PublicLinkService(db, auth, AuditService(db, SecretRedactor(db)))
        try:
            yield Case(db, service, contexts[0], contexts[1], *ids[2:])
        finally:
            db.engine.dispose()


@pytest.fixture
def manager(case: Case):
    from tests.test_public_link_foundation import create, enable, redeem

    enable(case)
    created = create(case)
    issued = redeem(case, created)
    return case.service.session_context(created["link"]["id"], issued.raw_browser_session)


class FakeCatalogue:
    def __init__(self, records):
        self.records = records
        self.calls = []
        self.settings_calls = []

    def _settings(self, *, db_session, workspace_id):
        from pandrator.web.tts_catalogue_service import TtsCatalogueService

        captured = TtsCatalogueService._settings_in_session(db_session, workspace_id=workspace_id)
        self.settings_calls.append((workspace_id, captured))
        return captured

    def snapshot(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["refresh"] is False
        assert kwargs["approved_only"] is True
        assert kwargs["view"] == "compact"
        return {
            "services": self.records,
            "value": {"private": "workspace-private"},
            "voices": ["private-voice"],
        }, 5


def services_for(case: Case, records):
    from types import SimpleNamespace

    from pandrator.runtime import DataPaths
    from pandrator.web.workspace_settings import WorkspaceSettingsService

    return SimpleNamespace(
        database=case.database,
        paths=DataPaths(case.database.path.parent),
        workspace_settings=WorkspaceSettingsService(case.database),
        tts_catalogue=FakeCatalogue(records),
    )


def seed_settings(case: Case):
    from pandrator.web.accounts.models import Workspace
    from pandrator.web.models import SessionSetting, WorkspaceSetting

    configurations = [
        {
            "id": "kokoro",
            "name": "Kokoro",
            "adapter": "kokoro",
            "models": ["model-a", "private-model"],
            "default_model": "model-a",
            "api_base": "https://private.invalid",
            "secret_ref": "private-reference",
        },
        {
            "id": "xtts",
            "name": "XTTS",
            "adapter": "xtts",
            "models": ["model-b", "private-model"],
            "default_model": "model-b",
        },
    ]
    with case.database.immediate_session() as session:
        owner = session.get(User, case.admin.actor.user_id)
        owner.legacy_subject = "owner"
        personal = Workspace(
            kind="personal", personal_owner_user_id=owner.id, name="Private personal namespace"
        )
        session.add(personal)
        session.flush()
        session.add(WorkspaceMembership(workspace_id=personal.id, user_id=owner.id, role="owner"))
        session.add(
            WorkspaceSetting(
                workspace_id=case.workspace_id,
                key="services.tts",
                value_json={"provider_configs": configurations},
                revision=5,
            )
        )
        session.add(
            WorkspaceSetting(
                workspace_id=case.workspace_id,
                key="services.stt",
                value_json={
                    "provider_configs": [
                        {
                            "id": "azure_mai_transcribe_2",
                            "name": "Cloud two",
                            "adapter": "azure",
                            "model": "MAI-Transcribe-2",
                            "models": ["MAI-Transcribe-2", "private-model"],
                            "api_base": "https://private.invalid",
                        },
                        {
                            "id": "azure_mai_transcribe_1_5",
                            "name": "Cloud one",
                            "adapter": "azure",
                            "model": "mai-transcribe-1.5",
                            "models": ["mai-transcribe-1.5", "private-model"],
                        },
                    ]
                },
                revision=7,
            )
        )
        other = session.scalar(
            select(SessionRecord.id).where(SessionRecord.project_id == case.other_project_id)
        )
        for session_id, service, model, engine, stt_model in [
            (case.source_id, "kokoro", "model-a", "azure_mai_transcribe_2", "MAI-Transcribe-2"),
            (other, "xtts", "model-b", "azure_mai_transcribe_1_5", "mai-transcribe-1.5"),
        ]:
            session.add(
                SessionSetting(
                    session_id=session_id,
                    section="tts",
                    value_json={"service": service, "model": model},
                )
            )
            session.add(
                SessionSetting(
                    session_id=session_id,
                    section="stt",
                    value_json={"stt_engine": engine, "model": stt_model},
                )
            )
    return other


def private_record(identifier="kokoro", model="model-a", *, available=False, online=None):
    return {
        "id": identifier,
        "name": identifier,
        "adapter": identifier,
        "available": available,
        "online": online,
        "supports_voice_cloning": True,
        "supports_predefined_voices": True,
        "has_multiple_models": True,
        "models": [model, "private-model"],
        "default_model": model,
        "model_catalog": [
            {
                "id": model,
                "label": "Selected",
                "loaded": False,
                "available": None,
                "path": "/private/model",
                "credentials": {"api_key": "private-secret"},
                "voice_reference_text": "private text",
                "voice_catalogues": ["private-voice"],
            },
            {"id": "private-model", "label": "Private"},
        ],
        "api_base": "https://private.invalid",
        "endpoint": "private-endpoint",
        "path": "/private/path",
        "credential_reference": "private-reference",
        "api_key": "private-secret",
        "voices": ["private-voice"],
        "managed_voices": ["private-voice"],
        "collections": ["private-collection"],
        "voice_catalogues": {model: ["private-voice"]},
        "voice_reference_text": "private text",
    }


def test_tts_projects_select_only_configured_service_model_and_strip_private_fields(
    case: Case, manager
) -> None:
    import json

    from pandrator.web.accounts.execution_actor import (
        current_execution_actor,
        execution_actor_scope,
    )
    from pandrator.web.public_links.resources import project_link_catalogue

    other = seed_settings(case)
    services = services_for(case, [private_record(), private_record("xtts", "model-b")])
    before = current_execution_actor.get()
    first = project_link_catalogue(services, manager, case.source_id, "tts")
    assert current_execution_actor.get() == before
    row = first["services"][0]
    assert row["id"] == "kokoro" and row["models"] == ["model-a"]
    assert row["available"] is False and row["online"] is None
    assert row["model_catalog"] == [
        {"id": "model-a", "label": "Selected", "loaded": False, "available": None}
    ]
    assert set(row) == {
        "id",
        "name",
        "adapter",
        "available",
        "online",
        "supports_voice_cloning",
        "supports_predefined_voices",
        "has_multiple_models",
        "models",
        "model_catalog",
    }
    encoded = json.dumps(first)
    for forbidden in [
        "private-model",
        "private.invalid",
        "private-secret",
        "private-reference",
        "/private/",
        "private-voice",
        "private-collection",
        "private text",
    ]:
        assert forbidden not in encoded
    assert (
        first["session_id"] == case.source_id
        and first["scope"] == "project"
        and first["can_edit"] is False
    )
    call = services.tts_catalogue.calls[0]
    assert (
        call["service_ids"] == ["kokoro"]
        and call["workspace_id"] == case.workspace_id
        and call["allow_host_adc"] is False
    )
    assert call["settings_snapshot"] == services.tts_catalogue.settings_calls[0][1]
    created = case.service.create_link(
        case.admin, case.other_project_id, label="Other", mode="manage_project"
    )
    from tests.test_public_link_foundation import redeem

    issued = redeem(case, created)
    other_context = case.service.session_context(created["link"]["id"], issued.raw_browser_session)
    with execution_actor_scope(other_context.actor):
        second = project_link_catalogue(services, other_context, other, "tts")
        assert current_execution_actor.get() == other_context.actor
    assert second["services"][0]["id"] == "xtts" and second["services"][0]["models"] == ["model-b"]
    assert services.tts_catalogue.calls[-1]["service_ids"] == ["xtts"]


def test_foreign_review_and_normal_contexts_deny_before_catalogue(case: Case, manager) -> None:
    from dataclasses import replace

    from pandrator.web.public_links.resources import project_link_catalogue

    other = seed_settings(case)
    services = services_for(case, [private_record()])
    contexts = [
        (manager, other, "tts"),
        (manager, case.source_id, "unknown"),
        (replace(manager, actor=case.admin.actor), case.source_id, "tts"),
    ]
    from tests.test_public_link_foundation import create, redeem

    created = create(case, mode="review_translation", target_artifact_id=case.translation_id)
    issued = redeem(case, created)
    review = case.service.session_context(created["link"]["id"], issued.raw_browser_session)
    contexts.append((review, case.source_id, "tts"))
    for context, session_id, kind in contexts:
        with pytest.raises(PublicLinkError):
            project_link_catalogue(services, context, session_id, kind)
    assert not services.tts_catalogue.calls and not services.tts_catalogue.settings_calls
    case.service.revoke_link(case.admin, manager.link["id"], expected_revision=1)
    with pytest.raises(PublicLinkError):
        project_link_catalogue(services, manager, case.source_id, "tts")


def test_missing_model_service_and_effective_default_preserve_availability(
    case: Case, manager
) -> None:
    from pandrator.web.models import SessionSetting
    from pandrator.web.public_links.resources import project_link_catalogue

    seed_settings(case)
    services = services_for(
        case, [private_record(model="different-model", available=None, online=False)]
    )
    result = project_link_catalogue(services, manager, case.source_id, "tts")
    assert result["warnings"] == [
        {"code": "configured_model_unavailable", "service_id": "kokoro", "model_id": "model-a"}
    ]
    assert result["services"][0]["models"] == [] and result["services"][0]["model_catalog"] == []
    assert result["services"][0]["available"] is None and result["services"][0]["online"] is False
    services.tts_catalogue.records = [private_record(identifier="xtts")]
    result = project_link_catalogue(services, manager, case.source_id, "tts")
    assert result["services"] == [] and result["warnings"] == [
        {"code": "configured_service_unavailable", "service_id": "kokoro", "model_id": ""}
    ]
    with case.database.immediate_session() as session:
        session.get(SessionSetting, (case.source_id, "tts")).value_json = {"service": "kokoro"}
    services.tts_catalogue.records = [private_record()]
    assert project_link_catalogue(services, manager, case.source_id, "tts")["services"][0][
        "models"
    ] == ["model-a"]


def test_stt_approved_projection_filters_service_model_and_local_auto_is_empty(
    case: Case, manager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pandrator.web.models import SessionSetting
    from pandrator.web.public_links import resources

    seed_settings(case)
    services = services_for(case, [])
    calls = []

    def snapshot(database, paths, **kwargs):
        calls.append(kwargs)
        assert kwargs["approved_only"] is True and "refresh" not in kwargs
        assert kwargs["workspace_id"] == case.workspace_id
        assert kwargs["settings_snapshot"][1] == 7
        return {
            "services": [
                private_record("azure_mai_transcribe_2", "MAI-Transcribe-2"),
                private_record("azure_mai_transcribe_1_5", "mai-transcribe-1.5"),
            ]
        }, 7

    monkeypatch.setattr(resources, "stt_catalogue_snapshot", snapshot)
    result = resources.project_link_catalogue(services, manager, case.source_id, "stt")
    assert result["services"][0]["id"] == "azure_mai_transcribe_2"
    assert result["services"][0]["models"] == ["MAI-Transcribe-2"] and result["warnings"] == []
    for engine in ["auto", "whisper", "qwen3"]:
        with case.database.immediate_session() as session:
            session.get(SessionSetting, (case.source_id, "stt")).value_json = {"stt_engine": engine}
        assert (
            resources.project_link_catalogue(services, manager, case.source_id, "stt")["services"]
            == []
        )
    assert len(calls) == 1


def test_new_catalogue_route_gate_is_exact_rooted_and_manager_only(case: Case, manager) -> None:
    from tests.test_public_link_http_policy import request_double

    rule = "/api/v1/public-links/<link_id>/services/<kind>"
    for kind in ["tts", "stt"]:
        request = request_double(
            manager,
            rule,
            "HEAD",
            arguments={"link_id": manager.link["id"], "kind": kind},
            query={"session_id": case.source_id},
        )
        assert request_allowed(request, manager, case.database)
    for arguments, query in [
        ({"link_id": manager.link["id"], "kind": "voices"}, {"session_id": case.source_id}),
        ({"link_id": new_id(), "kind": "tts"}, {"session_id": case.source_id}),
        ({"link_id": manager.link["id"], "kind": "tts"}, {}),
    ]:
        with pytest.raises(PublicLinkError):
            request_allowed(
                request_double(manager, rule, arguments=arguments, query=query),
                manager,
                case.database,
            )


def test_real_approved_helpers_never_resolve_credentials_probe_or_discover(
    case: Case, manager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from pandrator.web import stt_providers, tts_catalogue_service
    from pandrator.web.public_links.resources import project_link_catalogue
    from pandrator.web.tts_catalogue_service import TtsCatalogueService

    seed_settings(case)
    services = services_for(case, [])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("No provider, Manager inventory or credential resolution is permitted")

    providers = SimpleNamespace(capabilities=forbidden, health=forbidden, enrich_catalog=forbidden)
    manager_bridge = SimpleNamespace(configured=False, inventory=forbidden)
    services.tts_catalogue = TtsCatalogueService(
        case.database, services.paths, providers, manager_bridge=manager_bridge
    )
    monkeypatch.setattr(tts_catalogue_service, "resolve_provider_credentials", forbidden)
    monkeypatch.setattr(stt_providers, "_enrich_credential", forbidden)
    tts = project_link_catalogue(services, manager, case.source_id, "tts")
    assert [row["id"] for row in tts["services"]] == ["kokoro"]
    assert tts["services"][0]["available"] is None and tts["services"][0]["online"] is None
    stt = project_link_catalogue(services, manager, case.source_id, "stt")
    assert [row["id"] for row in stt["services"]] == ["azure_mai_transcribe_2"]
    assert stt["services"][0]["models"] == ["MAI-Transcribe-2"]
