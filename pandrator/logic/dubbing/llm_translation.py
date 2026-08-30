"""LLM-backed subtitle translation using Pandrator provider settings."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from .. import llm_handler
from .llm_config import resolve_dubbing_llm_settings
from .llm_correction import DEFAULT_LLM_CHAR_LIMIT, extract_json_payload
from .models import SubtitleSegment
from .srt_utils import (
    compose_srt,
    create_translation_blocks,
    normalize_timing_context_mode,
    parse_srt,
    split_speaker_label,
    subtitle_boundary_cue,
    subtitle_task_cue,
    timing_context_mode_from_settings,
)

logger = logging.getLogger(__name__)

DEEPL_MAX_REQUEST_BYTES = 120 * 1024
DEFAULT_TRANSLATION_STRUCTURED_ATTEMPTS = 3
DEFAULT_TRANSLATION_RECOVERY_SPLIT_DEPTH = 3
ProgressCallback = Callable[[float, str | None], None]
UnitCompletedCallback = Callable[[str, dict[str, Any]], None]
DEEPL_LANGUAGE_MAP = {
    "english": "EN-US",
    "en": "EN-US",
    "en-us": "EN-US",
    "en-gb": "EN-GB",
    "german": "DE",
    "de": "DE",
    "french": "FR",
    "fr": "FR",
    "spanish": "ES",
    "es": "ES",
    "italian": "IT",
    "it": "IT",
    "dutch": "NL",
    "nl": "NL",
    "polish": "PL",
    "pl": "PL",
    "russian": "RU",
    "ru": "RU",
    "portuguese": "PT-PT",
    "pt": "PT-PT",
    "pt-pt": "PT-PT",
    "pt-br": "PT-BR",
    "chinese": "ZH",
    "zh": "ZH",
    "japanese": "JA",
    "ja": "JA",
    "bulgarian": "BG",
    "bg": "BG",
    "czech": "CS",
    "cs": "CS",
    "danish": "DA",
    "da": "DA",
    "greek": "EL",
    "el": "EL",
    "estonian": "ET",
    "et": "ET",
    "finnish": "FI",
    "fi": "FI",
    "hungarian": "HU",
    "hu": "HU",
    "lithuanian": "LT",
    "lt": "LT",
    "latvian": "LV",
    "lv": "LV",
    "romanian": "RO",
    "ro": "RO",
    "slovak": "SK",
    "sk": "SK",
    "slovenian": "SL",
    "sl": "SL",
    "swedish": "SV",
    "sv": "SV",
}

TRANSLATION_PROMPT_TEMPLATE = """Your task: translate machine-generated subtitles from {source_lang} to {target_lang}. You will receive {subtitle_count} subtitles.

Instructions:
1. You will receive an array of subtitle cues in JSON format. Each cue has a stable `cue_id` and a `text` field.
2. Translate the "text" of each subtitle.
3. You MUST preserve each `cue_id` exactly.
4. {response_structure}
5. If a subtitle should be removed (e.g., it contains only filler words or you are confident it is a hallucination of the STT model), replace its text with "[REMOVE]".
6. Spell out numbers, especially Roman numerals, dates, amounts etc.
7. It is ok for a subtitle to not end in punctuation if the following subtitle continues the sentence/thought. You don't have to add "..." - in fact, don't do it.
8. Choose concise translations suitable for dubbing while maintaining accuracy, grammatical correctness in the target language and the tone of the source.
9. Use correct punctuation that enhances a natural flow of speech for optimal speech generation.
10. Do not add ANY comments, confirmations, explanations, or questions. {output_only_instruction}
11. Before outputting your answer, validate its formatting. {validation_instruction}
12. Do not add speaker names, speaker numbers, or bracketed speaker labels to translated text. Preserve each supplied `speaker` by default. {known_speakers_policy}
13. {cue_context_policy}
14. Overlapping cues from different speakers can be legitimate simultaneous speech. Preserve meaningful speech. You may mark a very short, inconsequential interjection as `[REMOVE]` only when it obscures a longer utterance, and remove one copy of clearly duplicated near-identical ASR text that occupies the same time span.
"""

CONTEXT_PROMPT_TEMPLATE = """
For additional context, this is the final version of the previous subtitle block processed by you before:
{context_previous_response}
""".strip()

GLOSSARY_INSTRUCTIONS_TRANSLATION = """
Use the following glossary. Apply it flexibly, considering different forms of speech parts, like declination and conjugation. The purpose of it is to make the translation coherent:
{glossary}

