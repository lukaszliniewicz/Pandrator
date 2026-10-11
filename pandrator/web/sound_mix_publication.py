"""Bounded sound-mix rendering and fenced artifact publication."""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService, PreparedArtifactRegistration
from .database import Database
from .media_process import MediaProcessCancelled
from .models import Artifact, new_id
from .settings_policy import stable_hash
from .sound_mix_admission import assert_current_pins
from .sound_mix_claims import (
    _cached_artifact,
    _cleanup_uncommitted,
    _stale_exact_duplicates,
    _validate_claim,
)
from .sound_mix_plan import build_sound_mix_plan, remap_audio_metadata
from .sound_mix_render import normalize_mix_audio, render_sound_mix
from .sound_mix_snapshot import (
    SOUND_MIX_RENDERER_VERSION,
    PreparedSoundMix,
    verify_prepared_files,
)


def _plan_clips(
    prepared: PreparedSoundMix,
    source_frames: dict[str, int],
) -> list[dict[str, Any]]:
    clips: list[dict[str, Any]] = []
    for cue in prepared.cues:
        rate = prepared.rate
        clip: dict[str, Any] = {
            "cue_id": cue["cue_id"],
            "variant_id": cue["variant_id"],
            "content_hash": cue["content_hash"],
            "anchor_frame": cue["anchor_frame"],
            "mode": cue["mode"],
            "offset_frames": round(cue["offset_ms"] * rate / 1000),
            "source_frames": source_frames[cue["cue_id"]],
            "trim_start_frame": round(cue["trim_start_seconds"] * rate),
            "gain_db": cue["gain_db"],
            "fade_in_frames": round(cue["fade_in_ms"] * rate / 1000),
            "fade_out_frames": round(cue["fade_out_ms"] * rate / 1000),
        }
        if cue["trim_end_seconds"] is not None:
            clip["trim_end_frame"] = round(cue["trim_end_seconds"] * rate)
        clips.append(clip)
    return clips


