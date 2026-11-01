"""Native source cleaning with current authority and atomic publication."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from .access.contracts import AccessDenied
from .access.session_policy import SessionNotFound
from .access.source_preparation_policy import require_source_cleaning_access
from .accounts.execution_actor import current_execution_actor
from .job_execution import current_job_lease
from .models import AgentRun, AgentStep, Artifact, UsageEvent, new_id, utcnow
from .usage_identity import bind_usage_ownership

if TYPE_CHECKING:
    from .workflow_source_context import Progress, SourceWorkflowContext


class _CleaningStopView:
    def __init__(self, event: threading.Event, denied: Callable[[], bool]) -> None:
        self._event = event
        self._denied = denied

    def is_set(self) -> bool:
        return self._event.is_set() or self._denied()


def _run_filters(
    run_id: str,
    session_id: str,
    source_id: str,
    present_actor: bool,
    job_id: str | None,
) -> list[Any]:
    filters = [AgentRun.id == run_id, AgentRun.session_id == session_id]
    if present_actor:
        filters.extend(
            (
                AgentRun.source_artifact_id == source_id,
                AgentRun.kind == "source_cleaning",
                AgentRun.job_id == job_id if job_id is not None else AgentRun.job_id.is_(None),
            )
        )
    return filters


def _load_source(
    context: SourceWorkflowContext,
    session_id: str,
    source_id: str,
    agent_run_id: str,
) -> tuple[str, Path]:
    if current_execution_actor.get() is None:
        source, path = context._resolve_input(source_id)
        return source.id, path
    with context.database.snapshot_session() as unit:
        require_source_cleaning_access(unit, session_id, source_id, agent_run_id=agent_run_id)
        row = unit.execute(
            select(Artifact.id, Artifact.relative_path).where(
                Artifact.id == source_id,
                Artifact.state != "deleted",
            )
        ).one_or_none()
        if row is None:
            raise KeyError("Source artifact not found.")
        try:
            path = context.paths.managed_path(row.relative_path)
        except (OSError, ValueError):
            raise KeyError("Source artifact not found.") from None
        if not path.is_file():
            raise KeyError("Source artifact not found.")
        return row.id, path


def clean_source(
    context: SourceWorkflowContext,
    payload: dict[str, Any],
    progress: Progress,
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Run deterministic extraction and the optional auditable agentic pipeline."""
    session_id = str(payload.get("session_id") or "")
    agent_run_id = str(payload.get("agent_run_id") or "")
    source_id = str(payload.get("source_artifact_id") or "")
    present_actor = current_execution_actor.get() is not None
    denied: AccessDenied | SessionNotFound | KeyError | None = None

    def prove() -> None:
        nonlocal denied
        if not present_actor:
            return
        if denied is not None:
            raise denied
        try:
            with context.database.snapshot_session() as unit:
                require_source_cleaning_access(
                    unit,
                    session_id,
                    source_id,
                    agent_run_id=agent_run_id,
                )
        except (AccessDenied, SessionNotFound, KeyError) as error:
            denied = error
            raise

    source_id, source_path = _load_source(context, session_id, source_id, agent_run_id)
    lease = current_job_lease.get() if present_actor else None
    job_id = lease["job_id"] if lease is not None else None
    if agent_run_id:
        with context.database.immediate_session() as unit:
            require_source_cleaning_access(
                unit,
                session_id,
                source_id,
                agent_run_id=agent_run_id,
            )
            unit.execute(
                update(AgentRun)
                .where(
                    *_run_filters(agent_run_id, session_id, source_id, present_actor, job_id),
                    AgentRun.status.in_(("queued", "running", "retrying")),
                )
                .values(status="running", updated_at=utcnow())
            )
    from pandrator.logic import file_handler, source_cleaning

    original_progress = progress

    def guarded_progress(value: float, detail: str | None = None) -> None:
        prove()
        try:
            original_progress(value, detail)
        finally:
            prove()

    if present_actor:
        progress = guarded_progress
    stop_event = (
        _CleaningStopView(cancel_event, lambda: denied is not None)
        if present_actor
        else cancel_event
    )
    operation_directory: Path | None = None

    def operation_dir() -> Path:
        nonlocal operation_directory
        if operation_directory is None:
            prove()
            operation_directory = context._operation_dir(session_id, "clean-source")
            prove()
        return operation_directory

    def session_dir() -> Path:
        prove()
        directory = context._session_dir(session_id)
        prove()
        return directory

    def fraction(start: float, end: float) -> Callable[[str], None]:
        prove()
        callback = context._fraction_message_callback(progress, start, end)
        prove()
        return callback

    settings = dict(payload.get("settings") or {})
    pdf_config = source_cleaning.PDFIngestionConfig(
        ocr_mode=str(settings.get("pdf_ocr_mode") or "auto"),
        ocr_language=str(settings.get("pdf_ocr_language") or "auto"),
        ocr_dpi=int(settings.get("pdf_ocr_dpi") or 200),
    )
    deterministic_operations: list[dict[str, Any]] = []
    baseline_text = ""
    progress(0.05, "Extracting source text")
    extension = source_path.suffix.lower()
    if extension == ".txt":
        prove()
        cleaned_text = source_path.read_text(encoding="utf-8-sig")
        baseline_text = cleaned_text
    elif extension == ".epub":
        prove()
        cleaned_text = file_handler.extract_text_from_epub(
            str(source_path),
            remove_footnotes=bool(settings.get("remove_footnotes", False)),
            filter_citations=bool(settings.get("filter_citations", True)),
            epub_table_mode=settings.get("epub_table_mode", "rows"),
            epub_image_mode=settings.get("epub_image_mode", "skip"),
        )
        baseline_text = cleaned_text
    elif extension == ".pdf":
        ingestion_dir = session_dir() / "source_ingestion"
        extraction_progress = fraction(0.05, 0.35)
        prove()
        document = source_cleaning.build_source_document(
            str(source_path),
            pdf_config=pdf_config,
            artifact_dir=str(ingestion_dir),
            progress_callback=extraction_progress,
        )
        prove()
        deterministic_operations = source_cleaning.propose_deterministic_operations(
            document,
            remove_footnotes=bool(settings.get("remove_footnotes", False)),
            remove_toc=bool(settings.get("pdf_remove_toc", True)),
            remove_repeated_marginals=bool(settings.get("pdf_remove_repeated_marginals", True)),
        )
        prove()
        baseline_text = document.plain_text()
        prove()
        cleaned_text = source_cleaning.apply_cleaning_operations(
            document, deterministic_operations
        ).cleaned_text
    elif extension in {".docx", ".mobi"}:
        extracted = operation_dir() / f"{source_path.stem}_extracted.txt"
        prove()
        if not file_handler.convert_doc_to_text(str(source_path), str(extracted)):
            raise RuntimeError(f"Could not extract text from {source_path.name}.")
        prove()
        cleaned_text = extracted.read_text(encoding="utf-8-sig")
        baseline_text = cleaned_text
    else:
        raise ValueError(f"Unsupported document type: {extension or 'unknown'}")
    prove()
    if cancel_event.is_set():
        return {}
    progress(0.38, "Source extraction complete")
    extraction = "deterministic"
    report: dict[str, Any] = {}
    if bool(settings.get("agentic", False)):
        from .provider_settings import build_llm_settings

        progress(0.4, "Building source-cleaning index")
        if extension == ".epub":
            prove()
            document = source_cleaning.build_cleaned_epub_source_document(
                str(source_path),
                cleaned_text,
            )
            prove()
            deterministic_operations = source_cleaning.propose_embedded_chapter_operations(document)
        elif extension == ".pdf":
            ingestion_dir = session_dir() / "source_ingestion"
            extraction_progress = fraction(0.4, 0.45)
            prove()
            document = source_cleaning.build_source_document(
                str(source_path),
                pdf_config=pdf_config,
                artifact_dir=str(ingestion_dir),
                progress_callback=extraction_progress,
            )
            prove()
        else:
            from pandrator.logic.source_cleaning.pdf_text_adapter import (
                build_source_document_from_text,
            )

            prove()
            document = build_source_document_from_text(
                cleaned_text,
                source_path=str(source_path),
                filename=source_path.name,
            )
        prove()
        llm_settings, model_name = build_llm_settings(
            context.database,
            context.paths,
            requested_model=str(settings.get("model_name") or settings.get("default_model") or ""),
            request_timeout_seconds=int(settings.get("request_timeout_seconds") or 600),
            session_id=session_id,
        )
        total_iterations = max(1, int(settings.get("max_iterations") or 53))
        phase_iterations = settings.get("phase_max_iterations")
        requested_phase_names = (
            settings.get("phase_names") if isinstance(settings.get("phase_names"), list) else None
        )
        phase_names = list(requested_phase_names or source_cleaning.PHASE_ORDER)
        phase_budgets = source_cleaning.resolve_phase_max_iterations(
            phase_iterations if isinstance(phase_iterations, dict) else None,
            total=total_iterations,
            phase_names=phase_names,
        )
        prove()
        phase_progress = context._source_cleaning_progress_callback(
            progress,
            0.45,
            0.9,
            phase_names=phase_names,
            phase_budgets=phase_budgets,
        )
        prove()
        completion_kwargs: dict[str, Any] = {}
        if present_actor:
            from .source_cleaning_llm import build_source_cleaning_completion

            completion_kwargs["completion_func"] = build_source_cleaning_completion(
                context.database,
                context.paths,
                session_id,
                model_name,
                int(settings.get("request_timeout_seconds") or 600),
                prove,
                job_id=job_id,
                cancel_event=cancel_event,
            )
        prove()
        pipeline = source_cleaning.run_cleaning_pipeline(
            document,
            llm_settings=llm_settings,
            config=source_cleaning.SourceCleaningPipelineConfig(
                model_name=model_name,
                remove_footnotes=bool(settings.get("remove_footnotes", False)),
                filter_citations=bool(settings.get("filter_citations", True)),
                total_max_iterations=total_iterations,
                phase_max_iterations=phase_iterations
                if isinstance(phase_iterations, dict)
                else None,
                phase_names=requested_phase_names,
                baseline_operations=deterministic_operations,
            ),
            progress_callback=phase_progress,
            stop_event=stop_event,
            **completion_kwargs,
        )
        prove()
        if cancel_event.is_set():
            return {}
        progress(0.9, "Source-cleaning analysis complete")
        all_operations = [*deterministic_operations, *pipeline.all_operations]
        prove()
        cleaning_result = source_cleaning.apply_cleaning_operations(document, all_operations)
        prove()
        validation = source_cleaning.validate_cleaning_result(
            document,
            cleaning_result,
            remove_footnotes=bool(settings.get("remove_footnotes", False)),
        )
        cleaned_text = cleaning_result.cleaned_text
        report = {
            **cleaning_result.report,
            "pipeline": pipeline.to_dict(),
            "validation": validation.to_dict(),
            "warnings": pipeline.warnings + validation.warnings + cleaning_result.warnings,
        }
        audit_dir = session_dir() / "source_cleaning"
        prove()
        source_cleaning.write_cleaning_artifacts(
            document,
            all_operations,
            cleaning_result,
            str(audit_dir),
        )
        if not present_actor:
            usage = pipeline.llm_usage
            models = list(usage.get("models") or [])
            details_raw = usage.get("token_details")
            details = details_raw if isinstance(details_raw, dict) else {}
            with context.database.immediate_session() as session:
                event = UsageEvent(
                    session_id=session_id,
                    job_id=str(payload.get("_job_id") or "") or None,
                    stage="source_cleaning",
                    provider_key=(
                        models[0].split("/", 1)[0] if models else model_name.split("/", 1)[0]
                    ),
                    model_id=(models[0] if models else model_name),
                    input_tokens=int(usage.get("prompt_tokens") or 0),
                    cached_input_tokens=int(details.get("cached_tokens") or 0),
                    output_tokens=int(usage.get("completion_tokens") or 0),
                    cost_usd=float(usage["cost_usd"])
                    if usage.get("cost_usd") is not None
                    else None,
                    cost_source=",".join(usage.get("cost_sources") or []) or None,
                    raw_usage_json=usage,
                )
                bind_usage_ownership(session, event)
                session.add(event)
        extraction = "agentic"
    progress(0.93, "Saving source-cleaning artifacts")
    comparison_dir = session_dir() / "source_cleaning"
    prove()
    comparison_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = comparison_dir / f"extracted-{new_id()}.txt"
    prove()
    baseline_path.write_text(baseline_text, encoding="utf-8", newline="\n")
    destination = operation_dir() / f"{source_path.stem}_cleaned.txt"
    prove()
    destination.write_text(cleaned_text, encoding="utf-8", newline="\n")
    prove()
    baseline_prepared = context.artifacts.prepare_registration(baseline_path)
    prove()
    clean_prepared = context.artifacts.prepare_registration(destination, settings=settings)
    prove()
    if cancel_event.is_set():
        return {}
    with context.database.immediate_session() as session:
        require_source_cleaning_access(
            session,
            session_id,
            source_id,
            agent_run_id=agent_run_id,
        )
        baseline_artifact = context.artifacts.register_in_session(
            session,
            baseline_path,
            kind="text",
            role="extracted_text",
            session_id=session_id,
            parent_ids=[source_id],
            metadata={"comparison_source": True, "source_filename": source_path.name},
            _prepared=baseline_prepared,
        )
        artifact = context.artifacts.register_in_session(
            session,
            destination,
            kind="text",
            role="clean_text",
            session_id=session_id,
            parent_ids=[source_id, baseline_artifact.id],
            settings=settings,
            metadata={"extraction": extraction, "report": report},
            _prepared=clean_prepared,
        )
        artifact_id, artifact_path = artifact.id, artifact.relative_path
        if agent_run_id:
            pipeline_report_raw = report.get("pipeline")
            pipeline_report = pipeline_report_raw if isinstance(pipeline_report_raw, dict) else {}
            phases_raw = pipeline_report.get("phases")
            phases = phases_raw if isinstance(phases_raw, list) else []
            matched = session.scalar(
                update(AgentRun)
                .where(
                    *_run_filters(agent_run_id, session_id, source_id, present_actor, job_id),
                )
                .values(status="completed", result_artifact_id=artifact_id, updated_at=utcnow())
                .returning(AgentRun.id)
            )
            if matched is not None:
                for ordinal, phase in enumerate(phases):
                    safe_phase = phase if isinstance(phase, dict) else {"name": str(phase)}
                    operations_raw = safe_phase.get("operations")
                    operations = operations_raw if isinstance(operations_raw, list) else []
                    warnings_raw = safe_phase.get("warnings")
                    warnings = warnings_raw if isinstance(warnings_raw, list) else []
                    operation_types = sorted(
                        {
                            str(item.get("type") or item.get("operation") or "edit")
                            for item in operations
                            if isinstance(item, dict)
                        }
                    )
                    session.add(
                        AgentStep(
                            agent_run_id=agent_run_id,
                            ordinal=ordinal,
                            phase=str(
                                safe_phase.get("name")
                                or safe_phase.get("phase")
                                or f"Phase {ordinal + 1}"
                            ),
                            status=str(safe_phase.get("status") or "completed"),
                            summary=str(
                                safe_phase.get("summary")
                                or f"{len(operations)} proposed operation(s), {len(warnings)} warning(s)."
                            ),
                            input_json={"operation_count": len(operations)},
                            output_json={
                                "warnings": warnings,
                                "operation_types": operation_types,
                            },
                        )
                    )
    original_progress(0.98, "Cleaned source artifacts registered")
    original_progress(1.0, "Source text ready")
    return {
        "artifact_id": artifact_id,
        "path": artifact_path,
        "characters": len(cleaned_text),
        "report": report,
    }
