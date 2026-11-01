"""Execution-local credential redaction without database or provider access."""

from __future__ import annotations

from contextvars import Context
from typing import Any, cast
from unittest.mock import Mock

import pytest

from pandrator.web.credentials import SecretRedactor


def _redactor(
    monkeypatch: pytest.MonkeyPatch,
    cached_values: tuple[str, ...] = (),
) -> tuple[SecretRedactor, Mock]:
    redactor = object.__new__(SecretRedactor)
    values = Mock(return_value=cached_values)
    monkeypatch.setattr(redactor, "values", cast(Any, values))
    return redactor, values


def test_current_secret_is_redacted_even_after_old_values_were_cached(
    monkeypatch: pytest.MonkeyPatch,
):
    previous_secret = "previous-workspace-key-1234"
    rotated_secret = "rotated-workspace-key-5678"
    redactor, cached_values = _redactor(monkeypatch, (previous_secret,))

    assert redactor.values() == (previous_secret,)
    with redactor.scoped_values((rotated_secret,)):
        assert redactor.redact(f"provider rejected {rotated_secret}") == (
            "provider rejected [REDACTED]"
        )
        assert redactor.redact(f"old credential {previous_secret}") == (
            "old credential [REDACTED]"
        )

    assert redactor.redact(rotated_secret) == rotated_secret
    assert cached_values.call_count == 4


def test_scoped_values_do_not_refresh_or_mutate_the_secret_cache(
    monkeypatch: pytest.MonkeyPatch,
):
    redactor, cached_values = _redactor(monkeypatch)

    with redactor.scoped_values(("current-workspace-secret-1234",)):
        assert cached_values.call_count == 0

    assert cached_values.call_count == 0


def test_nested_scopes_restore_after_success_and_exception(
    monkeypatch: pytest.MonkeyPatch,
):
    redactor, _cached_values = _redactor(monkeypatch)
    outer_secret = "outer-operation-secret-1234"
    inner_secret = "inner-operation-secret-5678"
    error_secret = "error-operation-secret-9012"

    with redactor.scoped_values((outer_secret,)):
        assert redactor.redact(outer_secret) == "[REDACTED]"
        with redactor.scoped_values((inner_secret,)):
            assert redactor.redact(f"{outer_secret}|{inner_secret}") == (
                "[REDACTED]|[REDACTED]"
            )
        assert redactor.redact(outer_secret) == "[REDACTED]"
        assert redactor.redact(inner_secret) == inner_secret

        with pytest.raises(RuntimeError, match="provider failed"):
            with redactor.scoped_values((error_secret,)):
                assert redactor.redact(error_secret) == "[REDACTED]"
                raise RuntimeError("provider failed")

        assert redactor.redact(outer_secret) == "[REDACTED]"
        assert redactor.redact(error_secret) == error_secret

    assert redactor.redact(outer_secret) == outer_secret


def test_scoped_values_are_isolated_to_the_current_context(
    monkeypatch: pytest.MonkeyPatch,
):
    secret = "single-operation-secret-1234"
    redactor, _cached_values = _redactor(monkeypatch)

    with redactor.scoped_values((secret,)):
        assert redactor.redact(secret) == "[REDACTED]"
        other_context_result = Context().run(redactor.redact, secret)

    assert other_context_result == secret
    assert redactor.redact(secret) == secret


def test_scoped_values_redact_scalar_and_recursive_values(
    monkeypatch: pytest.MonkeyPatch,
):
    secret = "recursive-operation-secret-1234"
    redactor, _cached_values = _redactor(monkeypatch)

    with redactor.scoped_values((secret,)):
        assert redactor.redact(secret) == "[REDACTED]"
        assert redactor.redact_value(
            {
                "message": secret,
                "items": [secret, {"details": secret}],
                "count": 2,
            }
        ) == {
            "message": "[REDACTED]",
            "items": ["[REDACTED]", {"details": "[REDACTED]"}],
            "count": 2,
        }
