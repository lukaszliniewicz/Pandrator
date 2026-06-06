from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .. import llm_handler
from .models import SourceDocument
from .operations import apply_cleaning_operations
from .prompts import build_initial_user_prompt, build_system_prompt
from .tools import SourceCleaningTools
from .validators import validate_cleaning_result


CompletionFunc = Callable[..., Any]
ProgressCallback = Callable[[str], None]
_INSPECTION_ACTIONS = {
    "inspect_document_structure",
    "inspect_navigation",
    "search",
    "regex_search",
    "preview",
    "inspect_block",
    "get_epub_markup_for_text",
    "preview_raw_markup_range",
    "list_epub_selectors",
    "preview_selector",
    "list_repeated_lines",
    "find_heading_candidates",
    "analyze_chapter_structure",
    "analyze_cleanup_structure",
    "find_footnote_candidates",
}


@dataclass
class SourceCleaningAgentConfig:
    model_name: str = "default"
    max_iterations: int = 30
    max_tool_result_chars: int = 12000
    max_tokens: int = 2200
    temperature: float = 0.2
    remove_footnotes: bool = False
    max_finish_reviews: int = 2
    require_verified_finish_for_long_sources: bool = True


@dataclass
class SourceCleaningAgentResult:
    operations: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    confidence: float = 0.0
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    raw_final_command: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    iterations: int = 0
    finish_reviews: list[dict[str, Any]] = field(default_factory=list)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    llm_usage: dict[str, Any] = field(default_factory=dict)

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
    completion = completion_func or llm_handler.chat_completion_with_metadata
    tools = SourceCleaningTools(document)
    result = SourceCleaningAgentResult()
    source_overview = tools.inspect_document_structure(max_documents=12)
    finish_review_attempts = 0
    inspection_actions: set[str] = set()
    evaluated_operations_key = ""

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(resolved_config.remove_footnotes)},
        {
            "role": "user",
            "content": build_initial_user_prompt(
                document,
                source_overview=source_overview,
            ),
        },
    ]

    for iteration in range(1, resolved_config.max_iterations + 1):
        result.iterations = iteration
        _emit(progress_callback, f"Source cleaning LLM step {iteration}/{resolved_config.max_iterations}...")
        completion_response = completion(
            messages=messages,
            model_name=resolved_config.model_name,
            llm_settings=llm_settings,
            max_tokens=resolved_config.max_tokens,
            temperature=resolved_config.temperature,
        )
        response, call_metadata = _normalize_completion_response(completion_response)
        _record_llm_call(result, iteration, call_metadata)
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
            proposed_operations = [
                operation
                for operation in command.get("operations", [])
                if isinstance(operation, dict)
            ]
            proposed_operations_key = _operations_key(proposed_operations)
            workflow_gaps: list[str] = []
            if resolved_config.require_verified_finish_for_long_sources and len(document.blocks) >= 40:
                if not inspection_actions:
                    workflow_gaps.append(
                        "Use at least one inspection tool chosen from the source structure before finishing."
                    )
                if evaluated_operations_key != proposed_operations_key:
                    workflow_gaps.append(
                        "Call evaluate_operations with the exact final operations before finishing."
                    )
            if workflow_gaps:
                observation = {"accepted": False, "workflow_gaps": workflow_gaps}
                result.tool_trace.append(
                    {
                        "iteration": iteration,
                        "action": "workflow_review",
                        "arguments": {"proposed_operation_count": len(proposed_operations)},
                        "observation": observation,
                    }
                )
                if iteration < resolved_config.max_iterations:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "The proposal is not ready to finish:\n"
                                + _json_dumps_truncated(observation, resolved_config.max_tool_result_chars)
                                + "\n\nContinue with the required inspection/evaluation command."
                            ),
                        }
                    )
                    continue
                result.raw_final_command = command
                result.warnings.extend(workflow_gaps)
                break

            finish_review = _review_finish_command(
                document,
                proposed_operations,
                remove_footnotes=resolved_config.remove_footnotes,
            )
            result.finish_reviews.append(finish_review)
            blocking_warnings = finish_review.get("blocking_warnings") or []
            if (
                blocking_warnings
                and finish_review_attempts < resolved_config.max_finish_reviews
                and iteration < resolved_config.max_iterations
            ):
                finish_review_attempts += 1
                result.tool_trace.append(
                    {
                        "iteration": iteration,
                        "action": "finish_review",
                        "arguments": {"proposed_operation_count": len(proposed_operations)},
                        "observation": finish_review,
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Finish review rejected the proposal because cleanup or chapter marking appears incomplete.\n"
                            + _json_dumps_truncated(finish_review, resolved_config.max_tool_result_chars)
                            + "\n\nInspect the remaining cleanup groups and any repeated chapter selector, then submit "
                            "a corrected finish command. Remove complete confirmed sections, not only their headings."
                        ),
                    }
                )
                continue

            result.operations = proposed_operations
            result.summary = str(command.get("summary") or "").strip()
            result.confidence = _coerce_confidence(command.get("confidence"))
            result.raw_final_command = command
            if blocking_warnings:
                result.warnings.extend(str(item) for item in blocking_warnings)
            _emit(progress_callback, "Source cleaning LLM proposed operations.")
            return result

        if action == "evaluate_operations":
            evaluation_arguments = command.get("arguments")
            if not isinstance(evaluation_arguments, dict):
                evaluation_arguments = {}
            proposed_operations = [
                operation
                for operation in evaluation_arguments.get("operations", [])
                if isinstance(operation, dict)
            ]
            observation = _review_finish_command(
                document,
                proposed_operations,
                remove_footnotes=resolved_config.remove_footnotes,
            )
            evaluated_operations_key = _operations_key(proposed_operations)
        else:
            observation = _execute_tool_action(tools, command)
            if action in _INSPECTION_ACTIONS:
                inspection_actions.add(action)
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

    candidates, error = _extract_json_command_candidates(raw)
    for candidate in candidates:
        if str(candidate.get("action") or "").strip():
            return candidate, ""

    if candidates:
        return {}, "JSON command must include a non-empty action."

    logging.debug("Could not parse source-cleaning command: %s", raw)
    return {}, error or "Could not find a JSON command object."


