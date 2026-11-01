"""Context-local authorization for operations that acquire missing models."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

__all__ = [
    "AcquisitionAuthorizer",
    "ModelAcquisitionDenied",
    "host_model_acquisition_scope",
    "model_acquisition_scope",
    "model_inference_uses_prepared_endpoint",
    "require_prepared_model_inference",
    "require_model_acquisition",
]

AcquisitionAuthorizer = Callable[[], None]
_DENIED_MESSAGE = "A required model may need to be downloaded. Ask an Administrator to prepare it."


class ModelAcquisitionDenied(RuntimeError):
    def __init__(self) -> None:
        super().__init__(_DENIED_MESSAGE)


_acquisition_authorizer: ContextVar[AcquisitionAuthorizer | None] = ContextVar(
    "pandrator_model_acquisition_authorizer", default=None
)
_inference_authorizer: ContextVar[Callable[[], bool] | None] = ContextVar(
    "pandrator_model_inference_authorizer", default=None
)


@contextmanager
def model_acquisition_scope(
    authorizer: AcquisitionAuthorizer | None,
    *,
    inference_authorizer: Callable[[], bool] | None = None,
) -> Iterator[None]:
    if authorizer is not None and not callable(authorizer):
        raise TypeError("The model acquisition authorizer must be callable or None.")
    if inference_authorizer is not None and not callable(inference_authorizer):
        raise TypeError("The model inference authorizer must be callable or None.")
    token: Token[AcquisitionAuthorizer | None] = _acquisition_authorizer.set(authorizer)
    inference_token: Token[Callable[[], bool] | None] = _inference_authorizer.set(
        inference_authorizer
    )
    try:
        yield
    finally:
        _inference_authorizer.reset(inference_token)
        _acquisition_authorizer.reset(token)


def require_model_acquisition() -> None:
    authorizer = _acquisition_authorizer.get()
    if authorizer is None:
        raise ModelAcquisitionDenied()
    try:
        result = authorizer()
    except PermissionError:
        raise ModelAcquisitionDenied() from None
    if result is not None:
        raise ModelAcquisitionDenied()


def _authorize_model_inference(authorizer: Callable[[], bool] | None) -> bool:
    if authorizer is None:
        raise ModelAcquisitionDenied()
    try:
        allowed = authorizer()
    except PermissionError:
        raise ModelAcquisitionDenied() from None
    if type(allowed) is not bool:
        raise ModelAcquisitionDenied()
    return allowed


def model_inference_uses_prepared_endpoint() -> bool:
    """Report whether current authority requires an inference-only endpoint."""
    authorizer = _inference_authorizer.get()
    if authorizer is None:
        return False
    return not _authorize_model_inference(authorizer)


def require_prepared_model_inference() -> None:
    """Require current inference authority for a route that cannot acquire assets."""
    _authorize_model_inference(_inference_authorizer.get())


def _allow_local_host_acquisition() -> None:
    return None


@contextmanager
def host_model_acquisition_scope() -> Iterator[None]:
    with model_acquisition_scope(_allow_local_host_acquisition):
        yield
