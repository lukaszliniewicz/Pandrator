"""Source acquisition and preparation jobs with explicit workflow dependencies."""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import load_only

from .access.contracts import AccessDenied
from .access.session_policy import SessionNotFound
from .access.source_ingress_policy import require_source_ingress_access
from .access.source_preparation_policy import require_text_preparation_access
from .accounts.execution_actor import current_execution_actor
from .models import (
    Artifact,
    SourceRecord,
    new_id,
)
from .projects.identity import session_project
from .projects.source_identity import source_creation_workspace
from .workflow_source_context import GenerationPlanStoreProtocol as GenerationPlanStoreProtocol
from .workflow_source_context import Progress as Progress
from .workflow_source_context import (
    SourceCleaningProgressFactoryProtocol as SourceCleaningProgressFactoryProtocol,
)
from .workflow_source_context import SourceWorkflowContext as SourceWorkflowContext


def _check_source_ingress(
    context: SourceWorkflowContext,
    session_id: str,
    job_kind: str,
    *,
    artifact_id: str | None = None,
) -> None:
    if current_execution_actor.get() is None:
        return
    with context.database.snapshot_session() as unit:
        require_source_ingress_access(
            unit,
            session_id,
            job_kind,
            artifact_id=artifact_id,
        )


def _forward_source_progress(
    context: SourceWorkflowContext,
    session_id: str,
    job_kind: str,
    progress: Progress,
    value: float,
    detail: str | None,
    *,
    artifact_id: str | None = None,
) -> None:
    _check_source_ingress(
        context,
        session_id,
        job_kind,
        artifact_id=artifact_id,
    )
    progress(value, detail)
    _check_source_ingress(
        context,
        session_id,
        job_kind,
        artifact_id=artifact_id,
    )


def _load_reusable_source(
    context: SourceWorkflowContext,
    session_id: str,
    artifact_id: str,
) -> tuple[Artifact, Path]:
    if current_execution_actor.get() is None:
        source, source_path = context._resolve_input(artifact_id)
        with context.database.session() as unit:
            source_creation_workspace(
                unit,
                source,
                workspace_id=session_project(unit, session_id).workspace_id,
            )
        return source, source_path

    with context.database.snapshot_session() as unit:
        require_source_ingress_access(
            unit,
            session_id,
            "source.reuse",
            artifact_id=artifact_id,
        )
        source = unit.scalar(
            select(Artifact)
            .where(
                Artifact.id == artifact_id,
                Artifact.state != "deleted",
            )
            .options(
                load_only(
                    Artifact.id,
                    Artifact.workspace_id,
                    Artifact.project_id,
                    Artifact.session_id,
                    Artifact.relative_path,
                    Artifact.state,
                    raiseload=True,
                )
            )
        )
        if source is None:
            raise KeyError("Source artifact not found.")

        source_creation_workspace(
            unit,
            source,
            workspace_id=session_project(unit, session_id).workspace_id,
        )
        try:
            source_path = context.paths.managed_path(source.relative_path)
        except (OSError, ValueError):
            raise KeyError("Source artifact not found.") from None
        if not source_path.is_file():
            raise KeyError("Source artifact not found.")
        unit.expunge(source)
    return source, source_path


