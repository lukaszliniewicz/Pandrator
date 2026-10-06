"""Web research and metadata projection for subtitle workflow stages."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .workflow_text_protocols import ResolveSecretReferenceProtocol

if TYPE_CHECKING:
    from pandrator.runtime import DataPaths

    from .database import Database
    from .models import Artifact
    from .workflow_generation_protocols import Progress


@dataclass(frozen=True, slots=True)
class WebResearchContext:
    database: Database
    paths: DataPaths
    _subtitle_speaker_map: Callable[[Artifact, Path], dict[int, str]]
    _resolve_secret_reference: ResolveSecretReferenceProtocol
    _database_reference: Callable[[str], str]
    _auxiliary_credential_key: Callable[[object], str]
    _fraction_message_callback: Callable[[Progress, float, float], Callable[[str], None]]


def run_stage_web_research(
    context: WebResearchContext,
    *,
    stage: str,
    session_id: str,
    source_artifact: Artifact,
    source_path: Path,
    settings: dict[str, Any],
    progress,
    cancel_event,
    completed_units: dict[str, dict[str, Any]],
    persist_checkpoint,
):
    if not bool(settings.get("web_research_enabled", False)):
        return None
    if cancel_event.is_set():
        raise RuntimeError("Web research was canceled.")
    provider_id = str(settings.get("web_research_provider") or "jina").strip().lower()
    if provider_id != "jina":
        raise ValueError(f"Unsupported web research provider: {provider_id}")
    if (
        stage == "translation"
        and str(settings.get("translation_backend") or settings.get("backend") or "llm").lower()
        != "llm"
    ):
        raise ValueError(
            "Web research currently grounds the LLM translation backend. "
            "Choose the LLM backend or disable web research."
        )

    from pandrator.logic.dubbing.srt_utils import parse_srt

    from .context_budget import ContextBudgetService
    from .knowledge import KnowledgeLedgerStore
    from .provider_settings import build_llm_settings
    from .web_research import (
        JinaResearchProvider,
        PersistentResearchCache,
        ResearchAgentConfig,
        WebResearchResult,
        merge_web_research_results,
        parse_domain_list,
        run_web_research_agent,
    )

    credential = context._resolve_secret_reference(
        context.database,
        context.paths,
        context._database_reference(context._auxiliary_credential_key("jina")),
        fallback_environment_variable="JINA_API_KEY",
    )
    research_provider = JinaResearchProvider(
        api_key=credential.resolved_value(),
        cache=PersistentResearchCache(context.database),
        timeout_seconds=int(settings.get("web_research_timeout_seconds") or 90),
    )
    model_key = "correction_model" if stage == "correction" else "translation_model"
    task_model = str(settings.get(model_key) or settings.get("llm_default_model") or "")
    requested_researcher = str(settings.get("web_research_model_name") or "").strip()
    llm_settings, model_name = build_llm_settings(
        context.database,
        context.paths,
        requested_model=requested_researcher or task_model,
        request_timeout_seconds=int(
            settings.get("web_research_timeout_seconds")
            or settings.get("request_timeout_seconds")
            or 600
        ),
    )
    source_language = str(
        settings.get("original_language") or settings.get("source_language") or "auto"
    )
    target_language = str(settings.get("target_language") or "") if stage == "translation" else ""
    knowledge = KnowledgeLedgerStore(context.database)
    saved_research = knowledge.get(
        session_id,
        "research",
        source_language=source_language,
        target_language=target_language,
    )["payload"]
    saved_glossary = knowledge.get(
        session_id,
        "glossary",
        source_language=source_language,
        target_language=target_language,
    )["payload"]
    accumulated = WebResearchResult(
        evidence=[
            dict(item) for item in saved_research.get("evidence", []) if isinstance(item, dict)
        ],
        glossary=[
            {
                "source": str(item.get("source") or ""),
                "target": str(item.get("target") or ""),
            }
            for item in saved_glossary.get("entries", [])
            if isinstance(item, dict) and str(item.get("status") or "active") != "disabled"
        ],
        summary=str(saved_research.get("summary") or ""),
        warnings=[str(item) for item in saved_research.get("warnings", [])],
    )
    try:
        cues = parse_srt(source_path.read_text(encoding="utf-8-sig"))
        speaker_map = context._subtitle_speaker_map(source_artifact, source_path)
        records = [
            {
                "id": cue.index,
                "start_ms": cue.start_ms,
                "end_ms": cue.end_ms,
                "speaker": str(speaker_map.get(cue.index) or cue.speaker or ""),
                "text": cue.text,
            }
            for cue in cues
        ]
    except (OSError, ValueError):
        records = [
            {"id": index, "text": text}
            for index, text in enumerate(
                source_path.read_text(encoding="utf-8-sig").splitlines(),
                start=1,
            )
            if text.strip()
        ]

    mode = str(settings.get("web_research_mode") or "global").strip().lower()
    if mode not in {"global", "per_chunk"}:
        raise ValueError("Web research mode must be 'global' or 'per_chunk'.")
    context_fraction = min(
        0.8,
        max(0.1, float(settings.get("web_research_context_fraction") or 0.8)),
    )
    budget = ContextBudgetService(context.database).resolve(
        model_name,
        fraction=context_fraction,
        fixed_prompt={
            "stage": stage,
            "source_language": source_language,
            "target_language": target_language,
            "instruction": "Research terminology and uncertain proper names using bounded web tools.",
        },
        ledger={
            "evidence": accumulated.evidence,
            "glossary": accumulated.glossary,
        },
        tools=["search_web", "read_url", "finish"],
    )
    partitioner = ContextBudgetService.partition
    if mode == "global":
        record_groups = partitioner(
            records,
            model=model_name,
            budget_tokens=budget.input_budget_tokens,
        )
    else:
        chunk_size = max(
            1,
            int(
                settings.get("max_segments_per_batch")
                or settings.get("max_subtitles_per_call")
                or 40
            ),
        )
        record_groups = []
        for offset in range(0, len(records), chunk_size):
            record_groups.extend(
                partitioner(
                    records[offset : offset + chunk_size],
                    model=model_name,
                    budget_tokens=budget.input_budget_tokens,
                )
            )

    run_settings = {
        "stage": stage,
        "provider": provider_id,
        "model": model_name,
        "mode": mode,
        "context_window_tokens": budget.context_window_tokens,
        "context_fraction": budget.fraction,
        "input_budget_tokens": budget.input_budget_tokens,
        "source_language": source_language,
        "target_language": target_language,
        "research_language": str(settings.get("web_research_language") or ""),
        "max_searches": max(0, int(settings.get("web_research_max_searches") or 3)),
        "max_extractions": max(0, int(settings.get("web_research_max_extractions") or 2)),
        "preferred_domains": list(
            parse_domain_list(settings.get("web_research_preferred_domains"))
        ),
        "blocked_domains": list(parse_domain_list(settings.get("web_research_blocked_domains"))),
    }
    configured_iterations = max(
        2,
        int(settings.get("web_research_max_iterations") or 8),
    )
    progress(0.02, f"Preparing {stage} web research")
    results = [accumulated]
    total_batches = max(1, len(record_groups))
    for batch_index, group in enumerate(record_groups):
        if cancel_event.is_set():
            raise RuntimeError("Web research was canceled.")
        research_source = json.dumps(group, ensure_ascii=False)
        unit_key = f"research:{mode}:{batch_index}"
        resume_state = completed_units.get(unit_key)

        def save_research_state(
            state: dict[str, Any],
            *,
            checkpoint_key: str = unit_key,
        ) -> None:
            raw_checkpoint_result = state.get("result")
            checkpoint_result = (
                raw_checkpoint_result if isinstance(raw_checkpoint_result, dict) else {}
            )
            persist_checkpoint(
                checkpoint_key,
                {
                    **state,
                    "cost": checkpoint_result.get("cost", 0.0),
                    "response_count": checkpoint_result.get("response_count", 0),
                    "cost_sources": checkpoint_result.get("cost_sources", []),
                    "usage": checkpoint_result.get("usage", {}),
                },
                phase="web_research",
                usage_stage="web_research",
                usage_settings={
                    **settings,
                    "web_research_model": model_name,
                },
            )

        batch_start = 0.02 + (0.16 * batch_index / total_batches)
        batch_end = 0.02 + (0.16 * (batch_index + 1) / total_batches)
        result = run_web_research_agent(
            research_source,
            provider=research_provider,
            model_name=model_name,
            llm_settings=llm_settings,
            config=ResearchAgentConfig(
                stage=stage,
                source_language=run_settings["source_language"],
                target_language=run_settings["target_language"],
                research_language=run_settings["research_language"],
                max_searches=run_settings["max_searches"],
                max_extractions=run_settings["max_extractions"],
                max_iterations=configured_iterations,
                max_source_chars=max(2_000, len(research_source) + 1),
                max_tool_result_chars=max(
                    2_000,
                    int(settings.get("web_research_result_chars") or 10_000),
                ),
                preferred_domains=tuple(run_settings["preferred_domains"]),
                blocked_domains=tuple(run_settings["blocked_domains"]),
                context_window_tokens=budget.context_window_tokens,
                context_input_fraction=budget.fraction,
            ),
            cancel_event=cancel_event,
            progress_callback=context._fraction_message_callback(
                progress,
                batch_start,
                batch_end,
            ),
            resume_state=resume_state,
            on_checkpoint=save_research_state,
            initial_ledger=merge_web_research_results(results),
        )
        if cancel_event.is_set():
            raise RuntimeError("Web research was canceled.")
        results.append(result)
    result = merge_web_research_results(results)
    progress(
        0.2,
        (
            f"Web research complete across {len(record_groups)} context batch(es) "
            f"after {result.response_count} model turn(s)"
        ),
    )
    if cancel_event.is_set():
        raise RuntimeError("Web research was canceled.")
    knowledge.merge_research(
        session_id,
        source_language=source_language,
        target_language=target_language,
        evidence=result.evidence,
        warnings=result.warnings,
        summary=result.summary,
    )
    if result.glossary:
        knowledge.merge_glossary(
            session_id,
            source_language=source_language,
            target_language=target_language,
            entries=result.glossary,
            origin="research",
        )
    return result


def research_metadata(result, run_id: str) -> dict[str, Any] | None:
    if result is None or not run_id:
        return None
    sources = []
    seen: set[str] = set()
    for item in result.evidence:
        url = str(item.get("source_url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        sources.append(
            {
                "url": url,
                "title": str(item.get("source_title") or ""),
            }
        )
    return {
        "agent_run_id": run_id,
        "summary": result.summary,
        "evidence_count": len(result.evidence),
        "glossary": list(result.glossary),
        "sources": sources,
        "warnings": list(result.warnings),
    }
