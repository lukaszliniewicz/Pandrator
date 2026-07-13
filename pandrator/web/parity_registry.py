"""Machine-readable Qt-to-web feature disposition and qualification registry."""

from __future__ import annotations

from .workspace import BUILTIN_DEFAULTS


FEATURES = (
    ("navigation", "application_routes", "replaced", "complete", "Qt tabs are replaced by real URL-addressable application and session routes."),
    ("sessions", "session_manager", "equivalent", "complete", "List, inspect artifacts, search, trash, restore, and reconcile sessions."),
    ("sources", "reusable_source_library", "replaced", "partial", "Reuse, attachment history, current-version selection, detach, reference-aware trash, restore, and rename are implemented; doctor/repair actions still need qualification."),
    ("workflow", "included_stages_checkbox", "replaced", "complete", "A revisioned outcome plan and contextual controls replace the generic inclusion checkbox."),
    ("workflow", "guided_creation", "replaced", "complete", "Source-aware outcome questions retain a prominent full-workspace path."),
    ("subtitles", "comparison_editor", "equivalent", "partial", "Lineage comparison and editing exist; merge/split/timing workflows still need browser and migration-fixture qualification."),
    ("subtitles", "word_timestamps", "replaced", "complete", "CrispASR timed words are first-class immutable records."),
    ("generation", "generated_sentences", "replaced", "partial", "Core paging, typed chapter segments, edits, takes, playback, regeneration and safe pause exist; ETA, bulk-selection ergonomics, and long-run recovery still need parity evidence."),
    ("providers", "llm_model_records", "equivalent", "complete", "Per-model request defaults and cached/uncached/output pricing."),
    ("providers", "tts_service_profiles", "equivalent", "partial", "Profiles, discovery, health, catalogues and defaults exist; every adapter-specific control has not yet been runtime-qualified."),
    ("voices", "voice_library", "equivalent", "partial", "Playback, recording, CrispASR transcription and review are implemented; device/browser coverage and cancellation need qualification."),
    ("training", "xtts_training", "equivalent", "complete", "Durable jobs expose the training and preparation controls, cancellation, interrupted-run reconciliation and retry, output manifests, and automatic XTTS catalogue activation."),
    ("rvc", "speech_to_speech", "replaced", "partial", "RVC has a dedicated model/conversion surface and per-run generation controls; service-backed selected/marked/all qualification remains."),
    ("source_cleaning", "agentic_loop", "replaced", "partial", "Dedicated structured run data exists; full action/diff acceptance UX and representative documents need qualification."),
    ("pdf", "stack_editor", "replaced", "partial", "Stack, crop, whiteout, deletion and undo are implemented; large, rotated, mixed-size and malformed PDF gates remain."),
    ("exports", "audio_and_subtitles", "equivalent", "partial", "Output profiles, cover upload/preview, metadata, multiple audio formats and chaptered M4B assembly exist; the full video audio/subtitle matrix is not yet qualified."),
    ("artifacts", "managed_preview", "replaced", "complete", "Produced text, subtitle, audio, video, image and PDF artifacts open in a reusable in-app preview instead of raw browser navigation."),
    ("runtime", "server_device_playback", "replaced", "complete", "Browser range playback replaces server sound-device playback."),
    ("runtime", "remote_multi_user", "removed", "complete", "The supported deployment remains authenticated single-owner."),
)


# Entries here are intentionally candid.  The control is rendered and saved,
# but the complete Qt behavior has not yet been proven at the runtime boundary.
SETTING_GAPS = {
    "stt.diarization_enabled": "The setting is persisted, but CrispASR diarization capability detection and speaker-output qualification are incomplete.",
    "rvc.enabled": "Per-run and post-generation RVC are exposed; automatic apply-during-generation behavior still needs qualification.",
    "translation.backend": "The common control is rendered, but each advertised backend must be hidden unless its adapter and credentials are available.",
}


def build_registry() -> dict:
    settings = []
    for section, values in BUILTIN_DEFAULTS.items():
        for key in values:
            identifier = f"{section}.{key}"
            rationale = SETTING_GAPS.get(identifier, "Rendered, revisioned, inherited, and passed through the runtime settings boundary.")
            settings.append({
            "id": identifier,
            "category": section,
            "qt_control": key,
            "disposition": "equivalent",
            "implementation": "partial" if identifier in SETTING_GAPS else "complete",
            "rationale": rationale,
            "web_surface": f"/sessions/:id/{'voice' if section in {'tts', 'audio', 'rvc'} else 'output' if section == 'output' else 'cleaning' if section == 'source_cleaning' else 'text'}",
            })
    features = [
        {"category": category, "id": identifier, "disposition": disposition, "implementation": implementation, "rationale": rationale}
        for category, identifier, disposition, implementation, rationale in FEATURES
    ]
    complete = sum(item["implementation"] == "complete" for item in [*features, *settings])
    total = len(features) + len(settings)
    return {"version": 3, "features": features, "settings": settings, "summary": {"complete": complete, "partial": total - complete, "total": total}}
