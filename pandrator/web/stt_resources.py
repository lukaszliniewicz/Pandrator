"""One resource convention for every ASR entry point."""

from typing import Any


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
    return keys
