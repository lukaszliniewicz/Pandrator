"""Shared input-selection flags for guided and directly configured sessions."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .artifact_selection import ROLE_TO_STAGE, choose_artifact, selected_artifacts, stage_histories
from .artifacts import sha256_file
from .database import Database
from .models import (
    Artifact,
    MediaEditPlan,
    MediaEditPlanRevision,
    OutcomePlan,
    SessionRecord,
    SessionSetting,
    SessionStageSelection,
)
from .outcome_plans import derive_legacy_outcome
from .settings_policy import RevisionConflict
from .source_management import assert_session_idle
from .source_resolution import resolve_primary_source
from .subtitle_media import artifact_accessible_in_session

WORKFLOW_INPUT_CONSUMERS = frozenset({"translation", "generation"})
WORKFLOW_INPUT_ROLES = frozenset({"source", "correction", "translation", "prepared_text", "tts_optimized"})
_SUBTITLE_SUFFIXES = frozenset({".srt", ".vtt"})


def _service(services: Any, key: str) -> Any:
    if isinstance(services, dict):
        return services[key]
    return getattr(services, key)


def _current_outcome(
    session: Session, record: SessionRecord, services: Any
) -> tuple[dict[str, Any], int]:
    plan = session.get(OutcomePlan, record.id)
    if plan is None:
        value = derive_legacy_outcome(record)
        transformations = value.get("transformations")
        transformations = transformations if isinstance(transformations, dict) else {}
        value["transformations"] = {
            **transformations,
            **workflow_transformations(
                session,
                record.id,
                None,
                _service(services, "database"),
            ),
        }
        revision = 0
    else:
        value = deepcopy(plan.value_json) if isinstance(plan.value_json, dict) else {}
        revision = int(plan.revision)
    return value, revision


def _input_roles(workflow_kind: str, role: str) -> tuple[str, ...]:
    if workflow_kind == "audiobook" and role in {"prepared_text", "tts_optimized"}:
        return (role,)
    if role == "correction":
        return ("correction",)
    if role == "translation":
        return ("translation",)
    if workflow_kind == "media_edit":
        return ("media_edit_subtitles",)
    return ("transcription", "upload")


def _canonical_input_role(workflow_kind: str, role: str) -> str:
    return "media_edit" if workflow_kind == "media_edit" and role == "source" else role


def _external_input_role(workflow_kind: str, role: str) -> str:
    return "source" if workflow_kind == "media_edit" and role == "media_edit" else role


def _subtitle_file_path(services: Any, artifact: Artifact) -> Path:
    try:
        path = _service(services, "artifacts").paths.managed_path(artifact.relative_path)
    except (AttributeError, OSError, ValueError) as error:
        raise ValueError("The selected subtitle artifact is outside managed storage.") from error
    filename = str((artifact.metadata_json or {}).get("original_filename") or path.name)
    suffix = Path(filename).suffix.lower() or path.suffix.lower()
    kind = str(artifact.kind or "").strip().lower().lstrip(".")
    if suffix not in _SUBTITLE_SUFFIXES and kind not in {"srt", "vtt"}:
        raise ValueError("The selected artifact is not an SRT or VTT subtitle.")
    if not path.is_file():
        raise ValueError("The selected subtitle file is no longer available.")
    return path


def _verified_content_hash(services: Any, artifact: Artifact, *, subtitle: bool = True) -> str:
    expected = str(artifact.content_hash or "").strip().lower()
    if not expected:
        raise ValueError("The selected artifact has no verified content hash.")
    if subtitle:
        path = _subtitle_file_path(services, artifact)
    else:
        try:
            path = _service(services, "artifacts").paths.managed_path(artifact.relative_path)
        except (AttributeError, OSError, ValueError) as error:
            raise ValueError("The selected text artifact is outside managed storage.") from error
        if not path.is_file():
            raise ValueError("The selected text file is no longer available.")
    try:
        actual = sha256_file(path).lower()
    except OSError as error:
        raise ValueError("The selected artifact file could not be verified.") from error
    if actual != expected:
        raise ValueError("The selected artifact file does not match its recorded content hash.")
    return actual


def _active_media_edit_revision(session: Session, session_id: str) -> MediaEditPlanRevision | None:
    plan = session.scalar(select(MediaEditPlan).where(MediaEditPlan.session_id == session_id))
    if plan is None or not plan.active_revision_id:
        return None
    return session.get(MediaEditPlanRevision, plan.active_revision_id)


def _matches_active_media_edit_revision(
    artifact: Artifact, revision: MediaEditPlanRevision | None
) -> bool:
    """Keep a source selection on the media-edit revision the session exposes."""
    if artifact.role not in {"media_edit_media", "media_edit_subtitles"}:
        return True
    if revision is None:
        return False
    metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
    if str(metadata.get("content_hash") or "") != revision.content_hash:
        return False
    explicit_revision = str(
        metadata.get("media_edit_revision_id") or metadata.get("revision_id") or ""
    )
    if explicit_revision:
        return explicit_revision == revision.id
    revision_value = metadata.get("revision")
    if revision_value is None:
        return False
    try:
        revision_number = int(revision_value)
    except (TypeError, ValueError):
        return False
    return (
        str(metadata.get("plan_id") or "") == revision.plan_id
        and revision_number == revision.revision_number
    )


def _producer_stage(role: str, workflow_kind: str) -> str | None:
    if role == "source":
        return "edit_media" if workflow_kind == "media_edit" else None
    return ROLE_TO_STAGE.get(role)


def _selection_revision(session: Session, session_id: str, stage_key: str | None) -> int:
    if stage_key is None:
        return 0
    selection = session.get(SessionStageSelection, (session_id, stage_key))
    return int(selection.revision) if selection is not None else 0


def _resolve_consumer_input(
    session: Session,
    services: Any,
    record: SessionRecord,
    consumer: str,
    role: str,
    translation_settings: SessionSetting | None,
) -> tuple[dict[str, Any], list[str]]:
    reasons: list[str] = []
    effective_role = _external_input_role(record.workflow_kind, role)
    actual_roles = _input_roles(record.workflow_kind, effective_role)
    stage_key: str | None = None
    artifact: Artifact | None = None

    explicit_translation_id = ""
    if consumer == "translation" and translation_settings is not None:
        value = translation_settings.value_json
        if isinstance(value, dict):
            explicit_translation_id = str(value.get("source_artifact_id") or "").strip()
    if explicit_translation_id:
        candidate = session.get(Artifact, explicit_translation_id)
        if candidate is None:
            reasons.append("The persisted translation source is unavailable.")
        elif candidate.role not in actual_roles:
            reasons.append(
                "The persisted translation source does not match the selected input role."
            )
        else:
            artifact = candidate
            stage_key = ROLE_TO_STAGE.get(candidate.role)

    if not explicit_translation_id:
        stage_key = _producer_stage(effective_role, record.workflow_kind)
        if effective_role == "source" and record.workflow_kind != "media_edit":
            selections = selected_artifacts(session, record.id)
            artifact = selections.get("transcribe")
            if artifact is None:
                primary = resolve_primary_source(session, record.id)
                artifact = (
                    primary.artifact
                    if primary.artifact and primary.artifact.role == "upload"
                    else None
                )
        elif stage_key is not None:
            artifact = selected_artifacts(session, record.id).get(stage_key)

    if artifact is not None:
        stage_key = ROLE_TO_STAGE.get(artifact.role)

    if artifact is None and not reasons:
        reasons.append("No artifact is selected for this workflow input.")
    if artifact is not None:
        if artifact.state == "deleted":
            reasons.append("The selected artifact has been deleted.")
        elif artifact.role not in actual_roles:
            reasons.append("The selected artifact does not match the workflow input role.")
        elif not artifact_accessible_in_session(session, record.id, artifact):
            reasons.append("The selected artifact is not accessible from this session.")
        elif role == "source" and record.workflow_kind == "media_edit":
            if not _matches_active_media_edit_revision(
                artifact, _active_media_edit_revision(session, record.id)
            ):
                reasons.append(
                    "The selected subtitles do not match the active media-edit revision."
                )
        if not reasons:
            try:
                verified_hash = _verified_content_hash(
                    services, artifact, subtitle=record.workflow_kind != "audiobook"
                )
            except ValueError as error:
                reasons.append(str(error))
                verified_hash = None
        else:
            verified_hash = None
    else:
        verified_hash = None

    selection_revision = _selection_revision(session, record.id, stage_key)
    artifact_info = None
    if artifact is not None:
        version = None
        if stage_key is not None:
            history, _selected = stage_histories(session, record.id, [stage_key], limit=1)
            version = next(
                (
                    item.get("version")
                    for item in history.get(stage_key, {}).get("items", [])
                    if item.get("id") == artifact.id
                ),
                None,
            )
        artifact_info = {
            "artifact_id": artifact.id,
            "role": artifact.role,
            "state": artifact.state,
            "version": version,
            "sha256": verified_hash,
        }

    return (
        {
            "consumer": consumer,
            "input_role": effective_role,
            "producer_stage": stage_key or "source",
            "producer_selection_revision": selection_revision,
            "artifact": artifact_info,
            "blocking_reasons": reasons,
        },
        reasons,
    )


def _workflow_inputs_manifest_in_session(
    session: Session, services: Any, session_id: str
) -> dict[str, Any]:
    record = session.get(SessionRecord, session_id)
    if record is None or record.trashed_at is not None or record.status == "purging":
        raise KeyError(session_id)
    value, outcome_revision = _current_outcome(session, record, services)
    stored_inputs = value.get("inputs")
    stored_inputs = stored_inputs if isinstance(stored_inputs, dict) else {}
    defaults = derive_legacy_outcome(record).get("inputs", {})
    inputs: dict[str, str] = {}
    for consumer in sorted(WORKFLOW_INPUT_CONSUMERS):
        role = (
            str(stored_inputs.get(consumer) or defaults.get(consumer) or "source").strip().lower()
        )
        if role not in {"source", "correction", "translation"} and not (
            record.workflow_kind == "media_edit" and role == "media_edit"
        ):
            role = str(defaults.get(consumer) or "source").strip().lower()
        if record.workflow_kind == "media_edit" and role == "source":
            role = "media_edit"
        if consumer == "translation" and role == "translation":
            role = str(defaults.get(consumer) or "correction").strip().lower()
        inputs[consumer] = role
    configured_transformations = value.get("transformations")
    configured_transformations = (
        configured_transformations
        if isinstance(configured_transformations, dict)
        else {}
    )
    if record.workflow_kind == "audiobook":
        inputs["generation"] = (
            "tts_optimized"
            if configured_transformations.get("llm_tts_document_optimization")
            else "prepared_text"
        )
    translation_settings = session.get(SessionSetting, (session_id, "translation"))
    text_settings = session.get(SessionSetting, (session_id, "text"))
    deliverables = value.get("deliverables")
    deliverables = deliverables if isinstance(deliverables, dict) else {}
    consumer_results: dict[str, dict[str, Any]] = {}
    blocking_reasons: list[str] = []
    for consumer in sorted(WORKFLOW_INPUT_CONSUMERS):
        role = inputs[consumer]
        public_role = _external_input_role(record.workflow_kind, role)
        applicable = not (record.workflow_kind == "audiobook" and consumer == "translation")
        enabled = (
            bool(configured_transformations.get("translate"))
            if consumer == "translation"
            else bool(configured_transformations.get("generate_audio") or deliverables.get("audiobook") or deliverables.get("voiceover"))
        )
        if applicable:
            result, reasons = _resolve_consumer_input(
                session, services, record, consumer, public_role, translation_settings
            )
        else:
            reasons = []
            result = {
                "consumer": consumer, "producer_stage": None,
                "producer_selection_revision": 0, "artifact": None,
                "blocking_reasons": [],
            }
        result["applicable"] = applicable
        result["input_role"] = role
        result["requested_role"] = public_role
        consumer_results[consumer] = result
        if applicable and enabled:
            blocking_reasons.extend(f"{consumer}: {reason}" for reason in reasons)
    try:
        assert_session_idle(session, session_id)
    except RevisionConflict as error:
        blocking_reasons.append(str(error))
    return {
        "session_id": session_id,
        "outcome_revision": outcome_revision,
        "inputs": inputs,
        "transformations": {
            key: bool(configured_transformations.get(key))
            for key in ("llm_tts_optimization", "llm_tts_document_optimization")
        },
        "consumers": consumer_results,
        "translation_settings_revision": (
            int(translation_settings.revision) if translation_settings is not None else 0
        ),
        "text_settings_revision": int(text_settings.revision) if text_settings is not None else 0,
        "blocking_reasons": list(dict.fromkeys(blocking_reasons)),
    }


def get_workflow_inputs(services: Any, session_id: str) -> dict[str, Any]:
    """Return a compact, text-free manifest for the selected workflow inputs."""
    with _service(services, "database").snapshot_session() as session:
        return _workflow_inputs_manifest_in_session(session, services, session_id)


def configure_speech_optimization(
    services: Any, session_id: str, *, mode: str,
    expected_outcome_revision: int, expected_text_settings_revision: int,
    annotation_mode: str = "off", annotation_only: bool = False,
    db_session: Session | None = None,
) -> dict[str, Any]:
    """Synchronize speech controls inside one revision-fenced transaction."""
    if mode not in {"off", "document", "inline"}:
        raise ValueError("mode must be off, document, or inline.")
    if annotation_mode not in {"off", "dialogue", "speakers"}:
        raise ValueError("annotation_mode must be off, dialogue, or speakers.")
    if annotation_mode != "off" and mode != "document":
        raise ValueError("Annotations require document optimization.")
    if annotation_only and annotation_mode == "off":
        raise ValueError("annotation_only requires an annotation mode.")
    for revision in (expected_outcome_revision, expected_text_settings_revision):
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("Expected revisions must be non-negative integers.")
    if db_session is None:
        with _service(services, "database").immediate_session() as session:
            return configure_speech_optimization(
                services, session_id, mode=mode,
                expected_outcome_revision=expected_outcome_revision,
                expected_text_settings_revision=expected_text_settings_revision,
                annotation_mode=annotation_mode, annotation_only=annotation_only,
                db_session=session,
            )
    session = db_session
    record = session.get(SessionRecord, session_id)
    if record is None or record.trashed_at is not None or record.status == "purging":
        raise KeyError(session_id)
    assert_session_idle(session, session_id)
    outcome_value, outcome_revision = _current_outcome(session, record, services)
    text = session.get(SessionSetting, (session_id, "text"))
    text_revision = int(text.revision) if text is not None else 0
    if outcome_revision != expected_outcome_revision:
        raise RevisionConflict("The workflow plan changed in another client.")
    if text_revision != expected_text_settings_revision:
        raise RevisionConflict("Text settings changed in another client.")
    flags = {
        "llm_tts_document_optimization": mode == "document",
        "llm_tts_optimization": mode == "inline",
    }
    transformations = outcome_value.get("transformations")
    transformations = transformations if isinstance(transformations, dict) else {}
    if outcome_revision == 0 or any(transformations.get(key) != value for key, value in flags.items()):
        outcome_value["transformations"] = {**transformations, **flags}
        _service(services, "outcome_plans").update_in_session(
            session, session_id, outcome_revision, outcome_value,
            preserve_input_selections=True,
        )
    changes = {
        **flags, "llm_tts_annotation_mode": annotation_mode,
        "llm_tts_annotation_only": annotation_only,
    }
    stored = dict(text.value_json or {}) if text is not None else {}
    if any(key not in stored or stored[key] != value for key, value in changes.items()):
        _service(services, "workspace_settings").patch_in_session(
            session, session_id, "text", text_revision, changes
        )
    return _workflow_inputs_manifest_in_session(session, services, session_id)


def select_workflow_input(
    services: Any,
    session_id: str,
    consumer: str,
    role: str,
    artifact_id: str,
    *,
    expected_outcome_revision: int,
    expected_selection_revision: int,
    expected_translation_settings_revision: int | None = None,
    expected_text_settings_revision: int | None = None,
    db_session: Session | None = None,
) -> dict[str, Any]:
    """Atomically select an exact, hash-verified producer input for one consumer."""
    normalized_consumer = str(consumer or "").strip().lower()
    normalized_role = str(role or "").strip().lower()
    normalized_artifact_id = str(artifact_id or "").strip()
    if normalized_consumer not in WORKFLOW_INPUT_CONSUMERS:
        raise ValueError("consumer must be 'translation' or 'generation'.")
    if normalized_role not in WORKFLOW_INPUT_ROLES:
        raise ValueError("Unknown workflow input role.")
    if normalized_consumer == "translation" and normalized_role == "translation":
        raise ValueError("Translation cannot use a translation artifact as its input.")
    if not normalized_artifact_id:
        raise ValueError("artifact_id is required.")
    if normalized_consumer == "translation" and expected_translation_settings_revision is None:
        raise ValueError("Translation input selection requires its settings revision.")

    if (
        isinstance(expected_outcome_revision, bool)
        or not isinstance(expected_outcome_revision, int)
        or expected_outcome_revision < 0
        or isinstance(expected_selection_revision, bool)
        or not isinstance(expected_selection_revision, int)
        or expected_selection_revision < 0
    ):
        raise ValueError("Expected revisions must be non-negative integers.")
    if expected_translation_settings_revision is not None and (
        isinstance(expected_translation_settings_revision, bool)
        or not isinstance(expected_translation_settings_revision, int)
        or expected_translation_settings_revision < 0
    ):
        raise ValueError("Expected translation settings revision must be non-negative.")
    if normalized_consumer == "generation" and expected_translation_settings_revision is not None:
        raise ValueError(
            "Translation settings revision is only valid for translation input selection."
        )

    values = {
        "session_id": session_id,
        "consumer": normalized_consumer,
        "role": normalized_role,
        "artifact_id": normalized_artifact_id,
        "expected_outcome_revision": expected_outcome_revision,
        "expected_selection_revision": expected_selection_revision,
        "expected_translation_settings_revision": expected_translation_settings_revision,
        "expected_text_settings_revision": expected_text_settings_revision,
    }
    if db_session is not None:
        return select_workflow_input_in_session(services, db_session, **values)
    with _service(services, "database").immediate_session() as session:
        return select_workflow_input_in_session(services, session, **values)


def select_workflow_input_in_session(
    services: Any,
    session: Session,
    session_id: str,
    consumer: str,
    role: str,
    artifact_id: str,
    *,
    expected_outcome_revision: int,
    expected_selection_revision: int,
    expected_translation_settings_revision: int | None = None,
    expected_text_settings_revision: int | None = None,
) -> dict[str, Any]:
    """Compose the input mutation into a caller-owned write transaction."""

    consumer = str(consumer or "").strip().lower()
    role = str(role or "").strip().lower()
    artifact_id = str(artifact_id or "").strip()
    if consumer not in WORKFLOW_INPUT_CONSUMERS:
        raise ValueError("consumer must be 'translation' or 'generation'.")
    if role not in WORKFLOW_INPUT_ROLES:
        raise ValueError("Unknown workflow input role.")
    if consumer == "translation" and role == "translation":
        raise ValueError("Translation cannot use a translation artifact as its input.")
    if not artifact_id:
        raise ValueError("artifact_id is required.")
    if (
        isinstance(expected_outcome_revision, bool)
        or not isinstance(expected_outcome_revision, int)
        or expected_outcome_revision < 0
        or isinstance(expected_selection_revision, bool)
        or not isinstance(expected_selection_revision, int)
        or expected_selection_revision < 0
    ):
        raise ValueError("Expected revisions must be non-negative integers.")
    if consumer == "translation":
        if (
            isinstance(expected_translation_settings_revision, bool)
            or not isinstance(expected_translation_settings_revision, int)
            or expected_translation_settings_revision < 0
        ):
            raise ValueError("Translation input selection requires its settings revision.")
    elif expected_translation_settings_revision is not None:
        raise ValueError(
            "Translation settings revision is only valid for translation input selection."
        )

    record = session.get(SessionRecord, session_id)
    if record is None or record.trashed_at is not None or record.status == "purging":
        raise KeyError(session_id)
    assert_session_idle(session, session_id)
    audiobook_generation = record.workflow_kind == "audiobook" and consumer == "generation"
    if audiobook_generation:
        if role not in {"prepared_text", "tts_optimized"}:
            raise ValueError("Audiobook generation requires prepared_text or tts_optimized.")
        if isinstance(expected_text_settings_revision, bool) or not isinstance(expected_text_settings_revision, int) or expected_text_settings_revision < 0:
            raise ValueError("Audiobook input selection requires its text settings revision.")
    elif role in {"prepared_text", "tts_optimized"} or expected_text_settings_revision is not None:
        raise ValueError("Text input roles and revision are only valid for audiobook generation.")

    outcome_value, current_outcome_revision = _current_outcome(session, record, services)
    if int(expected_outcome_revision) != current_outcome_revision:
        raise RevisionConflict("The workflow plan changed in another client.")
    text_settings = session.get(SessionSetting, (session_id, "text"))
    current_text_revision = int(text_settings.revision) if text_settings is not None else 0
    if audiobook_generation and expected_text_settings_revision != current_text_revision:
        raise RevisionConflict("Text settings changed in another client.")
    translation_settings = session.get(SessionSetting, (session_id, "translation"))
    current_translation_revision = (
        int(translation_settings.revision) if translation_settings is not None else 0
    )
    if consumer == "translation" and (
        expected_translation_settings_revision is None
        or int(expected_translation_settings_revision) != current_translation_revision
    ):
        raise RevisionConflict("Translation settings changed in another client.")

    artifact = session.get(Artifact, artifact_id)
    if (
        artifact is None
        or artifact.state == "deleted"
        or not artifact_accessible_in_session(session, session_id, artifact)
    ):
        raise KeyError(artifact_id)
    actual_roles = _input_roles(record.workflow_kind, role)
    if artifact.role not in actual_roles:
        raise ValueError("The selected artifact does not match the requested input role.")
    stage_key = ROLE_TO_STAGE.get(artifact.role)
    if stage_key is not None and artifact.session_id != session_id:
        raise ValueError("A selected producer artifact must belong to this session.")
    if record.workflow_kind == "media_edit" and role == "source":
        if not _matches_active_media_edit_revision(
            artifact, _active_media_edit_revision(session, session_id)
        ):
            raise ValueError("The selected subtitles do not match the active media-edit revision.")
    verified_hash = _verified_content_hash(services, artifact, subtitle=not audiobook_generation)

    if artifact.role == "upload":
        if int(expected_selection_revision) != 0:
            raise RevisionConflict("The source producer selection changed in another client.")
        primary = resolve_primary_source(session, session_id)
        if primary.artifact is None or primary.artifact.id != artifact.id:
            raise ValueError("An upload input must be the session's current primary source.")
        if str(primary.artifact.content_hash or "").lower() != verified_hash:
            raise ValueError("The current primary source hash changed during selection.")
        selection_revision = 0
        stage_key = None
    else:
        if stage_key is None:
            raise ValueError("The selected artifact has no compatible producer stage.")
        selection_revision = _selection_revision(session, session_id, stage_key)
        if int(expected_selection_revision) != selection_revision:
            raise RevisionConflict("The producer selection changed in another client.")

    if stage_key is not None:
        choose_artifact(session, session_id, stage_key, artifact.id)
        selection_revision = _selection_revision(session, session_id, stage_key)

    stored_role = _canonical_input_role(record.workflow_kind, role)
    inputs = outcome_value.get("inputs")
    if not isinstance(inputs, dict):
        inputs = {}
    if audiobook_generation:
        transformations = outcome_value.get("transformations")
        transformations = transformations if isinstance(transformations, dict) else {}
        flags = {
            "llm_tts_document_optimization": role == "tts_optimized",
            "llm_tts_optimization": False if role == "tts_optimized" else bool(transformations.get("llm_tts_optimization")),
        }
        outcome_value["transformations"] = {**transformations, **flags}
        stored = dict(text_settings.value_json or {}) if text_settings is not None else {}
        if any(key not in stored or stored[key] != value for key, value in flags.items()):
            _service(services, "workspace_settings").patch_in_session(
                session, session_id, "text", current_text_revision, flags
            )
    else:
        outcome_value["inputs"] = {**inputs, consumer: stored_role}
    _service(services, "outcome_plans").update_in_session(
        session,
        session_id,
        current_outcome_revision,
        outcome_value,
        preserve_input_selections=audiobook_generation,
    )
    if consumer == "translation":
        _service(services, "workspace_settings").patch_in_session(
            session,
            session_id,
            "translation",
            current_translation_revision,
            {"source_artifact_id": artifact.id},
        )

    manifest = _workflow_inputs_manifest_in_session(session, services, session_id)
    manifest["selected"] = {
        "consumer": consumer,
        "requested_role": role,
        "stored_role": stored_role,
        "producer_stage": stage_key or "source",
        "producer_selection_revision": selection_revision,
        "artifact_id": artifact.id,
        "sha256": verified_hash,
    }
    return manifest


def workflow_transformations(
    session: Session, session_id: str, outcome: OutcomePlan | None, database: Database,
) -> dict[str, Any]:
    # A saved outcome is an explicit workflow choice, including disabled steps.
    if outcome is not None:
        value = (outcome.value_json or {}).get("transformations")
        return dict(value) if isinstance(value, dict) else {}

    # MCP sessions can be configured directly without creating an outcome plan.
    # Use their effective text settings at every planning/execution boundary.
    from .workspace_settings import WorkspaceSettingsService

    settings = WorkspaceSettingsService(database).get_in_session(session, session_id, "text")["effective"]
    return {key: bool(settings.get(key)) for key in (
        "llm_tts_optimization", "llm_tts_document_optimization",
    )}
