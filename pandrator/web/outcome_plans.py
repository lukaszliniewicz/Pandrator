"""Revisioned outcome plans and their legacy workflow projection."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy.orm import Session

from .database import Database
from .models import OutcomePlan, OutcomePlanHistory, SessionRecord, utcnow
from .settings_policy import RevisionConflict


def derive_legacy_outcome(record: SessionRecord) -> dict[str, Any]:
    included = set(record.included_stages_json or [])
    kind = record.workflow_kind
    return {
        "version": 1,
        "workflow_kind": kind,
        "focus": "custom" if record.workflow_preset == "custom" else "guided",
        "deliverables": {
            "audiobook": kind == "audiobook",
            "subtitles": kind == "subtitles" or "export" in included,
            "voiceover": kind == "voiceover" or "generate_audio" in included,
            "edited_media": kind == "media_edit",
        },
        "transformations": {
            "transcribe": "transcribe" in included,
            "media_edit": kind == "media_edit" or "edit_media" in included,
            "correct": "correct" in included,
            "translate": "translate" in included,
            "deterministic_normalization": True,
            "llm_tts_optimization": False,
            "llm_tts_document_optimization": False,
            "generate_audio": "generate_audio" in included or kind == "audiobook",
            "rvc": False,
        },
        "inputs": {
            "translation": "correction" if "correct" in included else "source",
            "generation": "translation"
            if "translate" in included
            else "correction"
            if "correct" in included
            else "source",
        },
        "export": {
            "audio": "generated" if kind in {"audiobook", "voiceover"} else "preserve",
            "subtitles": "translation" if "translate" in included else "source",
        },
    }


def resolve_pipeline(
    plan: dict[str, Any], *, source_requires_transcription: bool = False
) -> list[dict[str, str]]:
    kind = str(plan.get("workflow_kind") or "audiobook")
    transformations = plan.get("transformations")
    if not isinstance(transformations, dict):
        transformations = {}
    deliverables = plan.get("deliverables")
    if not isinstance(deliverables, dict):
        deliverables = {}
    stages: list[tuple[str, str]] = []
    if kind == "audiobook":
        stages.append(("clean_source", "Clean source"))
        stages.append(("prepare_text", "Segment narration"))
    elif source_requires_transcription or transformations.get("transcribe"):
        stages.append(("transcribe", "Transcribe"))
    if kind == "media_edit" or transformations.get("media_edit"):
        stages.append(("edit_media", "Review and render edit"))
    if transformations.get("correct"):
        stages.append(("correct", "Correct subtitles"))
    if transformations.get("translate"):
        stages.append(("translate", "Translate"))
    if transformations.get("llm_tts_document_optimization") or transformations.get(
        "llm_tts_optimization"
    ):
        timing = (
            "before generation"
            if transformations.get("llm_tts_document_optimization")
            else "while generating"
        )
        stages.append(("optimize_tts", f"Optimize for speech {timing}"))
    if (
        transformations.get("generate_audio")
        or deliverables.get("audiobook")
        or deliverables.get("voiceover")
    ):
        stages.append(("generate_audio", "Generate audio"))
    if transformations.get("rvc"):
        stages.append(("apply_rvc", "Apply RVC"))
    if any(bool(value) for value in deliverables.values()):
        stages.append(("export", "Export"))
    return [{"key": key, "title": title} for key, title in stages]


class OutcomePlanService:
    def __init__(self, database: Database):
        self.database = database

    def get(self, session_id: str) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            plan = session.get(OutcomePlan, session_id)
            if plan is None:
                plan = OutcomePlan(
                    session_id=session_id, value_json=derive_legacy_outcome(record)
                )
                session.add(plan)
                session.flush()
            value = deepcopy(plan.value_json)
            revision = plan.revision
        return {
            "value": value,
            "revision": revision,
            "pipeline": resolve_pipeline(value),
        }

    def update(
        self, session_id: str, expected_revision: int, value: dict[str, Any]
    ) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            result = self.update_in_session(
                session, session_id, expected_revision, value
            )
            revision = result["revision"]
        return {
            "value": result["value"],
            "revision": revision,
            "pipeline": result["pipeline"],
        }

    def update_in_session(
        self,
        session: Session,
        session_id: str,
        expected_revision: int,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        """Update an outcome plan inside a caller-owned transaction."""

        record = session.get(SessionRecord, session_id)
        if record is None:
            raise KeyError(session_id)
        plan = session.get(OutcomePlan, session_id)
        previous_value = deepcopy(plan.value_json) if plan is not None else {}
        if plan is None:
            if expected_revision != 0:
                raise RevisionConflict(
                    "The workflow plan was created in another client."
                )
            plan = OutcomePlan(session_id=session_id, value_json=value, revision=1)
            session.add(plan)
        else:
            if expected_revision != plan.revision:
                raise RevisionConflict(
                    "The workflow plan changed in another client."
                )
            session.add(
                OutcomePlanHistory(
                    session_id=session_id,
                    value_json=plan.value_json,
                    revision=plan.revision,
                )
            )
            plan.value_json = value
            plan.revision += 1
            plan.updated_at = utcnow()
        previous_inputs = previous_value.get("inputs")
        if not isinstance(previous_inputs, dict):
            previous_inputs = {}
        next_inputs = value.get("inputs")
        if not isinstance(next_inputs, dict):
            next_inputs = {}
        if str(previous_inputs.get("translation") or "correction") != str(
            next_inputs.get("translation") or "correction"
        ):
            # The chosen translation and speech-optimized descendants may
            # belong to the other source branch. Preserve their immutable
            # history, but do not continue presenting that branch as the
            # selected/current workflow result.
            from .artifact_selection import clear_selection

            clear_selection(session, session_id, "translate")
        record.workflow_kind = str(
            value.get("workflow_kind") or record.workflow_kind
        )
        record.workflow_preset = "custom"
        pipeline_keys = {item["key"] for item in resolve_pipeline(value)}
        record.included_stages_json = [
            key
            for key in (
                "transcribe",
                "edit_media",
                "correct",
                "translate",
                "optimize_tts",
                "generate_audio",
                "export",
            )
            if key in pipeline_keys
        ]
        record.revision += 1
        record.updated_at = utcnow()
        session.flush()
        return {
            "value": deepcopy(value),
            "revision": plan.revision,
            "pipeline": resolve_pipeline(value),
        }