After your translation, if you identify important terms for consistent translation, add them below the [GLOSSARY] tag as 'word or phrase in source language = translated word or phrase in target language'. Include only NEW entries, not ones already in the glossary.
""".strip()

STRUCTURED_GLOSSARY_INSTRUCTIONS_TRANSLATION = """
Use the existing mappings supplied once in `task.glossary`, applying them flexibly across inflection and conjugation. If you identify important NEW terminology for later batches, return only those additions in `glossary_updates`; do not repeat existing mappings.
""".strip()


@dataclass(frozen=True)
class TranslationResult:
    srt_content: str
    block_responses: list[dict[str, Any]]
    glossary: dict[str, str]
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


def get_deepl_language_code(language: str) -> str:
    normalized = str(language or "").strip()
    if not normalized:
        return ""
    return DEEPL_LANGUAGE_MAP.get(
        normalized.lower(), normalized.upper() if len(normalized) <= 5 else normalized
    )


def _build_deepl_translator(auth_key: str) -> Any:
    if not str(auth_key or "").strip():
        raise ValueError("DeepL translation requires DEEPL_API_KEY.")

    try:
        import deepl
    except ImportError as error:  # pragma: no cover - depends on runtime environment
        raise RuntimeError(
            "DeepL translation requires the 'deepl' package to be installed."
        ) from error

    return deepl.Translator(auth_key)


def _split_deepl_request_texts(
    translation_blocks: list[list[dict[str, Any]]],
    max_bytes: int = DEEPL_MAX_REQUEST_BYTES,
) -> list[str]:
    request_texts: list[str] = []
    current_text = ""

    for block in translation_blocks:
        block_text = "\n\n".join(str(subtitle.get("text") or "") for subtitle in block)
        if not block_text:
            continue

        candidate = f"{current_text}\n\n{block_text}" if current_text else block_text
        if current_text and len(candidate.encode("utf-8")) > max_bytes:
            request_texts.append(current_text)
            current_text = block_text
        else:
            current_text = candidate

    if current_text:
        request_texts.append(current_text)

    return request_texts


def translate_blocks_deepl(
    translation_blocks: list[list[dict[str, Any]]],
    source_language: str,
    target_language: str,
    auth_key: str,
    *,
    translator_factory: Callable[[str], Any] | None = None,
    cancel_event: Any | None = None,
    progress_callback: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    del (
        source_language
    )  # DeepL can auto-detect here; keep the argument for provider parity.
    translator = (
        translator_factory(auth_key)
        if translator_factory is not None
        else _build_deepl_translator(auth_key)
    )
    target_code = get_deepl_language_code(target_language)
    request_texts = _split_deepl_request_texts(translation_blocks)

    translated_parts: list[str] = []
    request_count = len(request_texts)
    if request_count == 0:
        _report_progress(progress_callback, 1.0, "No subtitles require translation")
    for request_index, request_text in enumerate(request_texts, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("DeepL translation was canceled.")
        _report_progress(
            progress_callback,
            (request_index - 1) / request_count,
            f"Sending DeepL request {request_index} of {request_count}",
        )
        result = translator.translate_text(request_text, target_lang=target_code)
        translated_parts.append(str(getattr(result, "text", result) or ""))
        _report_progress(
            progress_callback,
            request_index / request_count,
            f"Completed DeepL request {request_index} of {request_count}",
        )

    translated_units = [
        part.strip() for part in "\n\n".join(translated_parts).split("\n\n")
    ]
    expected_count = sum(len(block) for block in translation_blocks)
    if len(translated_units) != expected_count:
        raise ValueError(
            f"DeepL response count mismatch: expected {expected_count}, got {len(translated_units)}."
        )

    translated_responses: list[dict[str, Any]] = []
    translated_index = 0
    for block in translation_blocks:
        block_translations = translated_units[
            translated_index : translated_index + len(block)
        ]
        translated_index += len(block)
        translated_responses.append(
            {
                "translation": block_translations,
                "new_glossary": "",
                "original_indices": [subtitle["index"] for subtitle in block],
            }
        )

    return translated_responses


def load_glossary(session_dir: str | os.PathLike[str]) -> dict[str, str]:
    glossary_path = Path(session_dir) / "translation_glossary.json"
    if not glossary_path.exists():
        return {}
    try:
        with glossary_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items()}


def normalize_glossary(value: object) -> dict[str, str]:
    """Normalize saved, manual, or research glossary values into one mapping."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {
            str(source).strip(): str(target).strip()
            for source, target in value.items()
            if str(source).strip() and str(target).strip()
        }
    if isinstance(value, (list, tuple)):
        normalized: dict[str, str] = {}
        for item in value:
            if not isinstance(item, Mapping):
                continue
            source = str(item.get("source") or item.get("term") or "").strip()
            target = str(
                item.get("target") or item.get("translation") or item.get("value") or ""
            ).strip()
            if source and target:
                normalized[source] = target
        return normalized
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return parse_glossary_entries(text)
    return normalize_glossary(payload)


def merge_glossaries(*values: object) -> dict[str, str]:
    """Merge glossary sources deterministically; later sources win case-insensitively."""
    merged: dict[str, str] = {}
    key_by_casefold: dict[str, str] = {}
    for value in values:
        for source, target in normalize_glossary(value).items():
            folded = source.casefold()
            previous_key = key_by_casefold.get(folded)
            if previous_key is not None and previous_key != source:
                merged.pop(previous_key, None)
            key_by_casefold[folded] = source
            merged[source] = target
    return merged


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


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


def save_glossary(
    session_dir: str | os.PathLike[str], glossary: dict[str, str]
) -> None:
    _write_json_atomic(Path(session_dir) / "translation_glossary.json", glossary)


