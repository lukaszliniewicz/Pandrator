"""One resource convention for every ASR entry point."""

from typing import Any


def audio_cpp_resource_keys(*backends: str) -> list[str]:
    """Native tools and managed speech share one local execution resource.

    Automatic selection can use a GPU, so it must not bypass the GPU claim.
    The process guard additionally protects callers outside the job queue.
    """
    keys = ["service:tts:audio_cpp"]
    if not backends or any(str(value or "auto").lower() != "cpu" for value in backends):
        keys.append("gpu:default")
    return keys


def stt_resource_keys(settings: dict[str, Any]) -> list[str]:
    keys = ["service:stt"]
    compute = str(
        settings.get("stt_compute_backend")
        or settings.get("compute_backend")
        or settings.get("device")
        or "auto"
    ).lower()
    if compute in {"cuda", "vulkan", "metal", "gpu"}:
        keys.append(f"gpu:{compute}")
    from pandrator.logic.dubbing.qwen_alignment import uses_qwen
    from pandrator.logic.dubbing.stt_backends import normalize_stt_backend

    native_backends = []
    if normalize_stt_backend(settings.get("stt_engine") or settings.get("stt_backend")) == "qwen3":
        native_backends.append(settings.get("qwen_asr_backend") or settings.get("stt_compute_backend") or "auto")
    if str(settings.get("transcription_vocal_isolation") or "off").lower() not in {"off", "none", "false", "disabled"}:
        native_backends.append(settings.get("audio_cpp_backend") or settings.get("stt_compute_backend") or "best")
    if uses_qwen(settings):
        native_backends.append(settings.get("qwen_aligner_backend") or settings.get("stt_compute_backend") or "auto")
    if native_backends:
        keys.extend(audio_cpp_resource_keys(*native_backends))
    return keys
