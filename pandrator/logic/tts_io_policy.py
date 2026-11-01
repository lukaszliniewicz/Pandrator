"""Optional execution-local authorization for speech transport boundaries."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


class TtsIoDenied(PermissionError):
    """The current speech execution lost authorization before transport I/O."""


@dataclass(frozen=True)
class TtsIoPolicy:
    before_io: Callable[[], None]
    allow_environment_credentials: bool
    allow_host_adc: bool
    redact_diagnostic: Callable[[object], str] | None = None


_policy: ContextVar[TtsIoPolicy | None] = ContextVar("tts_io_policy", default=None)


@contextmanager
def tts_io_scope(
    before_io: Callable[[], None], *, allow_environment_credentials: bool = True,
    allow_host_adc: bool = True,
    redact_diagnostic: Callable[[object], str] | None = None,
) -> Iterator[None]:
    token = _policy.set(TtsIoPolicy(
        before_io, allow_environment_credentials, allow_host_adc, redact_diagnostic,
    ))
    try:
        yield
    finally:
        _policy.reset(token)


def current_tts_io_policy() -> TtsIoPolicy | None:
    return _policy.get()


def check_tts_io() -> None:
    policy = current_tts_io_policy()
    if policy is not None:
        try:
            policy.before_io()
        except TtsIoDenied:
            raise
        except Exception as error:
            raise TtsIoDenied("The speech request is no longer authorized.") from error


def tts_environment_credentials_allowed() -> bool:
    policy = current_tts_io_policy()
    return policy.allow_environment_credentials if policy is not None else True


def tts_host_adc_allowed() -> bool:
    policy = current_tts_io_policy()
    return policy.allow_host_adc if policy is not None else True


def redact_tts_diagnostic(value: object) -> str:
    policy = _policy.get()
    if policy is not None and policy.redact_diagnostic is not None:
        return policy.redact_diagnostic(value)
    return str(value)
