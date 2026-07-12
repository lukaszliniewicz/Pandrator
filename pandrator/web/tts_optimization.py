"""Previewable, structure-preserving LLM optimization for spoken output."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from threading import Event
from typing import Any, Callable

from pandrator.logic.llm_handler import ChatCompletionResult, chat_completion_with_metadata


DEFAULT_PROMPT = """Rewrite the text so a speech synthesizer pronounces it naturally.
Expand ambiguous numerals and abbreviations when necessary, use phonetic spelling only where it helps,
and remove extraction artifacts. Preserve meaning, language, tone, names, and all factual content.
Return only the rewritten text, without commentary or quotation marks.

Text:
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
) -> tuple[list[str], OptimizationUsage]:
    """Optimize independent text units while retaining their exact order and count."""
    prompts = prompt_sequence(settings)
    workers = max(1, min(16, int(settings.get("llm_concurrent_calls") or 1)))
    output = list(texts)
    usage = OptimizationUsage()

    def process(index: int, original: str) -> tuple[int, str, list[ChatCompletionResult]]:
        current = original
        responses: list[ChatCompletionResult] = []
        for prompt in prompts:
            if cancel_event.is_set():
                return index, current, responses
            result = chat_completion_with_metadata(
                messages=[
                    {"role": "system", "content": "You optimize text for speech synthesis without changing its meaning."},
                    {"role": "user", "content": f"{prompt.rstrip()}\n\n{current}"},
                ],
                model_name=model_name,
                llm_settings=llm_settings,
            )
            revised = _clean_response(result.content)
            if not revised:
                raise RuntimeError(f"LLM speech optimization returned an empty response for item {index + 1}.")
            current = revised
            responses.append(result)
        return index, current, responses

    completed = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tts-optimize") as executor:
        futures = {
            executor.submit(process, index, text): index
            for index, text in enumerate(texts)
            if text.strip()
        }
        for future in as_completed(futures):
            index, revised, responses = future.result()
            output[index] = revised
            for response in responses:
                usage.add(response)
            completed += 1
            progress(completed / max(1, len(futures)), f"Optimized {completed} of {len(futures)} text units")
    return output, usage
