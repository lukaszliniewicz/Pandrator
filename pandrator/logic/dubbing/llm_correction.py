"""LLM-backed subtitle correction using Pandrator provider settings."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import llm_handler
from .llm_config import DubbingLLMSettings, resolve_dubbing_llm_settings as _resolve_dubbing_llm_settings
from .models import SubtitleSegment
from .srt_utils import compose_srt, create_translation_blocks

logger = logging.getLogger(__name__)

DEFAULT_LLM_CHAR_LIMIT = 6000
DEFAULT_MAX_LINE_LENGTH = 42
MAX_CORRECTION_ATTEMPTS = 3
CORRECTION_CONTEXT_CUES = 8
CORRECTION_SYSTEM_PROMPT = (
    "You are an expert subtitle transcript editor. Correct the supplied source-language "
    "cues accurately and conservatively. Return only valid JSON in the requested operation "
    "format, without comments, acknowledgments, markdown, or questions."
)

CORRECTION_PROMPT_TEMPLATE = """
Review the array of {subtitle_count} subtitle cues below and return this JSON shape:
{{"operations":[{{"action":"edit|delete|merge|split","ids":[1],"texts":["corrected text"]}}]}}

Instructions:
1. Use your editorial judgment to fix punctuation, capitalization, spelling, and clear transcription errors. Remove isolated filler and accidental repetition when appropriate.
2. Preserve the speaker's meaning, register, names, terminology, and speaker labels. Do not paraphrase text that is already correct.
3. Return operations only for cues that need a change; return an empty `operations` array when no changes are needed.
4. Available actions:
   - "edit": one cue ID and exactly one corrected text.
   - "delete": one or more sequential cue IDs that contain no meaningful speech, with an empty `texts` array.
   - "merge": two or more sequential cue IDs whose boundary breaks one thought, with one or more corrected replacement texts.
   - "split": one cue ID and two or more replacement texts, only when semantic correction genuinely requires separate cues.
5. Cue timing, reading speed, visual wrapping, and line layout are handled by Pandrator after editing. Do not insert line breaks or split/merge merely to change visual layout.
6. Every replacement must be complete, corrected plain text. Do not include IDs that are only context.
7. If prior corrected context is provided, use it only for continuity. Operate only on the current array (IDs 1 to {subtitle_count}).

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
        operations = payload.get("operations", [])
    elif isinstance(payload, list):
        operations = payload
    else:
        raise ValueError("Correction response must be a JSON object or list.")

    if not isinstance(operations, list):
        raise ValueError("Correction response 'operations' must be a list.")

    normalized: list[dict[str, Any]] = []
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        action = str(operation.get("action") or "").strip().lower()
        ids = operation.get("ids", [])
        texts = operation.get("texts", [])
        if action not in {"edit", "delete", "merge", "split"}:
            continue
        if not isinstance(ids, list) or not ids:
            continue
        normalized_ids: list[int] = []
        for item in ids:
            try:
                normalized_ids.append(int(item))
            except (TypeError, ValueError):
                pass
        if not normalized_ids:
            continue
        if not isinstance(texts, list):
            texts = [str(texts)]
        normalized.append(
            {
                "action": action,
                "ids": normalized_ids,
                "texts": [str(text) for text in texts],
            }
        )
    return normalized


def _split_timing(start: float, end: float, texts: list[str]) -> list[dict[str, Any]]:
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
        subtitles.append({"start": current_start, "end": current_end, "text": text})
        current_start = current_end
    return subtitles


def _normalize_replacement_text(value: Any) -> str:
    """Keep editorial output as cue text; visual layout is finalized later."""
    return " ".join(str(value or "").split()).strip()


def apply_correction_operations(
    block: list[dict[str, Any]],
    operations: list[dict[str, Any]],
    *,
    no_remove_subtitles: bool = False,
) -> list[dict[str, Any]]:
    """Apply Subdub-style correction operations to a local subtitle block."""
    block_by_local_id = {index + 1: subtitle.copy() for index, subtitle in enumerate(block)}
    processed_ids: set[int] = set()
    new_subtitles_by_primary_id: dict[int, list[dict[str, Any]]] = {}

    for operation in operations:
        action = operation.get("action")
        ids = list(dict.fromkeys(item for item in operation.get("ids", []) if item in block_by_local_id))
        texts = [text for value in operation.get("texts", []) if (text := _normalize_replacement_text(value))]
        if not ids or any(item in processed_ids for item in ids):
            continue

        sequential = ids == sorted(ids) and all(right == left + 1 for left, right in zip(ids, ids[1:]))
        valid_shape = (
            (action == "edit" and len(ids) == 1 and len(texts) == 1)
            or (action == "delete" and sequential and not texts)
            or (action == "merge" and len(ids) >= 2 and sequential and bool(texts))
            or (action == "split" and len(ids) == 1 and len(texts) >= 2)
        )
        if not valid_shape:
            continue

        valid_subtitles = [block_by_local_id[item] for item in ids]
        if not valid_subtitles:
            continue
        if action == "delete" and no_remove_subtitles:
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

        new_subtitles_by_primary_id[primary_id] = _split_timing(new_start, new_end, texts)

    corrected: list[dict[str, Any]] = []
    for local_id in range(1, len(block) + 1):
        if local_id not in processed_ids:
            corrected.append(block_by_local_id[local_id])
            continue
        corrected.extend(new_subtitles_by_primary_id.get(local_id, []))

    return corrected


