"""Explicit sound job registration shared by HTTP and CLI composition."""

from __future__ import annotations

from .job_registry import JobHandlerRegistry, JobPayloadContract
from .sound_mix_service import SoundMixService
from .sound_resolution_service import SoundResolutionService
from .sound_service import SoundService


def register_sound_jobs(
    registry: JobHandlerRegistry,
    sounds: SoundService,
    mixes: SoundMixService | None = None,
    resolution: SoundResolutionService | None = None,
) -> None:
    registry.register(
        "sound.generate",
        sounds.run_generate,
        domain="sounds",
        payload_contract=JobPayloadContract(("candidate_id", "request", "runtime_identity")),
    )
    if mixes is not None:
        registry.register(
            "sound.mix", mixes.run_mix, domain="sounds",
            payload_contract=JobPayloadContract(("session_id", "base_artifact_id", "request_fingerprint")),
        )
    if resolution is not None:
        registry.register(
            "sound.resolve", resolution.run_resolution, domain="sounds",
            payload_contract=JobPayloadContract(("session_id", "pins", "pin_hash")),
        )