def _extract_json_command_candidates(raw: str) -> tuple[list[dict[str, Any]], str]:
    segments = re.findall(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
    if not segments:
        segments = [raw]

    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    for segment in segments:
        segment_candidates, error = _parse_json_objects_from_text(segment)
        candidates.extend(segment_candidates)
        if error:
            errors.append(error)

    if candidates:
        return candidates, ""
    return [], errors[-1] if errors else "No JSON object found."


def _parse_json_objects_from_text(text: str) -> tuple[list[dict[str, Any]], str]:
    decoder = json.JSONDecoder()
    stripped = str(text or "").strip()
    if not stripped:
        return [], "Empty JSON segment."

    candidates: list[dict[str, Any]] = []
    errors: list[str] = []

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as e:
        errors.append(f"Invalid JSON command: {e}")
    else:
        candidates.extend(_coerce_json_command_candidates(payload))
        if candidates:
            return candidates, ""

    for start in _json_object_start_indices(stripped):
        try:
            payload, _end = decoder.raw_decode(stripped[start:])
        except json.JSONDecodeError as e:
            errors.append(f"Invalid JSON command: {e}")
            continue
        candidates.extend(_coerce_json_command_candidates(payload))

    return _dedupe_command_candidates(candidates), errors[-1] if errors else ""


def _json_object_start_indices(text: str) -> list[int]:
    return [match.start() for match in re.finditer(r"\{", text)]


def _coerce_json_command_candidates(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _dedupe_command_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = json.dumps(candidate, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        deduped.append(candidate)
        seen.add(key)
    return deduped


def _execute_tool_action(tools: SourceCleaningTools, command: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]]:
    action = str(command.get("action") or "").strip()
    arguments = command.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}

    try:
        if action == "inspect_document_structure":
            return tools.inspect_document_structure(
                max_documents=int(arguments.get("max_documents") or 30),
                scope=arguments.get("scope") if isinstance(arguments.get("scope"), dict) else None,
            )
        if action == "inspect_navigation":
            return tools.inspect_navigation(
                max_entries=int(arguments.get("max_entries") or 80),
                max_matches_per_entry=int(arguments.get("max_matches_per_entry") or 5),
            )
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
        if action == "preview_raw_markup_range":
            return tools.preview_raw_markup_range(
                start_line=int(arguments.get("start_line") or 1),
                end_line=int(arguments.get("end_line") or arguments.get("start_line") or 1),
                max_blocks=int(arguments.get("max_blocks") or 30),
            )
        if action == "list_epub_selectors":
            return tools.list_epub_selectors(
                min_count=int(arguments.get("min_count") or 2),
                max_items=int(arguments.get("max_items") or 80),
            )
        if action == "preview_selector":
            selector = arguments.get("selector")
            return tools.preview_selector(
                selector=selector if isinstance(selector, dict) else {},
                max_blocks=int(arguments.get("max_blocks") or 30),
                include_raw_markup=bool(arguments.get("include_raw_markup", False)),
            )
        if action == "list_repeated_lines":
            return tools.list_repeated_lines(
                min_repeats=int(arguments.get("min_repeats") or 3),
                max_length=int(arguments.get("max_length") or 120),
            )
        if action == "find_heading_candidates":
            return tools.find_heading_candidates(max_candidates=int(arguments.get("max_candidates") or 100))
        if action == "analyze_chapter_structure":
            return tools.analyze_chapter_structure(max_candidates=int(arguments.get("max_candidates") or 60))
        if action == "analyze_cleanup_structure":
            return tools.analyze_cleanup_structure(max_candidates=int(arguments.get("max_candidates") or 20))
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


def _normalize_completion_response(response: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(response, str):
        return response, {}

    if isinstance(response, dict):
        content = str(response.get("content") or "")
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        return content, {
            "model": str(response.get("model") or ""),
            "usage": usage,
            "cost": response.get("cost"),
            "cost_source": str(response.get("cost_source") or ""),
            "response_id": str(response.get("response_id") or ""),
        }

    usage = getattr(response, "usage", {})
    if hasattr(usage, "model_dump"):
        try:
            usage = usage.model_dump(mode="json")
        except TypeError:
            usage = usage.model_dump()
    if not isinstance(usage, dict):
        usage = {}
    return str(getattr(response, "content", "") or ""), {
        "model": str(getattr(response, "model", "") or ""),
        "usage": usage,
        "cost": getattr(response, "cost", None),
        "cost_source": str(getattr(response, "cost_source", "") or ""),
        "response_id": str(getattr(response, "response_id", "") or ""),
    }


def _record_llm_call(result: SourceCleaningAgentResult, iteration: int, metadata: dict[str, Any]):
    usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
    cost = _optional_float(metadata.get("cost"))
    call = {
        "iteration": iteration,
        "model": str(metadata.get("model") or ""),
        "usage": usage,
        "cost_usd": cost,
        "cost_source": str(metadata.get("cost_source") or ""),
        "response_id": str(metadata.get("response_id") or ""),
    }
    result.llm_calls.append(call)

    totals = result.llm_usage
    totals["call_count"] = int(totals.get("call_count") or 0) + 1
    if usage:
        totals["usage_available_calls"] = int(totals.get("usage_available_calls") or 0) + 1
    else:
        totals["usage_unavailable_calls"] = int(totals.get("usage_unavailable_calls") or 0) + 1

    for key, value in usage.items():
        if key.endswith("_tokens") and isinstance(value, int) and not isinstance(value, bool):
            totals[key] = int(totals.get(key) or 0) + value
        if not isinstance(value, dict):
            continue
        detail_totals = totals.setdefault("token_details", {})
        for detail_key, detail_value in value.items():
            if detail_key.endswith("_tokens") and isinstance(detail_value, int) and not isinstance(detail_value, bool):
                detail_totals[detail_key] = int(detail_totals.get(detail_key) or 0) + detail_value
    cached_prompt_tokens = int(totals.get("token_details", {}).get("cached_tokens") or 0)
    totals["uncached_prompt_tokens"] = max(0, int(totals.get("prompt_tokens") or 0) - cached_prompt_tokens)

    if cost is None:
        totals["cost_unavailable_calls"] = int(totals.get("cost_unavailable_calls") or 0) + 1
    else:
        totals["cost_available_calls"] = int(totals.get("cost_available_calls") or 0) + 1
        totals["cost_usd"] = round(float(totals.get("cost_usd") or 0.0) + cost, 12)
        totals["cost_currency"] = "USD"
        cost_source = str(metadata.get("cost_source") or "")
        cost_sources = totals.setdefault("cost_sources", [])
        if cost_source and cost_source not in cost_sources:
            cost_sources.append(cost_source)

    model = str(metadata.get("model") or "")
    models = totals.setdefault("models", [])
    if model and model not in models:
        models.append(model)


def _review_finish_command(
    document: SourceDocument,
    operations: list[dict[str, Any]],
    remove_footnotes: bool,
) -> dict[str, Any]:
    cleaning_result = apply_cleaning_operations(document, operations)
    validation = validate_cleaning_result(
        document,
        cleaning_result,
        remove_footnotes=remove_footnotes,
    )
    blocking_warnings = list(validation.errors) + list(validation.blocking_warnings)
    advisory_warnings = [
        warning
        for warning in validation.warnings
        if warning not in validation.blocking_warnings
    ]
    deleted_ids = set(cleaning_result.deleted_block_ids)
    retained_ids = [
        block.block_id
        for block in document.blocks
        if block.block_id not in deleted_ids
    ]
    return {
        "accepted": not blocking_warnings,
        "blocking_warnings": blocking_warnings,
        "advisory_warnings": advisory_warnings,
        "validation_stats": validation.stats,
        "chapter_structure": SourceCleaningTools(document).analyze_chapter_structure(max_candidates=6),
        "remaining_document_structure": SourceCleaningTools(document).inspect_document_structure(
            max_documents=20,
            scope={"block_ids": retained_ids},
        ),
        "remaining_cleanup_structure": SourceCleaningTools(document).analyze_cleanup_structure(
            max_candidates=8,
            scope={"block_ids": retained_ids},
        ),
    }


def _operations_key(operations: list[dict[str, Any]]) -> str:
    return json.dumps(operations, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