def parse_glossary_entries(glossary_text: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in str(glossary_text or "").splitlines():
        if "=" not in line:
            continue
        source, translated = line.split("=", 1)
        source = source.strip()
        translated = translated.strip()
        if source and translated:
            entries[source] = translated
    return entries


def parse_translation_response(
    response_text: str,
    expected_count: int | None = None,
    *,
    expected_numbers: list[int] | None = None,
) -> tuple[list[str], dict[str, str]]:
    translations, glossary, _speakers = parse_translation_response_details(
        response_text,
        expected_count,
        expected_numbers=expected_numbers,
    )
    return translations, glossary


def parse_translation_response_details(
    response_text: str,
    expected_count: int | None = None,
    *,
    expected_numbers: list[int] | None = None,
    known_speakers: set[str] | None = None,
) -> tuple[list[str], dict[str, str], list[str | None]]:
    """Parse translated text plus optional, validated speaker corrections."""
    translation_text, _separator, glossary_text = str(response_text or "").partition(
        "[GLOSSARY]"
    )
    payload = extract_json_payload(translation_text)
    if not isinstance(payload, list):
        raise ValueError("Translation response must be a JSON array.")
    return parse_translation_items_details(
        payload,
        expected_count,
        expected_numbers=expected_numbers,
        known_speakers=known_speakers,
        glossary=parse_glossary_entries(glossary_text),
    )


def parse_translation_items_details(
    payload: list[Any],
    expected_count: int | None = None,
    *,
    expected_numbers: list[int] | None = None,
    known_speakers: set[str] | None = None,
    glossary: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str], list[str | None]]:
    """Validate an already-structured translation result without text parsing."""

    if expected_numbers is None:
        if expected_count is None:
            raise ValueError("Expected subtitle identifiers are required.")
        expected_numbers = list(range(1, expected_count + 1))
    else:
        expected_numbers = [int(number) for number in expected_numbers]
        if expected_count is not None and len(expected_numbers) != expected_count:
            raise ValueError("Expected subtitle count and identifiers disagree.")
    if len(set(expected_numbers)) != len(expected_numbers):
        raise ValueError("Expected subtitle identifiers must be unique.")

    if len(payload) != len(expected_numbers):
        raise ValueError(
            "Translation response count mismatch: "
            f"expected {len(expected_numbers)}, got {len(payload)}."
        )

    translations_by_number: dict[int, str] = {}
    speakers_by_number: dict[int, str | None] = {}
    known_by_casefold = {
        speaker.casefold(): speaker for speaker in (known_speakers or set())
    }
    for item in payload:
        if (
            not isinstance(item, dict)
            or not ({"cue_id", "number"} & set(item))
            or "text" not in item
        ):
            raise ValueError(
                "Translation response items must contain 'cue_id' and 'text'."
            )
        raw_number = item.get("cue_id", item.get("number"))
        if isinstance(raw_number, bool):
            raise ValueError("Translation response identifiers must be integers.")
        if isinstance(raw_number, int):
            number = raw_number
        elif isinstance(raw_number, str) and raw_number.strip().isdigit():
            number = int(raw_number.strip())
        else:
            raise ValueError("Translation response identifiers must be integers.")
        if number in translations_by_number:
            raise ValueError(
                f"Translation response repeated subtitle identifier {number}."
            )
        if number not in expected_numbers:
            raise ValueError(
                f"Translation response returned unexpected subtitle identifier {number}."
            )
        if not isinstance(item["text"], str):
            raise ValueError(
                f"Translation text for subtitle {number} must be a string."
            )
        translations_by_number[number] = split_speaker_label(item["text"].strip())[1]
        raw_speaker = str(item.get("speaker") or "").strip()
        if raw_speaker:
            if (
                known_speakers is not None
                and raw_speaker.casefold() not in known_by_casefold
            ):
                raise ValueError(
                    f"Translation response returned unknown speaker ID {raw_speaker!r}."
                )
            speakers_by_number[number] = known_by_casefold.get(
                raw_speaker.casefold(), raw_speaker
            )
        else:
            speakers_by_number[number] = None

    missing = [
        number for number in expected_numbers if number not in translations_by_number
    ]
    if missing:
        raise ValueError(
            "Translation response omitted subtitle identifier(s): "
            + ", ".join(str(number) for number in missing)
            + "."
        )
    return (
        [translations_by_number[number] for number in expected_numbers],
        dict(glossary or {}),
        [speakers_by_number[number] for number in expected_numbers],
    )


def build_translation_task_instructions(
    *,
    subtitle_count: int,
    source_language: str,
    target_language: str,
    translation_instructions: str = "",
    glossary: dict[str, str] | None = None,
    no_remove_subtitles: bool = False,
    timing_context_mode: str | None = None,
    include_timing_context: bool | None = None,
    substantial_gap_ms: int = 2000,
    known_speakers: set[str] | None = None,
    dispatch_result: bool = False,
    structured_context: bool = False,
) -> str:
    """Build translation guidance without embedding source cue content."""

    mode = normalize_timing_context_mode(
        timing_context_mode,
        legacy_enabled=include_timing_context,
        default="none",
    )
    prompt_template = TRANSLATION_PROMPT_TEMPLATE
    if no_remove_subtitles:
        prompt_template = prompt_template.replace(
            '5. If a subtitle should be removed (e.g., it contains only filler words or you are confident it is a hallucination of the STT model), replace its text with "[REMOVE]".',
            "5. You MUST NOT remove any subtitles. Translate every subtitle, even if it contains filler words.",
        )

    prompt = prompt_template.format(
        source_lang=source_language,
        target_lang=target_language,
        subtitle_count=int(subtitle_count),
        response_structure=(
            f"Return one typed result object with `kind` equal to `translation` "
            f"and a `translations` array containing exactly {int(subtitle_count)} "
            '`{"cue_id": 1, "text": "translated text"}` items. The optional '
            "`glossary_updates` object contains only new terminology."
            if dispatch_result
            else f"Return EXACTLY {int(subtitle_count)} items as "
            '`{"cue_id": 1, "text": "translated text"}` objects.'
        ),
        output_only_instruction=(
            "Output only the typed result object described above."
            if dispatch_result
            else "Output only the translation JSON array."
        ),
        validation_instruction=(
            f"Return exactly {int(subtitle_count)} translations inside the typed "
            "result object, preserving every supplied `cue_id`."
            if dispatch_result
            else f"Return exactly {int(subtitle_count)} subtitles with the same "
            "cue IDs as the input."
        ),
        known_speakers_policy=(
            "Known speaker IDs are provided once in `task.known_speakers`; use "
            "only those IDs when correcting diarization. An empty list means "
            "speaker reassignment is unavailable. Omit `speaker` when none was "
            "supplied."
            if structured_context
            else (
                "If the discourse clearly reveals a diarization mistake, return "
                "a corrected `speaker` using only one of these known IDs: "
                + (
                    ", ".join(sorted(known_speakers, key=str.casefold))
                    if known_speakers
                    else "none (do not add speaker assignments)"
                )
                + ". Omit `speaker` when no speaker was supplied."
            )
        ),
        cue_context_policy=(
            "The optional `speaker` field is non-spoken evidence. Speaker IDs "
            "may change at ASR chunk seams: use the discourse to correct a "
            "likely mistake, but never copy it into translated text or invent "
            "an ID."
            if mode == "none"
            else (
                "The optional `speaker` field and "
                "`timing.overlap_with_previous_ms` are non-spoken evidence. "
                "Speaker IDs may change at ASR chunk seams: use the discourse "
                "to correct a likely mistake, but never copy context into "
                "translated text or invent an ID."
                if mode == "overlap_only"
                else "The optional `speaker` and `timing` objects are non-spoken "
                "evidence. Speaker IDs may change at ASR chunk seams: use the "
                "discourse to correct a likely mistake, but never copy context "
                "into translated text or invent an ID."
            )
        ),
    )
    if translation_instructions:
        prompt += (
            f"\n\nAdditional context and instructions:\n{translation_instructions}"
        )

    if mode == "full":
        gap_reference = (
            "`task.substantial_gap_ms`"
            if structured_context
            else f"{max(0, int(substantial_gap_ms))} ms"
        )
        prompt += (
            "\n\nTiming policy:\n"
            "- Each cue's optional `timing` object contains `start_ms`, `end_ms`, "
            "and its gap from or overlap with the preceding cue.\n"
            f"- A gap of at least {gap_reference} is a "
            "substantial audible pause: preserve the rhetorical boundary in "
            "punctuation and phrasing even when the thought continues.\n"
            "- A shorter gap is not by itself a new utterance. Keep coherent "
            "same-speaker phrasing natural across ordinary cue boundaries."
        )
    elif mode == "overlap_only":
        prompt += (
            "\n\nTiming policy:\n"
            "- A cue has a `timing` object only when it overlaps the preceding "
            "cue. Treat that overlap as non-spoken evidence of simultaneous "
            "speech or a possible duplicated ASR seam."
        )

    if glossary is not None:
        prompt += "\n\n" + (
            STRUCTURED_GLOSSARY_INSTRUCTIONS_TRANSLATION
            if structured_context
            else GLOSSARY_INSTRUCTIONS_TRANSLATION.format(
                glossary=json.dumps(glossary, ensure_ascii=False, indent=2)
            )
        )

    return prompt


