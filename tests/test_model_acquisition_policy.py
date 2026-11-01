"""Unit tests for the context-local model acquisition permission boundary."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import cast

import pytest

from pandrator.logic.model_acquisition import (
    AcquisitionAuthorizer,
    ModelAcquisitionDenied,
    host_model_acquisition_scope,
    model_acquisition_scope,
    require_model_acquisition,
)

_DENIED_MESSAGE = "A required model may need to be downloaded. Ask an Administrator to prepare it."


def test_default_context_denies_without_an_authorizer() -> None:
    with pytest.raises(ModelAcquisitionDenied) as error:
        require_model_acquisition()
    assert str(error.value) == _DENIED_MESSAGE
    assert isinstance(error.value, RuntimeError)
    assert not isinstance(error.value, OSError)


def test_authorizer_is_called_fresh_on_every_invocation() -> None:
    calls = 0

    def authorizer() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("synthetic denial")

    with model_acquisition_scope(authorizer):
        assert require_model_acquisition() is None
        with pytest.raises(ModelAcquisitionDenied) as error:
            require_model_acquisition()

    assert calls == 2
    assert str(error.value) == _DENIED_MESSAGE
    assert error.value.__cause__ is None


def test_non_none_callback_result_denies_and_unrelated_errors_propagate() -> None:
    non_none_authorizer = cast(AcquisitionAuthorizer, lambda: True)
    with model_acquisition_scope(non_none_authorizer):
        with pytest.raises(ModelAcquisitionDenied, match="^" + _DENIED_MESSAGE + "$"):
            require_model_acquisition()

    failure = LookupError("synthetic unrelated failure")

    def unrelated_error() -> None:
        raise failure

    with model_acquisition_scope(unrelated_error):
        with pytest.raises(LookupError) as error:
            require_model_acquisition()
    assert error.value is failure


def test_nested_scopes_clear_none_and_restore_after_exceptions() -> None:
    calls = 0

    def outer_authorizer() -> None:
        nonlocal calls
        calls += 1

    def inner_authorizer() -> None:
        return None

    with model_acquisition_scope(outer_authorizer):
        require_model_acquisition()
        with model_acquisition_scope(None):
            with pytest.raises(ModelAcquisitionDenied):
                require_model_acquisition()
        require_model_acquisition()
        assert calls == 2

        with pytest.raises(RuntimeError, match="synthetic scope failure"):
            with model_acquisition_scope(inner_authorizer):
                require_model_acquisition()
                raise RuntimeError("synthetic scope failure")
        require_model_acquisition()
        assert calls == 3

    with pytest.raises(ModelAcquisitionDenied):
        require_model_acquisition()


@pytest.mark.parametrize("invalid", [0, "not callable", object()])
def test_invalid_scope_input_fails_before_changing_outer_scope(invalid: object) -> None:
    calls = 0

    def authorizer() -> None:
        nonlocal calls
        calls += 1

    with model_acquisition_scope(authorizer):
        with pytest.raises(TypeError):
            with model_acquisition_scope(cast(AcquisitionAuthorizer, invalid)):
                pytest.fail("invalid scope unexpectedly entered")
        require_model_acquisition()
    assert calls == 1


def test_host_operator_scope_is_explicit_and_restores_default_deny() -> None:
    with host_model_acquisition_scope():
        assert require_model_acquisition() is None
        with model_acquisition_scope(None):
            with pytest.raises(ModelAcquisitionDenied):
                require_model_acquisition()
        assert require_model_acquisition() is None
    with pytest.raises(ModelAcquisitionDenied):
        require_model_acquisition()


def test_new_thread_defaults_to_deny_and_copy_context_propagates_deliberately() -> None:
    def thread_result() -> str:
        try:
            require_model_acquisition()
        except ModelAcquisitionDenied as error:
            return str(error)
        raise AssertionError("new thread unexpectedly inherited model authority")

    calls = 0

    def authorizer() -> None:
        nonlocal calls
        calls += 1

    with model_acquisition_scope(authorizer):
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(thread_result).result() == _DENIED_MESSAGE
            copied = copy_context()
            assert executor.submit(copied.run, require_model_acquisition).result() is None
            assert executor.submit(thread_result).result() == _DENIED_MESSAGE
    assert calls == 1