def _publish_ingressed_source(
    context: SourceWorkflowContext,
    session_id: str,
    job_kind: str,
    path: Path,
    *,
    kind: str,
    display_name: str,
    artifact_metadata: dict[str, Any],
    source_metadata: dict[str, Any],
    parent_ids: list[str] | None = None,
    artifact_id: str | None = None,
) -> dict[str, str]:
    _check_source_ingress(
        context,
        session_id,
        job_kind,
        artifact_id=artifact_id,
    )
    prepared = context.artifacts.prepare_registration(path)

    from .source_library import SourceLibraryService

    with context.database.immediate_session() as unit:
        require_source_ingress_access(
            unit,
            session_id,
            job_kind,
            artifact_id=artifact_id,
        )
        artifact = context.artifacts.register_in_session(
            unit,
            path,
            kind="source",
            role="upload",
            session_id=session_id,
            parent_ids=parent_ids,
            metadata=artifact_metadata,
            _prepared=prepared,
        )
        published_artifact_id = artifact.id
        content_hash = artifact.content_hash
        unit.add(
            SourceRecord(
                session_id=session_id,
                kind=kind,
                display_name=display_name,
                artifact_id=published_artifact_id,
                content_hash=content_hash,
                metadata_json=source_metadata,
            )
        )
        library = SourceLibraryService(context.database, context.artifacts)
        asset = library.ensure_for_artifact_in_session(
            unit,
            published_artifact_id,
            display_name=display_name,
            kind=kind,
        )
        library.attach_in_session(unit, session_id, asset.id)
        result = {"artifact_id": published_artifact_id, "filename": display_name}
    return result


def _check_text_preparation_access(
    context: SourceWorkflowContext,
    session_id: str,
    source_artifact_id: str,
    *,
    require_metadata: bool = False,
) -> bool:
    if current_execution_actor.get() is None:
        return True
    with context.database.snapshot_session() as unit:
        return require_text_preparation_access(
            unit,
            session_id,
            source_artifact_id,
            require_metadata=require_metadata,
        )


def _load_text_preparation_source(
    context: SourceWorkflowContext,
    session_id: str,
    source_artifact_id: str,
) -> tuple[str, Path, dict[str, Any]]:
    if current_execution_actor.get() is None:
        source, source_path = context._resolve_input(source_artifact_id)
        return str(source.id), source_path, source.metadata_json or {}

    with context.database.snapshot_session() as unit:
        ordinary_visibility = require_text_preparation_access(
            unit,
            session_id,
            source_artifact_id,
        )
        if ordinary_visibility:
            row = unit.execute(
                select(Artifact.id, Artifact.relative_path, Artifact.metadata_json).where(
                    Artifact.id == source_artifact_id,
                    Artifact.state != "deleted",
                )
            ).one_or_none()
            if row is None:
                raise KeyError("Source artifact not found.")
            source_id, relative_path, metadata_value = row
            if isinstance(metadata_value, Mapping):
                metadata = deepcopy(dict(metadata_value))
            else:
                metadata = {}
        else:
            row = unit.execute(
                select(Artifact.id, Artifact.relative_path).where(
                    Artifact.id == source_artifact_id,
                    Artifact.state != "deleted",
                )
            ).one_or_none()
            if row is None:
                raise KeyError("Source artifact not found.")
            source_id, relative_path = row
            metadata = {}

        try:
            source_path = context.paths.managed_path(relative_path)
        except (OSError, ValueError):
            raise KeyError("Source artifact not found.") from None
        if not source_path.is_file():
            raise KeyError("Source artifact not found.")
    return str(source_id), source_path, metadata


def _forward_text_preparation_progress(
    context: SourceWorkflowContext,
    session_id: str,
    source_artifact_id: str,
    progress: Progress,
    value: float,
    detail: str | None,
    *,
    require_metadata: bool = False,
) -> None:
    if current_execution_actor.get() is None:
        progress(value, detail)
        return
    _check_text_preparation_access(
        context,
        session_id,
        source_artifact_id,
        require_metadata=require_metadata,
    )
    progress(value, detail)
    _check_text_preparation_access(
        context,
        session_id,
        source_artifact_id,
        require_metadata=require_metadata,
    )


def _text_preparation_progress_callback(
    context: SourceWorkflowContext,
    session_id: str,
    source_artifact_id: str,
    progress: Progress,
    *,
    require_metadata: bool = False,
) -> Progress:
    if current_execution_actor.get() is None:
        return progress

    def guarded(value: float, detail: str | None = None) -> None:
        _forward_text_preparation_progress(
            context,
            session_id,
            source_artifact_id,
            progress,
            value,
            detail,
            require_metadata=require_metadata,
        )

    return guarded


