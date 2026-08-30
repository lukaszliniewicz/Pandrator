"""LLM-backed subtitle correction using Pandrator provider settings."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from .. import llm_handler
from .llm_config import DubbingLLMSettings
from .llm_config import resolve_dubbing_llm_settings as _resolve_dubbing_llm_settings
from .models import SubtitleSegment
from .srt_utils import (
    compose_srt,
    create_translation_blocks,
    normalize_timing_context_mode,
    split_speaker_label,
    subtitle_boundary_cue,
    subtitle_task_cue,
    timing_context_mode_from_settings,
)

logger = logging.getLogger(__name__)

DEFAULT_LLM_CHAR_LIMIT = 6000
DEFAULT_MAX_LINE_LENGTH = 42
MAX_CORRECTION_ATTEMPTS = 3
CORRECTION_CONTEXT_CUES = 8
ProgressCallback = Callable[[float, str | None], None]
UnitCompletedCallback = Callable[[str, dict[str, Any]], None]
CORRECTION_SYSTEM_PROMPT = (
    "You are an expert subtitle transcript editor. Correct the supplied source-language "
    "cues accurately and conservatively. Return only valid JSON in the requested operation "
    "format, without comments, acknowledgments, markdown, or questions."
)

CORRECTION_PROMPT_TEMPLATE = """
Review the array of {subtitle_count} subtitle cues below and return this JSON shape:
{response_shape}

Instructions:
1. Use your editorial judgment to fix punctuation, capitalization, spelling, and clear transcription errors. Remove isolated filler and accidental repetition when appropriate.
2. Preserve each speaker's meaning, register, names, and terminology. Do not add speaker labels to replacement text. Preserve the supplied speaker by default, but correct a likely diarization mistake when the discourse clearly supports it. When changing a speaker or merging across different supplied speakers, include one `speakers` entry for every replacement text and use only a supplied speaker ID. Otherwise omit `speakers`.
3. Return operations only for cues that need a change; return an empty `operations` array when no changes are needed.
4. Available actions:
   - "edit": one `cue_id` and exactly one corrected text.
   - "delete": one or more sequential `cue_id` values that contain no meaningful speech, with an empty `texts` array.
   - "merge": two or more sequential `cue_id` values whose boundary breaks one thought, with one or more corrected replacement texts.
   - "split": one `cue_id` and two or more replacement texts, only when semantic correction genuinely requires separate cues.
5. Cue timing, reading speed, visual wrapping, and line layout are handled by Pandrator after editing. Do not insert line breaks or split/merge merely to change visual layout.
6. Every replacement must be complete, corrected plain text. Do not include IDs that are only context.
7. If prior corrected context is provided, use it only for continuity. Operate only on the `cue_id` values present in the current array; they identify cues in the pinned source revision and are not batch-local positions.
8. {cue_context_policy}
9. Overlapping cues from different speakers can be legitimate simultaneous speech. Preserve meaningful speech. You may delete a very short, inconsequential interjection only when it obscures a longer utterance, and remove one copy of clearly duplicated near-identical ASR text that occupies the same time span.
10. {known_speakers_policy}

