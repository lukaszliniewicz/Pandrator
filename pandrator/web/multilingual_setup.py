"""Deferred language-branch intent, separate from active translation projects."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy.orm import Session

from .models import SessionSetting

_LANGUAGE = re.compile(r"^[a-z]{2,8}(?:-[a-z0-9]{1,8})*$")
SECTION = "multilingual_setup"
_DEFERRED_STAGES = frozenset({"translate", "generate_audio", "apply_rvc"})


def canonical_language(value: str) -> str:
    normalized = str(value or "").strip().replace("_", "-").lower()
    if not 2 <= len(normalized) <= 40 or not _LANGUAGE.fullmatch(normalized):
        raise ValueError("Use a language code of 2-40 letters, digits, and hyphens.")
    if normalized == "auto":
        raise ValueError("Choose a specific target language.")
    return normalized


class MultilingualSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_languages: list[str] = Field(min_length=1, max_length=20)
    generate_voiceover: StrictBool = False
    keep_source_subtitles: StrictBool = True

    @field_validator("target_languages")
    @classmethod
    def normalize_languages(_cls, languages: list[str]) -> list[str]:
        normalized = [canonical_language(language) for language in languages]
        if len(normalized) != len(set(normalized)):
            raise ValueError("Target languages must be unique.")
        return normalized


def validate_source(
    setup: MultilingualSetup,
    *,
    workflow_kind: str,
    source_language: str,
    target_language: str | None,
    included_stages: list[str],
) -> list[str]:
    if workflow_kind == "audiobook":
        raise ValueError("Multilingual setup requires a subtitle or media workflow.")
    if target_language is not None:
        raise ValueError("Multilingual source cannot have a target language.")
    if "correct" not in included_stages:
        raise ValueError("Multilingual source requires the correction stage.")
    if source_language and source_language.strip().lower() != "auto":
        language = canonical_language(source_language)
        if language in setup.target_languages:
            raise ValueError("A target language repeats the source language.")
    return [stage for stage in included_stages if stage not in _DEFERRED_STAGES]


def read_setup(db: Session, session_id: str) -> MultilingualSetup | None:
    setting = db.get(SessionSetting, (session_id, SECTION))
    return MultilingualSetup.model_validate(setting.value_json) if setting else None


def write_setup(db: Session, session_id: str, setup: MultilingualSetup | None) -> None:
    setting = db.get(SessionSetting, (session_id, SECTION))
    if setup is None:
        if setting is not None:
            db.delete(setting)
        return
    value: dict[str, Any] = setup.model_dump(mode="json")
    if setting is None:
        db.add(SessionSetting(session_id=session_id, section=SECTION, value_json=value))
    else:
        setting.value_json = value
        setting.revision += 1


def deferred_source_outcome(value: dict[str, Any], *, workflow_kind: str) -> dict[str, Any]:
    """Keep source correction active while deferring translation and speech."""
    result = deepcopy(value)
    result["workflow_kind"] = workflow_kind
    deliverables = dict(result.get("deliverables") or {})
    deliverables["voiceover"] = False
    result["deliverables"] = deliverables
    transformations = dict(result.get("transformations") or {})
    transformations.update({"translate": False, "generate_audio": False, "rvc": False})
    result["transformations"] = transformations
    inputs = dict(result.get("inputs") or {})
    inputs.update({"translation": "correction", "generation": "correction"})
    result["inputs"] = inputs
    export = dict(result.get("export") or {})
    export.update({"audio": "preserve", "subtitles": "source"})
    result["export"] = export
    return result
