"""Previewable, structure-preserving LLM optimization for spoken output."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from threading import Event
from typing import Any, Callable

from pandrator.logic.llm_handler import ChatCompletionResult, chat_completion_with_metadata


DEFAULT_PROMPT = """Rewrite the text so a speech synthesizer pronounces it naturally.
Expand ambiguous numerals and abbreviations when necessary, use phonetic spelling only where it helps,
and remove extraction artifacts. Preserve meaning, language, tone, names, and all factual content.
Keep every input item separate and return the same item indexes.
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
) -> tuple[list[str], OptimizationUsage]:
    """Optimize indexed text units in JSON batches while retaining order and count."""
    prompts = prompt_sequence(settings)
    workers = max(1, min(16, int(settings.get("llm_concurrent_calls") or 1)))
    batch_size = max(1, min(64, int(settings.get("llm_tts_batch_size") or 3)))
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
            result = chat_completion_with_metadata(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You optimize text for speech synthesis without changing meaning. "
                            "Return valid JSON only as {\"items\":[{\"index\":0,\"text\":\"...\"}]}. "
                            "Preserve every supplied index exactly once and never merge or split items."
                        ),
                    },
                    {"role": "user", "content": f"{prompt.rstrip()}\n\nInput JSON:\n{json.dumps(request_payload, ensure_ascii=False)}"},
                ],
                model_name=model_name,
                llm_settings=llm_settings,
            )
            current = _parse_batch_response(result.content, indexes)
            responses.append(result)
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
