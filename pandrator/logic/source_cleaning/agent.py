from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .. import llm_handler
from .models import SourceDocument
from .prompts import build_initial_user_prompt, build_system_prompt
from .tools import SourceCleaningTools


CompletionFunc = Callable[..., str]
ProgressCallback = Callable[[str], None]


@dataclass
class SourceCleaningAgentConfig:
    model_name: str = "default"
    max_iterations: int = 14
    max_tool_result_chars: int = 12000
    max_tokens: int = 2200
    temperature: float = 0.2
    remove_footnotes: bool = False


@dataclass
class SourceCleaningAgentResult:
    operations: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    confidence: float = 0.0
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    raw_final_command: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    iterations: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_source_cleaning_agent(
    document: SourceDocument,
    llm_settings: Any | None = None,
    config: SourceCleaningAgentConfig | None = None,
    completion_func: CompletionFunc | None = None,
    progress_callback: ProgressCallback | None = None,
) -> SourceCleaningAgentResult:
    """Runs a JSON-command LLM loop over deterministic source-cleaning tools."""
    resolved_config = config or SourceCleaningAgentConfig()
    completion = completion_func or llm_handler.chat_completion
    tools = SourceCleaningTools(document)
    result = SourceCleaningAgentResult()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(resolved_config.remove_footnotes)},
        {"role": "user", "content": build_initial_user_prompt(document)},
    ]

    for iteration in range(1, resolved_config.max_iterations + 1):
        result.iterations = iteration
        _emit(progress_callback, f"Source cleaning LLM step {iteration}/{resolved_config.max_iterations}...")
        response = completion(
            messages=messages,
            model_name=resolved_config.model_name,
            llm_settings=llm_settings,
            max_tokens=resolved_config.max_tokens,
            temperature=resolved_config.temperature,
        )
        if not response:
            result.warnings.append("LLM returned an empty response.")
            break

        command, parse_error = parse_json_command(response)
        messages.append({"role": "assistant", "content": response})
        if parse_error:
            result.warnings.append(parse_error)
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your previous response was not valid JSON. "
                        "Return exactly one JSON object using an allowed action."
                    ),
                }
            )
            continue

        action = str(command.get("action") or "").strip()
        if action in {"finish", "propose_operations"}:
            result.operations = [
                operation
                for operation in command.get("operations", [])
                if isinstance(operation, dict)
            ]
            result.summary = str(command.get("summary") or "").strip()
            result.confidence = _coerce_confidence(command.get("confidence"))
            result.raw_final_command = command
            _emit(progress_callback, "Source cleaning LLM proposed operations.")
            return result

        observation = _execute_tool_action(tools, command)
        trace_item = {
            "iteration": iteration,
            "action": action,
            "arguments": command.get("arguments") if isinstance(command.get("arguments"), dict) else {},
            "observation": observation,
        }
        result.tool_trace.append(trace_item)
        messages.append(
            {
                "role": "user",
                "content": (
                    "Tool result:\n"
                    + _json_dumps_truncated(observation, resolved_config.max_tool_result_chars)
                    + "\n\nChoose the next inspection command or finish with operations."
                ),
            }
        )

    if not result.summary:
        result.summary = "The source-cleaning agent stopped before proposing operations."
    if not result.warnings:
        result.warnings.append("The source-cleaning agent reached its iteration limit.")
    return result


def parse_json_command(response: str) -> tuple[dict[str, Any], str]:
    raw = str(response or "").strip()
    if not raw:
        return {}, "Empty LLM response."

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    elif not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        logging.debug("Could not parse source-cleaning command: %s", raw)
        return {}, f"Invalid JSON command: {e}"

    if not isinstance(payload, dict):
        return {}, "JSON command must be an object."
    return payload, ""


def _execute_tool_action(tools: SourceCleaningTools, command: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]]:
    action = str(command.get("action") or "").strip()
    arguments = command.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}

    try:
        if action == "search":
            return tools.search(**_filter_kwargs(arguments, {"query", "mode", "case_sensitive", "scope", "max_hits"}))
        if action == "regex_search":
            return tools.regex_search(**_filter_kwargs(arguments, {"pattern", "flags", "scope", "max_hits"}))
        if action == "preview":
            return tools.preview(**_filter_kwargs(arguments, {"start_line", "end_line", "before", "after", "around_hit_id"}))
        if action == "inspect_block":
            return tools.inspect_block(str(arguments.get("block_id") or ""))
        if action == "get_epub_markup_for_text":
            return tools.get_epub_markup_for_text(
                text=str(arguments.get("text") or ""),
                occurrence=int(arguments.get("occurrence") or 1),
                context_blocks=int(arguments.get("context_blocks") or 2),
            )
        if action == "list_repeated_lines":
            return tools.list_repeated_lines(
                min_repeats=int(arguments.get("min_repeats") or 3),
                max_length=int(arguments.get("max_length") or 120),
            )
        if action == "find_heading_candidates":
            return tools.find_heading_candidates(max_candidates=int(arguments.get("max_candidates") or 100))
        if action == "find_footnote_candidates":
            return tools.find_footnote_candidates(max_candidates=int(arguments.get("max_candidates") or 100))
        if action == "find_metadata_candidates":
            return tools.find_metadata_candidates()
    except Exception as e:
        logging.error("Source-cleaning tool action failed: %s", e, exc_info=True)
        return {"error": str(e), "action": action}

    return {"error": f"Unknown source-cleaning action: {action}"}


def _filter_kwargs(arguments: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    return {key: value for key, value in arguments.items() if key in allowed}


def _coerce_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _json_dumps_truncated(payload: Any, max_chars: int) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... <truncated>"


def _emit(callback: ProgressCallback | None, message: str):
    if callback is None:
        return
    try:
        callback(message)
    except Exception:
        pass
