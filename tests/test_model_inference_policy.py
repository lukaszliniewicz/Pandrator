"""Context-local policy for prepared inference versus model acquisition."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Callable, cast

import pytest

from pandrator.logic.model_acquisition import (
    ModelAcquisitionDenied,
    host_model_acquisition_scope,
    model_acquisition_scope,
    model_inference_uses_prepared_endpoint,
    require_model_acquisition,
    require_prepared_model_inference,
)

_DENIED_MESSAGE = "A required model may need to be downloaded. Ask an Administrator to prepare it."


def test_missing_inference_callback_is_mode_only_and_preserves_acquisition_gate():
    with pytest.raises(ModelAcquisitionDenied):
        require_model_acquisition()
    assert model_inference_uses_prepared_endpoint() is False
    with pytest.raises(ModelAcquisitionDenied):
        require_prepared_model_inference()

    acquisition_calls = 0

    def authorize_acquisition() -> None:
        nonlocal acquisition_calls
        acquisition_calls += 1

    with model_acquisition_scope(authorize_acquisition):
        assert model_inference_uses_prepared_endpoint() is False
        with pytest.raises(ModelAcquisitionDenied):
            require_prepared_model_inference()
        assert require_model_acquisition() is None
    assert acquisition_calls == 1

    with model_acquisition_scope(None, inference_authorizer=lambda: True):
        assert model_inference_uses_prepared_endpoint() is False
        with pytest.raises(ModelAcquisitionDenied):
            require_model_acquisition()


def test_inference_callback_is_fresh_and_either_exact_bool_allows_prepared_route():
    decisions = iter((False, True, False, True))
    calls = 0

    def authorize_inference() -> bool:
        nonlocal calls
        calls += 1
        return next(decisions)

    with model_acquisition_scope(None, inference_authorizer=authorize_inference):
        assert model_inference_uses_prepared_endpoint() is True
        assert model_inference_uses_prepared_endpoint() is False
        assert require_prepared_model_inference() is None
        assert require_prepared_model_inference() is None

    assert calls == 4


@pytest.mark.parametrize("invalid", [None, 0, 1, "allowed", object()])
def test_inference_callbacks_require_an_exact_bool(invalid: object):
    authorizer = cast(Callable[[], bool], lambda: invalid)
    with model_acquisition_scope(None, inference_authorizer=authorizer):
        with pytest.raises(ModelAcquisitionDenied, match="^" + _DENIED_MESSAGE + "$"):
            model_inference_uses_prepared_endpoint()
        with pytest.raises(ModelAcquisitionDenied, match="^" + _DENIED_MESSAGE + "$"):
            require_prepared_model_inference()


def test_inference_permission_errors_map_and_unrelated_errors_propagate():
    permission_error = PermissionError("synthetic denial")

    def denied() -> bool:
        raise permission_error

    with model_acquisition_scope(None, inference_authorizer=denied):
        with pytest.raises(ModelAcquisitionDenied) as mode_error:
            model_inference_uses_prepared_endpoint()
        with pytest.raises(ModelAcquisitionDenied) as prepared_error:
            require_prepared_model_inference()
    assert str(mode_error.value) == _DENIED_MESSAGE
    assert mode_error.value.__cause__ is None
    assert str(prepared_error.value) == _DENIED_MESSAGE
    assert prepared_error.value.__cause__ is None

    failure = LookupError("unrelated authorization failure")

    def unrelated() -> bool:
        raise failure

    with model_acquisition_scope(None, inference_authorizer=unrelated):
        with pytest.raises(LookupError) as mode_error:
            model_inference_uses_prepared_endpoint()
        with pytest.raises(LookupError) as prepared_error:
            require_prepared_model_inference()
    assert mode_error.value is failure
    assert prepared_error.value is failure


def test_nested_default_scope_masks_and_exception_restores_both_authorizers():
    acquisition_calls = 0
    inference_calls = 0

    def authorize_acquisition() -> None:
        nonlocal acquisition_calls
        acquisition_calls += 1

    def outer_inference() -> bool:
        nonlocal inference_calls
        inference_calls += 1
        return False

    def inner_acquisition() -> None:
        return None

    with model_acquisition_scope(
        authorize_acquisition,
        inference_authorizer=outer_inference,
    ):
        assert model_inference_uses_prepared_endpoint() is True
        assert require_model_acquisition() is None
        with model_acquisition_scope(None):
            assert model_inference_uses_prepared_endpoint() is False
            with pytest.raises(ModelAcquisitionDenied):
                require_prepared_model_inference()
            with pytest.raises(ModelAcquisitionDenied):
                require_model_acquisition()
        assert model_inference_uses_prepared_endpoint() is True
        assert require_model_acquisition() is None

        with pytest.raises(RuntimeError, match="synthetic scope failure"):
            with model_acquisition_scope(
                inner_acquisition,
                inference_authorizer=lambda: True,
            ):
                assert model_inference_uses_prepared_endpoint() is False
                assert require_prepared_model_inference() is None
                assert require_model_acquisition() is None
                raise RuntimeError("synthetic scope failure")

        assert model_inference_uses_prepared_endpoint() is True
        assert require_model_acquisition() is None

    assert acquisition_calls == 3
    assert inference_calls == 3
    with pytest.raises(ModelAcquisitionDenied):
        require_prepared_model_inference()


def test_invalid_nested_inference_callback_does_not_change_outer_scope():
    authorizer = cast(Callable[[], bool], lambda: False)
    with model_acquisition_scope(None, inference_authorizer=authorizer):
        with pytest.raises(TypeError):
            with model_acquisition_scope(None, inference_authorizer=cast(Callable[[], bool], 0)):
                pytest.fail("invalid inference authorizer unexpectedly entered")
        assert model_inference_uses_prepared_endpoint() is True
        assert require_prepared_model_inference() is None


def test_inference_context_isolated_across_threads_and_copied_deliberately():
    def copied_context_result() -> tuple[bool, None]:
        return (
            model_inference_uses_prepared_endpoint(),
            require_prepared_model_inference(),
        )

    with model_acquisition_scope(None, inference_authorizer=lambda: False):
        assert model_inference_uses_prepared_endpoint() is True
        copied = copy_context()
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(model_inference_uses_prepared_endpoint).result() is False
            with pytest.raises(ModelAcquisitionDenied):
                executor.submit(require_prepared_model_inference).result()
            assert executor.submit(copied.run, copied_context_result).result() == (
                True,
                None,
            )


def test_host_acquisition_scope_does_not_grant_prepared_inference_authority():
    with host_model_acquisition_scope():
        assert require_model_acquisition() is None
        assert model_inference_uses_prepared_endpoint() is False
        with pytest.raises(ModelAcquisitionDenied):
            require_prepared_model_inference()
