"""Machine-readable Qt-to-web feature disposition and qualification registry."""

from __future__ import annotations

from .workspace import BUILTIN_DEFAULTS


FEATURES = (
    ("navigation", "application_routes", "replaced", "complete", "Qt tabs are replaced by real URL-addressable application and session routes."),
    ("sessions", "session_manager", "equivalent", "complete", "List, inspect artifacts, search, trash, restore, and reconcile sessions."),
    ("sources", "reusable_source_library", "replaced", "complete", "Global source assets can be attached to multiple sessions with version history."),
    ("workflow", "included_stages_checkbox", "replaced", "complete", "A revisioned outcome plan and contextual controls replace the generic inclusion checkbox."),
    ("workflow", "guided_creation", "replaced", "complete", "Source-aware outcome questions retain a prominent full-workspace path."),
    ("subtitles", "comparison_editor", "equivalent", "complete", "Lineage comparison, editing, timing, split, merge, playback, and reviewed revisions."),
    ("subtitles", "word_timestamps", "replaced", "complete", "CrispASR timed words are first-class immutable records."),
    ("generation", "generated_sentences", "replaced", "partial", "Paging, marks, editing, takes, playback, regeneration, RVC, pause, resume, and cancel are available; regeneration-aware output reassembly still needs qualification."),
    ("providers", "llm_model_records", "equivalent", "complete", "Per-model request defaults and cached/uncached/output pricing."),
    ("providers", "tts_service_profiles", "equivalent", "partial", "Profiles, endpoint discovery, health refresh, catalogues, defaults, and removal are exposed; catalogue-backed session selectors and every provider-specific schema still need qualification."),
    ("voices", "voice_library", "equivalent", "complete", "Playback, recording, CrispASR transcription, transcript review, and reusable samples."),
    ("training", "xtts_training", "equivalent", "partial", "Durable jobs expose the training phases and controls; end-to-end model activation and recovery remain release-gate work."),
    ("source_cleaning", "agentic_loop", "replaced", "complete", "Dedicated view stores structured summaries, operations, warnings, diffs, and costs without chain-of-thought."),
    ("pdf", "stack_editor", "replaced", "complete", "Progressive all/left/right stacks, lazy thumbnails, synchronized crop dimensions, whiteout, deletion, undo, and derived output are implemented."),
    ("exports", "audio_and_subtitles", "equivalent", "partial", "The export combinations are implemented; generation-take reassembly, metadata/cover parity, and the full FFmpeg matrix remain release-gate work."),
    ("runtime", "server_device_playback", "replaced", "complete", "Browser range playback replaces server sound-device playback."),
    ("runtime", "remote_multi_user", "removed", "complete", "The supported deployment remains authenticated single-owner."),
)


# Entries here are intentionally candid.  The control is rendered and saved,
# but the complete Qt behavior has not yet been proven at the runtime boundary.
SETTING_GAPS = {
    "text.llm_tts_optimization": "The outcome plan exposes the choice, but the dedicated previewable LLM optimization transform is not wired yet.",
    "text.llm_processing_enabled": "Legacy segment-level LLM processing has not yet been replaced by the dedicated optimization transform.",
    "text.llm_concurrent_calls": "Awaiting the dedicated optimization transform.",
    "text.llm_multi_stage": "Awaiting the dedicated optimization transform.",
    "text.combined_prompt": "Saved, but not consumed until the dedicated optimization transform is implemented.",
    "text.first_prompt": "Saved, but not consumed until the dedicated optimization transform is implemented.",
    "text.second_prompt": "Saved, but not consumed until the dedicated optimization transform is implemented.",
    "text.third_prompt": "Saved, but not consumed until the dedicated optimization transform is implemented.",
    "audio.sentence_silence_ms": "Runtime alias exists; generation-take assembly still needs qualification.",
    "audio.paragraph_silence_ms": "Runtime alias exists; generation-take assembly still needs qualification.",
    "audio.fade_enabled": "Runtime alias exists; generation-take assembly still needs qualification.",
    "audio.fade_in_ms": "Runtime alias exists; generation-take assembly still needs qualification.",
    "audio.fade_out_ms": "Runtime alias exists; generation-take assembly still needs qualification.",
    "source_cleaning.pdf_ocr_mode": "The PDF editor supports OCR inputs, but the source-cleaning job does not yet apply every saved OCR option.",
    "source_cleaning.pdf_ocr_language": "The PDF editor supports OCR inputs, but the source-cleaning job does not yet apply every saved OCR option.",
    "source_cleaning.pdf_ocr_dpi": "The PDF editor supports OCR inputs, but the source-cleaning job does not yet apply every saved OCR option.",
    "output.title": "Saved, but complete container metadata writing still needs qualification.",
    "output.artist": "Saved, but complete container metadata writing still needs qualification.",
    "output.album": "Saved, but complete container metadata writing still needs qualification.",
    "output.genre": "Saved, but complete container metadata writing still needs qualification.",
    "output.language": "Saved, but complete container metadata writing still needs qualification.",
    "output.cover_artifact_id": "Saved, but complete cover embedding still needs qualification.",
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
    return {"version": 2, "features": features, "settings": settings, "summary": {"complete": complete, "partial": total - complete, "total": total}}