def build_translation_prompt(
    block: list[dict[str, Any]],
    *,
    source_language: str,
    target_language: str,
    translation_instructions: str = "",
    glossary: dict[str, str] | None = None,
    previous_response: str = "",
    next_block: list[dict[str, Any]] | None = None,
    no_remove_subtitles: bool = False,
    timing_context_mode: str | None = None,
    include_timing_context: bool | None = None,
    substantial_gap_ms: int = 2000,
    known_speakers: set[str] | None = None,
    context_after: int = 2,
) -> str:
    mode = normalize_timing_context_mode(
        timing_context_mode,
        legacy_enabled=include_timing_context,
        default="none",
    )
    prompt = build_translation_task_instructions(
        subtitle_count=len(block),
        source_language=source_language,
        target_language=target_language,
        translation_instructions=translation_instructions,
        glossary=glossary,
        no_remove_subtitles=no_remove_subtitles,
        timing_context_mode=mode,
        substantial_gap_ms=substantial_gap_ms,
        known_speakers=known_speakers,
    )

    if previous_response:
        prompt += "\n" + CONTEXT_PROMPT_TEMPLATE.format(
            context_previous_response=previous_response
        )

    if next_block and context_after > 0:
        following = [
            cue
            for subtitle in next_block[:context_after]
            if (cue := subtitle_boundary_cue(subtitle)) is not None
        ]
        if following:
            prompt += (
                "\n\nFollowing source cues for continuity only; do not return "
                "translations for them:\n"
                + json.dumps(following, ensure_ascii=False)
            )

    subtitles = json.dumps(
        [
            subtitle_task_cue(
                subtitle,
                timing_context_mode=mode,
            )
            for subtitle in block
        ],
        ensure_ascii=False,
    )
    return f"{prompt}\n\nThe subtitles:\n{subtitles}"


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


def translation_unit_key(unit: list[dict[str, Any]]) -> str:
    """Return a stable key for a translation block or recovery split leaf."""
    indices = [int(subtitle["index"]) for subtitle in unit]
    digest = hashlib.sha256(
        json.dumps(indices, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    return f"translation:{indices[0]}-{indices[-1]}:{digest}"


@dataclass(slots=True)
class _TranslationUnitResult:
    translations: list[str]
    speakers: list[str]
    glossary: dict[str, str]
    context: str
    cost: float = 0.0
    response_count: int = 0
    cost_sources: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)

    def merged_with(self, other: _TranslationUnitResult) -> _TranslationUnitResult:
        usage = dict(self.usage)
        for key, value in other.usage.items():
            usage[key] = int(usage.get(key) or 0) + int(value or 0)
        return _TranslationUnitResult(
            translations=[*self.translations, *other.translations],
            speakers=[*self.speakers, *other.speakers],
            glossary=merge_glossaries(self.glossary, other.glossary),
            context=other.context or self.context,
            cost=self.cost + other.cost,
            response_count=self.response_count + other.response_count,
            cost_sources=list(dict.fromkeys([*self.cost_sources, *other.cost_sources])),
            usage=usage,
        )


def _restore_translation_unit(
    unit: list[dict[str, Any]],
    completed_units: Mapping[str, Mapping[str, Any]] | None,
) -> _TranslationUnitResult | None:
    if not completed_units:
        return None
    key = translation_unit_key(unit)
    raw = completed_units.get(key)
    if raw is None:
        return None
    expected_indices = [int(subtitle["index"]) for subtitle in unit]
    if [int(value) for value in raw.get("original_indices", [])] != expected_indices:
        raise ValueError(
            f"Translation checkpoint {key} does not match its source unit."
        )
    translations = raw.get("translations")
    speakers = raw.get("speakers")
    if not isinstance(translations, list) or len(translations) != len(unit):
        raise ValueError(f"Translation checkpoint {key} has invalid translations.")
    if not isinstance(speakers, list) or len(speakers) != len(unit):
        raise ValueError(f"Translation checkpoint {key} has invalid speakers.")
    normalized_translations = [str(text or "").strip() for text in translations]
    if any(not text for text in normalized_translations):
        raise ValueError(f"Translation checkpoint {key} has an empty translation.")
    try:
        cost = float(raw.get("cost") or 0.0)
        response_count = int(raw.get("response_count") or 0)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Translation checkpoint {key} has invalid metrics."
        ) from error
    raw_usage = raw.get("usage")
    return _TranslationUnitResult(
        translations=normalized_translations,
        speakers=[str(speaker or "").strip() for speaker in speakers],
        glossary=normalize_glossary(raw.get("glossary")),
        context=str(raw.get("context") or ""),
        cost=cost,
        response_count=response_count,
        cost_sources=[
            str(source) for source in raw.get("cost_sources", []) if str(source or "")
        ],
        usage=dict(raw_usage) if isinstance(raw_usage, Mapping) else {},
    )


def _has_translation_descendant(
    unit: list[dict[str, Any]],
    completed_units: Mapping[str, Mapping[str, Any]] | None,
) -> bool:
    if not completed_units:
        return False
    expected = [int(subtitle["index"]) for subtitle in unit]
    expected_set = set(expected)
    for raw in completed_units.values():
        try:
            indices = [int(value) for value in raw.get("original_indices", [])]
        except (AttributeError, TypeError, ValueError):
            continue
        if (
            indices
            and len(indices) < len(expected)
            and set(indices).issubset(expected_set)
        ):
            return True
    return False


