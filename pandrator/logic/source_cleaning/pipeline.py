"""
Multi-phase source-cleaning pipeline.

Runs five focused LLM passes in sequence, each with a tight system prompt
and its own iteration budget:

  1. metadata          — extract title / author / language
  2. navigation        — delete TOC documents and inline contents lists
  3. boilerplate       — delete copyright, license, ads, "also by" lists
  4. repeated_elements — delete running headers/footers, page numbers, captions
  5. chapter_marking   — mark narrative chapter headings with [[Chapter]]

Between phases the pipeline builds a filtered view of the original document
(excluding already-deleted blocks). Because block IDs and line numbers are
never re-indexed, all operations can be applied in a single final call to
apply_cleaning_operations().
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .agent import SourceCleaningAgentConfig, run_source_cleaning_agent
from .models import PhaseResult, PipelineResult, SourceDocument
from .operations import apply_cleaning_operations

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

# ---------------------------------------------------------------------------
# Phase catalogue
# ---------------------------------------------------------------------------

_PHASE_ORDER = [
    "metadata",
    "navigation",
    "boilerplate",
    "repeated_elements",
    "chapter_marking",
]

_PHASE_DESCRIPTIONS = {
    "metadata": "Metadata extraction",
    "navigation": "Navigation/TOC removal",
    "boilerplate": "Boilerplate/copyright removal",
    "repeated_elements": "Repeated element removal",
    "chapter_marking": "Chapter marking",
}

# Operations each phase may propose (None = unrestricted, for legacy "full" mode)
_PHASE_ALLOWED_OPS: dict[str, frozenset[str] | None] = {
    "metadata": frozenset({"set_metadata"}),
    "navigation": frozenset({"delete_range", "delete_blocks", "delete_by_selector"}),
    "boilerplate": frozenset({"delete_range", "delete_blocks", "delete_by_selector"}),
    "repeated_elements": frozenset({"delete_range", "delete_blocks", "delete_by_selector"}),
    "chapter_marking": frozenset({"mark_chapter", "mark_chapters_by_selector"}),
}

# Source-overview components each phase needs (reduces pre-loop LLM-adjacent work)
_PHASE_OVERVIEW_COMPONENTS: dict[str, frozenset[str]] = {
    "metadata": frozenset({"structure"}),
    "navigation": frozenset({"structure", "cleanup_hypotheses"}),
    "boilerplate": frozenset({"structure", "cleanup_hypotheses"}),
    "repeated_elements": frozenset({"structure"}),
    "chapter_marking": frozenset({"structure", "chapter_hypotheses"}),
}

# Fraction of total budget allocated to each phase
_PHASE_BUDGET_WEIGHTS = {
    "metadata": 0.08,
    "navigation": 0.22,
    "boilerplate": 0.22,
    "repeated_elements": 0.16,
    "chapter_marking": 0.32,
}

# Hard minimum iterations per phase (ensures even tiny budgets are useful)
_PHASE_MIN_ITERATIONS = {
    "metadata": 3,
    "navigation": 4,
    "boilerplate": 4,
    "repeated_elements": 3,
    "chapter_marking": 5,
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class SourceCleaningPipelineConfig:
    """User-facing configuration for the multi-phase pipeline."""

    model_name: str = "default"
    remove_footnotes: bool = False
    # Total LLM turns across all phases; distributed proportionally
    total_max_iterations: int = 53
    max_tokens: int = 2200
    temperature: float = 0.2
    max_tool_result_chars: int = 12000
    max_evidence_ledger_chars: int = 10000
    recent_detailed_turns: int = 1
    max_batch_commands: int = 8
    # Override which phases run (None = all five in order)
    phase_names: list[str] | None = None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_cleaning_pipeline(
    document: SourceDocument,
    llm_settings: Any | None = None,
    config: SourceCleaningPipelineConfig | None = None,
    completion_func: Any | None = None,
    progress_callback: ProgressCallback | None = None,
    stop_event: Any | None = None,
) -> PipelineResult:
    """
    Execute the multi-phase source-cleaning pipeline and return a
    PipelineResult containing per-phase details and all accumulated operations.

    The caller is responsible for applying the operations to the original
    document via apply_cleaning_operations(document, result.all_operations).
    """
    resolved = config or SourceCleaningPipelineConfig()
    phase_names = resolved.phase_names or _PHASE_ORDER
    budgets = _distribute_budget(resolved.total_max_iterations, phase_names)

    pipeline_result = PipelineResult()
    accumulated_deleted_ids: set[str] = set()
    previous_summaries: list[dict[str, Any]] = []

    for idx, phase_name in enumerate(phase_names):
        if stop_event is not None and stop_event.is_set():
            pipeline_result.stopped = True
            break

        description = _PHASE_DESCRIPTIONS.get(phase_name, phase_name)
        _emit(progress_callback, f"Phase {idx + 1}/{len(phase_names)}: {description}...")
        logger.debug("Starting phase %s (%s), budget=%d", idx + 1, phase_name, budgets[phase_name])

        working_doc = _filtered_document(document, accumulated_deleted_ids)

        # Finish review budget per phase:
        #   chapter_marking → 2 chapter-completeness reviews
        #   boilerplate     → 1 lightweight boilerplate scan
        #   all others      → accept proposals directly (0 reviews)
        if phase_name == "chapter_marking":
            phase_max_finish_reviews = 2
        elif phase_name == "boilerplate":
            phase_max_finish_reviews = 1
        else:
            phase_max_finish_reviews = 0

        agent_config = SourceCleaningAgentConfig(
            model_name=resolved.model_name,
            phase_name=phase_name,
            max_iterations=budgets[phase_name],
            max_tokens=resolved.max_tokens,
            temperature=resolved.temperature,
            max_tool_result_chars=resolved.max_tool_result_chars,
            max_evidence_ledger_chars=resolved.max_evidence_ledger_chars,
            recent_detailed_turns=resolved.recent_detailed_turns,
            max_batch_commands=resolved.max_batch_commands,
            remove_footnotes=resolved.remove_footnotes,
            allowed_op_types=_PHASE_ALLOWED_OPS.get(phase_name),
            previous_phase_summaries=list(previous_summaries),
            source_overview_components=_PHASE_OVERVIEW_COMPONENTS.get(
                phase_name,
                frozenset({"structure", "cleanup_hypotheses", "chapter_hypotheses"}),
            ),
            max_finish_reviews=phase_max_finish_reviews,
            require_verified_finish_for_long_sources=(phase_name == "chapter_marking"),
        )

        agent_result = run_source_cleaning_agent(
            working_doc,
            llm_settings=llm_settings,
            config=agent_config,
            completion_func=completion_func,
            progress_callback=progress_callback,
            stop_event=stop_event,
        )

        # Apply this phase's operations to the working doc to track deletions
        if agent_result.operations:
            phase_cleaning = apply_cleaning_operations(working_doc, agent_result.operations)
            accumulated_deleted_ids.update(phase_cleaning.deleted_block_ids)

        pipeline_result.all_operations.extend(agent_result.operations)
        pipeline_result.total_iterations += agent_result.iterations
        pipeline_result.warnings.extend(agent_result.warnings)
        _merge_llm_usage(pipeline_result.llm_usage, agent_result.llm_usage)

        phase_result = PhaseResult(
            phase_name=phase_name,
            phase_description=description,
            operations=list(agent_result.operations),
            iterations=agent_result.iterations,
            warnings=list(agent_result.warnings),
            llm_usage=dict(agent_result.llm_usage),
            stopped=bool(stop_event and stop_event.is_set()),
        )
        pipeline_result.phases.append(phase_result)

        # Build a brief summary for subsequent phases
        previous_summaries.append({
            "phase": phase_name,
            "description": description,
            "operations_proposed": len(agent_result.operations),
            "iterations_used": agent_result.iterations,
            "warnings": list(agent_result.warnings[:3]),
        })

        logger.debug(
            "Phase %s done: %d operations, %d iterations",
            phase_name,
            len(agent_result.operations),
            agent_result.iterations,
        )

    return pipeline_result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _distribute_budget(total: int, phase_names: list[str]) -> dict[str, int]:
    """Allocate total iterations proportionally across phases, respecting minimums."""
    weights = {name: _PHASE_BUDGET_WEIGHTS.get(name, 0.2) for name in phase_names}
    total_weight = sum(weights.values()) or 1.0
    budgets: dict[str, int] = {}
    allocated = 0
    for i, name in enumerate(phase_names):
        if i == len(phase_names) - 1:
            budgets[name] = max(_PHASE_MIN_ITERATIONS.get(name, 2), total - allocated)
        else:
            raw = int(total * weights[name] / total_weight)
            budgets[name] = max(_PHASE_MIN_ITERATIONS.get(name, 2), raw)
            allocated += budgets[name]
    return budgets


def _filtered_document(document: SourceDocument, excluded_block_ids: set[str]) -> SourceDocument:
    """Return a view of the document with already-deleted blocks removed.

    Block IDs and line numbers are stable — no re-indexing occurs.
    All structural metadata (href, classes, tags, etc.) is preserved.
    """
    if not excluded_block_ids:
        return document
    return SourceDocument(
        source_type=document.source_type,
        source_path=document.source_path,
        filename=document.filename,
        blocks=[b for b in document.blocks if b.block_id not in excluded_block_ids],
        metadata_candidates=document.metadata_candidates,
        language=document.language,
        nav_titles=document.nav_titles,
        navigation_entries=document.navigation_entries,
        warnings=document.warnings,
    )


def _merge_llm_usage(totals: dict[str, Any], phase_usage: dict[str, Any]) -> None:
    """Aggregate per-phase LLM usage into a running total."""
    for key, value in phase_usage.items():
        if key in {"models", "cost_sources"}:
            existing = totals.setdefault(key, [])
            for item in (value or []):
                if item not in existing:
                    existing.append(item)
        elif key == "token_details" and isinstance(value, dict):
            detail_totals = totals.setdefault("token_details", {})
            for dk, dv in value.items():
                if isinstance(dv, (int, float)):
                    detail_totals[dk] = (detail_totals.get(dk) or 0) + dv
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            totals[key] = (totals.get(key) or 0) + value
        elif key not in totals:
            totals[key] = value


def _emit(callback: ProgressCallback | None, message: str) -> None:
    if callback is None:
        return
    try:
        callback(message)
    except Exception:
        pass
