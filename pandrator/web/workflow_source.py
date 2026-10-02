"""Source acquisition and preparation jobs with explicit workflow dependencies."""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .database import Database
from .models import (
    AgentRun,
    AgentStep,
    Artifact,
    SessionRecord,
    SourceRecord,
    UsageEvent,
    new_id,
    utcnow,
)

Progress = Callable[[float, str | None], None]


class GenerationPlanStoreProtocol(Protocol):
    def __call__(
        self,
        session_id: str,
        records: list[dict[str, Any]],
        *,
        settings: dict[str, Any],
        source_revision_id: str | None = None,
        source_artifact_id: str | None = None,
    ) -> tuple[str, list[str]]: ...


class SourceCleaningProgressFactoryProtocol(Protocol):
    def __call__(
        self,
        progress: Progress,
        start: float,
        end: float,
        *,
        phase_names: list[str],
        phase_budgets: dict[str, int],
    ) -> Callable[[str], None]: ...


@dataclass(frozen=True, slots=True)
class SourceWorkflowContext:
    database: Database
    paths: DataPaths
    artifacts: ArtifactService
    _resolve_input: Callable[[str], tuple[Artifact, Path]]
    _session_dir: Callable[[str], Path]
    _operation_dir: Callable[[str, str], Path]
    _session_record: Callable[[str], SessionRecord]
    _store_generation_plan: GenerationPlanStoreProtocol
    _validate_download_url: Callable[[str], str]
    _scaled_progress_callback: Callable[[Progress, float, float], Progress]
    _fraction_message_callback: Callable[[Progress, float, float], Callable[[str], None]]
    _source_cleaning_progress_callback: SourceCleaningProgressFactoryProtocol


def download_source_url(
    context: SourceWorkflowContext,
    payload: dict[str, Any],
    progress: Progress,
    cancel_event: threading.Event,
) -> dict[str, Any]:
    import yt_dlp
    from yt_dlp.utils import DownloadError

    from .source_library import SourceLibraryService

    session_id = str(payload.get("session_id") or "")
    url = context._validate_download_url(str(payload.get("url") or ""))
    destination_dir = context._session_dir(session_id) / "sources"
    destination_dir.mkdir(parents=True, exist_ok=True)
    progress(0.03, "Inspecting source URL")
    download_fraction = 0.0
    last_reported_fraction = -1.0
    last_reported_bytes = 0
    last_reported_at = 0.0

    def download_progress(status: Mapping[str, Any]) -> None:
        nonlocal download_fraction, last_reported_fraction, last_reported_bytes, last_reported_at
        if cancel_event.is_set():
            raise DownloadError("Source download was canceled.")
        state = str(status.get("status") or "")
        if state == "downloading":
            downloaded = max(0, int(status.get("downloaded_bytes") or 0))
            total = max(
                0,
                int(status.get("total_bytes") or status.get("total_bytes_estimate") or 0),
            )
            if total:
                download_fraction = max(
                    download_fraction,
                    min(1.0, downloaded / total),
                )
            now = time.monotonic()
            should_report = (
                last_reported_fraction < 0
                or (total and download_fraction - last_reported_fraction >= 0.005)
                or (not total and downloaded - last_reported_bytes >= 4 * 1024 * 1024)
                or now - last_reported_at >= 1.0
            )
            if not should_report:
                return
            detail = (
                f"Downloading source — {round(download_fraction * 100)}%"
                if total
                else f"Downloading source — {downloaded / (1024 * 1024):.1f} MiB received"
            )
            progress(0.05 + download_fraction * 0.8, detail)
            last_reported_fraction = download_fraction
            last_reported_bytes = downloaded
            last_reported_at = now
        elif state == "finished":
            download_fraction = 1.0
            progress(0.88, "Source download complete; processing media")

    with yt_dlp.YoutubeDL(
        {
            "outtmpl": str(destination_dir / "%(title).160B-%(id)s.%(ext)s"),
            "restrictfilenames": True,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [download_progress],
        }
    ) as downloader:
        information = downloader.extract_info(url, download=True)
        output = Path(downloader.prepare_filename(information)).resolve()
    if cancel_event.is_set():
        return {}
    if destination_dir.resolve() not in output.parents or not output.is_file():
        raise RuntimeError("Downloaded source was not created in the managed session directory.")
    progress(0.93, "Registering downloaded source")
    source_metadata = {
        "original_filename": output.name,
        "source_url": url,
        "downloader": "yt-dlp",
    }
    artifact = context.artifacts.register(
        output,
        kind="source",
        role="upload",
        session_id=session_id,
        metadata=source_metadata,
    )
    with context.database.session() as session:
        session.add(
            SourceRecord(
                session_id=session_id,
                kind=output.suffix.lower().lstrip(".") or "url",
                display_name=output.name,
                artifact_id=artifact.id,
                content_hash=artifact.content_hash,
                metadata_json={"url": url, "downloader": "yt-dlp"},
            )
        )
    library = SourceLibraryService(context.database, context.artifacts)
    asset = library.ensure_for_artifact(
        artifact.id,
        display_name=output.name,
        kind=output.suffix.lower().lstrip(".") or "url",
    )
    library.attach(session_id, asset.id)
    progress(1.0, "Source download ready")
    return {"artifact_id": artifact.id, "filename": output.name}


