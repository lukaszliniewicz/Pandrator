"""Compact, revisioned audiobook voice-mode setup operations."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Literal

from sqlalchemy.orm import Session

from pandrator.logic.audiobook_chunking import audiobook_chunk_budget

from .models import OutcomePlan, SessionRecord
from .workspace import (
    RevisionConflict,
    derive_legacy_outcome,
    stable_hash,
)

AudiobookMode = Literal["single_voice", "multi_voice"]


def _service(services: Any, name: str) -> Any:
    if isinstance(services, Mapping):
        return services[name]
    return getattr(services, name)


def _audiobook_record(session: Session, session_id: str) -> SessionRecord:
    record = session.get(SessionRecord, session_id)
    if record is None or record.trashed_at is not None:
        raise KeyError(session_id)
    if record.workflow_kind != "audiobook":
        raise ValueError("Audiobook setup is only available for audiobook sessions.")
    return record


def _outcome_snapshot(
    session: Session,
    record: SessionRecord,
) -> tuple[dict[str, Any], int]:
    plan = session.get(OutcomePlan, record.id)
    if plan is None:
        return derive_legacy_outcome(record), 0
    value = plan.value_json if isinstance(plan.value_json, dict) else {}
    return deepcopy(value), int(plan.revision)


def _snapshot(
    services: Any,
    session: Session,
    session_id: str,
) -> dict[str, Any]:
    record = _audiobook_record(session, session_id)
    settings = _service(services, "workspace_settings")
    text = settings.get_in_session(session, session_id, "text")
    tts = settings.get_in_session(session, session_id, "tts")
    outcome_value, outcome_revision = _outcome_snapshot(session, record)
    text_effective = deepcopy(text["effective"])
    tts_effective = deepcopy(tts["effective"])
    document_optimization_enabled = bool(
        text_effective.get("llm_tts_document_optimization")
    )
    annotation_mode = str(text_effective.get("llm_tts_annotation_mode") or "off")
    annotation_only = bool(text_effective.get("llm_tts_annotation_only"))
    casting_enabled = bool(tts_effective.get("casting_enabled"))
    mode: AudiobookMode = "multi_voice" if casting_enabled else "single_voice"
    transformations = outcome_value.get("transformations")
    actual_document_optimization = isinstance(transformations, dict) and bool(
        transformations.get("llm_tts_document_optimization")
    )
    configured = mode == "single_voice" or (
        casting_enabled
        and document_optimization_enabled
        and annotation_mode == "speakers"
        and annotation_only
        and actual_document_optimization
    )
    configuration_payload = {
        "record_revision": int(record.revision),
        "sections": {
            "text": {
                "revision": int(text["revision"]),
                "effective": text_effective,
            },
            "tts": {
                "revision": int(tts["revision"]),
                "effective": tts_effective,
            },
        },
        "outcome": {
            "revision": outcome_revision,
            "value": outcome_value,
        },
    }
    configuration_revision = stable_hash(configuration_payload)
    return {
        "session_id": session_id,
        "mode": mode,
        "configuration_revision": configuration_revision,
        "revisions": {
            "text": int(text["revision"]),
            "tts": int(tts["revision"]),
            "outcome": outcome_revision,
        },
        "annotation_mode": annotation_mode,
        "annotation_only": annotation_only,
        "document_optimization_enabled": document_optimization_enabled,
        "casting_enabled": casting_enabled,
        "segmentation": audiobook_chunk_budget(
            text_effective, tts_effective, str(record.source_language or "en")
        ),
        "tts": {
            "service": str(
                tts_effective.get("service")
                or tts_effective.get("tts_service")
                or ""
            ),
            "model": str(
                tts_effective.get("model")
                or tts_effective.get("xtts_model")
                or ""
            ),
            "voice": str(
                tts_effective.get("voice")
                or tts_effective.get("speaker")
                or ""
            ),
            "language": str(tts_effective.get("language") or ""),
        },
        "configured": configured,
    }


def get_audiobook_setup(
    services: Any,
    session: Session,
    session_id: str,
) -> dict[str, Any]:
    """Read the effective audiobook voice-mode setup without creating state."""

    return _snapshot(services, session, session_id)


def _patch_if_needed(
    settings: Any,
    session: Session,
    session_id: str,
    snapshot: dict[str, Any],
    fields: dict[str, Any],
) -> None:
    if all(snapshot["effective"].get(key) == value for key, value in fields.items()):
        return
    settings.patch_in_session(
        session,
        session_id,
        snapshot["section"],
        int(snapshot["revision"]),
        fields,
    )


def configure_audiobook_setup(
    services: Any,
    session: Session,
    session_id: str,
    *,
    expected_revision: str,
    mode: AudiobookMode,
) -> dict[str, Any]:
    """Apply one audiobook voice mode in the caller-owned transaction."""

    if mode not in {"single_voice", "multi_voice"}:
        raise ValueError(f"Unknown audiobook setup mode: {mode}")
    current = _snapshot(services, session, session_id)
    if expected_revision.lower() != current["configuration_revision"]:
        raise RevisionConflict("Audiobook setup changed in another client.")

    settings = _service(services, "workspace_settings")
    text = settings.get_in_session(session, session_id, "text")
    tts = settings.get_in_session(session, session_id, "tts")
    if mode == "multi_voice":
        _patch_if_needed(
            settings,
            session,
            session_id,
            text,
            {
                "llm_tts_document_optimization": True,
                "llm_tts_annotation_mode": "speakers",
                "llm_tts_annotation_only": True,
            },
        )
        _patch_if_needed(
            settings,
            session,
            session_id,
            tts,
            {"casting_enabled": True},
        )
        outcome_value, outcome_revision = _outcome_snapshot(
            session, _audiobook_record(session, session_id)
        )
        transformations = outcome_value.get("transformations")
        if not isinstance(transformations, dict):
            transformations = {}
            outcome_value["transformations"] = transformations
        if not bool(transformations.get("llm_tts_document_optimization")):
            transformations["llm_tts_document_optimization"] = True
            _service(services, "outcome_plans").update_in_session(
                session,
                session_id,
                outcome_revision,
                outcome_value,
            )
    else:
        _patch_if_needed(
            settings,
            session,
            session_id,
            text,
            {
                "llm_tts_annotation_mode": "off",
                "llm_tts_annotation_only": False,
            },
        )
        _patch_if_needed(
            settings,
            session,
            session_id,
            tts,
            {"casting_enabled": False},
        )

    session.flush()
    return get_audiobook_setup(services, session, session_id)
