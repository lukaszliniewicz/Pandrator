"""Database-only generation-run history reads and payload projections."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypedDict, overload

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .database import Database
from .generation_run_history import (
    EARLY_REPAIR_MARKER_KEY,
    EARLY_REPAIR_REASON,
    REGROUP_MARKER_KEY,
    REGROUP_REASON,
    GenerationRunHistory,
    build_generation_run_history,
)
from .models import (
    AudioTake,
    GenerationPlanRevision,
    GenerationRun,
    Job,
    OutputAssembly,
    SessionRecord,
    UsageEvent,
    utcnow,
)


@dataclass(frozen=True)
class _RunHistoryIndex:
    id: str
    session_id: str
    plan_revision_id: str
    source_generation_run_id: str | None
    output_generation_run_id: str | None
    sequence_number: int
    operation: str
    status: str
    created_at: datetime
    settings_snapshot_json: dict[str, str | None]


@dataclass(frozen=True)
class _RevisionHistoryIndex:
    operation_json: object


class RepairHistoryContext(TypedDict):
    """Inputs required by logical usage and optional repair projections."""

    workflow_kind: str | None
    runs: list[GenerationRun]
    usage_by_run_id: dict[str, list[UsageEvent]]


class RunHistoryContext(RepairHistoryContext):
    """Batched inputs for a session's full generation-run history."""

    runs_by_id: dict[str, GenerationRun]
    histories: dict[str, GenerationRunHistory]
    revisions_by_id: dict[str, GenerationPlanRevision]
    jobs_by_id: dict[str, Job]
    blocking_jobs: list[Job]
    assemblies_by_run_id: dict[str, OutputAssembly]
    take_counts: dict[str, int]
    visible_sequences: dict[str, int]


