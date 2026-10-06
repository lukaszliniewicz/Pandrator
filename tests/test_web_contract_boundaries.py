"""Native and compatibility boundaries behind the final static-debt slice."""

import socket
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult

from pandrator.logic import llm_handler
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import AuthService
from pandrator.web.automation_enrollment import AutomationEnrollmentService
from pandrator.web.bundles import SessionBundleService
from pandrator.web.context_budget import estimate_tokens
from pandrator.web.credentials import SecretRedactor
from pandrator.web.database import Database
from pandrator.web.idempotency import IdempotencyService
from pandrator.web.models import (
    ApiIdempotency,
    Artifact,
    AutomationClient,
    AutomationEnrollmentGrant,
    SessionRecord,
    utcnow,
)
from pandrator.web.sessions import SessionService
from pandrator.web.web_research import _completion_parts
from pandrator.web.work import _work_state
from pandrator_manager.auth.automation import _scopes
from tests.web_test_support import prepare_web_test_data_root


@pytest.mark.parametrize("kind", ["dict", "current", "legacy", "missing"])
def test_research_usage_accepts_current_legacy_and_dictionary_contracts(kind):
    usage = {"prompt_tokens": 7, "completion_tokens": 3}

    class Current:
        def model_dump(self, *, mode):
            assert mode == "json"
            return usage

    class Legacy:
        def model_dump(self):
            return usage

    value = {"dict": usage, "current": Current(), "legacy": Legacy(), "missing": None}[kind]
    result = SimpleNamespace(content="answer", cost=0.25, cost_source="provider", usage=value)
    content, cost, source, tokens = _completion_parts(result)
    assert (content, cost, source) == ("answer", 0.25, "provider")
    assert tokens["prompt_tokens"] == (0 if kind == "missing" else 7)
    assert tokens["completion_tokens"] == (0 if kind == "missing" else 3)
    assert tokens["total_tokens"] == (0 if kind == "missing" else 10)


def test_research_usage_does_not_retry_unrelated_serialization_failure():
    failure = ValueError("serializer failed")
    calls = []

    class Broken:
        def model_dump(self, **kwargs):
            calls.append(kwargs)
            raise failure

    with pytest.raises(ValueError) as caught:
        _completion_parts(SimpleNamespace(usage=Broken()))
    assert caught.value is failure
    assert calls == [{"mode": "json"}]


@pytest.mark.parametrize("message", [None, "invalid", [], {}])
def test_completion_missing_or_invalid_assistant_message_is_an_empty_dictionary(message):
    result = llm_handler._extract_chat_completion_result({"choices": [{"message": message}]})
    assert result.assistant_message == {}
    assert result.tool_calls == []


def test_completion_assistant_message_and_tool_calls_are_independent_copies():
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call", "extra_content": {"signature": "opaque"}}],
    }
    result = llm_handler._extract_chat_completion_result({"choices": [{"message": message}]})
    result.assistant_message["tool_calls"][0]["extra_content"]["signature"] = "changed"
    assert message["tool_calls"][0]["extra_content"]["signature"] == "opaque"
    assert result.tool_calls[0]["extra_content"]["signature"] == "opaque"


def test_model_free_token_budget_uses_installed_default_tokenizer():
    import litellm

    text = "A short context with Unicode: 日本語."
    expected = litellm.token_counter(model=None, text=text)
    assert isinstance(expected, int) and expected > 0
    assert estimate_tokens(text) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("queued", "queued"),
        ("running", "running"),
        ("succeeded", "succeeded"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("canceled", "cancelled"),
        ("cancel_requested", "running"),
        ("unknown", "waiting"),
        (" RUNNING ", "running"),
        ("", "queued"),
    ],
)
def test_work_state_retains_normalized_public_projection(raw, expected):
    assert _work_state(raw) == expected


@pytest.mark.parametrize("value", [1, True, 1.25, object()])
def test_manager_scopes_reject_noniterable_input_as_validation(value):
    with pytest.raises(ValueError, match="must be a string or an iterable"):
        _scopes(value)


