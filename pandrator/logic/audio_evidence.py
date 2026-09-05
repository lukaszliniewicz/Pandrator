"""Transport helpers for asking an LLM to inspect a local audio clip."""

from __future__ import annotations

import base64
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from numbers import Real
from pathlib import Path
from typing import Any, Literal, cast

from . import llm_handler

MAX_INLINE_AUDIO_BYTES = 18 * 1024 * 1024
AudioConsumption = Literal["confirmed", "unreported"]


@dataclass(frozen=True)
class AudioEvidenceResult:
    """Transcript, completion details, and safe transport accounting."""

    transcript: str
    completion: llm_handler.ChatCompletionResult
    transport_metadata: dict[str, str]

    @property
    def result(self) -> llm_handler.ChatCompletionResult:
        """Compatibility-friendly alias for the underlying completion result."""

        return self.completion

    @property
    def metadata(self) -> dict[str, str]:
        """Compatibility-friendly alias for non-secret transport metadata."""

        return self.transport_metadata

    @property
    def audio_consumption(self) -> AudioConsumption:
        """Return the provider-reported audio token accounting state."""

        return cast(AudioConsumption, self.transport_metadata["audio_consumption"])


def _audio_format(audio_path: Path) -> str:
    suffix = audio_path.suffix.casefold()
    if suffix not in {".wav", ".mp3"}:
        raise ValueError("Audio evidence accepts only .wav and .mp3 files.")
    return suffix[1:]


def _normalize_transcript(content: Any) -> str:
    transcript = str(content or "").strip()
    if not transcript:
        raise RuntimeError("Audio evidence provider returned empty content.")

    # A model may wrap a transcript in one Markdown code fence. Remove only
    # that outer pair; all linguistic content inside remains untouched.
    if (
        "\n" not in transcript
        and "\r" not in transcript
        and transcript.startswith("```")
        and transcript.endswith("```")
    ):
        transcript = transcript[3:-3].strip()
    else:
        fenced = re.fullmatch(
            r"```[^\r\n]*\r?\n?(.*?)\r?\n?```",
            transcript,
            flags=re.DOTALL,
        )
        if fenced is not None:
            transcript = fenced.group(1).strip()
    if not transcript:
        raise RuntimeError("Audio evidence provider returned empty content.")
    return transcript


def _iter_audio_token_values(value: Any) -> Sequence[Real]:
    """Collect numeric ``audio_tokens`` fields from nested usage structures."""

    found: list[Real] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if (
                key == "audio_tokens"
                and isinstance(item, Real)
                and not isinstance(item, bool)
            ):
                found.append(item)
            found.extend(_iter_audio_token_values(item))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            found.extend(_iter_audio_token_values(item))
    return found


def _audio_consumption(usage: Any) -> AudioConsumption:
    values = _iter_audio_token_values(usage)
    if not values:
        return "unreported"

    finite = [
        value
        for value in values
        if not isinstance(value, float) or math.isfinite(value)
    ]
    if not finite:
        return "unreported"
    if any(value > 0 for value in finite):
        return "confirmed"
    if all(value == 0 for value in finite):
        raise RuntimeError(
            "Provider reported zero audio tokens; audio consumption cannot be confirmed."
        )
    raise RuntimeError(f"Provider reported invalid audio token counts: {finite!r}.")


def _provider_wire_mapping(provider_key: str | None, is_custom: bool) -> str:
    normalized_provider = str(provider_key or "").strip().casefold()
    if not is_custom and normalized_provider == "gemini":
        return "gemini_generate_content.inlineData"
    if not is_custom and normalized_provider == "vertex_ai":
        return "vertex_generate_content.inlineData"
    return "openai_chat_completions.input_audio"


def transcribe_audio_evidence(
    audio_path: Path,
    prompt: str,
    model_name: str,
    llm_settings: Any | None = None,
    *,
    provider_key: str | None = None,
    is_custom: bool = False,
    cancel_event: Any | None = None,
    retry_callback: Callable[..., Any] | None = None,
) -> AudioEvidenceResult:
    """Send one local WAV/MP3 clip through the shared chat completion layer."""

    path = Path(audio_path)
    audio_format = _audio_format(path)
    audio_bytes = path.read_bytes()
    if not audio_bytes:
        raise ValueError("Audio evidence file is empty.")
    if len(audio_bytes) > MAX_INLINE_AUDIO_BYTES:
        raise ValueError("Audio evidence file exceeds the 18 MiB inline payload limit.")

    encoded_audio = base64.b64encode(audio_bytes).decode("ascii")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "input_audio",
                    "input_audio": {
                        "data": encoded_audio,
                        "format": audio_format,
                    },
                },
            ],
        }
    ]
    completion_kwargs: dict[str, Any] = {
        "messages": messages,
        "model_name": model_name,
        "llm_settings": llm_settings,
    }
    if cancel_event is not None:
        completion_kwargs["cancel_event"] = cancel_event
    if retry_callback is not None:
        completion_kwargs["retry_callback"] = retry_callback
    completion = llm_handler.chat_completion_with_metadata(**completion_kwargs)
    transcript = _normalize_transcript(completion.content)
    consumption = _audio_consumption(completion.usage)
    transport_metadata = {
        "input_contract": "openai_chat_completions.input_audio",
        "provider_wire_mapping": _provider_wire_mapping(provider_key, is_custom),
        "audio_consumption": consumption,
    }
    return AudioEvidenceResult(
        transcript=transcript,
        completion=completion,
        transport_metadata=transport_metadata,
    )
