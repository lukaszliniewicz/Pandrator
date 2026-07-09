"""LLM-backed subtitle correction using Pandrator provider settings."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
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
CORRECTION_SYSTEM_PROMPT = (
    "You are an excellent text and subtitle editor. You pay great attention to detail, "
    "including punctuation and logic in text, and analyse the instructions you are given "
    "very carefully. You return only the requested correction operations in the requested "
    "format, without comments, acknowledgments, remarks, or questions."
)

CORRECTION_PROMPT_TEMPLATE = """
Your task is to review an array of {subtitle_count} subtitles and output a list of operations to fix them.
You are an expert subtitle editor.

Instructions:
1. ONLY output operations for subtitles that require changes. If a subtitle is perfectly fine, do not include it in your response.
2. Fix punctuation and capitalization such that they are coherent and logical.
3. Correct spelling and obvious transcription errors.
4. Remove filler words (e.g., "um", "uh") and obvious repetitions.
5. You can perform the following actions:
   - "edit": Fix text in a single subtitle. Provide its ID in `ids` and the new text in `texts`.
   - "delete": Remove a subtitle (e.g., if it's only filler words). Provide the ID in `ids` and an empty array for `texts`.
   - "merge": Combine multiple sequential subtitles. Provide their IDs in `ids`. If you want a single combined subtitle, provide EXACTLY ONE string in `texts`. If you want to redistribute the combined text into new subtitle chunks (e.g., merge and then split differently), provide multiple strings in `texts`.
   - "split": Split a single subtitle into multiple parts (e.g., if it contains two distinct sentences or is too long). Provide the ID in `ids` and the new split strings in `texts`.
6. Ensure that any text you provide in `texts` (for edit, merge, or split) is fully corrected for spelling, punctuation, and grammar.
7. Subtitle formatting: Ensure subtitles are not too long (max {max_line_length} characters per line, max 2 lines per subtitle). Use a newline character `\n` to explicitly split lines within a single subtitle string.
8. If previous conversational context is provided, DO NOT include it in your output. ONLY operate on the JSON array of subtitles provided below (IDs 1 to {subtitle_count}).

Additional context and instructions specific to your particular batch, if any:
{correction_instructions}
""".strip()

CONTEXT_PROMPT_TEMPLATE = """
For additional context, this is the final version of the previous subtitle block processed by you before:
{context_previous_response}
""".strip()

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class CorrectionResult:
    srt_content: str
    cost: float = 0.0
    response_count: int = 0
    output_path: str = ""


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
        ids = [item for item in operation.get("ids", []) if item in block_by_local_id]
        texts = [str(text).strip() for text in operation.get("texts", []) if str(text).strip()]
        if not ids or any(item in processed_ids for item in ids):
            continue

        valid_subtitles = [block_by_local_id[item] for item in ids]
        if not valid_subtitles:
            continue

        processed_ids.update(ids)
        primary_id = ids[0]
        if action == "delete":
            new_subtitles_by_primary_id[primary_id] = [valid_subtitles[0]] if no_remove_subtitles else []
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
            '- "delete": Remove a subtitle (e.g., if it\'s only filler words). Provide its ID in `ids` and an empty array for `texts`.',
            '- "delete": DO NOT USE THIS ACTION. You MUST NOT remove any subtitles.',
        )

    base_prompt = prompt_template.format(
        correction_instructions=correction_instructions or "No additional instructions provided.",
        subtitle_count=len(block),
        max_line_length=max_line_length,
    )
    subtitles = json.dumps(
        [
            {
                "id": index + 1,
                "char_count": len(str(subtitle.get("text") or "")),
                "text": subtitle.get("text") or "",
            }
            for index, subtitle in enumerate(block)
        ],
        ensure_ascii=False,
    )

    if previous_response:
        context_prompt = CONTEXT_PROMPT_TEMPLATE.format(context_previous_response=previous_response)
        return f"{base_prompt}\n{context_prompt}\n\nThe subtitles:\n{subtitles}"
    return f"{base_prompt}\n\nThe subtitles:\n{subtitles}"


def _coerce_completion_content_and_cost(result: Any) -> tuple[str, float]:
    if isinstance(result, str):
        return result, 0.0
    content = str(getattr(result, "content", "") or "")
    cost = getattr(result, "cost", 0.0)
    try:
        normalized_cost = float(cost or 0.0)
    except (TypeError, ValueError):
        normalized_cost = 0.0
    return content, normalized_cost


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
    previous_response = ""
    corrected_subtitles: list[dict[str, Any]] = []
    total_cost = 0.0
    response_count = 0

    for block_number, block in enumerate(blocks, start=1):
        prompt = build_correction_prompt(
            block,
            correction_instructions=correction_instructions,
            previous_response=previous_response if use_context else "",
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
                content, cost = _coerce_completion_content_and_cost(result)
                total_cost += cost
                response_count += 1
                if not content:
                    raise ValueError("LLM correction returned an empty response.")

                operations = parse_correction_operations(content)
                corrected_subtitles.extend(
                    apply_correction_operations(
                        block,
                        operations,
                        no_remove_subtitles=no_remove_subtitles,
                    )
                )
                previous_response = content
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