def download_source_url(
    context: SourceWorkflowContext,
    payload: dict[str, Any],
    progress: Progress,
    cancel_event: threading.Event,
) -> dict[str, Any]:
    session_id = str(payload.get("session_id") or "")
    _check_source_ingress(context, session_id, "source.download_url")

    import yt_dlp
    from yt_dlp.utils import DownloadError

    url = context._validate_download_url(str(payload.get("url") or ""))
    destination_dir = context._session_dir(session_id) / "sources" / new_id()
    _check_source_ingress(context, session_id, "source.download_url")
    destination_dir.mkdir(parents=True, exist_ok=True)
    _forward_source_progress(
        context,
        session_id,
        "source.download_url",
        progress,
        0.03,
        "Inspecting source URL",
    )
    download_fraction = 0.0
    last_reported_fraction = -1.0
    last_reported_bytes = 0
    last_reported_at = 0.0
    ingress_error: AccessDenied | SessionNotFound | None = None

    def download_progress(status: Mapping[str, Any]) -> None:
        nonlocal download_fraction, ingress_error, last_reported_fraction
        nonlocal last_reported_bytes, last_reported_at
        try:
            if ingress_error is not None:
                raise ingress_error
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
                _forward_source_progress(
                    context,
                    session_id,
                    "source.download_url",
                    progress,
                    0.05 + download_fraction * 0.8,
                    detail,
                )
                last_reported_fraction = download_fraction
                last_reported_bytes = downloaded
                last_reported_at = now
            elif state == "finished":
                download_fraction = 1.0
                _forward_source_progress(
                    context,
                    session_id,
                    "source.download_url",
                    progress,
                    0.88,
                    "Source download complete; processing media",
                )
        except (AccessDenied, SessionNotFound) as error:
            ingress_error = error
            raise

    try:
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
            _check_source_ingress(context, session_id, "source.download_url")
            information = downloader.extract_info(url, download=True)
            output_candidate = Path(downloader.prepare_filename(information))
    except Exception as error:
        if ingress_error is not None:
            raise ingress_error from error
        raise
    if ingress_error is not None:
        raise ingress_error
    if cancel_event.is_set():
        return {}
    _check_source_ingress(context, session_id, "source.download_url")
    output = output_candidate.resolve()
    resolved_destination_dir = destination_dir.resolve()
    _check_source_ingress(context, session_id, "source.download_url")
    if resolved_destination_dir not in output.parents or not output.is_file():
        raise RuntimeError("Downloaded source was not created in the managed session directory.")
    _forward_source_progress(
        context,
        session_id,
        "source.download_url",
        progress,
        0.93,
        "Registering downloaded source",
    )
    source_metadata = {
        "original_filename": output.name,
        "source_url": url,
        "downloader": "yt-dlp",
    }
    result = _publish_ingressed_source(
        context,
        session_id,
        "source.download_url",
        output,
        kind=output.suffix.lower().lstrip(".") or "url",
        display_name=output.name,
        artifact_metadata=source_metadata,
        source_metadata={"url": url, "downloader": "yt-dlp"},
    )
    progress(1.0, "Source download ready")
    return result


def reuse_source(
    context: SourceWorkflowContext,
    payload: dict[str, Any],
    progress: Progress,
    cancel_event: threading.Event,
) -> dict[str, Any]:
    session_id = str(payload.get("session_id") or "")
    artifact_id = str(payload.get("artifact_id") or "")
    source, source_path = _load_reusable_source(context, session_id, artifact_id)
    destination_dir = context._session_dir(session_id) / "sources" / new_id()
    _check_source_ingress(
        context,
        session_id,
        "source.reuse",
        artifact_id=source.id,
    )
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{source.id}-{source_path.name}"
    _forward_source_progress(
        context,
        session_id,
        "source.reuse",
        progress,
        0.2,
        "Copying reusable source",
        artifact_id=source.id,
    )
    _check_source_ingress(
        context,
        session_id,
        "source.reuse",
        artifact_id=source.id,
    )
    shutil.copy2(source_path, destination)
    if cancel_event.is_set():
        destination.unlink(missing_ok=True)
        return {}
    result = _publish_ingressed_source(
        context,
        session_id,
        "source.reuse",
        destination,
        kind=destination.suffix.lower().lstrip(".") or "file",
        display_name=source_path.name,
        artifact_metadata={"original_filename": source_path.name, "reused_from": source.id},
        source_metadata={"reused_from": source.id},
        parent_ids=[source.id],
        artifact_id=source.id,
    )
    progress(1.0, "Reusable source ready")
    return result