Additional context and instructions specific to your particular batch, if any:
{correction_instructions}
""".strip()

CONTEXT_PROMPT_TEMPLATE = """
Prior corrected cues for continuity (context only; do not output operations for them):
{context_previous_cues}
""".strip()

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class CorrectionResult:
    srt_content: str
    cost: float = 0.0
    response_count: int = 0
    output_path: str = ""
    cost_sources: tuple[str, ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)
    speaker_by_subtitle: dict[int, str] = field(default_factory=dict)


def _report_progress(
    callback: ProgressCallback | None,
    value: float,
    detail: str,
) -> None:
    if callback is None:
        return
    callback(max(0.0, min(1.0, float(value))), detail)


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def resolve_dubbing_llm_settings(
    settings: dict[str, Any],
    *,
    stage: str = "correction",
) -> DubbingLLMSettings:
    """Backward-compatible entry point for the native stage resolver."""
    return _resolve_dubbing_llm_settings(settings, stage=stage)


def extract_json_payload(response_text: str) -> Any:
    """Extract and parse the first JSON object/array from an LLM response."""
    raw_text = str(response_text or "").strip()
    if not raw_text:
        raise ValueError("LLM response was empty.")

    fence_match = _FENCED_JSON_RE.search(raw_text)
    if fence_match:
        raw_text = fence_match.group(1).strip()

    decoder = json.JSONDecoder()
    for index, char in enumerate(raw_text):
        if char not in "[{":
            continue
        try:
            payload, _end = decoder.raw_decode(raw_text[index:])
            return payload
        except json.JSONDecodeError:
            continue

    raise ValueError("LLM response did not contain valid JSON.")


def parse_correction_operations(response_text: str) -> list[dict[str, Any]]:
    """Parse a Subdub-style correction response."""
    payload = extract_json_payload(response_text)
    if isinstance(payload, dict):
        if "operations" not in payload:
            raise ValueError("Correction response must contain an 'operations' field.")
        operations = payload["operations"]
    elif isinstance(payload, list):
        operations = payload
    else:
        raise ValueError("Correction response must be a JSON object or list.")

    if not isinstance(operations, list):
        raise ValueError("Correction response 'operations' must be a list.")

    normalized: list[dict[str, Any]] = []
    for operation_index, operation in enumerate(operations, start=1):
        if not isinstance(operation, dict):
            raise ValueError(
                f"Correction operation {operation_index} must be a JSON object."
            )
        action = str(operation.get("action") or "").strip().lower()
        if action not in {"edit", "delete", "merge", "split"}:
            raise ValueError(
                f"Correction operation {operation_index} has unsupported action "
                f"'{action or '<blank>'}'."
            )
        has_canonical_ids = "cue_ids" in operation
        ids = operation.get("cue_ids" if has_canonical_ids else "ids")
        texts = operation.get("texts")
        speakers = operation.get("speakers", [])
        if not isinstance(ids, list) or not ids:
            raise ValueError(
                f"Correction operation {operation_index} must contain a non-empty cue_ids list."
            )
        normalized_ids: list[int] = []
        for item in ids:
            if isinstance(item, bool):
                raise ValueError(
                    f"Correction operation {operation_index} contains a non-integer cue ID."
                )
            if isinstance(item, int):
                normalized_ids.append(item)
            elif isinstance(item, str) and item.strip().isdigit():
                normalized_ids.append(int(item.strip()))
            else:
                raise ValueError(
                    f"Correction operation {operation_index} contains a non-integer cue ID."
                )
        if len(set(normalized_ids)) != len(normalized_ids):
            raise ValueError(
                f"Correction operation {operation_index} repeats a cue ID."
            )
        if not isinstance(texts, list):
            raise ValueError(
                f"Correction operation {operation_index} must contain a texts list."
            )
        if any(not isinstance(text, str) for text in texts):
            raise ValueError(
                f"Correction operation {operation_index} texts must all be strings."
            )
        if speakers is None:
            speakers = []
        if not isinstance(speakers, list) or any(
            not isinstance(speaker, str) for speaker in speakers
        ):
            raise ValueError(
                f"Correction operation {operation_index} speakers must be a list of strings."
            )
        normalized_operation = {
            "action": action,
            "cue_ids": normalized_ids,
            "id_namespace": (
                "source_revision_cue" if has_canonical_ids else "legacy_batch_local"
            ),
            "texts": list(texts),
        }
        if speakers:
            normalized_operation["speakers"] = [
                str(speaker).strip() for speaker in speakers
            ]
        normalized.append(normalized_operation)
    return normalized


def _operation_source_ids(
    block: list[dict[str, Any]],
    operation: Mapping[str, Any],
) -> list[int]:
    """Resolve canonical source IDs while accepting the pre-contract local form."""

    raw_ids = operation.get("cue_ids")
    if raw_ids is None:
        raw_ids = operation.get("ids", [])
        namespace = "legacy_batch_local"
    else:
        namespace = str(operation.get("id_namespace") or "source_revision_cue")
    ids = [int(value) for value in raw_ids or []]
    if namespace != "legacy_batch_local":
        return ids
    source_ids = [int(item["index"]) for item in block]
    return [source_ids[value - 1] if 1 <= value <= len(source_ids) else value for value in ids]


def validate_correction_operations(
    block: list[dict[str, Any]],
    operations: list[dict[str, Any]],
    *,
    no_remove_subtitles: bool = False,
    known_speakers: set[str] | None = None,
) -> None:
    """Reject ambiguous operation sets before any subtitle mutation is applied."""
    valid_ids = {int(item["index"]) for item in block}
    claimed_ids: set[int] = set()
    for operation_index, operation in enumerate(operations, start=1):
        action = str(operation["action"])
        ids = _operation_source_ids(block, operation)
        texts = [
            text
            for raw_text in operation["texts"]
            if (text := _normalize_replacement_text(raw_text))
        ]
        speakers = [
            str(speaker or "").strip() for speaker in operation.get("speakers", [])
        ]
        unexpected = [cue_id for cue_id in ids if cue_id not in valid_ids]
        if unexpected:
            raise ValueError(
                f"Correction operation {operation_index} references cue ID(s) "
                f"outside this block: {unexpected}."
            )
        overlap = [cue_id for cue_id in ids if cue_id in claimed_ids]
        if overlap:
            raise ValueError(
                f"Correction operation {operation_index} reuses cue ID(s) "
                f"already handled by another operation: {overlap}."
            )
        sequential = ids == sorted(ids) and all(
            right == left + 1 for left, right in zip(ids, ids[1:])
        )
        valid_shape = (
            (action == "edit" and len(ids) == 1 and len(texts) == 1)
            or (action == "delete" and sequential and not texts)
            or (action == "merge" and len(ids) >= 2 and sequential and bool(texts))
            or (action == "split" and len(ids) == 1 and len(texts) >= 2)
        )
        if not valid_shape:
            raise ValueError(
                f"Correction operation {operation_index} has an invalid "
                f"{action} ids/texts shape."
            )
        if speakers and (action == "delete" or len(speakers) != len(texts)):
            raise ValueError(
                f"Correction operation {operation_index} must provide exactly one "
                "speaker for every replacement text, or omit speakers."
            )
        if speakers and known_speakers is not None:
            known_by_casefold = {
                speaker.casefold(): speaker for speaker in known_speakers
            }
            unknown = [
                speaker
                for speaker in speakers
                if speaker.casefold() not in known_by_casefold
            ]
            if unknown:
                raise ValueError(
                    f"Correction operation {operation_index} returned unknown speaker "
                    f"ID(s): {unknown}."
                )
        if action == "delete" and no_remove_subtitles:
            raise ValueError(
                f"Correction operation {operation_index} deletes subtitles even "
                "though deletion is disabled."
            )
        claimed_ids.update(ids)


def _split_timing(
    start: float,
    end: float,
    texts: list[str],
    *,
    speaker: str = "",
) -> list[dict[str, Any]]:
    if not texts:
        return []

    total_chars = sum(len(text) for text in texts)
    duration = max(0.1, end - start)
    current_start = start
    subtitles: list[dict[str, Any]] = []
    for index, text in enumerate(texts):
        ratio = len(text) / total_chars if total_chars > 0 else 1.0 / len(texts)
        part_duration = duration * ratio
        current_end = end if index == len(texts) - 1 else current_start + part_duration
        subtitle = {"start": current_start, "end": current_end, "text": text}
        if speaker:
            subtitle["speaker"] = speaker
        subtitles.append(subtitle)
        current_start = current_end
    return subtitles


def _normalize_replacement_text(value: Any) -> str:
    """Keep editorial output as cue text; visual layout is finalized later."""
    normalized = " ".join(str(value or "").split()).strip()
    return split_speaker_label(normalized)[1]


def apply_correction_operations(
    block: list[dict[str, Any]],
    operations: list[dict[str, Any]],
    *,
    no_remove_subtitles: bool = False,
    known_speakers: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Apply Subdub-style correction operations to a local subtitle block."""
    block_by_source_id = {int(subtitle["index"]): subtitle.copy() for subtitle in block}
    processed_ids: set[int] = set()
    new_subtitles_by_primary_id: dict[int, list[dict[str, Any]]] = {}

    for operation in operations:
        action = operation.get("action")
        ids = list(
            dict.fromkeys(
                item
                for item in _operation_source_ids(block, operation)
                if item in block_by_source_id
            )
        )
        texts = [
            text
            for value in operation.get("texts", [])
            if (text := _normalize_replacement_text(value))
        ]
        requested_speakers = [
            str(speaker or "").strip() for speaker in operation.get("speakers", [])
        ]
        if not ids or any(item in processed_ids for item in ids):
            continue

        sequential = ids == sorted(ids) and all(
            right == left + 1 for left, right in zip(ids, ids[1:])
        )
        valid_shape = (
            (action == "edit" and len(ids) == 1 and len(texts) == 1)
            or (action == "delete" and sequential and not texts)
            or (action == "merge" and len(ids) >= 2 and sequential and bool(texts))
            or (action == "split" and len(ids) == 1 and len(texts) >= 2)
        )
        if not valid_shape:
            continue

        valid_subtitles = [block_by_source_id[item] for item in ids]
        if not valid_subtitles:
            continue
        if action == "delete" and no_remove_subtitles:
            continue
        speakers = [
            str(subtitle.get("speaker") or "").strip() for subtitle in valid_subtitles
        ]
        if (
            action == "merge"
            and len({speaker.casefold() for speaker in speakers if speaker}) > 1
            and not requested_speakers
        ):
            logger.warning(
                "Ignoring correction merge across a speaker boundary: %s", ids
            )
            continue

        processed_ids.update(ids)
        primary_id = ids[0]
        if action == "delete":
            new_subtitles_by_primary_id[primary_id] = []
            continue

        new_start = min(float(subtitle["start"]) for subtitle in valid_subtitles)
        new_end = max(float(subtitle["end"]) for subtitle in valid_subtitles)
        if not texts:
            texts = [" ".join(str(subtitle["text"]) for subtitle in valid_subtitles)]

        default_speaker = speakers[0] if speakers else ""
        known_by_casefold = {
            speaker.casefold(): speaker for speaker in (known_speakers or set())
        }
        output_speakers = (
            [
                known_by_casefold.get(speaker.casefold(), speaker)
                for speaker in requested_speakers
            ]
            if requested_speakers
            else [default_speaker for _ in texts]
        )
        replacement_subtitles: list[dict[str, Any]] = []
        split_parts = _split_timing(new_start, new_end, texts)
        for part, speaker in zip(split_parts, output_speakers, strict=True):
            if speaker:
                part["speaker"] = speaker
            replacement_subtitles.append(part)
        new_subtitles_by_primary_id[primary_id] = replacement_subtitles

    corrected: list[dict[str, Any]] = []
    for subtitle in block:
        source_id = int(subtitle["index"])
        if source_id not in processed_ids:
            corrected.append(block_by_source_id[source_id])
            continue
        corrected.extend(new_subtitles_by_primary_id.get(source_id, []))

    return corrected


