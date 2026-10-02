"""Stable mapping between persisted artifact roles and document stages."""

ARTIFACT_ROLE_TO_STAGE = {
    "transcription": "transcription",
    "correction": "correction",
    "translation": "translation",
    "tts_optimized": "tts_optimization",
}


def document_stage_for_artifact_role(role: str | None) -> str | None:
    """Return a role's document stage, retaining same-name stage roles."""
    normalized = str(role or "")
    if not normalized:
        return None
    return ARTIFACT_ROLE_TO_STAGE.get(normalized, normalized)
