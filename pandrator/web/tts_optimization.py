"""Previewable, structure-preserving LLM optimization for spoken output."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from threading import Event
from typing import Any, Callable

from pandrator.logic.llm_handler import ChatCompletionResult, chat_completion_with_metadata


# Restored from the Qt application's LLM defaults. The legacy prompts' plain-text
# response clauses are intentionally left to the structured JSON system prompt below.
DEFAULT_PROMPT = """Your task is to preprocess and clean each supplied text item to optimize it for text-to-speech (TTS) synthesis.

Please perform the following adjustments:
1. Spell out abbreviations and titles (e.g., Prof. to Professor, Dr. to Doctor, et. al. to et alia, etc. to et cetera).
2. Convert Roman numerals to English words (e.g., Section III to Section Three, Chapter V to Chapter Five).
3. Correct any punctuation errors, misspelled words, or OCR artifacts (e.g., remove out-of-place page numbers).
4. Spell difficult foreign, non-English words phonetically so that an English TTS voice can pronounce them naturally.

Don't change anything else. Return each complete processed text item, leaving it unchanged if no adjustments are necessary.
"""

DEFAULT_FIRST_PROMPT = """Your task is to spell out abbreviations and titles and convert Roman numerals to English words in each supplied text item. For example: Prof. to Professor, Dr. to Doctor, et. al. to et alia, etc. to et cetera, Section III to Section Three, Chapter V to Chapter Five and so on. Don't change ANYTHING ELSE. If no adjustments are necessary, leave the text item unchanged.
"""

DEFAULT_SECOND_PROMPT = """Your task is to analyze each supplied text item carefully and correct punctuation. Also, correct any misspelled words and possible OCR artifacts based on context. If there is a number that looks out of place because it could have been a page number captured by OCR and doesn't fit in the context, remove it. Don't change ANYTHING ELSE, including when no changes are necessary.
"""

DEFAULT_THIRD_PROMPT = """Your task is to spell difficult FOREIGN, NON-ENGLISH words phonetically. Don't alter ANYTHING ELSE in each supplied text item: English words remain the same. Example: Jiyu means freedom in Japanese becomes jeeyou means freedom in Japanese; jiyu is spelled phonetically as a Japanese word, and the rest is not changed.
"""


@dataclass(slots=True)
class OptimizationUsage:
    cost: float = 0.0
    response_count: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    cost_sources: list[str] = field(default_factory=list)

    def add(self, result: ChatCompletionResult) -> None:
        self.response_count += 1
        self.cost += float(result.cost or 0.0)
        for key, value in (result.usage or {}).items():
            if isinstance(value, (int, float)):
                self.usage[key] = self.usage.get(key, 0) + int(value)
        if result.cost_source:
            self.cost_sources.append(result.cost_source)


def _clean_response(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^```(?:text)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    return " ".join(text.split())


def _parse_batch_response(value: str, expected_indexes: list[int]) -> dict[int, str]:
    text = str(value or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        # Keep compatibility with older local endpoints that ignored JSON
        # instructions for a single item. Multi-item calls must be structured
        # because otherwise segment identity cannot be proven.
        if len(expected_indexes) == 1:
            cleaned = _clean_response(text)
            if cleaned:
                return {expected_indexes[0]: cleaned}
        raise RuntimeError("LLM speech optimization did not return valid JSON.") from error
    rows = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise RuntimeError("LLM speech optimization JSON must contain an items list.")
    parsed: dict[int, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Every optimized item must be a JSON object.")
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError) as error:
            raise RuntimeError("Every optimized item must retain its numeric index.") from error
        revised = _clean_response(str(row.get("text") or ""))
        if index in parsed or index not in expected_indexes or not revised:
            raise RuntimeError("LLM speech optimization returned missing, duplicate, or unexpected items.")
        parsed[index] = revised
    if set(parsed) != set(expected_indexes):
        raise RuntimeError("LLM speech optimization changed the number or identity of text items.")
    return parsed


def prompt_sequence(settings: dict[str, Any]) -> list[str]:
    if bool(settings.get("llm_multi_stage")):
        prompts = [str(settings.get(key) or "").strip() for key in ("first_prompt", "second_prompt", "third_prompt")]
        prompts = [prompt for prompt in prompts if prompt]
        if prompts:
            return prompts
    return [str(settings.get("combined_prompt") or "").strip() or DEFAULT_PROMPT]


def optimize_texts(
    texts: list[str],
    settings: dict[str, Any],
    llm_settings: Any,
    model_name: str,
    cancel_event: Event,
    progress: Callable[[float, str | None], None],
    *,
    on_batch: Callable[[list[tuple[int, str]]], None] | None = None,
    on_plan_batch: Callable[[list[tuple[int, str, dict[str, Any]]]], None] | None = None,
    known_pronunciation_resolver: Callable[[str, str], list[dict[str, Any]]] | None = None,
    languages: list[str] | None = None,
    voice_languages: list[str] | None = None,
) -> tuple[list[str], OptimizationUsage]:
    """Optimize indexed text units in JSON batches while retaining order and count."""
    speech_mode = str(settings.get("speech_optimization_mode") or "").strip().lower()
    if speech_mode in {"guarded", "flexible"}:
        return _optimize_with_speech_plans(
            texts,
            settings,
            llm_settings,
            model_name,
            cancel_event,
            progress,
            mode=speech_mode,
            on_batch=on_batch,
            on_plan_batch=on_plan_batch,
            known_pronunciation_resolver=known_pronunciation_resolver,
            languages=languages,
            voice_languages=voice_languages,
        )
    prompts = prompt_sequence(settings)
    workers = max(1, min(16, int(settings.get("llm_concurrent_calls") or 1)))
    batch_size = max(1, min(64, int(settings.get("llm_tts_batch_size") or 3)))
    try:
        structured_attempts = max(
            1,
            min(
                5,
                int(
                    settings.get("tts_structured_max_attempts")
                    or settings.get("llm_structured_max_attempts")
                    or 3
                ),
            ),
        )
    except (TypeError, ValueError):
        structured_attempts = 3
    output = list(texts)
    usage = OptimizationUsage()
    populated = [(index, text) for index, text in enumerate(texts) if text.strip()]
    batches = [populated[index:index + batch_size] for index in range(0, len(populated), batch_size)]

    def process(batch: list[tuple[int, str]]) -> tuple[list[tuple[int, str]], list[ChatCompletionResult]]:
        current = {index: original for index, original in batch}
        responses: list[ChatCompletionResult] = []
        for prompt in prompts:
            if cancel_event.is_set():
                return list(current.items()), responses
            indexes = list(current)
            request_payload = {"items": [{"index": index, "text": current[index]} for index in indexes]}
            last_error: RuntimeError | None = None
            for attempt in range(1, structured_attempts + 1):
                if cancel_event.is_set():
                    return list(current.items()), responses
                system_prompt = (
                    "You optimize text for speech synthesis without changing meaning. "
                    "Return valid JSON only as {\"items\":[{\"index\":0,\"text\":\"...\"}]}. "
                    "Preserve every supplied index exactly once and never merge or split items."
                )
                if last_error is not None:
                    system_prompt += (
                        " Your previous response was rejected because "
                        f"{last_error}. Return a complete corrected response with every "
                        "required index and no explanation."
                    )
                result = chat_completion_with_metadata(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": (
                                f"{prompt.rstrip()}\n\nInput JSON:\n"
                                f"{json.dumps(request_payload, ensure_ascii=False)}"
                            ),
                        },
                    ],
                    model_name=model_name,
                    llm_settings=llm_settings,
                    cancel_event=cancel_event,
                )
                responses.append(result)
                try:
                    current = _parse_batch_response(result.content, indexes)
                except RuntimeError as error:
                    last_error = error
                    if attempt >= structured_attempts:
                        raise RuntimeError(
                            "LLM speech optimization failed structured validation "
                            f"after {structured_attempts} attempts: {error}"
                        ) from error
                    continue
                break
        return list(current.items()), responses

    completed = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tts-optimize") as executor:
        futures = {executor.submit(process, batch): batch for batch in batches}
        for future in as_completed(futures):
            revised_items, responses = future.result()
            for index, revised in revised_items:
                output[index] = revised
            for response in responses:
                usage.add(response)
            if on_batch:
                on_batch(revised_items)
            completed += len(revised_items)
            progress(completed / max(1, len(populated)), f"Optimized {completed} of {len(populated)} text units")
    return output, usage


def _optimize_with_speech_plans(
    texts: list[str],
    settings: dict[str, Any],
    llm_settings: Any,
    model_name: str,
    cancel_event: Event,
    progress: Callable[[float, str | None], None],
    *,
    mode: str,
    on_batch: Callable[[list[tuple[int, str]]], None] | None,
    on_plan_batch: Callable[[list[tuple[int, str, dict[str, Any]]]], None] | None,
    known_pronunciation_resolver: Callable[[str, str], list[dict[str, Any]]] | None,
    languages: list[str] | None,
    voice_languages: list[str] | None,
) -> tuple[list[str], OptimizationUsage]:
    """Plan one stable sentence per call and compile only validated responses."""
    from .speech_planning import plan_speech_text

    workers = max(1, min(16, int(settings.get("llm_concurrent_calls") or 1)))
    default_language = str(
        settings.get("language")
        or settings.get("target_language")
        or settings.get("source_language")
        or "en"
    )
    default_voice_language = str(
        settings.get("voice_language") or settings.get("language") or default_language
    )
    output = list(texts)
    usage = OptimizationUsage()
    populated = [(index, text) for index, text in enumerate(texts) if text.strip()]
    try:
        structured_attempts = max(
            1,
            min(
                5,
                int(settings.get("speech_plan_structured_max_attempts") or 2),
            ),
        )
    except (TypeError, ValueError):
        structured_attempts = 2

    def process(index: int, text: str):
        if cancel_event.is_set():
            return index, text, {}, []
        language = (
            str(languages[index] or default_language)
            if languages and index < len(languages)
            else default_language
        )
        voice_language = (
            str(voice_languages[index] or default_voice_language)
            if voice_languages and index < len(voice_languages)
            else default_voice_language
        )
        known = (
            known_pronunciation_resolver(text, language)
            if known_pronunciation_resolver is not None
            else []
        )
        result = plan_speech_text(
            text,
            language=language,
            voice_language=voice_language,
            mode=mode,
            model_name=model_name,
            llm_settings=llm_settings,
            known_pronunciations=known,
            cancel_event=cancel_event,
            min_retention=float(settings.get("speech_plan_min_retention") or 0.9),
            max_attempts_per_mode=structured_attempts,
        )
        return index, result.text, result.plan, result.responses

    completed = 0
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="speech-plan",
    ) as executor:
        futures = {
            executor.submit(process, index, text): index for index, text in populated
        }
        for future in as_completed(futures):
            index, revised, plan, responses = future.result()
            output[index] = revised
            for response in responses:
                usage.add(response)
            if on_batch:
                on_batch([(index, revised)])
            if on_plan_batch:
                on_plan_batch([(index, revised, plan)])
            completed += 1
            progress(
                completed / max(1, len(populated)),
                f"Planned speech for {completed} of {len(populated)} text units",
            )
    return output, usage