def build_correction_task_instructions(
    *,
    subtitle_count: int,
    correction_instructions: str = "",
    no_remove_subtitles: bool = False,
    timing_context_mode: str | None = None,
    include_timing_context: bool | None = None,
    substantial_gap_ms: int = 2000,
    known_speakers: set[str] | None = None,
    dispatch_result: bool = False,
    structured_context: bool = False,
) -> str:
    """Build correction guidance without embedding source cue content."""

    mode = normalize_timing_context_mode(
        timing_context_mode,
        legacy_enabled=include_timing_context,
        default="none",
    )
    prompt_template = CORRECTION_PROMPT_TEMPLATE
    if no_remove_subtitles:
        prompt_template = prompt_template.replace(
            '   - "delete": one or more sequential `cue_id` values that contain no meaningful speech, with an empty `texts` array.',
            '   - "delete": do not use this action; every input cue must be preserved.',
        )

    base_prompt = prompt_template.format(
        correction_instructions=correction_instructions
        or "No additional instructions provided.",
        subtitle_count=int(subtitle_count),
        response_shape=(
            '{"kind":"correction","operations":['
            '{"action":"edit|delete|merge|split","cue_ids":[1],'
            '"texts":["corrected text"],"speakers":["SPEAKER_00"]}]}'
            if dispatch_result
            else '{"operations":[{"action":"edit|delete|merge|split",'
            '"cue_ids":[1],"texts":["corrected text"],'
            '"speakers":["SPEAKER_00"]}]}'
        ),
        known_speakers_policy=(
            "Known speaker IDs are provided once in `task.known_speakers`; "
            "never invent another speaker ID. An empty list means speaker "
            "reassignment is unavailable."
            if structured_context
            else (
                "Known speaker IDs for this document: "
                + (
                    ", ".join(sorted(known_speakers, key=str.casefold))
                    if known_speakers
                    else "none (do not add speaker assignments)"
                )
                + ". Never invent another speaker ID."
            )
        ),
        cue_context_policy=(
            "The optional `speaker` field is non-spoken evidence. Speaker IDs "
            "may change at ASR chunk seams: never copy it into replacement "
            "text or assume that a changed ID always means a new person."
            if mode == "none"
            else (
                "The optional `speaker` field and "
                "`timing.overlap_with_previous_ms` are non-spoken evidence. "
                "Speaker IDs may change at ASR chunk seams: never copy context "
                "into replacement text or assume that a changed ID always "
                "means a new person."
                if mode == "overlap_only"
                else "The optional `speaker` and `timing` objects are non-spoken "
                "evidence. Speaker IDs may change at ASR chunk seams: never "
                "copy context into replacement text or assume that a changed "
                "ID always means a new person."
            )
        ),
    )
    if mode == "full":
        gap_reference = (
            "`task.substantial_gap_ms`"
            if structured_context
            else f"{max(0, int(substantial_gap_ms))} ms"
        )
        base_prompt += (
            "\n\nTiming policy:\n"
            "- Each cue's optional `timing` object contains `start_ms`, `end_ms`, "
            "and its gap from or overlap with the preceding cue.\n"
            f"- A gap of at least {gap_reference} is a "
            "substantial audible pause: normally preserve a cue boundary there "
            "even when the text is semantically related.\n"
            "- A shorter gap is not by itself a reason to split a coherent "
            "same-speaker utterance. Use semantics, punctuation, and the timing "
            "evidence together."
        )
    elif mode == "overlap_only":
        base_prompt += (
            "\n\nTiming policy:\n"
            "- A cue has a `timing` object only when it overlaps the preceding "
            "cue. Treat that overlap as non-spoken evidence of simultaneous "
            "speech or a possible duplicated ASR seam."
        )
    return base_prompt