def reuse_source(
    context: SourceWorkflowContext,
    payload: dict[str, Any],
    progress: Progress,
    cancel_event: threading.Event,
) -> dict[str, Any]:
    from .source_library import SourceLibraryService

    session_id = str(payload.get("session_id") or "")
    source, source_path = context._resolve_input(str(payload.get("artifact_id") or ""))
    destination_dir = context._session_dir(session_id) / "sources"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{source.id}-{source_path.name}"
    progress(0.2, "Copying reusable source")
    shutil.copy2(source_path, destination)
    if cancel_event.is_set():
        destination.unlink(missing_ok=True)
        return {}
    artifact = context.artifacts.register(
        destination,
        kind="source",
        role="upload",
        session_id=session_id,
        parent_ids=[source.id],
        metadata={"original_filename": source_path.name, "reused_from": source.id},
    )
    with context.database.session() as session:
        session.add(
            SourceRecord(
                session_id=session_id,
                kind=destination.suffix.lower().lstrip(".") or "file",
                display_name=source_path.name,
                artifact_id=artifact.id,
                content_hash=artifact.content_hash,
                metadata_json={"reused_from": source.id},
            )
        )
    library = SourceLibraryService(context.database, context.artifacts)
    asset = library.ensure_for_artifact(
        artifact.id,
        display_name=source_path.name,
        kind=destination.suffix.lower().lstrip(".") or "file",
    )
    library.attach(session_id, asset.id)
    progress(1.0, "Reusable source ready")
    return {"artifact_id": artifact.id, "filename": source_path.name}


