"""Previewable, structure-preserving LLM optimization for spoken output."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from threading import Event, Lock
from typing import Any

from pandrator.logic.llm_handler import (
    ChatCompletionResult,
    chat_completion_with_metadata,
)

UnitCompletedCallback = Callable[[str, dict[str, Any]], None]


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

    def merge(self, other: OptimizationUsage) -> None:
        self.response_count += other.response_count
        self.cost += other.cost
        for key, value in other.usage.items():
            self.usage[key] = self.usage.get(key, 0) + int(value)
        self.cost_sources.extend(other.cost_sources)


def optimization_unit_key(indexes: list[int], *, stage: int = 0) -> str:
    digest = hashlib.sha256(
        json.dumps(indexes, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    return f"tts-optimization:{indexes[0]}-{indexes[-1]}:{digest}:stage-{stage}"


def _restore_optimization_unit(
    indexes: list[int],
    *,
    stage: int,
    completed_units: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[dict[int, str], OptimizationUsage] | None:
    if not completed_units:
        return None
    key = optimization_unit_key(indexes, stage=stage)
    raw = completed_units.get(key)
    if raw is None:
        return None
    if [int(value) for value in raw.get("original_indices", [])] != indexes:
        raise ValueError(
            f"Speech-optimization checkpoint {key} has mismatched indexes."
        )
    raw_items = raw.get("items")
    if not isinstance(raw_items, list):
        raise TypeError(f"Speech-optimization checkpoint {key} has no items.")
    restored: dict[int, str] = {}
    for item in raw_items:
        if not isinstance(item, Mapping):
            raise TypeError(
                f"Speech-optimization checkpoint {key} has an invalid item."
            )
        try:
            index = int(item["index"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                f"Speech-optimization checkpoint {key} has an invalid index."
            ) from error
        text = _clean_response(str(item.get("text") or ""))
        if index not in indexes or index in restored or not text:
            raise ValueError(f"Speech-optimization checkpoint {key} has invalid items.")
        restored[index] = text
    if set(restored) != set(indexes):
        raise ValueError(f"Speech-optimization checkpoint {key} is incomplete.")
    raw_usage = raw.get("usage")
    try:
        unit_usage = OptimizationUsage(
            cost=float(raw.get("cost") or 0.0),
            response_count=int(raw.get("response_count") or 0),
            usage=(
                {str(name): int(value or 0) for name, value in raw_usage.items()}
                if isinstance(raw_usage, Mapping)
                else {}
            ),
            cost_sources=[
                str(source)
                for source in raw.get("cost_sources", [])
                if str(source or "")
            ],
        )
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Speech-optimization checkpoint {key} has invalid metrics."
        ) from error
    return restored, unit_usage


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
        raise RuntimeError(
            "LLM speech optimization did not return valid JSON."
        ) from error
    rows = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise TypeError("LLM speech optimization JSON must contain an items list.")
    parsed: dict[int, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("Every optimized item must be a JSON object.")
        try:
            index = int(str(row.get("index")))
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "Every optimized item must retain its numeric index."
            ) from error
        revised = _clean_response(str(row.get("text") or ""))
        if index in parsed or index not in expected_indexes or not revised:
            raise RuntimeError(
                "LLM speech optimization returned missing, duplicate, or unexpected items."
            )
        parsed[index] = revised
    if set(parsed) != set(expected_indexes):
        raise RuntimeError(
            "LLM speech optimization changed the number or identity of text items."
        )
    return parsed


def prompt_sequence(settings: dict[str, Any]) -> list[str]:
    if bool(settings.get("llm_multi_stage")):
        prompts = [
            str(settings.get(key) or "").strip()
            for key in ("first_prompt", "second_prompt", "third_prompt")
        ]
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
    on_plan_batch: Callable[[list[tuple[int, str, dict[str, Any]]]], None]
    | None = None,
    known_pronunciation_resolver: Callable[[str, str], list[dict[str, Any]]]
    | None = None,
    languages: list[str] | None = None,
    voice_languages: list[str] | None = None,
    completed_units: Mapping[str, Mapping[str, Any]] | None = None,
    on_unit_completed: UnitCompletedCallback | None = None,
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
            completed_units=completed_units,
            on_unit_completed=on_unit_completed,
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
    batches = [
        populated[index : index + batch_size]
        for index in range(0, len(populated), batch_size)
    ]
    if not populated:
        progress(1.0, "No non-empty text units require speech optimization")
        return output, usage

    total_requests = len(batches) * len(prompts)
    completed_requests = 0
    progress_lock = Lock()
    progress(
        0.0,
        (
            f"Preparing {total_requests} speech optimization "
            f"request{'s' if total_requests != 1 else ''}"
        ),
    )

    def report_completed_request(batch_length: int) -> None:
        nonlocal completed_requests
        # Worker completions can arrive concurrently. Serialize both the
        # counter and callback so persisted job progress never moves backward.
        with progress_lock:
            completed_requests += 1
            progress(
                completed_requests / total_requests,
                (
                    f"Completed speech optimization request "
                    f"{completed_requests} of {total_requests} "
                    f"for {batch_length} text unit"
                    f"{'s' if batch_length != 1 else ''}"
                ),
            )

    checkpoint_lock = Lock()

    def process(
        batch: list[tuple[int, str]],
    ) -> tuple[list[tuple[int, str]], OptimizationUsage]:
        current = {index: original for index, original in batch}
        batch_usage = OptimizationUsage()
        for prompt_index, prompt in enumerate(prompts):
            if cancel_event.is_set():
                return list(current.items()), batch_usage
            indexes = list(current)
            restored = _restore_optimization_unit(
                indexes,
                stage=prompt_index,
                completed_units=completed_units,
            )
            if restored is not None:
                current, unit_usage = restored
                batch_usage.merge(unit_usage)
                report_completed_request(len(batch))
                continue
            request_payload = {
                "items": [{"index": index, "text": current[index]} for index in indexes]
            }
            last_error: RuntimeError | None = None
            unit_usage = OptimizationUsage()
            for attempt in range(1, structured_attempts + 1):
                if cancel_event.is_set():
                    return list(current.items()), batch_usage
                system_prompt = (
                    "You optimize text for speech synthesis without changing meaning. "
                    'Return valid JSON only as {"items":[{"index":0,"text":"..."}]}. '
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
                unit_usage.add(result)
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
                if on_unit_completed is not None:
                    payload = {
                        "version": 1,
                        "kind": "tts_optimization",
                        "stage": prompt_index,
                        "original_indices": indexes,
                        "items": [
                            {"index": index, "text": current[index]}
                            for index in indexes
                        ],
                        "cost": unit_usage.cost,
                        "response_count": unit_usage.response_count,
                        "usage": unit_usage.usage,
                        "cost_sources": unit_usage.cost_sources,
                    }
                    with checkpoint_lock:
                        on_unit_completed(
                            optimization_unit_key(indexes, stage=prompt_index),
                            payload,
                        )
                batch_usage.merge(unit_usage)
                report_completed_request(len(batch))
                break
        return list(current.items()), batch_usage

    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="tts-optimize"
    ) as executor:
        futures = {executor.submit(process, batch): batch for batch in batches}
        for future in as_completed(futures):
            revised_items, batch_usage = future.result()
            for index, revised in revised_items:
                output[index] = revised
            usage.merge(batch_usage)
            if on_batch:
                on_batch(revised_items)
    if not cancel_event.is_set():
        progress(1.0, f"Optimized {len(populated)} of {len(populated)} text units")
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
    completed_units: Mapping[str, Mapping[str, Any]] | None,
    on_unit_completed: UnitCompletedCallback | None,
) -> tuple[list[str], OptimizationUsage]:
    """Plan stable units in optional transport batches with per-unit validation."""
    from .speech_planning import (
        SPEECH_PROMPT_REVISION,
        plan_speech_text,
        plan_speech_text_batch,
    )

    workers = max(1, min(16, int(settings.get("llm_concurrent_calls") or 1)))
    batch_size = max(1, min(64, int(settings.get("llm_tts_batch_size") or 1)))
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
    if not populated:
        progress(1.0, "No non-empty text units require speech planning")
        return output, usage
    progress(
        0.0,
        f"Preparing speech plans for {len(populated)} text units",
    )
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

    checkpoint_lock = Lock()

    def restore(index: int):
        unit_key = optimization_unit_key([index], stage=0)
        raw = completed_units.get(unit_key, {}) if completed_units else {}
        raw_plan = raw.get("plan") if isinstance(raw, Mapping) else None
        restored = (
            _restore_optimization_unit(
                [index],
                stage=0,
                completed_units=completed_units,
            )
            if isinstance(raw_plan, Mapping)
            and raw_plan.get("prompt_revision") == SPEECH_PROMPT_REVISION
            else None
        )
        if restored is None:
            return None
        restored_items, restored_usage = restored
        return (
            index,
            restored_items[index],
            dict(raw_plan),
            restored_usage,
        )

    def context(index: int, text: str) -> dict[str, Any]:
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
        return {
            "text": text,
            "language": language,
            "voice_language": voice_language,
            "known_pronunciations": known,
        }

    def checkpoint(
        index: int,
        revised: str,
        plan: dict[str, Any],
        unit_usage: OptimizationUsage,
    ) -> None:
        if on_unit_completed is None:
            return
        payload = {
            "version": 1,
            "kind": "tts_optimization_plan",
            "stage": 0,
            "original_indices": [index],
            "items": [{"index": index, "text": revised}],
            "plan": plan,
            "cost": unit_usage.cost,
            "response_count": unit_usage.response_count,
            "usage": unit_usage.usage,
            "cost_sources": unit_usage.cost_sources,
        }
        with checkpoint_lock:
            on_unit_completed(optimization_unit_key([index], stage=0), payload)

    def process(batch: list[tuple[int, str]]):
        if cancel_event.is_set():
            return [
                (index, text, {}, OptimizationUsage()) for index, text in batch
            ], OptimizationUsage()
        completed_items: list[tuple[int, str, dict[str, Any], OptimizationUsage]] = []
        pending: list[tuple[int, str]] = []
        batch_usage = OptimizationUsage()
        for index, text in batch:
            restored = restore(index)
            if restored is None:
                pending.append((index, text))
            else:
                completed_items.append(restored)
                batch_usage.merge(restored[3])
        if not pending:
            return completed_items, batch_usage

        contexts = [context(index, text) for index, text in pending]
        if len(contexts) == 1:
            result = plan_speech_text(
                contexts[0]["text"],
                language=contexts[0]["language"],
                voice_language=contexts[0]["voice_language"],
                mode=mode,
                model_name=model_name,
                llm_settings=llm_settings,
                known_pronunciations=contexts[0]["known_pronunciations"],
                cancel_event=cancel_event,
                min_retention=float(settings.get("speech_plan_min_retention") or 0.9),
                max_attempts_per_mode=structured_attempts,
            )
            results = [result]
            responses = result.responses
        else:
            planned = plan_speech_text_batch(
                contexts,
                mode=mode,
                model_name=model_name,
                llm_settings=llm_settings,
                cancel_event=cancel_event,
                min_retention=float(settings.get("speech_plan_min_retention") or 0.9),
                max_attempts_per_mode=structured_attempts,
            )
            results = planned.results
            responses = planned.responses
        request_usage = OptimizationUsage()
        for response in responses:
            request_usage.add(response)
        batch_usage.merge(request_usage)
        for offset, ((index, _text), result) in enumerate(
            zip(pending, results, strict=True)
        ):
            # Shared batch usage is stored once so checkpoint restoration neither
            # loses nor multiplies provider cost/token accounting.
            checkpoint_usage = request_usage if offset == 0 else OptimizationUsage()
            checkpoint(index, result.text, result.plan, checkpoint_usage)
            completed_items.append((index, result.text, result.plan, checkpoint_usage))
        completed_items.sort(key=lambda item: item[0])
        return completed_items, batch_usage

    completed = 0
    batches = [
        populated[offset : offset + batch_size]
        for offset in range(0, len(populated), batch_size)
    ]
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="speech-plan",
    ) as executor:
        futures = {executor.submit(process, batch): batch for batch in batches}
        for future in as_completed(futures):
            revised_items, batch_usage = future.result()
            public_items = [
                (index, revised) for index, revised, _plan, _usage in revised_items
            ]
            plan_items = [
                (index, revised, plan) for index, revised, plan, _usage in revised_items
            ]
            for index, revised in public_items:
                output[index] = revised
            usage.merge(batch_usage)
            if on_batch:
                on_batch(public_items)
            if on_plan_batch:
                on_plan_batch(plan_items)
            completed += len(revised_items)
            progress(
                completed / max(1, len(populated)),
                f"Planned speech for {completed} of {len(populated)} text units",
            )
    return output, usage