def build_correction_prompt(
    block: list[dict[str, Any]],
    *,
    correction_instructions: str = "",
    previous_response: str = "",
    max_line_length: int = DEFAULT_MAX_LINE_LENGTH,
    no_remove_subtitles: bool = False,
    timing_context_mode: str | None = None,
    include_timing_context: bool | None = None,
    substantial_gap_ms: int = 2000,
    known_speakers: set[str] | None = None,
    next_block: list[dict[str, Any]] | None = None,
    context_after: int = 2,
) -> str:
    """Build a correction prompt for one subtitle block."""

    mode = normalize_timing_context_mode(
        timing_context_mode,
        legacy_enabled=include_timing_context,
        default="none",
    )
    base_prompt = build_correction_task_instructions(
        subtitle_count=len(block),
        correction_instructions=correction_instructions,
        no_remove_subtitles=no_remove_subtitles,
        timing_context_mode=mode,
        substantial_gap_ms=substantial_gap_ms,
        known_speakers=known_speakers,
    )
    # Retained in the signature for callers using the old helper contract.
    # Layout limits intentionally do not belong in the LLM task.
    _ = max_line_length
    subtitles = json.dumps(
        [
            subtitle_task_cue(
                {**subtitle, "text": _normalize_replacement_text(subtitle.get("text"))},
                timing_context_mode=mode,
            )
            for subtitle in block
        ],
        ensure_ascii=False,
    )

    context_parts: list[str] = []
    if previous_response:
        context_parts.append(
            CONTEXT_PROMPT_TEMPLATE.format(
                context_previous_cues=previous_response
            )
        )
    if next_block and context_after > 0:
        following = [
            {
                "text": _normalize_replacement_text(item.get("text")),
                **(
                    {"speaker": str(item.get("speaker") or "").strip()}
                    if str(item.get("speaker") or "").strip()
                    else {}
                ),
            }
            for item in next_block[:context_after]
        ]
        if following:
            context_parts.append(
                "Following source cues for continuity only; do not return "
                "operations for them:\n"
                + json.dumps(following, ensure_ascii=False)
            )
    context = "\n\n".join(context_parts)
    if context:
        return f"{base_prompt}\n{context}\n\nThe subtitles:\n{subtitles}"
    return f"{base_prompt}\n\nThe subtitles:\n{subtitles}"