def clean_source(
    context: SourceWorkflowContext,
    payload: dict[str, Any],
    progress: Progress,
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Run deterministic extraction and the optional auditable agentic pipeline."""
    from pandrator.logic import file_handler, source_cleaning

    session_id = str(payload.get("session_id") or "")
    agent_run_id = str(payload.get("agent_run_id") or "")
    if agent_run_id:
        with context.database.session() as session:
            run = session.get(AgentRun, agent_run_id)
            if run is not None:
                run.status = "running"
                run.updated_at = utcnow()
    source_artifact, source_path = context._resolve_input(
        str(payload.get("source_artifact_id") or "")
    )
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
        cleaned_text = source_path.read_text(encoding="utf-8-sig")
        baseline_text = cleaned_text
    elif extension == ".epub":
        cleaned_text = file_handler.extract_text_from_epub(
            str(source_path),
            remove_footnotes=bool(settings.get("remove_footnotes", False)),
            filter_citations=bool(settings.get("filter_citations", True)),
        )
        baseline_text = cleaned_text
    elif extension == ".pdf":
        document = source_cleaning.build_source_document(
            str(source_path),
            pdf_config=pdf_config,
            artifact_dir=str(context._session_dir(session_id) / "source_ingestion"),
            progress_callback=context._fraction_message_callback(
                progress,
                0.05,
                0.35,
            ),
        )
        deterministic_operations = source_cleaning.propose_deterministic_operations(
            document,
            remove_footnotes=bool(settings.get("remove_footnotes", False)),
            remove_toc=bool(settings.get("pdf_remove_toc", True)),
            remove_repeated_marginals=bool(settings.get("pdf_remove_repeated_marginals", True)),
        )
        baseline_text = document.plain_text()
        cleaned_text = source_cleaning.apply_cleaning_operations(
            document, deterministic_operations
        ).cleaned_text
    elif extension in {".docx", ".mobi"}:
        extracted = context._session_dir(session_id) / f"{source_path.stem}_extracted.txt"
        if not file_handler.convert_doc_to_text(str(source_path), str(extracted)):
            raise RuntimeError(f"Could not extract text from {source_path.name}.")
        cleaned_text = extracted.read_text(encoding="utf-8-sig")
        baseline_text = cleaned_text
    else:
        raise ValueError(f"Unsupported document type: {extension or 'unknown'}")
    if cancel_event.is_set():
        return {}
    progress(0.38, "Source extraction complete")
    extraction = "deterministic"
    report: dict[str, Any] = {}
    if bool(settings.get("agentic", False)):
        from .provider_settings import build_llm_settings

        progress(0.4, "Building source-cleaning index")
        if extension == ".epub":
            document = source_cleaning.build_cleaned_epub_source_document(
                str(source_path),
                cleaned_text,
            )
            deterministic_operations = source_cleaning.propose_embedded_chapter_operations(document)
        elif extension == ".pdf":
            document = source_cleaning.build_source_document(
                str(source_path),
                pdf_config=pdf_config,
                artifact_dir=str(context._session_dir(session_id) / "source_ingestion"),
                progress_callback=context._fraction_message_callback(
                    progress,
                    0.4,
                    0.45,
                ),
            )
        else:
            from pandrator.logic.source_cleaning.pdf_text_adapter import (
                build_source_document_from_text,
            )

            document = build_source_document_from_text(
                cleaned_text,
                source_path=str(source_path),
                filename=source_path.name,
            )
        llm_settings, model_name = build_llm_settings(
            context.database,
            context.paths,
            requested_model=str(settings.get("model_name") or settings.get("default_model") or ""),
            request_timeout_seconds=int(settings.get("request_timeout_seconds") or 600),
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
            progress_callback=context._source_cleaning_progress_callback(
                progress,
                0.45,
                0.9,
                phase_names=phase_names,
                phase_budgets=phase_budgets,
            ),
            stop_event=cancel_event,
        )
        if cancel_event.is_set():
            return {}
        progress(0.9, "Source-cleaning analysis complete")
        all_operations = [*deterministic_operations, *pipeline.all_operations]
        cleaning_result = source_cleaning.apply_cleaning_operations(document, all_operations)
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
        audit_dir = context._session_dir(session_id) / "source_cleaning"
        source_cleaning.write_cleaning_artifacts(
            document,
            all_operations,
            cleaning_result,
            str(audit_dir),
        )
        usage = pipeline.llm_usage
        models = list(usage.get("models") or [])
        details_raw = usage.get("token_details")
        details = details_raw if isinstance(details_raw, dict) else {}
        with context.database.session() as session:
            session.add(
                UsageEvent(
                    session_id=session_id,
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
            )
        extraction = "agentic"
    progress(0.93, "Saving source-cleaning artifacts")
    comparison_dir = context._session_dir(session_id) / "source_cleaning"
    comparison_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = comparison_dir / f"extracted-{new_id()}.txt"
    baseline_path.write_text(baseline_text, encoding="utf-8", newline="\n")
    baseline_artifact = context.artifacts.register(
        baseline_path,
        kind="text",
        role="extracted_text",
        session_id=session_id,
        parent_ids=[source_artifact.id],
        metadata={"comparison_source": True, "source_filename": source_path.name},
    )
    destination = (
        context._operation_dir(session_id, "clean-source") / f"{source_path.stem}_cleaned.txt"
    )
    destination.write_text(cleaned_text, encoding="utf-8", newline="\n")
    artifact = context.artifacts.register(
        destination,
        kind="text",
        role="clean_text",
        session_id=session_id,
        parent_ids=[source_artifact.id, baseline_artifact.id],
        settings=settings,
        metadata={"extraction": extraction, "report": report},
    )
    if agent_run_id:
        pipeline_report_raw = report.get("pipeline")
        pipeline_report = pipeline_report_raw if isinstance(pipeline_report_raw, dict) else {}
        phases_raw = pipeline_report.get("phases")
        phases = phases_raw if isinstance(phases_raw, list) else []
        with context.database.session() as session:
            run = session.get(AgentRun, agent_run_id)
            if run is not None:
                run.status = "completed"
                run.result_artifact_id = artifact.id
                run.updated_at = utcnow()
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
    progress(0.98, "Cleaned source artifacts registered")
    progress(1.0, "Source text ready")
    return {
        "artifact_id": artifact.id,
        "path": artifact.relative_path,
        "characters": len(cleaned_text),
        "report": report,
    }


def prepare_source_cleaning_dispatch(
    context: SourceWorkflowContext,
    payload: dict[str, Any],
    progress: Progress,
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Prepare durable PDF/EPUB evidence without invoking a model provider."""
    from .source_cleaning_dispatch import prepare_source_cleaning_dispatch_job

    return prepare_source_cleaning_dispatch_job(
        context.database,
        context.artifacts,
        context._session_dir,
        payload,
        progress,
        cancel_event,
    )


def prepare_text(
    context: SourceWorkflowContext,
    payload: dict[str, Any],
    progress: Progress,
    cancel_event: threading.Event,
) -> dict[str, Any]:
    from pandrator.logic.audiobook_chunking import audiobook_chunk_budget
    from pandrator.logic.text_preprocessor import preprocess_text

    from .settings_policy import BUILTIN_DEFAULTS

    session_id = str(payload.get("session_id") or "")
    source_artifact, source_path = context._resolve_input(
        str(payload.get("source_artifact_id") or "")
    )
    settings = dict(payload.get("settings") or {})
    if source_path.suffix.lower() not in {".txt", ".md"}:
        raise ValueError("Prepare narration requires a cleaned text artifact.")
    text = source_path.read_text(encoding="utf-8-sig")
    record = context._session_record(session_id)
    source_language = str(record.source_language or "auto")
    text_defaults = BUILTIN_DEFAULTS["text"]
    captured_tts = settings.get("_audiobook_tts_settings")
    if not isinstance(captured_tts, dict):
        snapshot = payload.get("resolved_settings_snapshot")
        captured_tts = (
            snapshot.get("tts")
            if isinstance(snapshot, dict) and isinstance(snapshot.get("tts"), dict)
            else {"service": "XTTS"}
        )
    budget = audiobook_chunk_budget(settings, captured_tts, source_language)
    progress(0.1, "Segmenting narration")
    supplied_markup = (source_artifact.metadata_json or {}).get("speech_markup") or {}
    if supplied_markup:
        from pandrator.logic.speech_markup import parse_speech_markup

        from .generation_controls import get_generation_controls

        with context.database.session() as db:
            characters = get_generation_controls(db, session_id)["characters"]
        structures = [
            parse_speech_markup(xml, expected_segment_id=str(key), characters=characters)
            for key, xml in sorted(supplied_markup.items(), key=lambda item: int(item[0]))
        ]
        if " ".join(text.split()) != " ".join(
            " ".join(item.transcript for item in structures).split()
        ):
            raise ValueError("Annotated text changed. Reannotate it before preparing narration.")
        prepared = [
            {
                "original_sentence": item.transcript,
                "speech_plan": {"speech_xml": item.xml},
                "paragraph_break_after": item.boundary_after in {"paragraph", "scene", "chapter"},
                "speech_boundary_after": item.boundary_after,
            }
            for item in structures
        ]
    else:
        prepared = preprocess_text(
            text,
            {
                "source_file": str(source_path),
                "language": source_language,
                # Segmentation is intentionally provider-independent.  This
                # selects the shared multilingual sentence tokenizer only.
                "tts_service": "XTTS",
                # XTTS remains the tokenizer choice for language-aware
                # splitting; the captured provider budget controls only
                # the target length.
                "max_sentence_length": budget["target_chars"],
                "enable_sentence_splitting": bool(
                    settings.get(
                        "enable_sentence_splitting",
                        text_defaults["enable_sentence_splitting"],
                    )
                ),
                "enable_sentence_appending": bool(
                    settings.get(
                        "enable_sentence_appending",
                        text_defaults["enable_sentence_appending"],
                    )
                ),
                "enable_nemo_normalization": bool(
                    settings.get(
                        "enable_nemo_normalization",
                        text_defaults["enable_nemo_normalization"],
                    )
                ),
                "remove_diacritics": bool(
                    settings.get("remove_diacritics", text_defaults["remove_diacritics"])
                ),
                "remove_quotation_marks": bool(
                    settings.get(
                        "remove_quotation_marks",
                        text_defaults["remove_quotation_marks"],
                    )
                ),
                "normalize_all_caps": bool(
                    settings.get("normalize_all_caps", text_defaults["normalize_all_caps"])
                ),
            },
            progress_callback=context._scaled_progress_callback(progress, 0.1, 0.85),
        )
    if cancel_event.is_set():
        return {}
    segment_lengths = [
        len(
            str(
                item.get("text")
                or item.get("original_sentence")
                or item.get("tts_optimized_sentence")
                or ""
            )
        )
        for item in prepared
        if isinstance(item, dict)
    ]
    segment_summary = {
        "count": len(segment_lengths),
        "min_chars": min(segment_lengths) if segment_lengths else 0,
        "max_chars": max(segment_lengths) if segment_lengths else 0,
        "average_chars": round(sum(segment_lengths) / len(segment_lengths), 2)
        if segment_lengths
        else 0,
        "over_target_count": sum(
            length > int(budget["target_chars"]) for length in segment_lengths
        ),
    }
    annotated_source = bool(supplied_markup)
    segmentation = {
        "budget": budget,
        "segment_length_summary": segment_summary,
        "policy_applied": not annotated_source,
        "policy_explanation": (
            "Annotated speech markup is immutable; the chunk policy was recorded "
            "but did not repack its segments."
            if annotated_source
            else "The resolved audiobook chunk policy was applied during preparation."
        ),
    }
    artifact_settings = dict(settings)
    artifact_settings["_audiobook_chunk_budget"] = deepcopy(budget)
    progress(0.9, "Saving narration segments")
    destination = context._operation_dir(session_id, "prepare-text") / "prepared_narration.json"
    destination.write_text(
        json.dumps(prepared, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    artifact = context.artifacts.register(
        destination,
        kind="json",
        role="prepared_text",
        session_id=session_id,
        parent_ids=[source_artifact.id],
        settings=artifact_settings,
        metadata={"segment_count": len(prepared), "segmentation": segmentation},
    )
    generation_revision_id, _segment_ids = context._store_generation_plan(
        session_id,
        prepared,
        settings=artifact_settings,
        source_revision_id=str((source_artifact.metadata_json or {}).get("revision_id") or "")
        or None,
        source_artifact_id=artifact.id,
    )
    progress(1.0, "Narration segments ready")
    return {
        "artifact_id": artifact.id,
        "path": artifact.relative_path,
        "segments": len(prepared),
        "generation_plan_revision_id": generation_revision_id,
        "segmentation": segmentation,
        "segmentation_budget": budget,
    }