class GenerationHistoryReader:
    """Load run history and project job and repair state with only a database."""

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _run_label(
        run: GenerationRun,
        *,
        display_sequence: int | None = None,
        parent_label: str | None = None,
        repair_number: int | None = None,
    ) -> str:
        if parent_label is not None and repair_number is not None:
            return f"{parent_label} · Timing repair {repair_number}"
        snapshot = dict(run.settings_snapshot_json or {})
        tts = dict(snapshot.get("tts") or {})
        rvc = dict(snapshot.get("rvc") or {})
        details = []
        for value in (
            tts.get("service") or tts.get("tts_service") or tts.get("backend"),
            tts.get("model") or tts.get("xtts_model"),
            tts.get("voice") or tts.get("voice_name"),
        ):
            normalized = str(value or "").strip()
            if normalized and normalized.lower() not in {
                item.lower() for item in details
            }:
                details.append(normalized)
        if run.operation == "rvc":
            model = str(rvc.get("model") or rvc.get("rvc_model") or "").strip()
            details.append(f"RVC {model}".strip())
        if not details:
            details.append("Speech generation")
        sequence = (
            display_sequence
            if display_sequence is not None
            else run.sequence_number
        )
        return f"Run {sequence}: " + " · ".join(details)

    @staticmethod
    def _run_history_active(run: GenerationRun, job: Job | None) -> bool:
        return bool(
            run.status in {"queued", "running", "pausing", "pause_requested", "cancel_requested"}
            or (
                job is not None
                and job.status in {"queued", "running", "cancel_requested"}
            )
        )

    def _run_history_context(
        self,
        session: Session,
        session_id: str,
        runs: list[GenerationRun] | None = None,
    ) -> RunHistoryContext:
        """Load one session's run history and projection inputs in batches."""

        if runs is None:
            runs = list(
                session.scalars(
                    select(GenerationRun)
                    .where(GenerationRun.session_id == session_id)
                    .order_by(
                        GenerationRun.sequence_number.desc(),
                        GenerationRun.created_at.desc(),
                    )
                ).all()
            )
        run_by_id = {run.id: run for run in runs}
        revision_ids = {run.plan_revision_id for run in runs if run.plan_revision_id}
        revisions = (
            list(
                session.scalars(
                    select(GenerationPlanRevision).where(
                        GenerationPlanRevision.id.in_(revision_ids)
                    )
                ).all()
            )
            if revision_ids
            else []
        )
        revision_by_id = {revision.id: revision for revision in revisions}
        histories = build_generation_run_history(runs, revision_by_id)

        job_ids = {run.job_id for run in runs if run.job_id}
        jobs = (
            list(session.scalars(select(Job).where(Job.id.in_(job_ids))).all())
            if job_ids
            else []
        )
        job_by_id = {job.id: job for job in jobs}
        blocking_jobs: list[Job] = (
            list(
                session.scalars(
                    select(Job)
                    .where(
                        Job.session_id == session_id,
                        Job.status == "running",
                        Job.lease_expires_at > utcnow(),
                    )
                    .order_by(Job.created_at)
                    .limit(2)
                ).all()
            )
            if any(job.status == "queued" for job in jobs)
            else []
        )

        output_run_ids = {
            run.output_generation_run_id
            for run in runs
            if run.output_generation_run_id and run.output_generation_run_id in run_by_id
        }
        output_run_ids.update(run.id for run in runs)

        assemblies = list(
            session.scalars(
                select(OutputAssembly)
                .where(OutputAssembly.generation_run_id.in_(output_run_ids))
                .order_by(OutputAssembly.created_at.desc())
            ).all()
        ) if output_run_ids else []
        assembly_by_run_id: dict[str, OutputAssembly] = {}
        for assembly in assemblies:
            if assembly.generation_run_id is not None:
                assembly_by_run_id.setdefault(assembly.generation_run_id, assembly)

        assembly_job_ids = {
            assembly.job_id
            for assembly in assembly_by_run_id.values()
            if assembly.job_id and assembly.job_id not in job_by_id
        }
        if assembly_job_ids:
            assembly_jobs = session.scalars(
                select(Job).where(Job.id.in_(assembly_job_ids))
            ).all()
            job_by_id.update((job.id, job) for job in assembly_jobs)

        take_counts: dict[str, int] = {}
        if output_run_ids:
            for generation_run_id, count in session.execute(
                select(AudioTake.generation_run_id, func.count(AudioTake.id))
                .where(AudioTake.generation_run_id.in_(output_run_ids))
                .group_by(AudioTake.generation_run_id)
            ):
                if generation_run_id is not None:
                    take_counts[generation_run_id] = int(count)

        usage_by_run_id: dict[str, list[UsageEvent]] = {}
        if output_run_ids:
            usage_events = list(
                session.scalars(
                    select(UsageEvent).where(
                        UsageEvent.generation_run_id.in_(output_run_ids)
                    )
                ).all()
            )
            for event in usage_events:
                if event.generation_run_id is not None:
                    usage_by_run_id.setdefault(event.generation_run_id, []).append(event)

        visible_sequences = self._visible_run_sequences(runs, histories)
        return {
            "workflow_kind": getattr(session.get(SessionRecord, session_id), "workflow_kind", None),
            "runs": runs,
            "runs_by_id": run_by_id,
            "histories": histories,
            "revisions_by_id": revision_by_id,
            "jobs_by_id": job_by_id,
            "blocking_jobs": blocking_jobs,
            "assemblies_by_run_id": assembly_by_run_id,
            "take_counts": take_counts,
            "usage_by_run_id": usage_by_run_id,
            "visible_sequences": visible_sequences,
        }

    @staticmethod
    def _visible_run_sequences(
        runs: Sequence[GenerationRun | _RunHistoryIndex],
        histories: dict[str, GenerationRunHistory],
    ) -> dict[str, int]:
        visible_runs = [
            run
            for run in runs
            if run.output_generation_run_id is None
            and not histories[run.id].is_repair_child(run.id)
        ]
        visible_runs.sort(
            key=lambda item: (
                int(item.sequence_number or 0),
                str(item.created_at or ""),
                str(item.id),
            )
        )
        return {
            run.id: index for index, run in enumerate(visible_runs, start=1)
        }

    @staticmethod
    def _history_index(
        session: Session,
        session_id: str,
    ) -> tuple[list[_RunHistoryIndex], dict[str, _RevisionHistoryIndex]]:
        """Read ancestry columns without hydrating historical payloads."""
        early_marker = case(
            (
                func.json_type(
                    GenerationRun.settings_snapshot_json,
                    f"$.{EARLY_REPAIR_MARKER_KEY}",
                ) == "text",
                GenerationRun.settings_snapshot_json[EARLY_REPAIR_MARKER_KEY].as_string(),
            ),
            else_=None,
        )
        regroup_marker = case(
            (
                func.json_type(
                    GenerationRun.settings_snapshot_json,
                    f"$.{REGROUP_MARKER_KEY}",
                ) == "text",
                GenerationRun.settings_snapshot_json[REGROUP_MARKER_KEY].as_string(),
            ),
            else_=None,
        )
        rows = session.execute(
            select(
                GenerationRun.id,
                GenerationRun.session_id,
                GenerationRun.plan_revision_id,
                GenerationRun.source_generation_run_id,
                GenerationRun.output_generation_run_id,
                GenerationRun.sequence_number,
                GenerationRun.operation,
                GenerationRun.status,
                GenerationRun.created_at,
                early_marker,
                regroup_marker,
                GenerationPlanRevision.operation_json,
            )
            .outerjoin(
                GenerationPlanRevision,
                GenerationPlanRevision.id == GenerationRun.plan_revision_id,
            )
            .where(GenerationRun.session_id == session_id)
            .order_by(
                GenerationRun.sequence_number.desc(),
                GenerationRun.created_at.desc(),
            )
        )
        runs: list[_RunHistoryIndex] = []
        revisions: dict[str, _RevisionHistoryIndex] = {}
        for row in rows:
            run = _RunHistoryIndex(
                id=row[0],
                session_id=row[1],
                plan_revision_id=row[2],
                source_generation_run_id=row[3],
                output_generation_run_id=row[4],
                sequence_number=row[5],
                operation=row[6],
                status=row[7],
                created_at=row[8],
                settings_snapshot_json={
                    EARLY_REPAIR_MARKER_KEY: row[9],
                    REGROUP_MARKER_KEY: row[10],
                },
            )
            operation: object = row[11]
            runs.append(run)
            revisions[run.plan_revision_id] = _RevisionHistoryIndex(operation)
        return runs, revisions

    def _selected_run_context(
        self,
        session: Session,
        session_id: str,
        *,
        include_repairs: bool = True,
        limit: int | None = None,
        latest: bool = False,
    ) -> tuple[RunHistoryContext | None, list[GenerationRun]]:
        index_runs, revisions = self._history_index(session, session_id)
        histories = build_generation_run_history(index_runs, revisions)
        if latest:
            active_grouped = next(
                (
                    candidate
                    for candidate in index_runs
                    if candidate.output_generation_run_id
                    and candidate.status
                    in {"queued", "running", "pausing", "cancel_requested"}
                ),
                None,
            )
            run = active_grouped or next(
                (
                    candidate
                    for candidate in index_runs
                    if candidate.output_generation_run_id is None
                    and not histories[candidate.id].is_repair_child(candidate.id)
                ),
                None,
            )
            selected = [run] if run is not None else []
        else:
            selected = index_runs
            if not include_repairs:
                selected = [
                    run for run in selected
                    if not histories[run.id].is_repair_child(run.id)
                ]
            if limit is not None:
                selected = selected[:limit]
        if not selected:
            return None, []

        by_id = {run.id: run for run in index_runs}
        needed_ids = {run.id for run in selected}
        label_runs = list(selected)
        for selected_run in selected:
            output_run = by_id.get(selected_run.output_generation_run_id or "")
            if output_run is not None:
                needed_ids.add(output_run.id)
                label_runs.append(output_run)
        for label_run in label_runs:
            history = histories[label_run.id]
            needed_ids.add(history.root.id)
            needed_ids.update(child.id for child in history.repair_children)
            needed_ids.update(self._logical_run_ids(index_runs, history))
        hydrated = list(
            session.scalars(
                select(GenerationRun)
                .where(
                    GenerationRun.session_id == session_id,
                    GenerationRun.id.in_(needed_ids),
                )
                .order_by(
                    GenerationRun.sequence_number.desc(),
                    GenerationRun.created_at.desc(),
                )
            ).all()
        )
        context = self._run_history_context(session, session_id, runs=hydrated)
        context["visible_sequences"] = self._visible_run_sequences(index_runs, histories)
        return context, [context["runs_by_id"][run.id] for run in selected]

    @staticmethod
    def _logical_run_ids(
        runs: Sequence[GenerationRun | _RunHistoryIndex],
        history: GenerationRunHistory,
    ) -> set[str]:
        logical_ids = {history.root.id}
        logical_ids.update(child.id for child in history.repair_children)
        plan_ids = {history.root.plan_revision_id, *(child.plan_revision_id for child in history.repair_children)}
        for run in runs:
            if run.output_generation_run_id in logical_ids and run.plan_revision_id in plan_ids:
                logical_ids.add(run.id)
        return logical_ids

    @staticmethod
    def _logical_usage_events(
        context: RepairHistoryContext,
        history: GenerationRunHistory,
    ) -> list[UsageEvent]:
        """Collect usage for a logical run, de-duplicating shared ownership."""

        logical_ids = GenerationHistoryReader._logical_run_ids(context["runs"], history)
        events: list[UsageEvent] = []
        for run_id in logical_ids:
            events.extend(context["usage_by_run_id"].get(run_id, ()))
        return events

    @staticmethod
    def _timing_repair_status(
        root: GenerationRun,
        history: GenerationRunHistory,
        root_job: Job | None,
    ) -> tuple[str, bool]:
        """Infer repair state while keeping optional repair failures local."""

        children = history.repair_children
        job_active = root_job is not None and root_job.status in {
            "queued", "running", "cancel_requested"
        }
        # A crashed/stopped job can leave a child queued or an outcome pending.
        # Such leftovers must not keep the logical run "repairing" forever.
        if root.status == "failed" or (root_job is not None and root_job.status == "failed"):
            return "failed", False
        if root.status in {"canceled", "stopped", "paused"} or (
            root_job is not None and root_job.status in {"canceled", "interrupted"}
        ):
            return "stopped", False
        active_child = any(GenerationHistoryReader._run_history_active(child, None) for child in children)
        if (active_child and (root_job is None or job_active)) or (
            root_job is not None
            and job_active
            and (children or float(root_job.progress or 0) >= 0.85)
        ):
            return "running", True

        outcomes = {
            str(
                history.repair_operations.get(str(child.id), {}).get(
                    "repair_status"
                )
                or ""
            )
            for child in children
        }
        if "failed" in outcomes or any(child.status == "failed" for child in children):
            return "failed", False
        if "stopped" in outcomes or any(
            child.status in {"canceled", "stopped"} for child in children
        ):
            return "stopped", False
        if "pending" in outcomes or active_child:
            return "stopped", False
        return "completed", False

    def _timing_repair_payload(
        self,
        context: RepairHistoryContext,
        history: GenerationRunHistory,
        root: GenerationRun,
        root_job: Job | None,
    ) -> tuple[dict[str, Any], bool]:
        status, active = self._timing_repair_status(root, history, root_job)
        result = history.result
        versions = []
        for child in history.repair_children:
            operation = history.repair_operations.get(str(child.id), {})
            versions.append(
                {
                    "generation_run_id": child.id,
                    "plan_revision_id": child.plan_revision_id,
                    "sequence_number": child.sequence_number,
                    "status": child.status,
                    "repair_status": operation.get("repair_status") or "unknown",
                    "repair_reason": operation.get("repair_reason"),
                    # Persisted operation reason (early_timing_repair vs
                    # passage_regroup); additive and authoritative for kind.
                    "reason": operation.get("reason"),
                    "source_block_ordinal": operation.get("source_block_ordinal"),
                    "created_at": child.created_at.isoformat(),
                }
            )
        reasons = {
            str(history.repair_operations.get(str(child.id), {}).get("reason") or "")
            for child in history.repair_children
        }
        if REGROUP_REASON in reasons and EARLY_REPAIR_REASON in reasons:
            kind = "mixed"
        elif REGROUP_REASON in reasons:
            kind = "regroup"
        elif EARLY_REPAIR_REASON in reasons:
            kind = "repair"
        elif not versions:
            # Zero children (e.g. repair enabled but nothing staged yet):
            # fall back to the immutable root snapshot's second-pass
            # selection. With children present the persisted reasons above
            # stay authoritative; never infer the operation from counts.
            from pandrator.logic.dubbing.passage_regroup import (
                select_second_pass,
            )

            snapshot = (
                root.settings_snapshot_json
                if isinstance(getattr(root, "settings_snapshot_json", None), dict)
                else {}
            )
            second_pass = select_second_pass(
                snapshot,
                operation=str(getattr(root, "operation", "") or ""),
                has_selected_ids=False,
                workflow_kind=str(context.get("workflow_kind") or ""),
            )
            kind = (
                "regroup"
                if second_pass == "regroup"
                else "repair"
                if second_pass == "repair"
                else "unknown"
            )
        else:
            kind = "unknown"
        usage_events = self._logical_usage_events(context, history)
        from .usage import usage_summary

        summary = {
            "result_generation_run_id": result.id,
            "result_plan_revision_id": result.plan_revision_id,
            "result_sequence_number": result.sequence_number,
            "applied_count": len(history.applied_children),
            "attempt_count": len(history.repair_children),
            "status": status,
            "kind": kind,
            "versions": versions,
            "usage": usage_summary(usage_events),
        }
        return summary, active

    @overload
    def _run_payload(
        self,
        session: Session,
        run: None,
        *,
        _context: RunHistoryContext | None = None,
    ) -> None: ...

    @overload
    def _run_payload(
        self,
        session: Session,
        run: GenerationRun,
        *,
        _context: RunHistoryContext | None = None,
    ) -> dict[str, Any]: ...

    def _run_payload(
        self,
        session: Session,
        run: GenerationRun | None,
        *,
        _context: RunHistoryContext | None = None,
    ) -> dict[str, Any] | None:
        if run is None:
            return None
        context = _context or self._run_history_context(session, run.session_id)
        history = context["histories"].get(run.id)
        if history is None:
            history = GenerationRunHistory(
                root=run,
                repair_children=(),
                result=run,
                repair_operations={},
            )
        job = context["jobs_by_id"].get(run.job_id) if run.job_id else None
        output_run = (
            context["runs_by_id"].get(run.output_generation_run_id)
            if run.output_generation_run_id
            else run
        )
        output_run_id = output_run.id if output_run is not None else run.id
        label_run = output_run or run
        visible_sequence = context["visible_sequences"].get(
            label_run.id, label_run.sequence_number
        )
        repair_number = None
        parent_label = None
        label_history = context["histories"].get(label_run.id, history)
        if label_history.is_repair_child(label_run.id):
            repair_number = next(
                index
                for index, child in enumerate(label_history.repair_children, start=1)
                if child.id == label_run.id
            )
            root_sequence = context["visible_sequences"].get(
                label_history.root.id, label_history.root.sequence_number
            )
            parent_label = self._run_label(
                label_history.root,
                display_sequence=root_sequence,
            )
        assembly = context["assemblies_by_run_id"].get(output_run_id)
        take_count = context["take_counts"].get(output_run_id, 0)
        from .usage import usage_summary

        usage = context["usage_by_run_id"].get(output_run_id, [])
        snapshot = dict(run.settings_snapshot_json or {})
        modal_snapshot = {
            key: deepcopy(snapshot[key])
            for key in ("tts", "rvc", "selected_segment_override")
            if isinstance(snapshot.get(key), dict)
        }
        payload = {
            "id": run.id,
            "session_id": run.session_id,
            "plan_revision_id": run.plan_revision_id,
            "source_generation_run_id": run.source_generation_run_id,
            "output_generation_run_id": run.output_generation_run_id,
            "sequence_number": run.sequence_number,
            "operation": run.operation,
            "label": self._run_label(
                label_run,
                display_sequence=visible_sequence,
                parent_label=parent_label,
                repair_number=repair_number,
            ),
            "job_id": run.job_id,
            "status": run.status,
            "progress": 1.0
            if run.status == "completed"
            else float(job.progress)
            if job
            else 0.0,
            "pause_requested": run.pause_requested,
            "cancel_requested": run.cancel_requested,
            "resume_source_on_completion": run.resume_source_on_completion,
            "settings_hash": run.settings_hash,
            # A run has a full reproducibility snapshot, but the history UI
            # needs only speech settings to seed an alternate take.  Do not
            # turn this list endpoint into a settings-data side channel.
            "settings_snapshot": modal_snapshot,
            "error_message": job.error_message
            if job and run.status == "failed"
            else None,
            "take_count": take_count,
            "usage": usage_summary(usage),
            "progress_detail": job.progress_detail if job else None,
            "assembly": (
                self._assembly_payload(
                    assembly,
                    context["jobs_by_id"].get(assembly.job_id) if assembly.job_id else None,
                )
                if assembly
                else None
            ),
            "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
        }

        # A stale recording may be waiting for its replacement, rather than
        # being stuck in synthesis. Expose queue state without changing takes.
        payload["queued_segment_ids"] = (
            list((job.payload_json or {}).get("segment_ids") or [])
            if job is not None and job.status == "queued" else []
        )
        payload["waiting_for_job"] = None
        if job is not None and job.status == "queued":
            blocker = next(
                (candidate for candidate in context["blocking_jobs"] if candidate.id != job.id),
                None,
            )
            if blocker is not None:
                payload["waiting_for_job"] = {
                    "id": blocker.id,
                    "kind": blocker.kind,
                    "progress_detail": blocker.progress_detail,
                }

        is_original_root = (
            run.output_generation_run_id is None and history.root.id == run.id
        )
        timing_repair = None
        repair_active = False
        from pandrator.logic.dubbing.passage_regroup import select_second_pass

        second_pass = select_second_pass(
            snapshot,
            operation=str(run.operation or ""),
            has_selected_ids=False,
            workflow_kind=str(context["workflow_kind"] or ""),
        )
        repair_enabled = second_pass == "repair" and run.operation in {
            "generate",
            "resume",
        }
        if is_original_root and (history.repair_children or repair_enabled):
            timing_repair, repair_active = self._timing_repair_payload(
                context,
                history,
                run,
                job,
            )
            payload["timing_repair"] = timing_repair
            if repair_active:
                payload["status"] = "running"
                payload["phase"] = "repairing_timing"
                if job is not None and job.status in {
                    "queued",
                    "running",
                    "cancel_requested",
                }:
                    payload["progress"] = float(job.progress)

        payload["early_repair_parent_run_id"] = (
            history.root.id if history.is_repair_child(run.id) else None
        )
        # Which optional second pass this run's mode selects (repair for
        # legacy planning, regroup for passage-first planning, else None).
        payload["second_pass"] = second_pass
        payload["result_generation_run_id"] = (
            history.result.id if is_original_root else run.id
        )
        return payload

    def list_runs(
        self, session_id: str, *, include_repairs: bool = True, limit: int | None = None
    ) -> list[dict[str, Any]]:
        if limit is not None:
            with self.database.snapshot_session() as session:
                context, runs = self._selected_run_context(
                    session, session_id, include_repairs=include_repairs, limit=limit
                )
                if context is None:
                    return []
                return [self._run_payload(session, run, _context=context) for run in runs]
        with self.database.session() as session:
            context = self._run_history_context(session, session_id)
            runs = context["runs"]
            if not include_repairs:
                runs = [
                    run for run in runs
                    if not context["histories"][run.id].is_repair_child(run.id)
                ]
            return [self._run_payload(session, run, _context=context) for run in runs]

    def latest_run(self, session_id: str) -> dict[str, Any] | None:
        with self.database.snapshot_session() as session:
            context, runs = self._selected_run_context(session, session_id, latest=True)
            if context is None:
                return None
            return self._run_payload(session, runs[0], _context=context)

    @staticmethod
    def _assembly_payload(
        record: OutputAssembly,
        job: Job | None = None,
    ) -> dict[str, Any]:
        return {
            "id": record.id,
            "session_id": record.session_id,
            "generation_run_id": record.generation_run_id,
            "job_id": record.job_id,
            "artifact_id": record.artifact_id,
            "status": record.status,
            "progress": (
                1.0
                if record.status in {"completed", "stale"}
                else float(job.progress)
                if job
                else 0.0
            ),
            "progress_detail": job.progress_detail if job else None,
            "settings_hash": record.settings_hash,
            "error_message": record.error_message,
            "settings": deepcopy(record.settings_json or {}),
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
        }
