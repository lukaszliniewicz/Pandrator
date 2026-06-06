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
    phase_name: str = "full"
    max_iterations: int = 30
    max_tool_result_chars: int = 12000
    max_evidence_ledger_chars: int = 10000
    recent_detailed_turns: int = 1
    max_batch_commands: int = 8
    max_tokens: int = 2200
    temperature: float = 0.2
    remove_footnotes: bool = False
    max_finish_reviews: int = 2
    require_verified_finish_for_long_sources: bool = True
    # Phase-pipeline extensions
    allowed_op_types: frozenset | None = None  # None = all ops permitted
    previous_phase_summaries: list = field(default_factory=list)
    source_overview_components: frozenset = field(
        default_factory=lambda: frozenset({"structure", "cleanup_hypotheses", "chapter_hypotheses"})
    )


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
    stop_event=None,
) -> SourceCleaningAgentResult:
    """Runs a JSON-command LLM loop over deterministic source-cleaning tools."""
    resolved_config = config or SourceCleaningAgentConfig()
    completion = completion_func or llm_handler.chat_completion_with_metadata
    tools = SourceCleaningTools(document)
    result = SourceCleaningAgentResult()

    # Build only the source-overview components needed for this phase
    source_overview: dict[str, Any] = {}
    components = resolved_config.source_overview_components
    if "structure" in components:
        source_overview["structure"] = tools.inspect_document_structure(max_documents=10)
    if "cleanup_hypotheses" in components:
        source_overview["cleanup_hypotheses"] = tools.analyze_cleanup_structure(max_candidates=6)
    if "chapter_hypotheses" in components:
        source_overview["chapter_hypotheses"] = tools.analyze_chapter_structure(max_candidates=4)

    finish_review_attempts = 0
    inspection_actions: set[str] = set()

    base_messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(
            resolved_config.remove_footnotes,
            phase_name=resolved_config.phase_name,
        )},
        {
            "role": "user",
            "content": build_initial_user_prompt(
                document,
                source_overview=source_overview,
                phase_name=resolved_config.phase_name,
                previous_phase_summaries=list(resolved_config.previous_phase_summaries or []),
            ),
        },
    ]
    conversation_turns: list[dict[str, str]] = []

    for iteration in range(1, resolved_config.max_iterations + 1):
        result.iterations = iteration
        if stop_event is not None and stop_event.is_set():
            _emit(progress_callback, "Source cleaning stopped by user request.")
            result.warnings.append("Stopped by user request.")
            break
        _emit(progress_callback, f"Source cleaning LLM step {iteration}/{resolved_config.max_iterations}...")
        request_messages = _build_completion_messages(
            base_messages,
            conversation_turns,
            recent_detailed_turns=resolved_config.recent_detailed_turns,
            max_evidence_ledger_chars=resolved_config.max_evidence_ledger_chars,
        )
        completion_response = completion(
            messages=request_messages,
            model_name=resolved_config.model_name,
            llm_settings=llm_settings,
            max_tokens=resolved_config.max_tokens,
            temperature=resolved_config.temperature,
        )
        response, call_metadata = _normalize_completion_response(completion_response)
        _record_llm_call(
            result,
            iteration,
            call_metadata,
            request_context_chars=sum(len(str(message.get("content") or "")) for message in request_messages),
        )
        if not response:
            result.warnings.append("LLM returned an empty response.")
            break

        command, parse_error = parse_json_command(response)
        if parse_error:
            result.warnings.append(parse_error)
            correction = (
                "Your previous response was not valid JSON. "
                "Return exactly one JSON object using an allowed action."
            )
            _append_conversation_turn(
                conversation_turns,
                response,
                correction,
                action="parse_error",
                arguments={},
                observation={"error": parse_error},
            )
            continue

        action = str(command.get("action") or "").strip()
        if action in {"finish", "propose_operations"}:
            proposed_operations = [
                operation
                for operation in command.get("operations", [])
                if isinstance(operation, dict)
            ]

            # Filter to ops allowed for this phase
            if resolved_config.allowed_op_types is not None:
                proposed_operations = [
                    op for op in proposed_operations
                    if str(op.get("op") or "").strip() in resolved_config.allowed_op_types
                ]

            workflow_gaps: list[str] = []
            if resolved_config.require_verified_finish_for_long_sources and len(document.blocks) >= 40:
                if not inspection_actions:
                    workflow_gaps.append(
                        "Use at least one inspection tool chosen from the source structure before finishing."
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
                    feedback = (
                        "The proposal is not ready to finish:\n"
                        + _json_dumps_truncated(observation, resolved_config.max_tool_result_chars)
                        + "\n\nContinue with the required inspection/evaluation command."
                    )
                    _append_conversation_turn(
                        conversation_turns,
                        response,
                        feedback,
                        action="workflow_review",
                        arguments={
                            "proposed_operation_count": len(proposed_operations),
                            "proposed_operations": proposed_operations,
                        },
                        observation=observation,
                    )
                    continue
                result.raw_final_command = command
                result.warnings.extend(workflow_gaps)
                break

            # For chapter_marking and the legacy full pass, run the completeness
            # review. Deletion-only phases accept the proposal immediately.
            _run_finish_review = resolved_config.phase_name in {"full", "chapter_marking"}

            if _run_finish_review:
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
                    feedback = (
                        "Finish review rejected the proposal because cleanup or chapter marking appears incomplete.\n"
                        + _json_dumps_truncated(finish_review, resolved_config.max_tool_result_chars)
                        + "\n\nInspect the remaining cleanup groups and any repeated chapter selector, then submit "
                        "a corrected finish command. Remove complete confirmed sections, not only their headings."
                    )
                    _append_conversation_turn(
                        conversation_turns,
                        response,
                        feedback,
                        action="finish_review",
                        arguments={
                            "proposed_operation_count": len(proposed_operations),
                            "proposed_operations": proposed_operations,
                        },
                        observation=finish_review,
                    )
                    continue
            else:
                blocking_warnings = []

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
        else:
            observation = _execute_tool_action(
                tools,
                command,
                max_batch_commands=resolved_config.max_batch_commands,
            )
            if action in _INSPECTION_ACTIONS:
                inspection_actions.add(action)
            if action == "batch":
                for batch_command in _batch_commands(command, resolved_config.max_batch_commands):
                    batch_action = str(batch_command.get("action") or "").strip()
                    if batch_action in _INSPECTION_ACTIONS:
                        inspection_actions.add(batch_action)
        trace_item = {
            "iteration": iteration,
            "action": action,
            "arguments": command.get("arguments") if isinstance(command.get("arguments"), dict) else {},
            "observation": observation,
        }
        result.tool_trace.append(trace_item)
        model_observation = _observation_for_model(action, observation)
        feedback = (
            "Tool result:\n"
            + _json_dumps_truncated(model_observation, resolved_config.max_tool_result_chars)
            + "\n\nChoose the next inspection command, batch independent inspections, or finish with operations."
        )
        _append_conversation_turn(
            conversation_turns,
            response,
            feedback,
            action=action,
            arguments=trace_item["arguments"],
            observation=observation,
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
        if (
            isinstance(candidate.get("operations"), list)
            and "confidence" in candidate
        ):
            inferred = dict(candidate)
            inferred["action"] = "finish"
            return inferred, ""

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


def _execute_tool_action(
    tools: SourceCleaningTools,
    command: dict[str, Any],
    max_batch_commands: int = 8,
) -> dict[str, Any] | list[dict[str, Any]]:
    action = str(command.get("action") or "").strip()
    arguments = command.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}

    try:
        if action == "batch":
            commands = _batch_commands(command, max_batch_commands)
            results: list[dict[str, Any]] = []
            for index, batch_command in enumerate(commands, start=1):
                batch_action = str(batch_command.get("action") or "").strip()
                batch_arguments = (
                    batch_command.get("arguments")
                    if isinstance(batch_command.get("arguments"), dict)
                    else {}
                )
                if batch_action in {"batch", "finish", "propose_operations", "evaluate_operations"}:
                    batch_observation: Any = {
                        "error": f"Action '{batch_action}' is not allowed inside a batch."
                    }
                else:
                    batch_observation = _execute_tool_action(
                        tools,
                        batch_command,
                        max_batch_commands=0,
                    )
                results.append(
                    {
                        "index": index,
                        "action": batch_action,
                        "arguments": batch_arguments,
                        "observation": batch_observation,
                    }
                )
            raw_commands = arguments.get("commands")
            raw_count = len(raw_commands) if isinstance(raw_commands, list) else 0
            return {
                "requested_commands": raw_count,
                "executed_commands": len(results),
                "commands_truncated": raw_count > len(results),
                "results": results,
            }
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
            return tools.preview(
                **_filter_kwargs(
                    arguments,
                    {"start_line", "end_line", "before", "after", "around_hit_id", "max_blocks"},
                )
            )
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


def _batch_commands(command: dict[str, Any], max_batch_commands: int) -> list[dict[str, Any]]:
    arguments = command.get("arguments")
    if not isinstance(arguments, dict):
        return []
    commands = arguments.get("commands")
    if not isinstance(commands, list):
        return []
    limit = max(0, int(max_batch_commands))
    return [
        item
        for item in commands[:limit]
        if isinstance(item, dict)
    ]


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


def _record_llm_call(
    result: SourceCleaningAgentResult,
    iteration: int,
    metadata: dict[str, Any],
    request_context_chars: int = 0,
):
    usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
    cost = _optional_float(metadata.get("cost"))
    call = {
        "iteration": iteration,
        "model": str(metadata.get("model") or ""),
        "usage": usage,
        "cost_usd": cost,
        "cost_source": str(metadata.get("cost_source") or ""),
        "response_id": str(metadata.get("response_id") or ""),
        "request_context_chars": max(0, int(request_context_chars)),
    }
    result.llm_calls.append(call)

    totals = result.llm_usage
    totals["call_count"] = int(totals.get("call_count") or 0) + 1
    totals["request_context_chars_total"] = (
        int(totals.get("request_context_chars_total") or 0) + call["request_context_chars"]
    )
    totals["max_request_context_chars"] = max(
        int(totals.get("max_request_context_chars") or 0),
        call["request_context_chars"],
    )
    totals["last_request_context_chars"] = call["request_context_chars"]
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


def _append_conversation_turn(
    turns: list[dict[str, str]],
    assistant_content: str,
    user_content: str,
    *,
    action: str,
    arguments: dict[str, Any],
    observation: Any,
):
    summary_payload = {
        "action": action,
        "arguments": _compact_for_ledger(arguments),
        "observation": _compact_for_ledger(observation),
    }
    turns.append(
        {
            "assistant": assistant_content,
            "user": user_content,
            "summary": json.dumps(summary_payload, ensure_ascii=False, separators=(",", ":")),
        }
    )


def _build_completion_messages(
    base_messages: list[dict[str, Any]],
    turns: list[dict[str, str]],
    *,
    recent_detailed_turns: int,
    max_evidence_ledger_chars: int,
) -> list[dict[str, Any]]:
    messages = [dict(message) for message in base_messages]
    detailed_count = max(0, int(recent_detailed_turns))
    older_turns = turns[:-detailed_count] if detailed_count else turns
    recent_turns = turns[-detailed_count:] if detailed_count else []

    if older_turns:
        summaries = [
            f"{index}. {turn['summary']}"
            for index, turn in enumerate(older_turns, start=1)
        ]
        ledger = "\n".join(summaries)
        ledger = _truncate_middle(ledger, max(1000, int(max_evidence_ledger_chars)))
        messages.append(
            {
                "role": "user",
                "content": (
                    "Evidence ledger from earlier tool turns. Treat it as established context; "
                    "request a fresh preview only when the compact evidence is insufficient.\n"
                    + ledger
                ),
            }
        )

    for turn in recent_turns:
        messages.append({"role": "assistant", "content": turn["assistant"]})
        messages.append({"role": "user", "content": turn["user"]})
    return messages


def _compact_for_ledger(
    value: Any,
    key: str = "",
    depth: int = 0,
    *,
    string_limit: int = 220,
    block_list_limit: int = 4,
) -> Any:
    if depth >= 7:
        return "<nested data omitted>"
    if isinstance(value, str):
        return value if len(value) <= string_limit else value[: max(0, string_limit - 3)] + "..."
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for item_key, item_value in value.items():
            if item_key in {"raw_markup", "dom_path", "attributes", "context"}:
                continue
            compact[str(item_key)] = _compact_for_ledger(
                item_value,
                key=str(item_key),
                depth=depth + 1,
                string_limit=string_limit,
                block_list_limit=block_list_limit,
            )
        return compact
    if isinstance(value, list):
        if key in {"operations", "proposed_operations"}:
            limit = 20
        elif key in {"blocks", "matches", "documents", "likely_chapters", "entries"}:
            limit = block_list_limit
        else:
            limit = 8
        if len(value) <= limit:
            return [
                _compact_for_ledger(
                    item,
                    depth=depth + 1,
                    string_limit=string_limit,
                    block_list_limit=block_list_limit,
                )
                for item in value
            ]
        head_count = (limit + 1) // 2
        tail_count = limit - head_count
        compact_items = [
            _compact_for_ledger(
                item,
                depth=depth + 1,
                string_limit=string_limit,
                block_list_limit=block_list_limit,
            )
            for item in value[:head_count]
        ]
        compact_items.append({"omitted_items": len(value) - limit})
        compact_items.extend(
            _compact_for_ledger(
                item,
                depth=depth + 1,
                string_limit=string_limit,
                block_list_limit=block_list_limit,
            )
            for item in value[-tail_count:]
        )
        return compact_items
    return value


def _observation_for_model(action: str, observation: Any) -> Any:
    if action == "batch":
        return _compact_for_ledger(
            observation,
            string_limit=140,
            block_list_limit=2,
        )
    return observation


def _truncate_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    marker = "\n... <older evidence compacted> ...\n"
    head_chars = max_chars // 3
    tail_chars = max_chars - head_chars - len(marker)
    return text[:head_chars] + marker + text[-max(0, tail_chars):]


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
    tools = SourceCleaningTools(document)
    chapter_structure = tools.analyze_chapter_structure(max_candidates=6)
    remaining_cleanup = tools.analyze_cleanup_structure(
        max_candidates=8,
        scope={"block_ids": retained_ids},
    )
    review = {
        "accepted": not blocking_warnings,
        "blocking_warnings": blocking_warnings,
        "advisory_warnings": advisory_warnings,
        "validation_stats": validation.stats,
        "chapter_structure": {
            "nav_title_count": chapter_structure.get("nav_title_count"),
            "likely_chapter_count": chapter_structure.get("likely_chapter_count"),
            "numbered_heading_count": chapter_structure.get("numbered_heading_count"),
            "selector_suggestions": chapter_structure.get("selector_suggestions", []),
        },
        "remaining_cleanup_structure": remaining_cleanup,
    }
    if blocking_warnings:
        review["remaining_document_structure"] = tools.inspect_document_structure(
            max_documents=8,
            scope={"block_ids": retained_ids},
        )
        review["chapter_structure"]["likely_chapters"] = chapter_structure.get("likely_chapters", [])
    return review


def _review_boilerplate_finish(
    document: SourceDocument,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Lightweight finish review for the boilerplate phase.

    Applies proposed operations speculatively, then checks whether the
    remaining blocks still contain boilerplate candidate groups.  Deliberately
    generic — does not match on any specific publisher or project name.
    """
    cleaning_result = apply_cleaning_operations(document, operations)
    deleted_ids = set(cleaning_result.deleted_block_ids)
    retained_ids = [
        block.block_id
        for block in document.blocks
        if block.block_id not in deleted_ids
    ]
    tools = SourceCleaningTools(document)
    remaining_cleanup = tools.analyze_cleanup_structure(
        max_candidates=8,
        scope={"block_ids": retained_ids},
    )

    blocking_warnings: list[str] = []
    candidate_groups = remaining_cleanup.get("candidate_groups") or []
    remaining_boilerplate = int(remaining_cleanup.get("likely_boilerplate_block_count") or 0)
    remaining_copyright = int(remaining_cleanup.get("copyright_block_count") or 0)

    if candidate_groups:
        blocking_warnings.append(
            f"{len(candidate_groups)} boilerplate/copyright candidate group(s) remain unreviewed. "
            "Inspect the candidate groups and delete any confirmed boilerplate. "
            "Check both the beginning and the end of the document."
        )
    elif remaining_boilerplate >= 3:
        blocking_warnings.append(
            f"~{remaining_boilerplate} likely boilerplate block(s) remain. "
            "Check the beginning of the document for a front-matter preamble."
        )
    elif remaining_copyright >= 5:
        blocking_warnings.append(
            f"{remaining_copyright} copyright-tagged block(s) remain. "
            "Verify they are narrative epigraphs rather than publisher boilerplate."
        )

    return {
        "accepted": not blocking_warnings,
        "blocking_warnings": blocking_warnings,
        "remaining_cleanup": {
            "likely_boilerplate_block_count": remaining_boilerplate,
            "copyright_block_count": remaining_copyright,
            "candidate_groups": candidate_groups[:4],
        },
    }




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