def test_manager_scopes_keep_order_deduplication_and_iterable_compatibility():
    for value in (
        "manager.read manager.runtime manager.read",
        ["manager.read", "manager.runtime", "manager.read"],
        ("manager.read", "manager.runtime", "manager.read"),
        iter(["manager.read", "manager.runtime", "manager.read"]),
        {"manager.read": "ignored", "manager.runtime": "ignored"},
    ):
        assert _scopes(value) == ("manager.read", "manager.runtime")
    for empty in (None, 0, False, "", [], ()):
        with pytest.raises(ValueError, match="At least one"):
            _scopes(empty)
    with pytest.raises(ValueError, match="Unknown Manager"):
        _scopes(["unknown"])


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_waitress_numeric_bind_exposes_daemon_lifecycle_contract(host):
    from waitress.server import create_server

    from pandrator_manager import daemon
    from pandrator_manager.network import EndpointExposure

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family) as probe:
            probe.bind((host, 0))
    except OSError:
        pytest.skip("Loopback address unavailable on this host")
    assert EndpointExposure(bind_host=host).bind_host == host
    assert daemon.create_server is create_server
    server = daemon.create_server(
        lambda environ, start_response: [],
        host=host,
        port=0,
        threads=1,
    )
    try:
        assert 0 < int(server.effective_port) < 65536
        assert isinstance(server._map, dict) and server._map
        assert callable(server.trigger.pull_trigger)
        assert server.task_dispatcher.shutdown(cancel_pending=True, timeout=float("inf"))
    finally:
        server.task_dispatcher.shutdown(cancel_pending=True, timeout=float("inf"))
        server.close()
    assert server._map == {}


def test_native_cleanup_row_counts_remove_only_expired_records(tmp_path):
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    now = utcnow()
    try:
        with database.session() as session:
            client = AutomationClient(
                id="client",
                name="Client",
                subject="client-subject",
                target_instance_id="instance",
                canonical_origin="https://example.test",
                created_by="owner",
            )
            session.add(client)
            session.flush()
            for label, expiry in (
                ("expired", now - timedelta(days=1)),
                ("current", now + timedelta(days=1)),
            ):
                session.add(
                    ApiIdempotency(
                        principal_subject="owner",
                        operation_id="probe",
                        idempotency_key=label,
                        request_digest="digest",
                        expires_at=expiry,
                    )
                )
                session.add(
                    AutomationEnrollmentGrant(
                        client_id=client.id,
                        code_hash=label,
                        code_prefix=label,
                        code_challenge="challenge",
                        expires_at=expiry,
                        token_expires_at=now + timedelta(days=2),
                    )
                )
        idempotency = IdempotencyService(database, SecretRedactor(database))
        assert idempotency.cleanup() == 1
        enrollment = AutomationEnrollmentService(database, AuthService(database))
        assert enrollment.cleanup() == 1
        assert idempotency.cleanup() == 0
        assert enrollment.cleanup() == 0
        with database.session() as session:
            assert [row.idempotency_key for row in session.scalars(select(ApiIdempotency))] == [
                "current"
            ]
            assert [
                row.code_hash for row in session.scalars(select(AutomationEnrollmentGrant))
            ] == ["current"]
            result = session.execute(
                delete(ApiIdempotency).where(ApiIdempotency.idempotency_key == "absent")
            )
            assert isinstance(result, CursorResult) and result.rowcount == 0
    finally:
        database.dispose()


def test_bundle_missing_registered_artifact_aborts_and_cleans_import(tmp_path, monkeypatch):
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    sessions = SessionService(database)
    artifacts = ArtifactService(database, paths)
    bundles = SessionBundleService(database, paths)
    try:
        original = sessions.create("Original")
        original_dir = paths.sessions / original.storage_key
        original_dir.mkdir()
        source = original_dir / "source.txt"
        source.write_text("Keep the source.", encoding="utf-8")
        source_bytes = source.read_bytes()
        artifact = artifacts.register(source, kind="text", role="source", session_id=original.id)
        bundle = paths.root / "session.pandrator-session"
        bundles.export_bundle(original.id, bundle)
        native_register = bundles.artifacts.register
        imported_ids = []

        def register_then_remove(*args, **kwargs):
            record = native_register(*args, **kwargs)
            imported_ids.append(record.session_id)
            with database.session() as session:
                session.delete(session.get(Artifact, record.id))
            return record

        monkeypatch.setattr(bundles.artifacts, "register", register_then_remove)
        with pytest.raises(RuntimeError, match="Imported artifact is no longer available"):
            bundles.import_bundle(bundle)
        assert len(imported_ids) == 1
        with database.session() as session:
            assert session.get(SessionRecord, imported_ids[0]) is None
            assert [row.id for row in session.scalars(select(Artifact))] == [artifact.id]
            assert [row.id for row in session.scalars(select(SessionRecord))] == [original.id]
        assert source.read_bytes() == source_bytes
        assert list(paths.sessions.iterdir()) == [original_dir]
    finally:
        database.dispose()
