"""TTS provider value types, error contracts and adapter protocol."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol, TypeGuard, runtime_checkable

from pydub import AudioSegment

from .tts_catalogue_projection import normalize_service_id


@dataclass(frozen=True, slots=True)
class TtsHealth:
    online: bool
    available: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class TtsCapabilities:
    synthesis: bool = True
    health: bool = True
    dynamic_catalog: bool = False
    model_upload: bool = False
    voice_upload: bool = False
    voice_delete: bool = False
    batch_synthesis: bool = False
    streaming_batch: bool = False
    parallel_synthesis: bool = False
    default_batch_size: int = 1
    max_batch_size: int = 1


@dataclass(frozen=True, slots=True)
class TtsBatchItem:
    id: str
    text: str
    settings: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TtsBatchResult:
    id: str
    audio: AudioSegment | None = None
    error: TtsProviderError | None = None


@dataclass(frozen=True, slots=True)
class TtsRetryPolicy:
    max_attempts: int = 5
    maximum_delay_seconds: float = 90.0

    @classmethod
    def from_settings(
        cls,
        settings: dict[str, Any],
        *,
        max_attempts: float | str | None = None,
    ) -> TtsRetryPolicy:
        try:
            attempts = int(
                max_attempts
                if max_attempts is not None
                else settings.get("max_attempts") or 5
            )
        except (TypeError, ValueError):
            attempts = 5
        try:
            maximum_delay = float(settings.get("retry_max_delay_seconds") or 90.0)
        except (TypeError, ValueError):
            maximum_delay = 90.0
        return cls(
            max_attempts=max(1, min(20, attempts)),
            maximum_delay_seconds=max(1.0, min(300.0, maximum_delay)),
        )


class TtsProviderError(RuntimeError):
    """Stable provider failure projected across adapter implementations."""

    def __init__(
        self,
        service_id: str,
        operation: str,
        message: str,
        *,
        retryable: bool,
    ):
        super().__init__(message)
        self.service_id = normalize_service_id(service_id)
        self.operation = operation
        self.retryable = retryable


class TtsProviderConfigurationError(TtsProviderError):
    def __init__(self, service_id: str, operation: str, message: str):
        super().__init__(
            service_id,
            operation,
            message,
            retryable=False,
        )


@runtime_checkable
class TtsProviderAdapter(Protocol):
    """Standard operations supported by a TTS provider adapter."""

    service_id: str

    def capabilities(
        self,
        service: dict[str, Any],
    ) -> TtsCapabilities: ...

    def health(self, service: dict[str, Any]) -> TtsHealth: ...

    def enrich_catalog(
        self,
        service: dict[str, Any],
        *,
        api_key: str = "",
    ) -> dict[str, Any]: ...

    def synthesize(
        self,
        text: str,
        settings: dict[str, Any],
        **options: Any,
    ) -> AudioSegment | None: ...

    def upload_voice(
        self,
        wav_file_path: str | list[str],
        *,
        base_url: str,
        service: str,
        prompt_text: str | None = None,
        mode: str | None = None,
        voice_id: str | None = None,
        api_key: str = "",
    ) -> str: ...

    def delete_voice(
        self,
        voice_id: str,
        *,
        base_url: str,
        service: str,
        api_key: str = "",
    ) -> bool: ...


class TtsBatchSynthesizer(Protocol):
    """Optional batch hook whose implementation owns its signature and result contract."""

    def __call__(
        self,
        items: list[TtsBatchItem],
        *,
        batch_size: int,
        **options: Any,
    ) -> Iterator[TtsBatchResult]: ...


def _is_tts_batch_synthesizer(method: object) -> TypeGuard[TtsBatchSynthesizer]:
    """Check only callability; implementations own the hook signature and result contract."""
    return callable(method)
