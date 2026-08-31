"""Passive, lease-based PDF and EPUB source-cleaning dispatch."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from pandrator.logic import source_cleaning
from pandrator.logic.source_cleaning.agent import execute_tool_action
from pandrator.logic.source_cleaning.deterministic import (
    extract_epub_with_diagnostics,
)
from pandrator.logic.source_cleaning.operations import ALLOWED_METADATA_KEYS
from pandrator.logic.source_cleaning.selectors import blocks_matching_selector

from .artifacts import ArtifactService, sha256_file
from .database import Database
from .dispatch import DispatchError
from .jobs import JobQueue
from .models import (
    Artifact,
    SessionRecord,
    SessionSource,
    SessionStageSelection,
    SourceAsset,
    SourceCleaningDispatchBatch,
    SourceCleaningDispatchRun,
    utcnow,
)
from .workspace import WorkspaceSettingsService, resolve_primary_source

_PHASE_ALLOWED_OPERATIONS: dict[str, tuple[str, ...]] = {
    "metadata": ("set_metadata",),
    "navigation": ("delete_blocks",),
    "boilerplate": ("delete_blocks",),
    "repeated_elements": ("delete_blocks",),
    "chapter_marking": ("mark_chapter", "unmark_chapter"),
    "text_repair": ("replace_block", "delete_blocks"),
}
_DISPATCH_PHASE_ORDER = [*source_cleaning.PHASE_ORDER, "text_repair"]
_DISPATCH_PHASE_DESCRIPTIONS = {
    **source_cleaning.PHASE_DESCRIPTIONS,
    "text_repair": "Extraction text repair",
}
_DISPATCH_PHASE_HELP = {
    **source_cleaning.PHASE_HELP_TEXT,
    "text_repair": (
        "Browse or search the working extraction and repair confirmed OCR, joining, "
        "encoding, or parser defects without rewriting sound source prose."
    ),
}
_PACKET_VERSION = 4
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_INSPECTION_ACTIONS = frozenset(
    {
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
        "find_metadata_candidates",
    }
)
_MAX_INSPECTION_COMMANDS = 25


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _active_lease(value: datetime | None, now: datetime) -> bool:
    expires = _aware(value)
    return expires is not None and expires > now


def _expired_lease(value: datetime | None, now: datetime) -> bool:
    expires = _aware(value)
    return expires is not None and expires <= now


def _operation_phase(operation: dict[str, Any]) -> str:
    op = str(operation.get("op") or "")
    reason = str(operation.get("reason") or "").casefold()
    if op == "set_metadata":
        return "metadata"
    if op.startswith("mark_") or op == "unmark_chapter":
        return "chapter_marking"
    if "toc" in reason or "table of contents" in reason or "navigation" in reason:
        return "navigation"
    if any(
        token in reason
        for token in ("repeat", "margin", "header", "footer", "page number")
    ):
        return "repeated_elements"
    return "boilerplate"


def _operation_id(phase: str, operation: dict[str, Any], ordinal: int) -> str:
    digest = _canonical_hash({"phase": phase, "operation": operation})[:16]
    return f"proposal:{phase}:{ordinal}:{digest}"


def _inspection_block_ids(value: Any) -> set[str]:
    identifiers: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "block_id" and isinstance(item, str) and item:
                identifiers.add(item)
            elif key == "block_ids" and isinstance(item, list):
                identifiers.update(
                    str(block_id) for block_id in item if str(block_id).strip()
                )
            else:
                identifiers.update(_inspection_block_ids(item))
    elif isinstance(value, list):
        for item in value:
            identifiers.update(_inspection_block_ids(item))
    return identifiers


def _validated_inspection_command(
    action: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if action == "batch":
        commands = arguments.get("commands")
        if not isinstance(commands, list) or not commands:
            raise DispatchError(
                "invalid_inspection",
                "A batch inspection requires a non-empty commands list.",
                422,
            )
        if len(commands) > _MAX_INSPECTION_COMMANDS:
            raise DispatchError(
                "invalid_inspection",
                f"A batch inspection accepts at most {_MAX_INSPECTION_COMMANDS} commands.",
                422,
            )
        for item in commands:
            nested_action = (
                str(item.get("action") or "").strip() if isinstance(item, dict) else ""
            )
            if nested_action not in _INSPECTION_ACTIONS:
                raise DispatchError(
                    "invalid_inspection",
                    f"Inspection action '{nested_action or '<missing>'}' is not supported.",
                    422,
                )
    elif action not in _INSPECTION_ACTIONS:
        raise DispatchError(
            "invalid_inspection",
            f"Inspection action '{action or '<missing>'}' is not supported.",
            422,
        )
    if len(json.dumps(arguments, ensure_ascii=False, default=str)) > 256_000:
        raise DispatchError(
            "invalid_inspection",
            "Inspection arguments exceed the 256 KB transport bound.",
            422,
        )
    return {"action": action, "arguments": arguments}


def _proposal(
    phase: str,
    operation: dict[str, Any],
    ordinal: int,
    *,
    origin: str,
) -> dict[str, Any]:
    canonical = dict(operation)
    return {
        "operation_id": _operation_id(phase, canonical, ordinal),
        "operation": canonical,
        "origin": origin,
        "reason": str(canonical.get("reason") or ""),
        "confidence": canonical.get("confidence"),
    }


def _applicable_operation(document, operation: dict[str, Any]) -> dict[str, Any] | None:
    """Return a proposal narrowed to the current working document."""

    narrowed = dict(operation)
    current_ids = {block.block_id for block in document.blocks}
    if "block_ids" in narrowed:
        block_ids = [
            str(item)
            for item in narrowed.get("block_ids") or []
            if str(item) in current_ids
        ]
        if not block_ids:
            return None
        narrowed["block_ids"] = block_ids
    block_id = str(narrowed.get("block_id") or "").strip()
    if block_id and block_id not in current_ids:
        return None
    selector = narrowed.get("selector")
    if (
        isinstance(selector, dict)
        and selector
        and not blocks_matching_selector(document.blocks, selector)
    ):
        return None
    if (
        narrowed.get("start_line") is not None
        and narrowed.get("end_line") is not None
        and not document.blocks_in_line_range(
            int(narrowed["start_line"]),
            int(narrowed["end_line"]),
        )
    ):
        return None
    return narrowed


def _source_name(artifact: Artifact) -> str:
    metadata = dict(artifact.metadata_json or {})
    return str(metadata.get("original_filename") or artifact.relative_path).strip()


def _preview_block(block) -> dict[str, Any]:
    text = " ".join(str(block.text or "").split())
    if len(text) > 500:
        text = text[:497] + "..."
    return {
        "block_id": block.block_id,
        "line_start": block.line_start,
        "line_end": block.line_end,
        "text": text,
        "page": block.page,
        "href": block.href,
        "tag": block.tag,
        "classes": list(block.classes),
        "role_candidates": list(block.role_candidates),
        "role_evidence": dict(block.attributes.get("role_evidence") or {}),
    }


def _bounded_blocks(document, block_ids: set[str], limit: int) -> list[dict[str, Any]]:
    selected = [block for block in document.blocks if block.block_id in block_ids]
    return [_preview_block(block) for block in selected[:limit]]


def _proposal_block_ids(document, proposals: list[dict[str, Any]]) -> set[str]:
    identifiers: set[str] = set()
    for proposal in proposals:
        operation = dict(proposal.get("operation") or {})
        identifiers.update(str(item) for item in operation.get("block_ids") or [])
        block_id = str(operation.get("block_id") or "").strip()
        if block_id:
            identifiers.add(block_id)
        selector = operation.get("selector")
        if isinstance(selector, dict) and selector:
            identifiers.update(
                block.block_id
                for block in blocks_matching_selector(document.blocks, selector)
            )
    return identifiers


def _build_phase_packets(
    document,
    deterministic_operations: list[dict[str, Any]],
    *,
    instructions: str,
    evidence_limit: int,
) -> list[dict[str, Any]]:
    tools = source_cleaning.SourceCleaningTools(document)
    structure = tools.inspect_document_structure(max_documents=30)
    navigation = tools.inspect_navigation(
        max_entries=evidence_limit,
        max_matches_per_entry=4,
    )
    cleanup = tools.analyze_cleanup_structure(max_candidates=evidence_limit)
    repeated = tools.list_repeated_lines(
        min_repeats=3,
        max_length=120,
    )[:evidence_limit]
    heading_candidates = tools.find_heading_candidates(max_candidates=evidence_limit)
    chapters = tools.analyze_chapter_structure(max_candidates=evidence_limit)
    footnotes = tools.find_footnote_candidates(max_candidates=evidence_limit)
    metadata = tools.find_metadata_candidates()

    proposals_by_phase: dict[str, list[dict[str, Any]]] = {
        phase: [] for phase in _DISPATCH_PHASE_ORDER
    }
    passive_chapter_ids = {
        str(item.get("block_id") or "")
        for item in chapters.get("likely_chapters", [])
        if str(item.get("block_id") or "")
    }
    for raw_operation in deterministic_operations:
        operation = _applicable_operation(document, raw_operation)
        if operation is None:
            continue
        phase = _operation_phase(operation)
        if (
            document.source_type == "pdf_structured"
            and phase == "chapter_marking"
            and str(operation.get("block_id") or "") not in passive_chapter_ids
        ):
            continue
        rows = proposals_by_phase[phase]
        rows.append(_proposal(phase, operation, len(rows) + 1, origin="deterministic"))

    if document.source_type == "pdf_structured":
        for candidate in cleanup.get("candidate_groups", []):
            if not isinstance(candidate, dict):
                continue
            selector = candidate.get("selector")
            reasons = [str(item) for item in candidate.get("reasons") or []]
            if not isinstance(selector, dict) or not selector:
                continue
            phase = (
                "navigation"
                if any("toc" in reason or "navigation" in reason for reason in reasons)
                else "boilerplate"
            )
            operation = {
                "op": "delete_by_selector",
                "selector": selector,
                "reason": ", ".join(reasons) or "structured cleanup candidate",
                "confidence": 0.75,
            }
            rows = proposals_by_phase[phase]
            if not any(item.get("operation") == operation for item in rows):
                rows.append(
                    _proposal(phase, operation, len(rows) + 1, origin="candidate")
                )

    for item in repeated:
        block_ids = [str(value) for value in item.get("block_ids") or []]
        if not block_ids:
            continue
        operation = {
            "op": "delete_blocks",
            "block_ids": block_ids,
            "reason": f"repeated short element: {item.get('text')}",
            "confidence": 0.7,
        }
        rows = proposals_by_phase["repeated_elements"]
        if not any(item.get("operation") == operation for item in rows):
            rows.append(
                _proposal(
                    "repeated_elements",
                    operation,
                    len(rows) + 1,
                    origin="candidate",
                )
            )

    existing_chapter_ids = {
        str(item.get("operation", {}).get("block_id") or "")
        for item in proposals_by_phase["chapter_marking"]
    }
    for item in chapters.get("likely_chapters", [])[:evidence_limit]:
        block_id = str(item.get("block_id") or "")
        if not block_id or block_id in existing_chapter_ids:
            continue
        operation = {
            "op": "mark_chapter",
            "block_id": block_id,
            "title": str(item.get("text") or "").strip(),
            "reason": str(item.get("chapter_evidence") or "chapter candidate"),
            "confidence": float(item.get("score") or 0.0),
        }
        rows = proposals_by_phase["chapter_marking"]
        rows.append(
            _proposal(
                "chapter_marking",
                operation,
                len(rows) + 1,
                origin="candidate",
            )
        )
        existing_chapter_ids.add(block_id)

    first_ids = {block.block_id for block in document.blocks[: min(35, evidence_limit)]}
    last_ids = {block.block_id for block in document.blocks[-min(35, evidence_limit) :]}
    toc_ids = {
        block.block_id
        for block in document.blocks
        if "toc" in block.role_candidates or "navigation" in block.role_candidates
    }
    boilerplate_ids = {
        block.block_id
        for block in document.blocks
        if set(block.role_candidates)
        & {"copyright", "boilerplate", "footnote", "footnote_candidate"}
    }
    repeated_ids = {
        str(block_id) for item in repeated for block_id in item.get("block_ids") or []
    }
    chapter_ids = {
        str(item.get("block_id") or "")
        for item in [
            *heading_candidates,
            *chapters.get("likely_chapters", []),
        ]
        if str(item.get("block_id") or "")
    }
    chapter_ids.update(
        block.block_id
        for block in document.blocks
        if set(block.role_candidates)
        & {"heading", "heading_candidate", "chapter_heading", "deterministic_chapter"}
    )
    footnote_ids = {
        str(item.get("block_id") or "")
        for item in footnotes
        if str(item.get("block_id") or "")
    }
    evidence_ids = {
        "metadata": first_ids,
        "navigation": first_ids | toc_ids,
        "boilerplate": first_ids | last_ids | boilerplate_ids | footnote_ids,
        "repeated_elements": repeated_ids,
        "chapter_marking": chapter_ids,
        "text_repair": first_ids | last_ids,
    }
    evidence_by_phase: dict[str, dict[str, Any]] = {
        "metadata": {"metadata": metadata},
        "navigation": {"navigation": navigation, "cleanup": cleanup},
        "boilerplate": {
            "cleanup": cleanup,
            "footnote_candidates": footnotes,
        },
        "repeated_elements": {"repeated_elements": repeated},
        "chapter_marking": {
            "chapter_structure": chapters,
            "heading_candidates": heading_candidates,
        },
        "text_repair": {
            "guidance": (
                "Heuristics are only starting points. Use the leased extraction "
                "inspection tool to browse, search, inspect context or raw EPUB "
                "markup, then repair only confirmed extraction defects."
            )
        },
    }

    packets: list[dict[str, Any]] = []
    for phase in _DISPATCH_PHASE_ORDER:
        proposals = proposals_by_phase[phase]
        evidence_and_proposal_ids = set(evidence_ids[phase]) | _proposal_block_ids(
            document, proposals
        )
        blocks = _bounded_blocks(document, evidence_and_proposal_ids, evidence_limit)
        # Custom operations are restricted to blocks whose text was exposed to
        # the agent. Accepting a server-owned proposal may still apply its wider
        # selector or block set, but the client cannot manufacture unseen targets.
        valid_ids = {str(item["block_id"]) for item in blocks}
        raw_source_available = isinstance(
            document.attributes.get("epub_source_inspection_document"),
            dict,
        )
        packets.append(
            {
                "phase": phase,
                "phase_description": _DISPATCH_PHASE_DESCRIPTIONS[phase],
                "phase_help": _DISPATCH_PHASE_HELP[phase],
                "instructions": instructions,
                "allowed_operation_types": list(_PHASE_ALLOWED_OPERATIONS[phase]),
                "capabilities": {
                    "source_type": document.source_type,
                    "source_format": "pdf"
                    if document.source_type == "pdf_structured"
                    else "epub",
                    "raw_markup_available": raw_source_available,
                    "raw_source_inspection_available": raw_source_available,
                    "server_owned_selector_proposals": document.source_type
                    == "pdf_structured",
                },
                "document_summary": structure,
                "evidence": {
                    **evidence_by_phase[phase],
                    "candidate_blocks": blocks,
                },
                "proposals": proposals,
                "valid_block_ids": sorted(valid_ids),
                "valid_metadata_keys": sorted(ALLOWED_METADATA_KEYS)
                if phase == "metadata"
                else [],
            }
        )
    return packets


class SourceCleaningDispatchRunService:
    """Durable preparation, leasing, validation, and finalization."""

    def __init__(
        self,
        database: Database,
        artifacts: ArtifactService,
        session_dir_resolver,
        *,
        jobs: JobQueue | None = None,
        workspace_settings: WorkspaceSettingsService | None = None,
    ) -> None:
        self.database = database
        self.artifacts = artifacts
        self.session_dir_resolver = session_dir_resolver
        self.jobs = jobs
        self.workspace_settings = workspace_settings or WorkspaceSettingsService(
            database
        )

    @staticmethod
    def _selection_snapshot(
        session: Session,
        session_id: str,
        source_artifact_id: str,
    ) -> dict[str, Any]:
        row = session.get(SessionStageSelection, (session_id, "clean_source"))
        source_attachments = session.execute(
            select(SessionSource, SourceAsset)
            .join(SourceAsset, SourceAsset.id == SessionSource.source_asset_id)
            .where(
                SessionSource.session_id == session_id,
                SourceAsset.artifact_id == source_artifact_id,
            )
            .order_by(SessionSource.id)
        ).all()
        return {
            "clean_source": {
                "exists": row is not None,
                "artifact_id": row.artifact_id if row is not None else None,
                "revision": int(row.revision) if row is not None else 0,
            },
            "source_attachments": [
                {
                    "attachment_id": attachment.id,
                    "source_asset_id": asset.id,
                    "role": attachment.role,
                    "is_current": bool(attachment.is_current),
                    "attachment_revision": int(attachment.revision),
                    "asset_revision": int(asset.revision),
                    "asset_state": asset.state,
                    "asset_content_hash": asset.content_hash,
                }
                for attachment, asset in source_attachments
            ],
        }

    @staticmethod
    def _run_payload(run: SourceCleaningDispatchRun) -> dict[str, Any]:
        validation = dict(run.validation_json or {})
        return {
            "id": run.id,
            "run_id": run.id,
            "session_id": run.session_id,
            "kind": "source_cleaning",
            "source_artifact_id": run.source_artifact_id,
            "source_format": run.source_format,
            "source_content_hash": run.source_content_hash,
            "job_id": run.job_id,
            "status": run.status,
            "batch_count": run.batch_count,
            "total_batches": run.batch_count,
            "completed_batch_count": run.completed_batch_count,
            "accepted_batch_count": run.completed_batch_count,
            "remaining_batch_count": max(
                0, run.batch_count - run.completed_batch_count
            ),
            "accepted_operation_count": run.accepted_operation_count,
            "rejected_proposal_count": run.rejected_proposal_count,
            "baseline_artifact_id": run.baseline_artifact_id,
            "index_artifact_id": run.index_artifact_id,
            "result_artifact_id": run.result_artifact_id,
            "final_artifact_id": run.result_artifact_id,
            "finalized": run.status == "completed",
            "requires_review": bool(
                validation.get("warnings") or validation.get("blocking_warnings")
            ),
            "validation": validation if run.status == "completed" else {},
            "error_code": run.error_code,
            "error_message": run.error_message,
            "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
        }

    @classmethod
    def _run_detail_payload(
        cls,
        run: SourceCleaningDispatchRun,
        batches: list[SourceCleaningDispatchBatch],
    ) -> dict[str, Any]:
        payload = cls._run_payload(run)
        payload["batches"] = [
            {
                "id": batch.id,
                "batch_ordinal": batch.ordinal + 1,
                "phase": batch.phase,
                "status": batch.status,
                "lease_expires_at": _iso(batch.lease_expires_at),
                "accepted_at": _iso(batch.accepted_at),
            }
            for batch in batches
        ]
        return payload

    def list_runs(
        self,
        session_id: str,
        *,
        limit: int = 50,
        db_session: Session | None = None,
    ) -> list[dict[str, Any]]:
        if db_session is not None:
            runs = list(
                db_session.scalars(
                    select(SourceCleaningDispatchRun)
                    .where(SourceCleaningDispatchRun.session_id == session_id)
                    .order_by(
                        SourceCleaningDispatchRun.created_at.desc(),
                        SourceCleaningDispatchRun.id.desc(),
                    )
                    .limit(max(1, min(int(limit), 100)))
                ).all()
            )
            return [self._run_payload(run) for run in runs]
        with self.database.session() as session:
            return self.list_runs(session_id, limit=limit, db_session=session)

    def get(self, run_id: str, *, db_session: Session | None = None) -> dict[str, Any]:
        if db_session is not None:
            run = db_session.get(SourceCleaningDispatchRun, run_id)
            if run is None:
                raise DispatchError(
                    "not_found", "Source-cleaning dispatch run not found.", 404
                )
            batches = list(
                db_session.scalars(
                    select(SourceCleaningDispatchBatch)
                    .where(SourceCleaningDispatchBatch.dispatch_run_id == run.id)
                    .order_by(SourceCleaningDispatchBatch.ordinal)
                ).all()
            )
            return self._run_detail_payload(run, batches)
        with self.database.session() as session:
            return self.get(run_id, db_session=session)

    @staticmethod
    def _artifact_attached(
        session: Session,
        session_id: str,
        artifact_id: str,
    ) -> bool:
        return (
            session.scalar(
                select(SessionSource.id)
                .join(SourceAsset, SourceAsset.id == SessionSource.source_asset_id)
                .where(
                    SessionSource.session_id == session_id,
                    SessionSource.is_current.is_(True),
                    SourceAsset.artifact_id == artifact_id,
                    SourceAsset.state == "current",
                )
                .limit(1)
            )
            is not None
        )

    def _load_source(
        self,
        session: Session,
        *,
        session_id: str,
        source_artifact_id: str | None,
    ) -> tuple[Artifact, Path, str]:
        record = session.get(SessionRecord, session_id)
        if record is None or record.trashed_at is not None:
            raise DispatchError("not_found", "Session not found.", 404)
        if source_artifact_id:
            source = session.get(Artifact, source_artifact_id)
        else:
            source = resolve_primary_source(session, session_id).artifact
        if source is None:
            raise DispatchError(
                "source_not_found",
                "No current source is attached to this session.",
                422,
            )
        attached = self._artifact_attached(session, session_id, source.id)
        has_source_asset = (
            session.scalar(
                select(SourceAsset.id)
                .where(SourceAsset.artifact_id == source.id)
                .limit(1)
            )
            is not None
        )
        if not attached and (source.session_id != session_id or has_source_asset):
            raise DispatchError(
                "source_session_mismatch",
                "Source artifact is not currently attached to this session.",
                409,
            )
        if source.state == "deleted":
            raise DispatchError(
                "source_deleted", "The source artifact was deleted.", 409
            )
        source_format = Path(_source_name(source)).suffix.lower().lstrip(".")
        if source_format not in {"pdf", "epub"}:
            raise DispatchError(
                "ineligible_source",
                "Passive source cleaning currently supports PDF and EPUB sources.",
                422,
            )
        if not source.content_hash:
            raise DispatchError(
                "source_hash_missing", "The source artifact has no content hash.", 409
            )
        try:
            path = self.artifacts.paths.managed_path(source.relative_path)
            actual_hash = sha256_file(path)
        except (OSError, ValueError) as error:
            raise DispatchError(
                "source_unavailable",
                "The source artifact content is unavailable.",
                409,
                retryable=True,
            ) from error
        if actual_hash != source.content_hash:
            raise DispatchError(
                "source_changed",
                "The source artifact content no longer matches its stored hash.",
                409,
            )
        return source, path, source_format

    def create_in_session(
        self,
        session: Session,
        *,
        session_id: str,
        source_artifact_id: str | None,
        instructions: str,
        evidence_limit: int,
        remove_footnotes: bool | None,
        filter_citations: bool | None,
        pdf_ocr_mode: str | None,
        pdf_ocr_language: str | None,
        pdf_ocr_dpi: int | None,
        pdf_remove_toc: bool | None,
        pdf_remove_repeated_marginals: bool | None,
    ) -> dict[str, Any]:
        if self.jobs is None:
            raise RuntimeError(
                "Source-cleaning dispatch creation requires a job queue."
            )
        source, _path, source_format = self._load_source(
            session,
            session_id=session_id,
            source_artifact_id=source_artifact_id,
        )
        effective = dict(
            self.workspace_settings.get_in_session(
                session, session_id, "source_cleaning"
            )["effective"]
        )
        overrides = {
            "remove_footnotes": remove_footnotes,
            "filter_citations": filter_citations,
            "pdf_ocr_mode": pdf_ocr_mode,
            "pdf_ocr_language": pdf_ocr_language,
            "pdf_ocr_dpi": pdf_ocr_dpi,
            "pdf_remove_toc": pdf_remove_toc,
            "pdf_remove_repeated_marginals": pdf_remove_repeated_marginals,
        }
        effective.update(
            {key: value for key, value in overrides.items() if value is not None}
        )
        settings = {
            "mode": "passive",
            "agentic": False,
            "instructions": str(instructions or ""),
            "evidence_limit": max(20, min(int(evidence_limit), 2_000)),
            "remove_footnotes": bool(effective.get("remove_footnotes", False)),
            "filter_citations": bool(effective.get("filter_citations", True)),
            "pdf_ocr_mode": str(effective.get("pdf_ocr_mode") or "auto"),
            "pdf_ocr_language": str(effective.get("pdf_ocr_language") or "auto"),
            "pdf_ocr_dpi": int(effective.get("pdf_ocr_dpi") or 200),
            "pdf_remove_toc": bool(effective.get("pdf_remove_toc", True)),
            "pdf_remove_repeated_marginals": bool(
                effective.get("pdf_remove_repeated_marginals", True)
            ),
        }
        selection_snapshot = self._selection_snapshot(session, session_id, source.id)
        output_head = session.scalar(
            select(Artifact)
            .where(
                Artifact.session_id == session_id,
                Artifact.role == "clean_text",
                Artifact.state == "current",
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        )
        semantic_input = {
            "session_id": session_id,
            "source_artifact_id": source.id,
            "source_content_hash": source.content_hash,
            "source_format": source_format,
            "settings": settings,
            "selection_snapshot": selection_snapshot,
            "output_head_artifact_id": output_head.id if output_head else None,
        }
        run = SourceCleaningDispatchRun(
            session_id=session_id,
            source_artifact_id=source.id,
            source_state=source.state,
            source_content_hash=str(source.content_hash),
            source_format=source_format,
            settings_json=settings,
            selection_snapshot_json=selection_snapshot,
            input_hash=_canonical_hash(semantic_input),
            output_head_artifact_id=output_head.id if output_head else None,
            status="preparing",
        )
        session.add(run)
        session.flush()
        job = self.jobs.enqueue_in_session(
            session,
            "source.cleaning_dispatch.prepare",
            {
                "session_id": session_id,
                "source_artifact_id": source.id,
                "source_cleaning_dispatch_run_id": run.id,
            },
            session_id=session_id,
            # Failures are persisted on the run. A generic second job attempt
            # would otherwise retry a run that has already become terminal.
            max_attempts=1,
            resource_keys=[f"session:{session_id}"],
        )
        run.job_id = job.id
        run.updated_at = utcnow()
        session.flush()
        return self._run_payload(run)

    def _verify_pinned_source(
        self, session: Session, run: SourceCleaningDispatchRun
    ) -> tuple[Artifact, Path]:
        source = session.get(Artifact, run.source_artifact_id)
        if source is None or source.state == "deleted":
            raise DispatchError(
                "source_changed", "The pinned source is no longer available.", 409
            )
        if (
            source.state != run.source_state
            or source.content_hash != run.source_content_hash
        ):
            raise DispatchError(
                "source_changed",
                "The pinned source state or hash changed while this run was active.",
                409,
            )
        try:
            path = self.artifacts.paths.managed_path(source.relative_path)
            if sha256_file(path) != run.source_content_hash:
                raise DispatchError(
                    "source_changed", "The pinned source content changed.", 409
                )
        except (OSError, ValueError) as error:
            raise DispatchError(
                "source_changed",
                "The pinned source content is no longer available.",
                409,
            ) from error
        return source, path

    def prepare_run(
        self,
        run_id: str,
        progress,
        cancel_event,
    ) -> dict[str, Any]:
        try:
            with self.database.session() as session:
                run = session.get(SourceCleaningDispatchRun, run_id)
                if run is None:
                    raise DispatchError(
                        "not_found", "Source-cleaning dispatch run not found.", 404
                    )
                if run.status == "ready":
                    return self._run_payload(run)
                if run.status not in {"preparing", "retrying"}:
                    raise DispatchError(
                        "run_not_preparable",
                        "Source-cleaning dispatch run is no longer preparable.",
                        409,
                    )
                _source, source_path = self._verify_pinned_source(session, run)
                settings = dict(run.settings_json or {})
                source_format = run.source_format
                session_id = run.session_id

            if cancel_event.is_set():
                self._mark_preparation_cancelled(run_id)
                return {"run_id": run_id, "status": "cancelled"}
            dispatch_dir = (
                self.session_dir_resolver(session_id)
                / "source-cleaning-dispatch"
                / run_id
            )
            dispatch_dir.mkdir(parents=True, exist_ok=True)
            progress(0.05, "Preparing deterministic source extraction")
            if source_format == "pdf":
                document = source_cleaning.build_source_document(
                    str(source_path),
                    pdf_config=source_cleaning.PDFIngestionConfig(
                        ocr_mode=str(settings.get("pdf_ocr_mode") or "auto"),
                        ocr_language=str(settings.get("pdf_ocr_language") or "auto"),
                        ocr_dpi=int(settings.get("pdf_ocr_dpi") or 200),
                    ),
                    artifact_dir=str(dispatch_dir / "pdf-ingestion"),
                    progress_callback=lambda message: progress(0.25, str(message)),
                )
                baseline_text = document.plain_text()
                deterministic_operations = (
                    source_cleaning.propose_deterministic_operations(
                        document,
                        remove_footnotes=bool(settings.get("remove_footnotes", False)),
                        remove_toc=bool(settings.get("pdf_remove_toc", True)),
                        remove_repeated_marginals=bool(
                            settings.get("pdf_remove_repeated_marginals", True)
                        ),
                    )
                )
            else:
                extraction_result = extract_epub_with_diagnostics(
                    str(source_path),
                    remove_footnotes=bool(settings.get("remove_footnotes", False)),
                    filter_citations=bool(settings.get("filter_citations", True)),
                )
                if not extraction_result.ok:
                    raise DispatchError(
                        extraction_result.error_code or "epub_extraction_failed",
                        extraction_result.message,
                        422,
                    )
                baseline_text = extraction_result.text
                document = source_cleaning.build_cleaned_epub_source_document(
                    str(source_path),
                    baseline_text,
                    include_source_inspection=True,
                )
                deterministic_operations = (
                    source_cleaning.propose_embedded_chapter_operations(document)
                )
            document.filename = _source_name(_source)
            if cancel_event.is_set():
                self._mark_preparation_cancelled(run_id)
                return {"run_id": run_id, "status": "cancelled"}
            progress(0.55, "Saving the sequential editorial phase plan")
            index_path = dispatch_dir / "source-index.json"
            baseline_path = dispatch_dir / "extracted-text.txt"
            index_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "document": document.to_dict(),
                        "deterministic_operations": deterministic_operations,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
                newline="\n",
            )
            baseline_path.write_text(baseline_text, encoding="utf-8", newline="\n")
            prepared_index = self.artifacts.prepare_registration(index_path)
            prepared_baseline = self.artifacts.prepare_registration(baseline_path)
            progress(0.8, "Saving passive source-cleaning work")
            with self.database.immediate_session() as session:
                run = session.get(SourceCleaningDispatchRun, run_id)
                if run is None:
                    raise DispatchError(
                        "not_found", "Source-cleaning dispatch run not found.", 404
                    )
                source, _source_path = self._verify_pinned_source(session, run)
                existing_count = int(
                    session.scalar(
                        select(func.count(SourceCleaningDispatchBatch.id)).where(
                            SourceCleaningDispatchBatch.dispatch_run_id == run.id
                        )
                    )
                    or 0
                )
                if existing_count:
                    if existing_count != len(_DISPATCH_PHASE_ORDER):
                        raise DispatchError(
                            "preparation_conflict",
                            "Prepared batches no longer match this run.",
                            409,
                        )
                    run.status = "ready"
                    run.updated_at = utcnow()
                    return self._run_payload(run)
                baseline_artifact = self.artifacts.register_in_session(
                    session,
                    baseline_path,
                    kind="text",
                    role="extracted_text",
                    session_id=run.session_id,
                    parent_ids=[source.id],
                    metadata={
                        "comparison_source": True,
                        "source_filename": document.filename,
                        "source_cleaning_dispatch_run_id": run.id,
                    },
                    _prepared=prepared_baseline,
                )
                index_artifact = self.artifacts.register_in_session(
                    session,
                    index_path,
                    kind="json",
                    role="source_cleaning_index",
                    session_id=run.session_id,
                    parent_ids=[source.id, baseline_artifact.id],
                    metadata={
                        "source_cleaning_dispatch_run_id": run.id,
                        "source_type": document.source_type,
                    },
                    _prepared=prepared_index,
                )
                for ordinal, phase in enumerate(_DISPATCH_PHASE_ORDER):
                    placeholder = {
                        "phase": phase,
                        "packet_status": "pending",
                    }
                    session.add(
                        SourceCleaningDispatchBatch(
                            dispatch_run_id=run.id,
                            ordinal=ordinal,
                            phase=phase,
                            input_json=placeholder,
                            input_hash=_canonical_hash(placeholder),
                            status="ready",
                        )
                    )
                run.baseline_artifact_id = baseline_artifact.id
                run.index_artifact_id = index_artifact.id
                run.batch_count = len(_DISPATCH_PHASE_ORDER)
                run.completed_batch_count = 0
                run.status = "ready"
                run.error_code = None
                run.error_message = None
                run.updated_at = utcnow()
                session.flush()
                payload = self._run_payload(run)
            progress(1.0, "Passive source-cleaning batches are ready")
            return payload
        except Exception as error:
            self._mark_preparation_failed(run_id, error)
            raise

    def _mark_preparation_cancelled(self, run_id: str) -> None:
        with self.database.session() as session:
            run = session.get(SourceCleaningDispatchRun, run_id)
            if run is not None and run.status not in _TERMINAL_STATUSES:
                run.status = "cancelled"
                run.error_code = "preparation_cancelled"
                run.error_message = "Source-cleaning preparation was cancelled."
                run.updated_at = utcnow()

    def _mark_preparation_failed(self, run_id: str, error: Exception) -> None:
        with self.database.session() as session:
            run = session.get(SourceCleaningDispatchRun, run_id)
            if run is not None and run.status not in _TERMINAL_STATUSES:
                run.status = "failed"
                run.error_code = (
                    error.code
                    if isinstance(error, DispatchError)
                    else "preparation_failed"
                )
                run.error_message = str(error)[:2000]
                run.updated_at = utcnow()

    @staticmethod
    def _claim_response(
        run: SourceCleaningDispatchRun,
        batch: SourceCleaningDispatchBatch,
    ) -> dict[str, Any]:
        packet = dict(batch.input_json or {})
        return {
            "schema_version": "1",
            "run_id": run.id,
            "batch_id": batch.id,
            "batch_ordinal": batch.ordinal + 1,
            "status": batch.status,
            "run_status": run.status,
            "batch_status": batch.status,
            "task": {
                "kind": "source_cleaning",
                "source_format": run.source_format,
                "phase": batch.phase,
                "phase_description": packet.get("phase_description"),
                "phase_help": packet.get("phase_help"),
                "instructions": packet.get("instructions") or "",
                "allowed_operation_types": list(
                    packet.get("allowed_operation_types") or []
                ),
                "result_contract": {
                    "kind": "source_cleaning",
                    "phase": batch.phase,
                    "decisions": {
                        "required_operation_ids": [
                            str(item.get("operation_id") or "")
                            for item in packet.get("proposals") or []
                            if isinstance(item, dict)
                        ],
                        "verdicts": ["accept", "reject"],
                    },
                    "operations": list(packet.get("allowed_operation_types") or []),
                },
                "inspection": {
                    "tool": "pandrator_inspect_source_cleaning_dispatch_extraction",
                    "lease_scoped": True,
                    "views": [
                        "working",
                        "baseline",
                        *(
                            ["source"]
                            if dict(packet.get("capabilities") or {}).get(
                                "raw_source_inspection_available"
                            )
                            else []
                        ),
                    ],
                    "guidance": (
                        "Initial candidates are hints. Browse, search, inspect context "
                        "or structure, and batch independent lookups when more evidence "
                        "is needed. Working/baseline blocks can become valid operation "
                        "targets; the source view is read-only evidence."
                    ),
                },
            },
            "batch": {
                "id_namespace": "source_cleaning_block",
                "phase": batch.phase,
                "document_summary": packet.get("document_summary") or {},
                "capabilities": packet.get("capabilities") or {},
                "evidence": packet.get("evidence") or {},
                "proposals": packet.get("proposals") or [],
                "valid_block_ids": packet.get("valid_block_ids") or [],
                "valid_metadata_keys": packet.get("valid_metadata_keys") or [],
            },
            "lease_token": batch.lease_token,
            "lease_expires_at": _iso(batch.lease_expires_at),
        }

    def claim_in_session(
        self,
        session: Session,
        *,
        run_id: str,
        claim_key: str,
        lease_seconds: int,
    ) -> dict[str, Any]:
        run = session.get(SourceCleaningDispatchRun, run_id)
        if run is None:
            raise DispatchError(
                "not_found", "Source-cleaning dispatch run not found.", 404
            )
        if run.status == "preparing":
            raise DispatchError(
                "run_preparing",
                "Deterministic PDF/EPUB preparation is still running.",
                409,
                retryable=True,
                details={"job_id": run.job_id},
            )
        batches = list(
            session.scalars(
                select(SourceCleaningDispatchBatch)
                .where(SourceCleaningDispatchBatch.dispatch_run_id == run.id)
                .order_by(SourceCleaningDispatchBatch.ordinal)
            ).all()
        )
        now = utcnow()
        replayed = next((item for item in batches if item.claim_key == claim_key), None)
        if replayed is not None and (
            replayed.status == "completed"
            or (
                replayed.status == "leased"
                and _active_lease(replayed.lease_expires_at, now)
            )
        ):
            return self._claim_response(run, replayed)
        if run.status in _TERMINAL_STATUSES:
            raise DispatchError(
                "run_not_claimable",
                "Source-cleaning dispatch run is no longer claimable.",
                409,
            )
        for item in batches:
            if item.status == "leased" and _expired_lease(item.lease_expires_at, now):
                item.status = "ready"
                item.lease_token = None
                item.claim_key = None
                item.lease_expires_at = None
                item.updated_at = now
        active = next(
            (
                item
                for item in batches
                if item.status == "leased" and _active_lease(item.lease_expires_at, now)
            ),
            None,
        )
        if active is not None:
            if active.claim_key == claim_key:
                return self._claim_response(run, active)
            expires = _aware(active.lease_expires_at) or now
            raise DispatchError(
                "dispatch_busy",
                "Another source-cleaning phase is currently leased.",
                409,
                retryable=True,
                details={
                    "batch_id": active.id,
                    "retry_after_seconds": max(1, int((expires - now).total_seconds())),
                },
            )
        next_batch = next((item for item in batches if item.status == "ready"), None)
        if next_batch is None:
            raise DispatchError(
                "run_not_claimable",
                "No ready source-cleaning phase remains.",
                409,
            )
        if any(item.status != "completed" for item in batches[: next_batch.ordinal]):
            raise DispatchError(
                "dispatch_sequential",
                "Source-cleaning phases must be completed in order.",
                409,
                retryable=True,
            )
        self._ensure_batch_packet(session, run, next_batch)
        next_batch.status = "leased"
        next_batch.claim_key = claim_key
        next_batch.lease_token = secrets.token_urlsafe(32)
        next_batch.lease_expires_at = now + timedelta(seconds=lease_seconds)
        next_batch.updated_at = now
        run.status = "running"
        run.updated_at = now
        session.flush()
        return self._claim_response(run, next_batch)

    def renew_in_session(
        self,
        session: Session,
        *,
        batch_id: str,
        lease_token: str,
        lease_seconds: int,
    ) -> dict[str, Any]:
        batch = session.get(SourceCleaningDispatchBatch, batch_id)
        if batch is None:
            raise DispatchError(
                "not_found", "Source-cleaning dispatch batch not found.", 404
            )
        now = utcnow()
        if batch.status != "leased" or batch.lease_token != lease_token:
            raise DispatchError(
                "lease_conflict", "The lease token is not current.", 409
            )
        if not _active_lease(batch.lease_expires_at, now):
            raise DispatchError(
                "lease_expired", "The dispatch lease has expired.", 409, retryable=True
            )
        batch.lease_expires_at = now + timedelta(seconds=lease_seconds)
        batch.updated_at = now
        session.flush()
        return {
            "batch_id": batch.id,
            "status": batch.status,
            "lease_expires_at": _iso(batch.lease_expires_at),
        }

    def release_in_session(
        self,
        session: Session,
        *,
        batch_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        batch = session.get(SourceCleaningDispatchBatch, batch_id)
        if batch is None:
            raise DispatchError(
                "not_found", "Source-cleaning dispatch batch not found.", 404
            )
        if batch.lease_token != lease_token:
            raise DispatchError(
                "lease_conflict", "The lease token is not current.", 409
            )
        if batch.status == "completed":
            raise DispatchError(
                "batch_completed", "The dispatch batch is already completed.", 409
            )
        batch.status = "ready"
        batch.lease_token = None
        batch.claim_key = None
        batch.lease_expires_at = None
        batch.updated_at = utcnow()
        session.flush()
        return {"batch_id": batch.id, "status": "ready", "lease_expires_at": None}

    def inspect_in_session(
        self,
        session: Session,
        *,
        batch_id: str,
        lease_token: str,
        action: str,
        arguments: dict[str, Any],
        view: str,
    ) -> dict[str, Any]:
        batch = session.get(SourceCleaningDispatchBatch, batch_id)
        if batch is None:
            raise DispatchError(
                "not_found", "Source-cleaning dispatch batch not found.", 404
            )
        run = session.get(SourceCleaningDispatchRun, batch.dispatch_run_id)
        if run is None:
            raise DispatchError(
                "not_found", "Source-cleaning dispatch run not found.", 404
            )
        now = utcnow()
        if batch.status != "leased" or batch.lease_token != lease_token:
            raise DispatchError(
                "lease_conflict", "The lease token is not current.", 409
            )
        if not _active_lease(batch.lease_expires_at, now):
            raise DispatchError(
                "lease_expired", "The dispatch lease has expired.", 409, retryable=True
            )
        command = _validated_inspection_command(action, arguments)
        baseline_document, _deterministic_operations = self._load_index(session, run)
        prior_operations, _rejected = self._completed_operations(session, run.id)
        if prior_operations:
            prior_result = source_cleaning.apply_cleaning_operations(
                baseline_document,
                prior_operations,
                max_replacement_chars=50_000,
            )
            working_document = baseline_document.excluding_blocks(
                set(prior_result.deleted_block_ids)
            )
        else:
            working_document = baseline_document
        if view == "source":
            source_payload = baseline_document.attributes.get(
                "epub_source_inspection_document"
            )
            if not isinstance(source_payload, dict):
                raise DispatchError(
                    "source_inspection_unavailable",
                    "Raw source inspection is unavailable for this run.",
                    409,
                )
            inspected_document = source_cleaning.SourceDocument.from_dict(
                source_payload
            )
        else:
            inspected_document = (
                baseline_document if view == "baseline" else working_document
            )
        observation = execute_tool_action(
            source_cleaning.SourceCleaningTools(inspected_document),
            command,
            max_batch_commands=_MAX_INSPECTION_COMMANDS,
        )
        returned_ids = _inspection_block_ids(observation)
        working_ids = {block.block_id for block in working_document.blocks}
        promoted_ids = (
            set() if view == "source" else returned_ids & working_ids
        )
        baseline_only_ids = (
            returned_ids - working_ids if view == "baseline" else set()
        )
        source_only_ids = returned_ids if view == "source" else set()

        packet = dict(batch.input_json or {})
        valid_ids = {
            str(item) for item in packet.get("valid_block_ids") or [] if str(item)
        }
        valid_ids.update(promoted_ids)
        packet["valid_block_ids"] = sorted(valid_ids)
        inspection_log = list(packet.get("inspection_log") or [])
        inspection_id = (
            f"inspection:{len(inspection_log) + 1}:{_canonical_hash(command)[:16]}"
        )
        inspection_log.append(
            {
                "inspection_id": inspection_id,
                "view": view,
                "action": action,
                "arguments": arguments,
                "returned_block_ids": sorted(returned_ids),
                "promoted_block_ids": sorted(promoted_ids),
                "inspected_at": now.isoformat(),
            }
        )
        packet["inspection_log"] = inspection_log
        batch.input_json = packet
        batch.input_hash = _canonical_hash(packet)
        batch.updated_at = now
        session.flush()
        return {
            "schema_version": "1",
            "run_id": run.id,
            "batch_id": batch.id,
            "phase": batch.phase,
            "inspection_id": inspection_id,
            "view": view,
            "action": action,
            "observation": observation,
            "promoted_block_ids": sorted(promoted_ids),
            "baseline_only_block_ids": sorted(baseline_only_ids),
            "source_only_block_ids": sorted(source_only_ids),
            "valid_block_id_count": len(valid_ids),
            "lease_expires_at": _iso(batch.lease_expires_at),
        }

    @staticmethod
    def _normalize_custom_operation(
        batch: SourceCleaningDispatchBatch,
        operation: dict[str, Any],
        *,
        valid_block_ids: set[str],
        valid_metadata_keys: set[str],
        marked_chapter_ids: set[str],
    ) -> dict[str, Any]:
        op = str(operation.get("op") or "").strip()
        allowed = set(_PHASE_ALLOWED_OPERATIONS.get(batch.phase, ()))
        if op not in allowed:
            raise DispatchError(
                "invalid_model_response",
                f"Operation '{op or '<missing>'}' is not allowed in phase '{batch.phase}'.",
                422,
            )
        reason = str(operation.get("reason") or "").strip()
        if op == "set_metadata":
            raw = operation.get("metadata")
            if not isinstance(raw, dict):
                raise DispatchError(
                    "invalid_model_response",
                    "set_metadata requires a metadata object.",
                    422,
                )
            metadata = {
                str(key): str(value).strip()
                for key, value in raw.items()
                if str(key) in valid_metadata_keys and str(value).strip()
            }
            if not metadata or len(metadata) != len(raw):
                raise DispatchError(
                    "invalid_model_response",
                    "Metadata contains an empty or unsupported key.",
                    422,
                    details={"valid_metadata_keys": sorted(valid_metadata_keys)},
                )
            return {
                "op": op,
                "metadata": metadata,
                **({"reason": reason} if reason else {}),
            }
        if op == "delete_blocks":
            block_ids = list(
                dict.fromkeys(
                    str(item).strip() for item in operation.get("block_ids") or []
                )
            )
            invalid = [item for item in block_ids if item not in valid_block_ids]
            if not block_ids or invalid:
                raise DispatchError(
                    "invalid_model_response",
                    "delete_blocks must reference only block IDs exposed in this batch.",
                    422,
                    details={"invalid_block_ids": invalid[:100]},
                )
            return {
                "op": op,
                "block_ids": block_ids,
                **({"reason": reason} if reason else {}),
            }
        if op == "replace_block":
            block_id = str(operation.get("block_id") or "").strip()
            replacement = str(operation.get("replacement") or "")
            if not block_id or block_id not in valid_block_ids:
                raise DispatchError(
                    "invalid_model_response",
                    "replace_block must reference a block exposed in this batch.",
                    422,
                    details={"block_id": block_id},
                )
            if not replacement.strip() or len(replacement) > 50_000:
                raise DispatchError(
                    "invalid_model_response",
                    "replace_block requires 1 to 50,000 replacement characters.",
                    422,
                )
            return {
                "op": op,
                "block_id": block_id,
                "replacement": replacement.strip(),
                **({"reason": reason} if reason else {}),
            }
        block_id = str(operation.get("block_id") or "").strip()
        if not block_id or block_id not in valid_block_ids:
            raise DispatchError(
                "invalid_model_response",
                f"{op} must reference a block ID exposed in this batch.",
                422,
                details={"block_id": block_id},
            )
        if op == "unmark_chapter" and block_id not in marked_chapter_ids:
            raise DispatchError(
                "invalid_model_response",
                "unmark_chapter may only undo an accepted chapter mark.",
                422,
            )
        normalized = {"op": op, "block_id": block_id}
        title = str(operation.get("title") or "").strip()
        if op == "mark_chapter" and title:
            normalized["title"] = title
        if reason:
            normalized["reason"] = reason
        return normalized

    @staticmethod
    def _completed_operations(
        session: Session,
        run_id: str,
    ) -> tuple[list[dict[str, Any]], int]:
        operations: list[dict[str, Any]] = []
        rejected = 0
        batches = list(
            session.scalars(
                select(SourceCleaningDispatchBatch)
                .where(
                    SourceCleaningDispatchBatch.dispatch_run_id == run_id,
                    SourceCleaningDispatchBatch.status == "completed",
                )
                .order_by(SourceCleaningDispatchBatch.ordinal)
            ).all()
        )
        for batch in batches:
            output = dict(batch.normalized_output_json or {})
            operations.extend(
                dict(item)
                for item in output.get("accepted_proposal_operations") or []
                if isinstance(item, dict)
            )
            operations.extend(
                dict(item)
                for item in output.get("operations") or []
                if isinstance(item, dict)
            )
            rejected += len(output.get("rejected_proposal_ids") or [])
        return operations, rejected

    def _normalize_submission(
        self,
        session: Session,
        batch: SourceCleaningDispatchBatch,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if str(result.get("kind") or "") != "source_cleaning":
            raise DispatchError(
                "result_kind_mismatch",
                "This batch requires a source_cleaning result.",
                422,
            )
        if str(result.get("phase") or "") != batch.phase:
            raise DispatchError(
                "result_phase_mismatch",
                f"This batch requires phase '{batch.phase}'.",
                422,
            )
        packet = dict(batch.input_json or {})
        proposals = {
            str(item.get("operation_id") or ""): item
            for item in packet.get("proposals") or []
            if isinstance(item, dict) and str(item.get("operation_id") or "")
        }
        decisions: dict[str, str] = {}
        for item in result.get("decisions") or []:
            if not isinstance(item, dict):
                raise DispatchError(
                    "invalid_model_response", "Each decision must be an object.", 422
                )
            operation_id = str(item.get("operation_id") or "")
            verdict = str(item.get("verdict") or "")
            if operation_id in decisions:
                raise DispatchError(
                    "invalid_model_response",
                    f"Proposal '{operation_id}' was decided more than once.",
                    422,
                )
            decisions[operation_id] = verdict
        missing = sorted(set(proposals) - set(decisions))
        unexpected = sorted(set(decisions) - set(proposals))
        invalid_verdicts = sorted(
            operation_id
            for operation_id, verdict in decisions.items()
            if verdict not in {"accept", "reject"}
        )
        if missing or unexpected or invalid_verdicts:
            raise DispatchError(
                "invalid_model_response",
                "Decide every server proposal exactly once using accept or reject.",
                422,
                details={
                    "missing_operation_ids": missing,
                    "unexpected_operation_ids": unexpected,
                    "invalid_verdict_operation_ids": invalid_verdicts,
                },
            )
        accepted_proposals = [
            dict(proposals[operation_id].get("operation") or {})
            for operation_id, verdict in decisions.items()
            if verdict == "accept"
        ]
        prior_operations, _rejected = self._completed_operations(
            session, batch.dispatch_run_id
        )
        marked_ids = {
            str(item.get("block_id") or "")
            for item in [*prior_operations, *accepted_proposals]
            if item.get("op") == "mark_chapter"
        }
        valid_block_ids = {str(item) for item in packet.get("valid_block_ids") or []}
        valid_metadata_keys = {
            str(item) for item in packet.get("valid_metadata_keys") or []
        }
        operations = [
            self._normalize_custom_operation(
                batch,
                dict(item),
                valid_block_ids=valid_block_ids,
                valid_metadata_keys=valid_metadata_keys,
                marked_chapter_ids=marked_ids,
            )
            for item in result.get("operations") or []
            if isinstance(item, dict)
        ]
        if len(operations) != len(result.get("operations") or []):
            raise DispatchError(
                "invalid_model_response", "Each operation must be an object.", 422
            )
        return {
            "kind": "source_cleaning",
            "phase": batch.phase,
            "accepted_proposal_ids": [
                operation_id
                for operation_id, verdict in decisions.items()
                if verdict == "accept"
            ],
            "rejected_proposal_ids": [
                operation_id
                for operation_id, verdict in decisions.items()
                if verdict == "reject"
            ],
            "accepted_proposal_operations": accepted_proposals,
            "operations": operations,
            "summary": str(result.get("summary") or "").strip(),
            "confidence": float(result.get("confidence") or 0.0),
        }

    @staticmethod
    def _submit_payload(
        run: SourceCleaningDispatchRun,
        batch: SourceCleaningDispatchBatch,
    ) -> dict[str, Any]:
        return {
            "run_id": run.id,
            "batch_id": batch.id,
            "output_role": "clean_text",
            "status": run.status,
            "run_status": run.status,
            "batch_status": batch.status,
            "accepted": batch.status == "completed",
            "completed_batch_count": run.completed_batch_count,
            "completed_batches": run.completed_batch_count,
            "batch_count": run.batch_count,
            "total_batches": run.batch_count,
            "remaining_batches": max(0, run.batch_count - run.completed_batch_count),
            "accepted_operation_count": run.accepted_operation_count,
            "rejected_proposal_count": run.rejected_proposal_count,
            "result_artifact_id": run.result_artifact_id,
            "final_artifact_id": run.result_artifact_id,
            "finalized": run.status == "completed",
            "requires_review": bool(
                (run.validation_json or {}).get("warnings")
                or (run.validation_json or {}).get("blocking_warnings")
            ),
            "validation": dict(run.validation_json or {})
            if run.status == "completed"
            else {},
            "error_code": run.error_code,
            "error_message": run.error_message,
        }

    def submit_in_session(
        self,
        session: Session,
        *,
        batch_id: str,
        lease_token: str,
        submission_key: str,
        result: dict[str, Any],
    ) -> tuple[dict[str, Any], int]:
        batch = session.get(SourceCleaningDispatchBatch, batch_id)
        if batch is None:
            raise DispatchError(
                "not_found", "Source-cleaning dispatch batch not found.", 404
            )
        run = session.get(SourceCleaningDispatchRun, batch.dispatch_run_id)
        if run is None:
            raise DispatchError(
                "not_found", "Source-cleaning dispatch run not found.", 404
            )
        raw_hash = _canonical_hash(result)
        if batch.status == "completed":
            if batch.submission_key == submission_key and batch.output_hash == raw_hash:
                if run.status == "finalizing":
                    self._retry_finalize(session, run)
                return self._submit_payload(run, batch), (
                    200 if run.status == "completed" else 202
                )
            raise DispatchError(
                "batch_completed",
                "The dispatch batch already has a different accepted submission.",
                409,
            )
        now = utcnow()
        if batch.status != "leased" or batch.lease_token != lease_token:
            raise DispatchError(
                "lease_conflict", "The lease token is not current.", 409
            )
        if not _active_lease(batch.lease_expires_at, now):
            raise DispatchError(
                "lease_expired", "The dispatch lease has expired.", 409, retryable=True
            )
        normalized = self._normalize_submission(session, batch, result)
        batch.status = "completed"
        batch.normalized_output_json = normalized
        batch.output_hash = raw_hash
        batch.submission_key = submission_key
        batch.accepted_at = now
        batch.lease_expires_at = None
        batch.updated_at = now
        run.completed_batch_count = int(
            session.scalar(
                select(func.count(SourceCleaningDispatchBatch.id)).where(
                    SourceCleaningDispatchBatch.dispatch_run_id == run.id,
                    SourceCleaningDispatchBatch.status == "completed",
                )
            )
            or 0
        )
        operations, rejected = self._completed_operations(session, run.id)
        run.accepted_operation_count = len(operations)
        run.rejected_proposal_count = rejected
        if run.completed_batch_count >= run.batch_count:
            run.status = "finalizing"
            run.error_code = None
            run.error_message = None
            self._retry_finalize(session, run)
            status = 200 if run.status == "completed" else 202
        else:
            run.status = "running"
            run.updated_at = now
            status = 200
        session.flush()
        return self._submit_payload(run, batch), status

    def _load_index(
        self,
        session: Session,
        run: SourceCleaningDispatchRun,
    ) -> tuple[Any, list[dict[str, Any]]]:
        artifact = session.get(Artifact, run.index_artifact_id)
        if artifact is None or artifact.state == "deleted" or not artifact.content_hash:
            raise DispatchError(
                "index_unavailable",
                "The persisted source-cleaning index is unavailable.",
                409,
            )
        try:
            path = self.artifacts.paths.managed_path(artifact.relative_path)
            if sha256_file(path) != artifact.content_hash:
                raise DispatchError(
                    "index_changed", "The persisted source-cleaning index changed.", 409
                )
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise DispatchError(
                "index_unavailable",
                "The persisted source-cleaning index cannot be read.",
                409,
            ) from error
        if not isinstance(payload, dict):
            raise DispatchError(
                "index_unavailable",
                "The persisted source-cleaning index is invalid.",
                409,
            )
        document_payload = payload.get("document")
        if not isinstance(document_payload, dict):
            # Compatibility with indexes written before the preparation
            # manifest began carrying deterministic proposals alongside it.
            document_payload = payload
        operations_payload = payload.get("deterministic_operations") or []
        deterministic_operations = [
            dict(item) for item in operations_payload if isinstance(item, dict)
        ]
        return (
            source_cleaning.SourceDocument.from_dict(document_payload),
            deterministic_operations,
        )

    def _ensure_batch_packet(
        self,
        session: Session,
        run: SourceCleaningDispatchRun,
        batch: SourceCleaningDispatchBatch,
    ) -> None:
        current = dict(batch.input_json or {})
        if current.get("packet_version") == _PACKET_VERSION:
            return
        document, deterministic_operations = self._load_index(session, run)
        prior_operations, _rejected = self._completed_operations(session, run.id)
        if prior_operations:
            prior_result = source_cleaning.apply_cleaning_operations(
                document,
                prior_operations,
                max_replacement_chars=50_000,
            )
            working_document = document.excluding_blocks(
                set(prior_result.deleted_block_ids)
            )
        else:
            working_document = document
        packets = _build_phase_packets(
            working_document,
            deterministic_operations,
            instructions=str(dict(run.settings_json or {}).get("instructions") or ""),
            evidence_limit=int(
                dict(run.settings_json or {}).get("evidence_limit") or 500
            ),
        )
        packet = next(
            (item for item in packets if item.get("phase") == batch.phase),
            None,
        )
        if packet is None:
            raise DispatchError(
                "preparation_conflict",
                f"No editorial packet could be built for phase '{batch.phase}'.",
                409,
            )
        packet["packet_version"] = _PACKET_VERSION
        packet["prior_operation_count"] = len(prior_operations)
        batch.input_json = packet
        batch.input_hash = _canonical_hash(packet)
        batch.updated_at = utcnow()
        session.flush()

    def _retry_finalize(self, session: Session, run: SourceCleaningDispatchRun) -> None:
        if run.status == "completed":
            return
        try:
            with session.begin_nested():
                self._materialize(session, run)
        except DispatchError as error:
            run.status = "failed"
            run.error_code = error.code
            run.error_message = str(error)[:2000]
            run.updated_at = utcnow()
            details = dict(error.details) if isinstance(error.details, dict) else {}
            details.update({"batch_accepted": True, "run_id": run.id})
            error.details = details
            session.flush()
            raise
        except (OSError, RuntimeError, SQLAlchemyError, TypeError, ValueError) as error:
            run.status = "finalizing"
            run.error_code = "materialization_failed"
            run.error_message = str(error)[:2000]
            run.updated_at = utcnow()
            session.flush()

    def retry_finalization_in_session(
        self,
        session: Session,
        *,
        run_id: str,
    ) -> tuple[dict[str, Any], int]:
        run = session.get(SourceCleaningDispatchRun, run_id)
        if run is None:
            raise DispatchError(
                "not_found", "Source-cleaning dispatch run not found.", 404
            )
        if run.status == "finalizing":
            self._retry_finalize(session, run)
        batch = session.scalar(
            select(SourceCleaningDispatchBatch)
            .where(SourceCleaningDispatchBatch.dispatch_run_id == run.id)
            .order_by(SourceCleaningDispatchBatch.ordinal.desc())
        )
        if batch is None:
            raise DispatchError(
                "not_found", "Source-cleaning dispatch batch not found.", 404
            )
        return self._submit_payload(run, batch), (
            200 if run.status == "completed" else 202
        )

    def _materialize(self, session: Session, run: SourceCleaningDispatchRun) -> None:
        current_snapshot = self._selection_snapshot(
            session,
            run.session_id,
            run.source_artifact_id,
        )
        if current_snapshot != dict(run.selection_snapshot_json or {}):
            raise DispatchError(
                "finalization_conflict",
                "The clean-source selection changed while this run was active.",
                409,
            )
        source, _path = self._verify_pinned_source(session, run)
        current_head = session.scalar(
            select(Artifact)
            .where(
                Artifact.session_id == run.session_id,
                Artifact.role == "clean_text",
                Artifact.state == "current",
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        )
        if (current_head.id if current_head else None) != run.output_head_artifact_id:
            raise DispatchError(
                "finalization_conflict",
                "A newer cleaned source already exists for this session.",
                409,
            )
        batches = list(
            session.scalars(
                select(SourceCleaningDispatchBatch)
                .where(SourceCleaningDispatchBatch.dispatch_run_id == run.id)
                .order_by(SourceCleaningDispatchBatch.ordinal)
            ).all()
        )
        if not batches or any(batch.status != "completed" for batch in batches):
            raise DispatchError(
                "finalization_incomplete",
                "Not all source-cleaning phases are accepted.",
                409,
                retryable=True,
            )
        document, _deterministic_operations = self._load_index(session, run)
        operations, rejected = self._completed_operations(session, run.id)
        result = source_cleaning.apply_cleaning_operations(
            document,
            operations,
            max_replacement_chars=50_000,
        )
        validation = source_cleaning.validate_cleaning_result(
            document,
            result,
            remove_footnotes=bool(
                dict(run.settings_json or {}).get("remove_footnotes", False)
            ),
        )
        if validation.errors:
            raise DispatchError(
                "invalid_cleanup_result",
                "Source cleaning failed final validation.",
                422,
                details={"validation": validation.to_dict()},
            )
        output_dir = (
            self.session_dir_resolver(run.session_id)
            / "source-cleaning-dispatch"
            / run.id
            / "final"
        )
        paths = source_cleaning.write_cleaning_artifacts(
            document, operations, result, str(output_dir)
        )
        cleaned_path = Path(paths["cleaned_text"])
        prepared = self.artifacts.prepare_registration(
            cleaned_path, settings=dict(run.settings_json or {})
        )
        parent_ids = [source.id]
        if run.baseline_artifact_id:
            parent_ids.append(run.baseline_artifact_id)
        artifact = self.artifacts.register_in_session(
            session,
            cleaned_path,
            kind="text",
            role="clean_text",
            session_id=run.session_id,
            parent_ids=parent_ids,
            settings=dict(run.settings_json or {}),
            metadata={
                "extraction": "passive_dispatch",
                "source_cleaning_dispatch_run_id": run.id,
                "source_artifact_id": source.id,
                "baseline_artifact_id": run.baseline_artifact_id,
                "index_artifact_id": run.index_artifact_id,
                "accepted_operation_count": len(operations),
                "rejected_proposal_count": rejected,
                "report": {
                    **result.report,
                    "validation": validation.to_dict(),
                    "phase_summaries": [
                        {
                            "phase": batch.phase,
                            "summary": dict(batch.normalized_output_json or {}).get(
                                "summary", ""
                            ),
                            "confidence": dict(batch.normalized_output_json or {}).get(
                                "confidence", 0.0
                            ),
                        }
                        for batch in batches
                    ],
                },
            },
            _prepared=prepared,
        )
        run.result_artifact_id = artifact.id
        run.accepted_operation_count = len(operations)
        run.rejected_proposal_count = rejected
        run.validation_json = validation.to_dict()
        run.status = "completed"
        run.error_code = None
        run.error_message = None
        run.updated_at = utcnow()


def prepare_source_cleaning_dispatch_job(
    database: Database,
    artifacts: ArtifactService,
    session_dir_resolver,
    payload: dict[str, Any],
    progress,
    cancel_event,
) -> dict[str, Any]:
    run_id = str(payload.get("source_cleaning_dispatch_run_id") or "")
    if not run_id:
        raise ValueError("Source-cleaning dispatch run ID is required.")
    service = SourceCleaningDispatchRunService(
        database,
        artifacts,
        session_dir_resolver,
    )
    return service.prepare_run(run_id, progress, cancel_event)