def _coerce_completion_content_and_cost(result: Any) -> tuple[str, float, str]:
    if isinstance(result, str):
        return result, 0.0, ""
    content = str(getattr(result, "content", "") or "")
    cost = getattr(result, "cost", 0.0)
    try:
        normalized_cost = float(cost or 0.0)
    except (TypeError, ValueError):
        normalized_cost = 0.0
    return content, normalized_cost, str(getattr(result, "cost_source", "") or "")


def _merge_completion_usage(totals: dict[str, Any], result: Any) -> None:
    raw = getattr(result, "usage", {})
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump(mode="json")
    normalized = llm_handler.normalize_usage_tokens(
        raw if isinstance(raw, dict) else {}
    )
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_prompt_tokens",
        "uncached_prompt_tokens",
    ):
        totals[key] = int(totals.get(key) or 0) + int(normalized.get(key) or 0)


def correction_unit_key(block: list[dict[str, Any]]) -> str:
    """Return a stable, database-safe key for one correction block."""
    indices = [int(subtitle["index"]) for subtitle in block]
    digest = hashlib.sha256(
        json.dumps(indices, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    return f"correction:{indices[0]}-{indices[-1]}:{digest}"


def _restore_correction_unit(
    block: list[dict[str, Any]],
    completed_units: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], str, float, int, list[str], dict[str, Any]] | None:
    if not completed_units:
        return None
    key = correction_unit_key(block)
    raw = completed_units.get(key)
    if raw is None:
        return None
    expected_indices = [int(subtitle["index"]) for subtitle in block]
    if [int(value) for value in raw.get("original_indices", [])] != expected_indices:
        raise ValueError(
            f"Correction checkpoint {key} does not match its source block."
        )
    raw_subtitles = raw.get("corrected_subtitles")
    if not isinstance(raw_subtitles, list):
        raise ValueError(f"Correction checkpoint {key} has no corrected subtitle list.")
    corrected: list[dict[str, Any]] = []
    for item in raw_subtitles:
        if not isinstance(item, Mapping):
            raise ValueError(
                f"Correction checkpoint {key} contains an invalid subtitle."
            )
        text = _normalize_replacement_text(item.get("text"))
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                f"Correction checkpoint {key} contains invalid timing."
            ) from error
        if not text or end <= start:
            raise ValueError(
                f"Correction checkpoint {key} contains an invalid subtitle."
            )
        subtitle = {"start": start, "end": end, "text": text}
        speaker = str(item.get("speaker") or "").strip()
        if speaker:
            subtitle["speaker"] = speaker
        corrected.append(subtitle)
    context = str(raw.get("context") or "")
    if not context:
        context = json.dumps(
            [
                _normalize_replacement_text(item.get("text"))
                for item in corrected[-CORRECTION_CONTEXT_CUES:]
            ],
            ensure_ascii=False,
        )
    try:
        cost = float(raw.get("cost") or 0.0)
        response_count = int(raw.get("response_count") or 0)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Correction checkpoint {key} has invalid metrics.") from error
    cost_sources = [
        str(source) for source in raw.get("cost_sources", []) if str(source or "")
    ]
    raw_usage = raw.get("usage")
    usage = dict(raw_usage) if isinstance(raw_usage, Mapping) else {}
    return corrected, context, cost, response_count, cost_sources, usage


