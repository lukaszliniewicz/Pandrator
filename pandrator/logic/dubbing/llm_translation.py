"""LLM-backed subtitle translation using Pandrator provider settings."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import llm_handler
from .llm_config import resolve_dubbing_llm_settings
from .llm_correction import DEFAULT_LLM_CHAR_LIMIT, extract_json_payload
from .models import SubtitleSegment
from .srt_utils import compose_srt, create_translation_blocks, parse_srt

logger = logging.getLogger(__name__)

DEEPL_MAX_REQUEST_BYTES = 120 * 1024
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
1. You will receive an array of numbered subtitles in JSON format. Each subtitle has a "number" and "text" field.
2. Translate the "text" of each subtitle.
3. You MUST preserve the "number" field exactly as it is for each subtitle.
4. You MUST return EXACTLY {subtitle_count} subtitles in the EXACT SAME numbered format.
5. If a subtitle should be removed (e.g., it contains only filler words or you are confident it is a hallucination of the STT model), replace its text with "[REMOVE]".
6. Spell out numbers, especially Roman numerals, dates, amounts etc.
7. It is ok for a subtitle to not end in punctuation if the following subtitle continues the sentence/thought. You don't have to add "..." - in fact, don't do it.
8. Choose concise translations suitable for dubbing while maintaining accuracy, grammatical correctness in the target language and the tone of the source.
9. Use correct punctuation that enhances a natural flow of speech for optimal speech generation.
10. Do not add ANY comments, confirmations, explanations, or questions. Output only the translation formatted like the original JSON array.
11. Before outputting your answer, validate its formatting. Return EXACTLY {subtitle_count} subtitles with the same structure as the input.
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


def get_deepl_language_code(language: str) -> str:
    normalized = str(language or "").strip()
    if not normalized:
        return ""
    return DEEPL_LANGUAGE_MAP.get(normalized.lower(), normalized.upper() if len(normalized) <= 5 else normalized)


def _build_deepl_translator(auth_key: str) -> Any:
    if not str(auth_key or "").strip():
        raise ValueError("DeepL translation requires DEEPL_API_KEY.")

    try:
        import deepl
    except ImportError as error:  # pragma: no cover - depends on runtime environment
        raise RuntimeError("DeepL translation requires the 'deepl' package to be installed.") from error

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
) -> list[dict[str, Any]]:
    del source_language  # DeepL can auto-detect here; keep the argument for provider parity.
    translator = translator_factory(auth_key) if translator_factory is not None else _build_deepl_translator(auth_key)
    target_code = get_deepl_language_code(target_language)
    request_texts = _split_deepl_request_texts(translation_blocks)

    translated_parts: list[str] = []
    for request_text in request_texts:
        result = translator.translate_text(request_text, target_lang=target_code)
        translated_parts.append(str(getattr(result, "text", result) or ""))

    translated_units = [part.strip() for part in "\n\n".join(translated_parts).split("\n\n")]
    expected_count = sum(len(block) for block in translation_blocks)
    if len(translated_units) != expected_count:
        raise ValueError(
            f"DeepL response count mismatch: expected {expected_count}, got {len(translated_units)}."
        )

    translated_responses: list[dict[str, Any]] = []
    translated_index = 0
    for block in translation_blocks:
        block_translations = translated_units[translated_index: translated_index + len(block)]
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


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
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


def save_glossary(session_dir: str | os.PathLike[str], glossary: dict[str, str]) -> None:
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
    expected_count: int,
) -> tuple[list[str], dict[str, str]]:
    translation_text, _separator, glossary_text = str(response_text or "").partition("[GLOSSARY]")
    payload = extract_json_payload(translation_text)
    if not isinstance(payload, list):
        raise ValueError("Translation response must be a JSON array.")
    if len(payload) != expected_count:
        raise ValueError(f"Translation response count mismatch: expected {expected_count}, got {len(payload)}.")

    translations: list[str] = []
    for item in payload:
        if not isinstance(item, dict) or "number" not in item or "text" not in item:
            raise ValueError("Translation response items must contain 'number' and 'text'.")
        translations.append(str(item["text"]).strip())

    return translations, parse_glossary_entries(glossary_text)


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
) -> str:
    prompt_template = TRANSLATION_PROMPT_TEMPLATE
    if no_remove_subtitles:
        prompt_template = prompt_template.replace(
            '5. If a subtitle should be removed (e.g., it contains only filler words or you are confident it is a hallucination of the STT model), replace its text with "[REMOVE]".',
            "5. You MUST NOT remove any subtitles. Translate every subtitle, even if it contains filler words.",
        )

    prompt = prompt_template.format(
        source_lang=source_language,
        target_lang=target_language,
        subtitle_count=len(block),
    )
    if translation_instructions:
        prompt += f"\n\nAdditional context and instructions:\n{translation_instructions}"

    if glossary is not None:
        prompt += "\n\n" + GLOSSARY_INSTRUCTIONS_TRANSLATION.format(
            glossary=json.dumps(glossary, ensure_ascii=False, indent=2)
        )

    if previous_response:
        prompt += "\n" + CONTEXT_PROMPT_TEMPLATE.format(context_previous_response=previous_response)

    if next_block:
        next_subtitles = next_block[:2]
        if next_subtitles:
            next_text = "\n".join(f'- "{subtitle["text"]}"' for subtitle in next_subtitles)
            prompt += (
                f"\n\nFor additional context, here are the next {len(next_subtitles)} subtitle(s) from the following block. "
                "DO NOT TRANSLATE THEM. They are only provided to help with continuity.\n"
                f"{next_text}"
            )

    subtitles = json.dumps(
        [
            {"number": index + 1, "text": subtitle.get("text") or ""}
            for index, subtitle in enumerate(block)
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
    normalized = llm_handler.normalize_usage_tokens(raw if isinstance(raw, dict) else {})
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_prompt_tokens", "uncached_prompt_tokens"):
        totals[key] = int(totals.get(key) or 0) + int(normalized.get(key) or 0)


def translation_responses_to_srt(
    translated_responses: list[dict[str, Any]],
    original_srt: str,
    *,
    remove_marked_subtitles: bool = True,
) -> str:
    original_segments = parse_srt(original_srt)
    segments_by_index = {segment.index: segment for segment in original_segments}
    translated_segments: list[SubtitleSegment] = []

    for response in translated_responses:
        translations = response.get("translation", [])
        indices = response.get("original_indices", [])
        for translated_text, original_index in zip(translations, indices):
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
) -> TranslationResult:
    source_language = str(
        settings.get("original_language")
        or settings.get("stt_language")
        or settings.get("whisper_language")
        or "English"
    )
    target_language = str(settings.get("target_language") or "en")
    char_limit = int(settings.get("llm_char") or DEFAULT_LLM_CHAR_LIMIT)
    max_subtitles_per_call = max(1, int(settings.get("max_subtitles_per_call") or 40))
    use_context = bool(settings.get("context", True))
    no_remove_subtitles = bool(settings.get("no_remove_subtitles", False))
    use_glossary = bool(settings.get("glossary_enabled", False))
    active_glossary = dict(glossary or {}) if use_glossary else {}

    blocks = create_translation_blocks(
        srt_content,
        char_limit,
        source_language,
        max_subtitles_per_block=max_subtitles_per_call,
    )
    if not blocks:
        return TranslationResult("", [], active_glossary, cost=0.0, response_count=0)

    resolved = resolve_dubbing_llm_settings(settings, stage="translation")
    completion = completion_func or llm_handler.chat_completion_with_metadata
    previous_response = ""
    translated_responses: list[dict[str, Any]] = []
    total_cost = 0.0
    cost_sources: list[str] = []
    usage: dict[str, Any] = {}

    for index, block in enumerate(blocks):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("LLM translation was canceled.")
        prompt = build_translation_prompt(
            block,
            source_language=source_language,
            target_language=target_language,
            translation_instructions=translation_instructions,
            glossary=active_glossary if use_glossary else None,
            previous_response=previous_response if use_context else "",
            next_block=blocks[index + 1] if use_context and index < len(blocks) - 1 else None,
            no_remove_subtitles=no_remove_subtitles,
        )
        completion_kwargs = {
            "messages": [{"role": "user", "content": prompt}],
            "model_name": resolved.model_name,
            "llm_settings": resolved.llm_settings,
        }
        if completion_func is None:
            completion_kwargs["cancel_event"] = cancel_event
        result = completion(**completion_kwargs)
        content, cost, cost_source = _coerce_completion_content_and_cost(result)
        _merge_completion_usage(usage, result)
        if not content:
            raise ValueError("LLM translation returned an empty response.")

        translated_texts, new_glossary = parse_translation_response(content, len(block))
        if use_glossary:
            active_glossary.update(new_glossary)

        translated_responses.append(
            {
                "translation": translated_texts,
                "new_glossary": "\n".join(f"{key} = {value}" for key, value in new_glossary.items()),
                "original_indices": [subtitle["index"] for subtitle in block],
            }
        )
        previous_response = content
        total_cost += cost
        if cost_source and cost_source not in cost_sources:
            cost_sources.append(cost_source)

    return TranslationResult(
        srt_content=translation_responses_to_srt(translated_responses, srt_content),
        block_responses=translated_responses,
        glossary=active_glossary,
        cost=total_cost,
        response_count=len(translated_responses),
        cost_sources=tuple(cost_sources),
        usage=usage,
    )


def translate_srt_file_with_result(
    session_dir: str | os.PathLike[str],
    srt_file: str | os.PathLike[str],
    settings: dict[str, Any],
    translation_instructions: str = "",
    *,
    completion_func: Callable[..., Any] | None = None,
    cancel_event: Any | None = None,
) -> TranslationResult:
    session_path = Path(session_dir)
    srt_path = Path(srt_file)
    with srt_path.open("r", encoding="utf-8-sig") as handle:
        srt_content = handle.read()

    glossary = load_glossary(session_path) if settings.get("glossary_enabled") else {}
    result = translate_srt_content(
        srt_content,
        settings,
        translation_instructions=translation_instructions,
        glossary=glossary,
        completion_func=completion_func,
        cancel_event=cancel_event,
    )

    target_language = str(settings.get("target_language") or "en")
    output_path = session_path / f"{srt_path.stem}_{target_language}.srt"
    final_blocks_path = session_path / f"{srt_path.stem}_{target_language}_final_blocks.json"
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
    completion_func: Callable[..., Any] | None = None,
) -> str:
    return translate_srt_file_with_result(
        session_dir=session_dir,
        srt_file=srt_file,
        settings=settings,
        translation_instructions=translation_instructions,
        completion_func=completion_func,
    ).output_path


def translate_srt_content_deepl(
    srt_content: str,
    settings: dict[str, Any],
    auth_key: str,
    *,
    translator_factory: Callable[[str], Any] | None = None,
) -> TranslationResult:
    source_language = str(
        settings.get("original_language")
        or settings.get("stt_language")
        or settings.get("whisper_language")
        or "English"
    )
    target_language = str(settings.get("target_language") or "en")
    char_limit = int(settings.get("llm_char") or DEFAULT_LLM_CHAR_LIMIT)
    max_subtitles_per_call = max(1, int(settings.get("max_subtitles_per_call") or 40))
    translation_blocks = create_translation_blocks(
        srt_content,
        char_limit,
        source_language,
        max_subtitles_per_block=max_subtitles_per_call,
    )
    translated_responses = translate_blocks_deepl(
        translation_blocks,
        source_language,
        target_language,
        auth_key,
        translator_factory=translator_factory,
    )
    return TranslationResult(
        srt_content=translation_responses_to_srt(
            translated_responses,
            srt_content,
            remove_marked_subtitles=False,
        ),
        block_responses=translated_responses,
        glossary={},
        cost=0.0,
        response_count=len(translated_responses),
    )


def translate_srt_file_deepl_with_result(
    session_dir: str | os.PathLike[str],
    srt_file: str | os.PathLike[str],
    settings: dict[str, Any],
    *,
    auth_key: str | None = None,
    translator_factory: Callable[[str], Any] | None = None,
) -> TranslationResult:
    session_path = Path(session_dir)
    srt_path = Path(srt_file)
    with srt_path.open("r", encoding="utf-8-sig") as handle:
        srt_content = handle.read()

    resolved_auth_key = str(auth_key or settings.get("deepl_api_key") or os.environ.get("DEEPL_API_KEY") or "").strip()
    result = translate_srt_content_deepl(
        srt_content,
        settings,
        resolved_auth_key,
        translator_factory=translator_factory,
    )

    target_language = str(settings.get("target_language") or "en")
    output_path = session_path / f"{srt_path.stem}_{target_language}.srt"
    final_blocks_path = session_path / f"{srt_path.stem}_{target_language}_final_blocks.json"
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
) -> str:
    return translate_srt_file_deepl_with_result(
        session_dir=session_dir,
        srt_file=srt_file,
        settings=settings,
        auth_key=auth_key,
        translator_factory=translator_factory,
    ).output_path