def translation_responses_to_srt(
    translated_responses: list[dict[str, Any]],
    original_srt: str,
    *,
    remove_marked_subtitles: bool = True,
    speaker_by_subtitle: Mapping[int, str] | None = None,
) -> str:
    original_segments = parse_srt(original_srt)
    segments_by_index = {segment.index: segment for segment in original_segments}
    translated_segments: list[SubtitleSegment] = []

    for response in translated_responses:
        translations = response.get("translation", [])
        indices = response.get("original_indices", [])
        response_speakers = response.get("speakers", [])
        for position, (translated_text, original_index) in enumerate(
            zip(translations, indices)
        ):
            if remove_marked_subtitles and str(translated_text).strip() == "[REMOVE]":
                continue
            original = segments_by_index.get(int(original_index))
            if original is None:
                continue
            translated_segments.append(
                SubtitleSegment(
                    index=len(translated_segments) + 1,
                    start_ms=original.start_ms,
                    end_ms=original.end_ms,
                    text=str(translated_text).strip(),
                    speaker=str(
                        (
                            response_speakers[position]
                            if isinstance(response_speakers, list)
                            and position < len(response_speakers)
                            and response_speakers[position]
                            else None
                        )
                        or (speaker_by_subtitle or {}).get(original.index)
                        or original.speaker
                        or ""
                    ).strip(),
                )
            )

    return compose_srt(translated_segments)


