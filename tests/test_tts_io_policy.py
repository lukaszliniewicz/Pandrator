"""Pure, execution-local speech authorization policy contracts."""

from contextvars import Context
from dataclasses import FrozenInstanceError

import pytest

from pandrator.logic.tts_io_policy import (
    TtsIoDenied,
    check_tts_io,
    current_tts_io_policy,
    redact_tts_diagnostic,
    tts_environment_credentials_allowed,
    tts_host_adc_allowed,
    tts_io_scope,
)


def test_absent_policy_is_inert_and_scope_is_context_local() -> None:
    assert current_tts_io_policy() is None
    assert tts_environment_credentials_allowed() and tts_host_adc_allowed()
    check_tts_io()
    calls: list[str] = []
    with tts_io_scope(lambda: calls.append("outer"), allow_environment_credentials=False):
        outer = current_tts_io_policy()
        assert outer is not None
        assert not tts_environment_credentials_allowed() and tts_host_adc_allowed()
        with pytest.raises(FrozenInstanceError):
            outer.__setattr__("allow_host_adc", False)
        assert Context().run(current_tts_io_policy) is None
        with tts_io_scope(lambda: calls.append("inner"), allow_host_adc=False):
            assert tts_environment_credentials_allowed() and not tts_host_adc_allowed()
            check_tts_io()
        assert current_tts_io_policy() is outer
        check_tts_io()
    assert calls == ["inner", "outer"]
    assert current_tts_io_policy() is None
    assert tts_environment_credentials_allowed() and tts_host_adc_allowed()


def test_denial_has_stable_message_cause_and_restores_nested_policy() -> None:
    cause = RuntimeError("synthetic revoked identity")

    def deny() -> None:
        raise cause

    with tts_io_scope(lambda: None):
        outer = current_tts_io_policy()
        with pytest.raises(TtsIoDenied) as caught, tts_io_scope(deny):
            check_tts_io()
        assert str(caught.value) == "The speech request is no longer authorized."
        assert caught.value.__cause__ is cause
        assert isinstance(caught.value, PermissionError)
        assert current_tts_io_policy() is outer
    assert current_tts_io_policy() is None


def test_existing_denial_propagates_unchanged() -> None:
    denial = TtsIoDenied("The speech request is no longer authorized.")

    def deny() -> None:
        raise denial

    with pytest.raises(TtsIoDenied) as caught, tts_io_scope(deny):
        check_tts_io()
    assert caught.value is denial
    assert current_tts_io_policy() is None


def test_diagnostic_redaction_is_context_local_and_restores_outer_scope() -> None:
    assert redact_tts_diagnostic("provider detail") == "provider detail"
    with tts_io_scope(
        lambda: None, redact_diagnostic=lambda value: str(value).replace("secret", "hidden")
    ):
        assert redact_tts_diagnostic("provider secret") == "provider hidden"
        assert Context().run(redact_tts_diagnostic, "provider secret") == "provider secret"
        with tts_io_scope(lambda: None):
            assert redact_tts_diagnostic("provider secret") == "provider secret"
        assert redact_tts_diagnostic("provider secret") == "provider hidden"
    assert redact_tts_diagnostic("provider secret") == "provider secret"