def build_correction_prompt(
    block: list[dict[str, Any]],
    *,
    correction_instructions: str = "",
    previous_response: str = "",
    max_line_length: int = DEFAULT_MAX_LINE_LENGTH,
    no_remove_subtitles: bool = False,
) -> str:
    """Build a correction prompt for one subtitle block."""
    prompt_template = CORRECTION_PROMPT_TEMPLATE
    if no_remove_subtitles:
        prompt_template = prompt_template.replace(
            '   - "delete": one or more sequential cue IDs that contain no meaningful speech, with an empty `texts` array.',
            '   - "delete": do not use this action; every input cue must be preserved.',
        )

    base_prompt = prompt_template.format(
        correction_instructions=correction_instructions or "No additional instructions provided.",
        subtitle_count=len(block),
    )
    # Retained in the signature for callers using the old helper contract.
    # Layout limits intentionally do not belong in the LLM task.
    _ = max_line_length
    subtitles = json.dumps(
        [
            {
                "id": index + 1,
                "text": _normalize_replacement_text(subtitle.get("text")),
            }
            for index, subtitle in enumerate(block)
        ],
        ensure_ascii=False,
    )

    if previous_response:
        context_prompt = CONTEXT_PROMPT_TEMPLATE.format(context_previous_cues=previous_response)
        return f"{base_prompt}\n{context_prompt}\n\nThe subtitles:\n{subtitles}"
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
    normalized = llm_handler.normalize_usage_tokens(raw if isinstance(raw, dict) else {})
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_prompt_tokens", "uncached_prompt_tokens"):
        totals[key] = int(totals.get(key) or 0) + int(normalized.get(key) or 0)


def correct_srt_content(
    srt_content: str,
    settings: dict[str, Any],
    correction_instructions: str = "",
    *,
    completion_func: Callable[..., Any] | None = None,
) -> CorrectionResult:
    """Correct SRT content with Pandrator's LLM provider layer."""
    char_limit = _coerce_int(settings.get("llm_char"), DEFAULT_LLM_CHAR_LIMIT)
    max_line_length = _coerce_int(settings.get("max_line_length"), DEFAULT_MAX_LINE_LENGTH)
    source_language = str(
        settings.get("original_language")
        or settings.get("stt_language")
        or settings.get("whisper_language")
        or "English"
    )
    use_context = bool(settings.get("context", True))
    no_remove_subtitles = bool(settings.get("no_remove_subtitles", False))

    blocks = create_translation_blocks(srt_content, char_limit, source_language)
    if not blocks:
        return CorrectionResult(srt_content="", cost=0.0, response_count=0)

    resolved = resolve_dubbing_llm_settings(settings, stage="correction")
    completion = completion_func or llm_handler.chat_completion_with_metadata
    previous_context = ""
    corrected_subtitles: list[dict[str, Any]] = []
    total_cost = 0.0
    response_count = 0
    cost_sources: list[str] = []
    usage: dict[str, Any] = {}

    for block_number, block in enumerate(blocks, start=1):
        prompt = build_correction_prompt(
            block,
            correction_instructions=correction_instructions,
            previous_response=previous_context if use_context else "",
            max_line_length=max_line_length,
            no_remove_subtitles=no_remove_subtitles,
        )
        for attempt in range(1, MAX_CORRECTION_ATTEMPTS + 1):
            try:
                result = completion(
                    messages=[
                        {"role": "system", "content": CORRECTION_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    model_name=resolved.model_name,
                    llm_settings=resolved.llm_settings,
                )
                content, cost, cost_source = _coerce_completion_content_and_cost(result)
                _merge_completion_usage(usage, result)
                total_cost += cost
                if cost_source and cost_source not in cost_sources:
                    cost_sources.append(cost_source)
                response_count += 1
                if not content:
                    raise ValueError("LLM correction returned an empty response.")

                operations = parse_correction_operations(content)
                corrected_block = apply_correction_operations(
                    block,
                    operations,
                    no_remove_subtitles=no_remove_subtitles,
                )
                corrected_subtitles.extend(corrected_block)
                previous_context = json.dumps(
                    [_normalize_replacement_text(item.get("text")) for item in corrected_block[-CORRECTION_CONTEXT_CUES:]],
                    ensure_ascii=False,
                )
                break
            except Exception as error:
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

    segments = [
        SubtitleSegment(
            index=index,
            start_ms=max(0, int(round(float(subtitle["start"]) * 1000))),
            end_ms=max(1, int(round(float(subtitle["end"]) * 1000))),
            text=str(subtitle.get("text") or "").strip(),
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
    )


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
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
) -> str:
    """Correct an SRT file and return the corrected file path."""
    return correct_srt_file_with_result(
        session_dir=session_dir,
        srt_file=srt_file,
        settings=settings,
        correction_instructions=correction_instructions,
        completion_func=completion_func,
    ).output_path