def translate_srt_content(
    srt_content: str,
    settings: dict[str, Any],
    translation_instructions: str = "",
    *,
    glossary: dict[str, str] | None = None,
    completion_func: Callable[..., Any] | None = None,
    cancel_event: Any | None = None,
    speaker_by_subtitle: Mapping[int, str] | None = None,
    progress_callback: ProgressCallback | None = None,
    completed_units: Mapping[str, Mapping[str, Any]] | None = None,
    on_unit_completed: UnitCompletedCallback | None = None,
) -> TranslationResult:
    source_language = str(
        settings.get("original_language")
        or settings.get("stt_language")
        or settings.get("whisper_language")
        or "English"
    )
    target_language = str(settings.get("target_language") or "en")
    char_limit = int(
        settings.get("char_limit")
        or settings.get("llm_char")
        or DEFAULT_LLM_CHAR_LIMIT
    )
    max_subtitles_per_call = max(
        1,
        int(
            settings.get("max_segments_per_batch")
            or settings.get("max_subtitles_per_call")
            or 40
        ),
    )
    use_context = bool(settings.get("context", True))
    try:
        raw_context_before = settings.get("context_before")
        context_before = max(
            0,
            min(20, int(8 if raw_context_before is None else raw_context_before)),
        )
    except (TypeError, ValueError):
        context_before = 8
    try:
        raw_context_after = settings.get("context_after")
        context_after = max(
            0,
            min(20, int(2 if raw_context_after is None else raw_context_after)),
        )
    except (TypeError, ValueError):
        context_after = 2
    no_remove_subtitles = bool(settings.get("no_remove_subtitles", False))
    timing_context_mode = timing_context_mode_from_settings(settings)
    try:
        configured_gap = settings.get(
            "substantial_gap_ms",
            settings.get("timing_context_gap_ms"),
        )
        substantial_gap_ms = max(
            0,
            int(
                2000
                if configured_gap is None or configured_gap == ""
                else configured_gap
            ),
        )
    except (TypeError, ValueError):
        substantial_gap_ms = 2000
    manual_glossary = normalize_glossary(settings.get("glossary"))
    use_glossary = bool(settings.get("glossary_enabled", False) or glossary)
    active_glossary = (
        merge_glossaries(glossary, manual_glossary) if use_glossary else {}
    )
    try:
        workers = max(1, min(16, int(settings.get("llm_concurrent_calls") or 1)))
    except (TypeError, ValueError):
        workers = 1

    blocks = create_translation_blocks(
        srt_content,
        char_limit,
        source_language,
        max_subtitles_per_block=max_subtitles_per_call,
        speaker_by_subtitle=speaker_by_subtitle,
    )
    if not blocks:
        _report_progress(progress_callback, 1.0, "No subtitles require translation")
        return TranslationResult("", [], active_glossary, cost=0.0, response_count=0)

    total_subtitles = sum(len(block) for block in blocks)
    completed_subtitle_ids: set[int] = set()
    progress_lock = Lock()
    checkpoint_lock = Lock()
    resolved = resolve_dubbing_llm_settings(settings, stage="translation")
    completion = completion_func or llm_handler.chat_completion_with_metadata
    translated_responses: list[dict[str, Any]] = []
    total_cost = 0.0
    response_count = 0
    cost_sources: list[str] = []
    usage: dict[str, Any] = {}
    known_speakers = {
        str(subtitle.get("speaker") or "").strip()
        for block in blocks
        for subtitle in block
        if str(subtitle.get("speaker") or "").strip()
    }
    try:
        structured_attempts = max(
            1,
            min(
                5,
                int(
                    settings.get("translation_structured_max_attempts")
                    or settings.get("llm_structured_max_attempts")
                    or DEFAULT_TRANSLATION_STRUCTURED_ATTEMPTS
                ),
            ),
        )
    except (TypeError, ValueError):
        structured_attempts = DEFAULT_TRANSLATION_STRUCTURED_ATTEMPTS
    try:
        recovery_split_depth = max(
            0,
            min(
                6,
                int(
                    str(
                        settings.get("translation_recovery_split_depth")
                        if settings.get("translation_recovery_split_depth") is not None
                        else DEFAULT_TRANSLATION_RECOVERY_SPLIT_DEPTH
                    )
                ),
            ),
        )
    except (TypeError, ValueError):
        recovery_split_depth = DEFAULT_TRANSLATION_RECOVERY_SPLIT_DEPTH

    def translate_block(
        index: int,
        block: list[dict[str, Any]],
        previous_context: str,
        glossary_snapshot: dict[str, str],
    ) -> _TranslationUnitResult:
        local_glossary = dict(glossary_snapshot)

        def update_local_glossary(new_values: object) -> None:
            nonlocal local_glossary
            if use_glossary:
                # Explicit manual entries are the user's decision and stay
                # authoritative over research/model-proposed additions.
                local_glossary = merge_glossaries(
                    local_glossary,
                    new_values,
                    manual_glossary,
                )

        def context_for(
            unit: list[dict[str, Any]],
            translations: list[str],
            speakers: list[str],
        ) -> str:
            if context_before <= 0:
                return ""
            context_items = [
                cue
                for _subtitle, translated_text, speaker in list(
                    zip(unit, translations, speakers, strict=True)
                )[-context_before:]
                if (
                    cue := subtitle_boundary_cue(
                        {"text": translated_text, "speaker": speaker}
                    )
                )
                is not None
            ]
            return json.dumps(context_items, ensure_ascii=False)

        def report_unit_complete(unit: list[dict[str, Any]], *, restored: bool) -> None:
            expected_numbers = [int(subtitle["index"]) for subtitle in unit]
            with progress_lock:
                completed_subtitle_ids.update(expected_numbers)
                _report_progress(
                    progress_callback,
                    len(completed_subtitle_ids) / total_subtitles,
                    (
                        f"{'Restored' if restored else 'Translated'} "
                        f"{len(completed_subtitle_ids)} of {total_subtitles} subtitles"
                    ),
                )

        def translate_unit(
            unit: list[dict[str, Any]],
            *,
            previous_unit_context: str,
            next_context: list[dict[str, Any]] | None,
            label: str,
            split_depth: int = 0,
            carried_metrics: _TranslationUnitResult | None = None,
        ) -> _TranslationUnitResult:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("LLM translation was canceled.")
            restored = _restore_translation_unit(unit, completed_units)
            if restored is not None:
                unknown_speakers = [
                    speaker
                    for speaker in restored.speakers
                    if speaker
                    and speaker.casefold()
                    not in {known.casefold() for known in known_speakers}
                ]
                if unknown_speakers:
                    raise ValueError(
                        f"Translation checkpoint {translation_unit_key(unit)} contains "
                        f"unknown speaker ID(s): {unknown_speakers}."
                    )
                update_local_glossary(restored.glossary)
                if carried_metrics is not None:
                    restored.cost += carried_metrics.cost
                    restored.response_count += carried_metrics.response_count
                    restored.cost_sources = list(
                        dict.fromkeys(
                            [*carried_metrics.cost_sources, *restored.cost_sources]
                        )
                    )
                    for key, value in carried_metrics.usage.items():
                        restored.usage[key] = int(restored.usage.get(key) or 0) + int(
                            value or 0
                        )
                report_unit_complete(unit, restored=True)
                return restored

            if len(unit) > 1 and _has_translation_descendant(unit, completed_units):
                midpoint = len(unit) // 2
                left = unit[:midpoint]
                right = unit[midpoint:]
                left_result = translate_unit(
                    left,
                    previous_unit_context=previous_unit_context,
                    next_context=right,
                    label=f"{label}.1",
                    split_depth=split_depth + 1,
                    carried_metrics=carried_metrics,
                )
                right_result = translate_unit(
                    right,
                    previous_unit_context=left_result.context,
                    next_context=next_context,
                    label=f"{label}.2",
                    split_depth=split_depth + 1,
                )
                combined = left_result.merged_with(right_result)
                combined.context = context_for(
                    unit, combined.translations, combined.speakers
                )
                return combined

            metrics = carried_metrics or _TranslationUnitResult([], [], {}, "")
            prompt = build_translation_prompt(
                unit,
                source_language=source_language,
                target_language=target_language,
                translation_instructions=translation_instructions,
                glossary=local_glossary if use_glossary else None,
                previous_response=previous_unit_context if use_context else "",
                next_block=next_context if use_context else None,
                no_remove_subtitles=no_remove_subtitles,
                timing_context_mode=timing_context_mode,
                substantial_gap_ms=substantial_gap_ms,
                known_speakers=known_speakers,
                context_after=context_after,
            )
            expected_numbers = [int(subtitle["index"]) for subtitle in unit]
            last_error: ValueError | None = None

            for attempt in range(1, structured_attempts + 1):
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("LLM translation was canceled.")
                request_number = metrics.response_count + 1
                with progress_lock:
                    _report_progress(
                        progress_callback,
                        len(completed_subtitle_ids) / total_subtitles,
                        (
                            f"Translating {label} — request {request_number}"
                            + (
                                f", validation attempt {attempt} of {structured_attempts}"
                                if attempt > 1
                                else ""
                            )
                        ),
                    )
                messages = [{"role": "user", "content": prompt}]
                if last_error is not None:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your previous response was rejected because "
                                f"{last_error}. Return a complete replacement JSON array now. "
                                f"Include each identifier exactly once: {expected_numbers}. "
                                "Do not explain the correction."
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
                _merge_completion_usage(metrics.usage, result)
                metrics.cost += cost
                metrics.response_count += 1
                if cost_source and cost_source not in metrics.cost_sources:
                    metrics.cost_sources.append(cost_source)

                try:
                    if not content:
                        raise ValueError("the model returned an empty response")
                    translated_texts, new_glossary, returned_speakers = (
                        parse_translation_response_details(
                            content,
                            expected_numbers=expected_numbers,
                            known_speakers=known_speakers,
                        )
                    )
                except ValueError as error:
                    last_error = error
                    if attempt < structured_attempts:
                        logger.warning(
                            "Translation protocol attempt %d/%d failed for %s: %s",
                            attempt,
                            structured_attempts,
                            label,
                            error,
                        )
                        continue
                    break

                update_local_glossary(new_glossary)
                speakers = [
                    returned_speaker or str(subtitle.get("speaker") or "").strip()
                    for subtitle, returned_speaker in zip(
                        unit, returned_speakers, strict=True
                    )
                ]
                context_response = context_for(unit, translated_texts, speakers)
                unit_result = _TranslationUnitResult(
                    translations=translated_texts,
                    speakers=speakers,
                    glossary=new_glossary,
                    context=context_response,
                    cost=metrics.cost,
                    response_count=metrics.response_count,
                    cost_sources=metrics.cost_sources,
                    usage=metrics.usage,
                )
                if on_unit_completed is not None:
                    payload = {
                        "version": 1,
                        "kind": "translation",
                        "original_indices": expected_numbers,
                        "translations": translated_texts,
                        "speakers": speakers,
                        "glossary": new_glossary,
                        "context": context_response,
                        "cost": unit_result.cost,
                        "response_count": unit_result.response_count,
                        "cost_sources": unit_result.cost_sources,
                        "usage": unit_result.usage,
                    }
                    with checkpoint_lock:
                        on_unit_completed(translation_unit_key(unit), payload)
                report_unit_complete(unit, restored=False)
                return unit_result

            assert last_error is not None
            if len(unit) > 1 and split_depth < recovery_split_depth:
                midpoint = len(unit) // 2
                left = unit[:midpoint]
                right = unit[midpoint:]
                logger.warning(
                    "Translation protocol remained invalid for %s; retrying as %d + %d subtitles.",
                    label,
                    len(left),
                    len(right),
                )
                left_result = translate_unit(
                    left,
                    previous_unit_context=previous_unit_context,
                    next_context=right,
                    label=f"{label}.1",
                    split_depth=split_depth + 1,
                    carried_metrics=metrics,
                )
                right_result = translate_unit(
                    right,
                    previous_unit_context=left_result.context,
                    next_context=next_context,
                    label=f"{label}.2",
                    split_depth=split_depth + 1,
                )
                combined = left_result.merged_with(right_result)
                combined.context = context_for(
                    unit, combined.translations, combined.speakers
                )
                return combined

            raise ValueError(
                f"Failed to translate {label} after {structured_attempts} structured "
                f"response attempts: {last_error}"
            ) from last_error

        next_block = blocks[index + 1] if index < len(blocks) - 1 else None
        return translate_unit(
            block,
            previous_unit_context=previous_context,
            next_context=next_block,
            label=f"subtitle block {index + 1}",
        )

    def append_block_result(index: int, result: _TranslationUnitResult) -> str:
        nonlocal total_cost, response_count
        if use_glossary:
            merged = merge_glossaries(active_glossary, result.glossary, manual_glossary)
            active_glossary.clear()
            active_glossary.update(merged)
        total_cost += result.cost
        response_count += result.response_count
        for source in result.cost_sources:
            if source and source not in cost_sources:
                cost_sources.append(source)
        for key, value in result.usage.items():
            usage[key] = int(usage.get(key) or 0) + int(value or 0)
        translated_responses.append(
            {
                "translation": result.translations,
                "speakers": result.speakers,
                "new_glossary": "\n".join(
                    f"{key} = {value}" for key, value in result.glossary.items()
                ),
                "original_indices": [subtitle["index"] for subtitle in blocks[index]],
            }
        )
        return result.context

    if workers == 1 or len(blocks) == 1:
        previous_response = ""
        for index, block in enumerate(blocks):
            previous_response = append_block_result(
                index,
                translate_block(index, block, previous_response, active_glossary),
            )
    else:
        base_glossary = dict(active_glossary)
        ordered: dict[int, _TranslationUnitResult] = {}
        executor = ThreadPoolExecutor(max_workers=min(workers, len(blocks)))
        futures = {
            executor.submit(translate_block, index, block, "", base_glossary): index
            for index, block in enumerate(blocks)
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
        for index in range(len(blocks)):
            append_block_result(index, ordered[index])

    translated_srt = translation_responses_to_srt(
        translated_responses,
        srt_content,
        speaker_by_subtitle=speaker_by_subtitle,
    )
    translated_speakers: dict[int, str] = {}
    output_index = 0
    for response in translated_responses:
        response_speakers = response.get("speakers", [])
        for position, translated_text in enumerate(response.get("translation", [])):
            if (
                not str(translated_text or "").strip()
                or str(translated_text).strip() == "[REMOVE]"
            ):
                continue
            output_index += 1
            speaker = (
                str(response_speakers[position] or "").strip()
                if isinstance(response_speakers, list)
                and position < len(response_speakers)
                else ""
            )
            if speaker:
                translated_speakers[output_index] = speaker
    return TranslationResult(
        srt_content=translated_srt,
        block_responses=translated_responses,
        glossary=active_glossary,
        cost=total_cost,
        response_count=response_count,
        cost_sources=tuple(cost_sources),
        usage=usage,
        speaker_by_subtitle=translated_speakers,
    )


def translate_srt_file_with_result(
    session_dir: str | os.PathLike[str],
    srt_file: str | os.PathLike[str],
    settings: dict[str, Any],
    translation_instructions: str = "",
    *,
    glossary: object | None = None,
    completion_func: Callable[..., Any] | None = None,
    cancel_event: Any | None = None,
    speaker_by_subtitle: Mapping[int, str] | None = None,
    progress_callback: ProgressCallback | None = None,
    completed_units: Mapping[str, Mapping[str, Any]] | None = None,
    on_unit_completed: UnitCompletedCallback | None = None,
) -> TranslationResult:
    session_path = Path(session_dir)
    srt_path = Path(srt_file)
    with srt_path.open("r", encoding="utf-8-sig") as handle:
        srt_content = handle.read()

    saved_glossary = (
        load_glossary(session_path) if settings.get("glossary_enabled") else {}
    )
    glossary_seed = merge_glossaries(saved_glossary, glossary)
    result = translate_srt_content(
        srt_content,
        settings,
        translation_instructions=translation_instructions,
        glossary=glossary_seed,
        completion_func=completion_func,
        cancel_event=cancel_event,
        speaker_by_subtitle=speaker_by_subtitle,
        progress_callback=progress_callback,
        completed_units=completed_units,
        on_unit_completed=on_unit_completed,
    )

    target_language = str(settings.get("target_language") or "en")
    output_path = session_path / f"{srt_path.stem}_{target_language}.srt"
    final_blocks_path = (
        session_path / f"{srt_path.stem}_{target_language}_final_blocks.json"
    )
    _write_text_atomic(output_path, result.srt_content)
    _write_json_atomic(final_blocks_path, result.block_responses)
    if settings.get("glossary_enabled"):
        save_glossary(session_path, result.glossary)

    file_result = TranslationResult(
        srt_content=result.srt_content,
        block_responses=result.block_responses,
        glossary=result.glossary,
        cost=result.cost,
        response_count=result.response_count,
        output_path=str(output_path),
        cost_sources=result.cost_sources,
        usage=result.usage,
        speaker_by_subtitle=result.speaker_by_subtitle,
    )
    logger.info(
        "Translated subtitles written to %s (%d LLM response(s), cost %.6f).",
        output_path,
        file_result.response_count,
        file_result.cost,
    )
    return file_result


def translate_srt_file(
    session_dir: str | os.PathLike[str],
    srt_file: str | os.PathLike[str],
    settings: dict[str, Any],
    translation_instructions: str = "",
    *,
    glossary: object | None = None,
    completion_func: Callable[..., Any] | None = None,
    speaker_by_subtitle: Mapping[int, str] | None = None,
    progress_callback: ProgressCallback | None = None,
    completed_units: Mapping[str, Mapping[str, Any]] | None = None,
    on_unit_completed: UnitCompletedCallback | None = None,
) -> str:
    return translate_srt_file_with_result(
        session_dir=session_dir,
        srt_file=srt_file,
        settings=settings,
        translation_instructions=translation_instructions,
        glossary=glossary,
        completion_func=completion_func,
        speaker_by_subtitle=speaker_by_subtitle,
        progress_callback=progress_callback,
        completed_units=completed_units,
        on_unit_completed=on_unit_completed,
    ).output_path


def translate_srt_content_deepl(
    srt_content: str,
    settings: dict[str, Any],
    auth_key: str,
    *,
    translator_factory: Callable[[str], Any] | None = None,
    speaker_by_subtitle: Mapping[int, str] | None = None,
    cancel_event: Any | None = None,
    progress_callback: ProgressCallback | None = None,
) -> TranslationResult:
    source_language = str(
        settings.get("original_language")
        or settings.get("stt_language")
        or settings.get("whisper_language")
        or "English"
    )
    target_language = str(settings.get("target_language") or "en")
    char_limit = int(
        settings.get("char_limit")
        or settings.get("llm_char")
        or DEFAULT_LLM_CHAR_LIMIT
    )
    max_subtitles_per_call = max(
        1,
        int(
            settings.get("max_segments_per_batch")
            or settings.get("max_subtitles_per_call")
            or 40
        ),
    )
    translation_blocks = create_translation_blocks(
        srt_content,
        char_limit,
        source_language,
        max_subtitles_per_block=max_subtitles_per_call,
        speaker_by_subtitle=speaker_by_subtitle,
    )
    translated_responses = translate_blocks_deepl(
        translation_blocks,
        source_language,
        target_language,
        auth_key,
        translator_factory=translator_factory,
        cancel_event=cancel_event,
        progress_callback=progress_callback,
    )
    source_segments = parse_srt(srt_content)
    return TranslationResult(
        srt_content=translation_responses_to_srt(
            translated_responses,
            srt_content,
            remove_marked_subtitles=False,
            speaker_by_subtitle=speaker_by_subtitle,
        ),
        block_responses=translated_responses,
        glossary={},
        cost=0.0,
        response_count=len(translated_responses),
        speaker_by_subtitle={
            output_index: speaker
            for output_index, segment in enumerate(source_segments, start=1)
            if (
                speaker := str(
                    (speaker_by_subtitle or {}).get(segment.index)
                    or segment.speaker
                    or ""
                ).strip()
            )
        },
    )


def translate_srt_file_deepl_with_result(
    session_dir: str | os.PathLike[str],
    srt_file: str | os.PathLike[str],
    settings: dict[str, Any],
    *,
    auth_key: str | None = None,
    translator_factory: Callable[[str], Any] | None = None,
    speaker_by_subtitle: Mapping[int, str] | None = None,
    cancel_event: Any | None = None,
    progress_callback: ProgressCallback | None = None,
) -> TranslationResult:
    session_path = Path(session_dir)
    srt_path = Path(srt_file)
    with srt_path.open("r", encoding="utf-8-sig") as handle:
        srt_content = handle.read()

    resolved_auth_key = str(
        auth_key
        or settings.get("deepl_api_key")
        or os.environ.get("DEEPL_API_KEY")
        or ""
    ).strip()
    result = translate_srt_content_deepl(
        srt_content,
        settings,
        resolved_auth_key,
        translator_factory=translator_factory,
        speaker_by_subtitle=speaker_by_subtitle,
        cancel_event=cancel_event,
        progress_callback=progress_callback,
    )

    target_language = str(settings.get("target_language") or "en")
    output_path = session_path / f"{srt_path.stem}_{target_language}.srt"
    final_blocks_path = (
        session_path / f"{srt_path.stem}_{target_language}_final_blocks.json"
    )
    _write_text_atomic(output_path, result.srt_content)
    _write_json_atomic(final_blocks_path, result.block_responses)

    file_result = TranslationResult(
        srt_content=result.srt_content,
        block_responses=result.block_responses,
        glossary=result.glossary,
        cost=result.cost,
        response_count=result.response_count,
        output_path=str(output_path),
        cost_sources=result.cost_sources,
        usage=result.usage,
        speaker_by_subtitle=result.speaker_by_subtitle,
    )
    logger.info(
        "Translated subtitles written to %s (%d DeepL response block(s)).",
        output_path,
        file_result.response_count,
    )
    return file_result


def translate_srt_file_deepl(
    session_dir: str | os.PathLike[str],
    srt_file: str | os.PathLike[str],
    settings: dict[str, Any],
    *,
    auth_key: str | None = None,
    translator_factory: Callable[[str], Any] | None = None,
    speaker_by_subtitle: Mapping[int, str] | None = None,
    cancel_event: Any | None = None,
    progress_callback: ProgressCallback | None = None,
) -> str:
    return translate_srt_file_deepl_with_result(
        session_dir=session_dir,
        srt_file=srt_file,
        settings=settings,
        auth_key=auth_key,
        translator_factory=translator_factory,
        speaker_by_subtitle=speaker_by_subtitle,
        cancel_event=cancel_event,
        progress_callback=progress_callback,
    ).output_path