def render_prepared(
    database: Database,
    paths: DataPaths,
    artifacts: ArtifactService,
    prepared: PreparedSoundMix,
    *,
    progress: Callable[[float, str], None],
    cancel_event: threading.Event,
    require_sound_mix_lease: bool = False,
) -> tuple[Artifact, dict[str, Any]]:
    """Render from a frozen snapshot, then publish once under a short writer."""

    if not prepared.has_cues:
        with database.immediate_session() as session:
            _validate_claim(
                session,
                prepared,
                cancel_event,
                require_sound_mix=require_sound_mix_lease,
            )
            assert_current_pins(session, prepared)
            artifact = session.get(Artifact, prepared.base_artifact_id)
            if (
                artifact is None
                or artifact.state != "current"
                or artifact.kind != "audio"
                or artifact.session_id != prepared.session_id
            ):
                raise ValueError("The original audio artifact is no longer current.")
            session.expunge(artifact)
        return artifact, {
            "artifact_id": artifact.id,
            "status": "inactive",
            "cache_hit": False,
            "request_fingerprint": prepared.request_fingerprint,
            "sound_plan_revision": prepared.sound_plan_revision,
        }
    cached = _cached_artifact(
        database,
        artifacts,
        prepared,
        cancel_event,
        require_sound_mix=require_sound_mix_lease,
    )
    if cached is not None:
        return cached

    session_root = paths.sessions / prepared.storage_key / "sounds"
    staging_root = session_root / "staging"
    output_dir = session_root / "mixes"
    staging_root.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    final_path = artifacts.next_available_path(
        output_dir / f"{prepared.request_fingerprint[:16]}-{new_id()}.wav"
    )
    final_relative_path = paths.relative_managed_path(final_path)
    output_content_hash: str | None = None
    moved_to_final = False
    committed = False
    registered_artifact: Artifact | None = None

    try:
        with tempfile.TemporaryDirectory(prefix="sound-mix-", dir=staging_root) as temp_name:
            temp_dir = Path(temp_name)
            normalized_base = temp_dir / "base.wav"
            progress(0.02, "Normalizing base audio")
            normalized_base_frames = normalize_mix_audio(
                prepared.base_path,
                normalized_base,
                rate=prepared.rate,
                channels=prepared.channels,
                cancel_event=cancel_event,
            )
            if normalized_base_frames != prepared.base_frames:
                raise ValueError(
                    "Audio timing mismatch: normalized base frame count differs from the frozen timeline."
                )
            normalized_clips: dict[str, Path] = {}
            clip_frames: dict[str, int] = {}
            for index, cue in enumerate(prepared.cues):
                if cancel_event.is_set():
                    raise MediaProcessCancelled("Sound-mix rendering was canceled.")
                normalized_path = temp_dir / f"clip-{index:04d}.wav"
                clip_frames[cue["cue_id"]] = normalize_mix_audio(
                    cue["source_path"],
                    normalized_path,
                    rate=prepared.rate,
                    channels=prepared.channels,
                    cancel_event=cancel_event,
                )
                normalized_clips[cue["cue_id"]] = normalized_path
                progress(
                    0.04 + 0.18 * (index + 1) / len(prepared.cues),
                    f"Normalized sound {index + 1} of {len(prepared.cues)}",
                )
            frame_plan = build_sound_mix_plan(
                base_frames=prepared.base_frames,
                rate=prepared.rate,
                channels=prepared.channels,
                base_content_hash=prepared.base_content_hash,
                clips=_plan_clips(prepared, clip_frames),
                fixed_timeline=prepared.fixed_timeline,
            )
            mix_signature = stable_hash(
                {
                    "sound_plan_id": prepared.sound_plan_id,
                    "sound_plan_revision": prepared.sound_plan_revision,
                    "speech_plan_revision_id": prepared.speech_plan_revision_id,
                    "request_fingerprint": prepared.request_fingerprint,
                    "frame_plan": frame_plan,
                }
            )
            rendered_path = temp_dir / "rendered.wav"
            render_result = render_sound_mix(
                normalized_base,
                normalized_clips,
                frame_plan,
                rendered_path,
                cancel_event=cancel_event,
                progress=lambda fraction, detail: progress(0.22 + 0.68 * fraction, detail),
            )
            if render_result.get("mix_signature") != frame_plan.get("mix_signature"):
                raise RuntimeError("The sound-mix renderer returned a different frame-plan signature.")
            mixed_metadata = remap_audio_metadata(prepared.base_metadata, frame_plan)
            audio_timeline = mixed_metadata.get("audio_timeline")
            if not isinstance(audio_timeline, dict):
                audio_timeline = {}
                mixed_metadata["audio_timeline"] = audio_timeline
            audio_timeline.update(
                {"version": 1, "sample_rate_hz": prepared.rate, "total_frames": frame_plan["total_frames"]}
            )
            base_timeline = prepared.base_metadata.get("audio_timeline")
            if not isinstance(base_timeline, dict):
                base_timeline = {
                    "version": 1,
                    "sample_rate_hz": prepared.rate,
                    "total_frames": prepared.base_frames,
                }
            sound_mix = {
                "request_fingerprint": prepared.request_fingerprint,
                "mix_signature": mix_signature,
                "sound_plan_id": prepared.sound_plan_id,
                "sound_plan_revision": prepared.sound_plan_revision,
                "speech_plan_revision_id": prepared.speech_plan_revision_id,
                "base_artifact_id": prepared.base_artifact_id,
                "base_content_hash": prepared.base_content_hash,
                "timeline_artifact_id": prepared.timeline_artifact_id,
                "base_audio_timeline": dict(base_timeline),
                "insertions": frame_plan["insertions"],
                "clips": frame_plan["clips"],
                "renderer_version": SOUND_MIX_RENDERER_VERSION,
                "frame_plan": frame_plan,
            }
            mixed_metadata["sound_mix"] = sound_mix
            parents = [prepared.base_artifact_id]
            if prepared.timeline_artifact_id:
                parents.append(prepared.timeline_artifact_id)
            parents.extend(cue["artifact_id"] for cue in prepared.cues)
            registration_settings = {
                "request_fingerprint": prepared.request_fingerprint,
                "mix_signature": mix_signature,
                "sound_plan_id": prepared.sound_plan_id,
                "sound_plan_revision": prepared.sound_plan_revision,
            }
            staged_registration = artifacts.prepare_registration(
                rendered_path,
                settings=registration_settings,
            )
            output_content_hash = staged_registration.content_hash
            registration = PreparedArtifactRegistration(
                relative_path=final_relative_path,
                mime_type=staged_registration.mime_type,
                size_bytes=staged_registration.size_bytes,
                content_hash=staged_registration.content_hash,
                settings_hash=staged_registration.settings_hash,
            )
            verify_prepared_files(prepared)
            if cancel_event.is_set():
                raise MediaProcessCancelled("Sound-mix rendering was canceled.")
            progress(0.93, "Publishing sound mix")
            with database.immediate_session() as session:
                _validate_claim(
                    session,
                    prepared,
                    cancel_event,
                    require_sound_mix=require_sound_mix_lease,
                )
                assert_current_pins(session, prepared)
                _stale_exact_duplicates(session, prepared)
                if os.path.lexists(final_path):
                    raise FileExistsError("Allocated sound-mix output path is already present.")
                os.replace(rendered_path, final_path)
                moved_to_final = True
                registered_artifact = artifacts.register_in_session(
                    session,
                    final_path,
                    kind="audio",
                    role="sound_mix.audio",
                    session_id=prepared.session_id,
                    parent_ids=list(dict.fromkeys(parents)),
                    metadata=mixed_metadata,
                    settings=registration_settings,
                    _prepared=registration,
                )
                _validate_claim(
                    session,
                    prepared,
                    cancel_event,
                    require_sound_mix=require_sound_mix_lease,
                )
                assert_current_pins(session, prepared)
                session.expunge(registered_artifact)
            committed = True
    except BaseException:
        if moved_to_final and not committed:
            _cleanup_uncommitted(
                database,
                final_path,
                final_relative_path,
                output_content_hash,
            )
        raise

    assert registered_artifact is not None
    receipt = {
        "artifact_id": registered_artifact.id,
        "status": "ready",
        "cache_hit": False,
        "request_fingerprint": prepared.request_fingerprint,
        "mix_signature": mix_signature,
        "sound_plan_revision": prepared.sound_plan_revision,
    }
    progress(1.0, "Sound mix ready")
    return registered_artifact, receipt


__all__ = ["render_prepared"]