def clean_source(
    context: SourceWorkflowContext,
    payload: dict[str, Any],
    progress: Progress,
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Delegate native cleaning through the captured source workflow ports."""
    from .workflow_source_cleaning import clean_source as clean_source_job

    return clean_source_job(context, payload, progress, cancel_event)


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
    session_id = str(payload.get("session_id") or "")
    from .artifacts import sha256_file
    from .audiobook_generation_preparation import (
        GUARD_KEY,
        validate_automatic_narration_refresh,
    )
    from .settings_policy import RevisionConflict
    from .workspace_settings import WorkspaceSettingsService

    refresh_guard = payload.get(GUARD_KEY)
    if refresh_guard is not None:
        with context.database.snapshot_session() as unit:
            validate_automatic_narration_refresh(
                unit, session_id, payload, WorkspaceSettingsService(context.database)
            )
    requested_source_id = str(payload.get("source_artifact_id") or "")
    source_artifact_id, source_path, source_metadata = _load_text_preparation_source(
        context,
        session_id,
        requested_source_id,
    )
    if refresh_guard is not None and sha256_file(source_path) != refresh_guard.get("source_content_hash"):
        raise RevisionConflict("The source text file changed before narration preparation.")
    supplied_markup = source_metadata.get("speech_markup") or {}
    source_revision_id = str(source_metadata.get("revision_id") or "") or None
    requires_source_metadata = bool(supplied_markup or source_revision_id)

    _check_text_preparation_access(
        context,
        session_id,
        source_artifact_id,
        require_metadata=requires_source_metadata,
    )

    from pandrator.logic.audiobook_chunking import audiobook_chunk_budget
    from pandrator.logic.text_preprocessor import preprocess_text

    from .settings_policy import BUILTIN_DEFAULTS

    settings = dict(payload.get("settings") or {})
    if source_path.suffix.lower() not in {".txt", ".md"}:
        raise ValueError("Prepare narration requires a cleaned text artifact.")

    _check_text_preparation_access(
        context,
        session_id,
        source_artifact_id,
        require_metadata=requires_source_metadata,
    )
    text = source_path.read_text(encoding="utf-8-sig")
    _check_text_preparation_access(
        context,
        session_id,
        source_artifact_id,
        require_metadata=requires_source_metadata,
    )
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
    _forward_text_preparation_progress(
        context,
        session_id,
        source_artifact_id,
        progress,
        0.1,
        "Segmenting narration",
        require_metadata=requires_source_metadata,
    )
    if cancel_event.is_set():
        return {}

    if supplied_markup:
        from pandrator.logic.speech_markup import parse_speech_markup

        from .generation_controls import get_generation_controls

        with context.database.session() as db:
            require_text_preparation_access(
                db,
                session_id,
                source_artifact_id,
                require_metadata=requires_source_metadata,
            )
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
        preprocessor_progress = _text_preparation_progress_callback(
            context,
            session_id,
            source_artifact_id,
            progress,
            require_metadata=requires_source_metadata,
        )
        if current_execution_actor.get() is not None:
            _check_text_preparation_access(
                context,
                session_id,
                source_artifact_id,
                require_metadata=requires_source_metadata,
            )
        scaled_progress = context._scaled_progress_callback(
            preprocessor_progress,
            0.1,
            0.85,
        )
        if current_execution_actor.get() is not None:
            _check_text_preparation_access(
                context,
                session_id,
                source_artifact_id,
                require_metadata=requires_source_metadata,
            )
        prepared = preprocess_text(
            text,
            {
                "source_file": str(source_path),
                "language": source_language,
                # Segmentation is intentionally provider-independent.  This
                # selects the shared multilingual sentence tokenizer only.
                "tts_service": "XTTS",
                "audiobook_preparation": record.workflow_kind == "audiobook",
                "audiobook_dialogue_flow": settings.get(
                    "audiobook_dialogue_flow", text_defaults["audiobook_dialogue_flow"]
                ),
                "audiobook_sentence_packing": settings.get(
                    "audiobook_sentence_packing",
                    text_defaults["audiobook_sentence_packing"],
                ),
                "audiobook_sentence_packing_threshold": settings.get(
                    "audiobook_sentence_packing_threshold",
                    text_defaults["audiobook_sentence_packing_threshold"],
                ),
                "audiobook_dialogue_merge_allowed": not bool((captured_tts or {}).get("casting_enabled")),
                # XTTS remains the tokenizer choice for language-aware
                # splitting; the captured provider budget controls only
                # the target length.
                "max_sentence_length": budget["target_chars"],
                "max_segment_words": budget.get("target_words"),
                "max_segment_tokenizer_bytes": budget.get("target_tokenizer_bytes"),
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
            progress_callback=scaled_progress,
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
    _forward_text_preparation_progress(
        context,
        session_id,
        source_artifact_id,
        progress,
        0.9,
        "Saving narration segments",
        require_metadata=requires_source_metadata,
    )
    if cancel_event.is_set():
        return {}
    _check_text_preparation_access(
        context,
        session_id,
        source_artifact_id,
        require_metadata=requires_source_metadata,
    )
    destination = context._operation_dir(session_id, "prepare-text") / "prepared_narration.json"
    _check_text_preparation_access(
        context,
        session_id,
        source_artifact_id,
        require_metadata=requires_source_metadata,
    )
    if cancel_event.is_set():
        return {}
    destination.write_text(
        json.dumps(prepared, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if cancel_event.is_set():
        return {}
    _check_text_preparation_access(
        context,
        session_id,
        source_artifact_id,
        require_metadata=requires_source_metadata,
    )
    prepared_registration = context.artifacts.prepare_registration(
        destination,
        settings=artifact_settings,
    )
    if cancel_event.is_set():
        return {}

    with context.database.immediate_session() as unit:
        require_text_preparation_access(
            unit,
            session_id,
            source_artifact_id,
            require_metadata=requires_source_metadata,
        )
        if refresh_guard is not None:
            validate_automatic_narration_refresh(
                unit, session_id, payload, WorkspaceSettingsService(context.database)
            )
            if sha256_file(source_path) != refresh_guard.get("source_content_hash"):
                raise RevisionConflict("The source text file changed during narration preparation.")
        artifact = context.artifacts.register_in_session(
            unit,
            destination,
            kind="json",
            role="prepared_text",
            session_id=session_id,
            parent_ids=[source_artifact_id],
            settings=artifact_settings,
            metadata={"segment_count": len(prepared), "segmentation": segmentation},
            _prepared=prepared_registration,
        )
        artifact_id = str(artifact.id)
        relative_path = str(artifact.relative_path)
        generation_revision_id, _segment_ids = context._store_generation_plan(
            session_id,
            prepared,
            settings=artifact_settings,
            source_revision_id=source_revision_id,
            source_artifact_id=artifact_id,
            db_session=unit,
        )
        result = {
            "artifact_id": artifact_id,
            "path": relative_path,
            "segments": len(prepared),
            "generation_plan_revision_id": generation_revision_id,
            "segmentation": segmentation,
            "segmentation_budget": budget,
        }

    progress(1.0, "Narration segments ready")
    return result