def correct_srt_content(
    srt_content: str,
    settings: dict[str, Any],
    correction_instructions: str = "",
    *,
    completion_func: Callable[..., Any] | None = None,
    cancel_event: Any | None = None,
    speaker_by_subtitle: Mapping[int, str] | None = None,
    progress_callback: ProgressCallback | None = None,
    completed_units: Mapping[str, Mapping[str, Any]] | None = None,
    on_unit_completed: UnitCompletedCallback | None = None,
) -> CorrectionResult:
    """Correct SRT content with Pandrator's LLM provider layer."""
    char_limit = _coerce_int(
        settings.get("char_limit", settings.get("llm_char")),
        DEFAULT_LLM_CHAR_LIMIT,
    )
    max_subtitles_per_call = max(
        1,
        _coerce_int(
            settings.get(
                "max_segments_per_batch",
                settings.get("max_subtitles_per_call"),
            ),
            40,
        ),
    )
    max_line_length = _coerce_int(
        settings.get("max_line_length"), DEFAULT_MAX_LINE_LENGTH
    )
    source_language = str(
        settings.get("original_language")
        or settings.get("stt_language")
        or settings.get("whisper_language")
        or "English"
    )
    use_context = bool(settings.get("context", True))
    context_before = max(
        0,
        min(20, _coerce_int(settings.get("context_before"), CORRECTION_CONTEXT_CUES)),
    )
    context_after = max(0, min(20, _coerce_int(settings.get("context_after"), 2)))
    no_remove_subtitles = bool(settings.get("no_remove_subtitles", False))
    timing_context_mode = timing_context_mode_from_settings(settings)
    substantial_gap_ms = max(
        0,
        _coerce_int(
            settings.get(
                "substantial_gap_ms",
                settings.get("timing_context_gap_ms"),
            ),
            2000,
        ),
    )
    workers = max(1, min(16, _coerce_int(settings.get("llm_concurrent_calls"), 1)))

    blocks = create_translation_blocks(
        srt_content,
        char_limit,
        source_language,
        max_subtitles_per_block=max_subtitles_per_call,
        speaker_by_subtitle=speaker_by_subtitle,
    )
    if not blocks:
        _report_progress(progress_callback, 1.0, "No subtitles require correction")
        return CorrectionResult(srt_content="", cost=0.0, response_count=0)

    total_subtitles = sum(len(block) for block in blocks)
    completed_subtitles = 0
    resolved = resolve_dubbing_llm_settings(settings, stage="correction")
    completion = completion_func or llm_handler.chat_completion_with_metadata
    corrected_subtitles: list[dict[str, Any]] = []
    total_cost = 0.0
    response_count = 0
    cost_sources: list[str] = []
    usage: dict[str, Any] = {}
    progress_lock = Lock()
    checkpoint_lock = Lock()
    known_speakers = {
        str(subtitle.get("speaker") or "").strip()
        for block in blocks
        for subtitle in block
        if str(subtitle.get("speaker") or "").strip()
    }

    def correct_block(
        block_number: int,
        block: list[dict[str, Any]],
        previous_context: str,
        following_context: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], str, float, int, list[str], dict[str, Any]]:
        nonlocal completed_subtitles
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("LLM correction was canceled.")
        restored = _restore_correction_unit(block, completed_units)
        if restored is not None:
            with progress_lock:
                completed_subtitles += len(block)
                _report_progress(
                    progress_callback,
                    completed_subtitles / total_subtitles,
                    (
                        f"Restored {completed_subtitles} of {total_subtitles} "
                        "corrected subtitles"
                    ),
                )
            return restored
        prompt = build_correction_prompt(
            block,
            correction_instructions=correction_instructions,
            previous_response=previous_context if use_context else "",
            max_line_length=max_line_length,
            no_remove_subtitles=no_remove_subtitles,
            timing_context_mode=timing_context_mode,
            substantial_gap_ms=substantial_gap_ms,
            known_speakers=known_speakers,
            next_block=following_context if use_context else None,
            context_after=context_after,
        )
        last_protocol_error: ValueError | None = None
        block_cost = 0.0
        block_response_count = 0
        block_cost_sources: list[str] = []
        block_usage: dict[str, Any] = {}
        for attempt in range(1, MAX_CORRECTION_ATTEMPTS + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("LLM correction was canceled.")
            with progress_lock:
                _report_progress(
                    progress_callback,
                    completed_subtitles / total_subtitles,
                    (
                        f"Correcting subtitle block {block_number} of {len(blocks)}"
                        + (
                            f" — validation attempt {attempt} of {MAX_CORRECTION_ATTEMPTS}"
                            if attempt > 1
                            else ""
                        )
                    ),
                )
            messages = [
                {"role": "system", "content": CORRECTION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
            if last_protocol_error is not None:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was rejected because "
                            f"{last_protocol_error}. Return a complete replacement "
                            'object with an explicit "operations" array. Use only cue '
                            "IDs present in this batch: "
                            + ", ".join(str(item["index"]) for item in block)
                            + ". Do not explain the correction."
                        ),
                    }
                )
            completion_kwargs: dict[str, Any] = {
                "messages": messages,
                "model_name": resolved.model_name,
                "llm_settings": resolved.llm_settings,
            }
            if completion_func is None:
                completion_kwargs["cancel_event"] = cancel_event
            result = completion(**completion_kwargs)
            content, cost, cost_source = _coerce_completion_content_and_cost(result)
            _merge_completion_usage(block_usage, result)
            block_cost += cost
            if cost_source and cost_source not in block_cost_sources:
                block_cost_sources.append(cost_source)
            block_response_count += 1
            try:
                if not content:
                    raise ValueError("LLM correction returned an empty response.")

                operations = parse_correction_operations(content)
                validate_correction_operations(
                    block,
                    operations,
                    no_remove_subtitles=no_remove_subtitles,
                    known_speakers=known_speakers,
                )
                corrected_block = apply_correction_operations(
                    block,
                    operations,
                    no_remove_subtitles=no_remove_subtitles,
                    known_speakers=known_speakers,
                )
                context_items = [
                    cue
                    for item in corrected_block[-context_before:]
                    if (
                        cue := subtitle_boundary_cue(
                            {
                                **item,
                                "text": _normalize_replacement_text(
                                    item.get("text")
                                ),
                            }
                        )
                    )
                    is not None
                ]
                next_context = (
                    json.dumps(context_items, ensure_ascii=False)
                    if context_before
                    else ""
                )
                if on_unit_completed is not None:
                    payload = {
                        "version": 1,
                        "kind": "correction",
                        "original_indices": [
                            int(subtitle["index"]) for subtitle in block
                        ],
                        "corrected_subtitles": corrected_block,
                        "context": next_context,
                        "cost": block_cost,
                        "response_count": block_response_count,
                        "cost_sources": block_cost_sources,
                        "usage": block_usage,
                    }
                    with checkpoint_lock:
                        try:
                            on_unit_completed(correction_unit_key(block), payload)
                        except Exception as error:
                            raise RuntimeError(
                                "Could not persist the completed correction unit."
                            ) from error
                with progress_lock:
                    completed_subtitles += len(block)
                    _report_progress(
                        progress_callback,
                        completed_subtitles / total_subtitles,
                        (
                            f"Corrected {completed_subtitles} of "
                            f"{total_subtitles} subtitles"
                        ),
                    )
                return (
                    corrected_block,
                    next_context,
                    block_cost,
                    block_response_count,
                    block_cost_sources,
                    block_usage,
                )
            except ValueError as error:
                last_protocol_error = error
                if attempt == MAX_CORRECTION_ATTEMPTS:
                    raise ValueError(
                        f"Failed to correct subtitle block {block_number} after "
                        f"{MAX_CORRECTION_ATTEMPTS} attempts: {error}"
                    ) from error
                logger.warning(
                    "Correction attempt %d/%d failed for subtitle block %d: %s",
                    attempt,
                    MAX_CORRECTION_ATTEMPTS,
                    block_number,
                    error,
                )

        raise AssertionError("Correction retry loop exited without a result.")

    def append_metrics(
        cost: float,
        count: int,
        sources: list[str],
        block_usage: dict[str, Any],
    ) -> None:
        nonlocal total_cost, response_count
        total_cost += cost
        response_count += count
        for source in sources:
            if source and source not in cost_sources:
                cost_sources.append(source)
        for key, value in block_usage.items():
            usage[key] = int(usage.get(key) or 0) + int(value or 0)

    if workers == 1 or len(blocks) == 1:
        previous_context = ""
        for block_number, block in enumerate(blocks, start=1):
            corrected, previous_context, cost, count, sources, block_usage = (
                correct_block(
                    block_number,
                    block,
                    previous_context,
                    blocks[block_number] if block_number < len(blocks) else None,
                )
            )
            corrected_subtitles.extend(corrected)
            append_metrics(cost, count, sources, block_usage)
    else:
        ordered: dict[
            int, tuple[list[dict[str, Any]], str, float, int, list[str], dict[str, Any]]
        ] = {}
        executor = ThreadPoolExecutor(max_workers=min(workers, len(blocks)))
        futures = {
            executor.submit(
                correct_block,
                block_number,
                block,
                "",
                blocks[block_number] if block_number < len(blocks) else None,
            ): block_number
            for block_number, block in enumerate(blocks, start=1)
        }
        try:
            for future in as_completed(futures):
                ordered[futures[future]] = future.result()
        except BaseException:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)
        for block_number in range(1, len(blocks) + 1):
            corrected, _context, cost, count, sources, block_usage = ordered[
                block_number
            ]
            corrected_subtitles.extend(corrected)
            append_metrics(cost, count, sources, block_usage)

    segments = [
        SubtitleSegment(
            index=index,
            start_ms=max(0, int(round(float(subtitle["start"]) * 1000))),
            end_ms=max(1, int(round(float(subtitle["end"]) * 1000))),
            text=str(subtitle.get("text") or "").strip(),
            speaker=str(subtitle.get("speaker") or "").strip(),
        )
        for index, subtitle in enumerate(corrected_subtitles, start=1)
        if str(subtitle.get("text") or "").strip()
    ]
    return CorrectionResult(
        srt_content=compose_srt(segments),
        cost=total_cost,
        response_count=response_count,
        cost_sources=tuple(cost_sources),
        usage=usage,
        speaker_by_subtitle={
            segment.index: segment.speaker for segment in segments if segment.speaker
        },
    )


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def correct_srt_file_with_result(
    session_dir: str | os.PathLike[str],
    srt_file: str | os.PathLike[str],
    settings: dict[str, Any],
    correction_instructions: str = "",
    *,
    completion_func: Callable[..., Any] | None = None,
    cancel_event: Any | None = None,
    speaker_by_subtitle: Mapping[int, str] | None = None,
    progress_callback: ProgressCallback | None = None,
    completed_units: Mapping[str, Mapping[str, Any]] | None = None,
    on_unit_completed: UnitCompletedCallback | None = None,
) -> CorrectionResult:
    """Correct an SRT file and return the corrected content plus file path."""
    srt_path = Path(srt_file)
    with srt_path.open("r", encoding="utf-8-sig") as handle:
        srt_content = handle.read()

    result = correct_srt_content(
        srt_content,
        settings,
        correction_instructions=correction_instructions,
        completion_func=completion_func,
        cancel_event=cancel_event,
        speaker_by_subtitle=speaker_by_subtitle,
        progress_callback=progress_callback,
        completed_units=completed_units,
        on_unit_completed=on_unit_completed,
    )
    output_path = Path(session_dir) / f"{srt_path.stem}_corrected.srt"
    _write_text_atomic(output_path, result.srt_content)
    file_result = CorrectionResult(
        srt_content=result.srt_content,
        cost=result.cost,
        response_count=result.response_count,
        output_path=str(output_path),
        cost_sources=result.cost_sources,
        usage=result.usage,
        speaker_by_subtitle=result.speaker_by_subtitle,
    )
    logger.info(
        "Corrected subtitles written to %s (%d LLM response(s), cost %.6f).",
        output_path,
        file_result.response_count,
        file_result.cost,
    )
    return file_result


def correct_srt_file(
    session_dir: str | os.PathLike[str],
    srt_file: str | os.PathLike[str],
    settings: dict[str, Any],
    correction_instructions: str = "",
    *,
    completion_func: Callable[..., Any] | None = None,
    speaker_by_subtitle: Mapping[int, str] | None = None,
    progress_callback: ProgressCallback | None = None,
    completed_units: Mapping[str, Mapping[str, Any]] | None = None,
    on_unit_completed: UnitCompletedCallback | None = None,
) -> str:
    """Correct an SRT file and return the corrected file path."""
    return correct_srt_file_with_result(
        session_dir=session_dir,
        srt_file=srt_file,
        settings=settings,
        correction_instructions=correction_instructions,
        completion_func=completion_func,
        speaker_by_subtitle=speaker_by_subtitle,
        progress_callback=progress_callback,
        completed_units=completed_units,
        on_unit_completed=on_unit_completed,
    ).output_path
