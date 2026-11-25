"""Name the audio version an export that chose no run would use.

An export that names no generation run uses the session's current assembled
audio, narrowed to the active speech plan's one assembly. This module describes
that choice for callers (the export plan, the Output page and the refusal) from
one place, so they cannot disagree. It never changes the rule: it reports the
same candidates the export itself sees. ``active_plan_assemblies``, the
narrowing itself, lives here so that ``export_inputs`` can use it and the
description without an import cycle.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Artifact,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    OutputAssembly,
)

AMBIGUOUS_VERSIONS_SENTENCE = (
    "Multiple assembled audio versions are available. Select a completed "
    "audio version and export again."
)
# The refusal lists this many candidates and counts the rest; the export plan's
# ``export_audio`` lists them all.
MESSAGE_CANDIDATE_LIMIT = 10


def utc_iso(moment: datetime) -> str:
    """A stored time as aware UTC; SQLite hands back naive UTC values."""

    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC).isoformat()
    return moment.astimezone(UTC).isoformat()


def describe_export_audio(db_session: Session, session_id: str) -> dict[str, Any]:
    """Describe the audio an export with no run chosen would use today.

    ``status`` is ``named`` (``version`` is the one assembly), ``ambiguous``
    (``version`` is null and ``candidates`` lists every assembly, newest first)
    or ``none`` (no current assembled audio).
    """

    current = tuple(
        db_session.scalars(
            select(Artifact)
            .where(
                Artifact.session_id == session_id,
                Artifact.role == "assembled_audio",
                Artifact.state == "current",
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        )
    )
    narrowed = active_plan_assemblies(db_session, session_id, current)
    details = describe_assemblies(db_session, session_id, narrowed)
    if not details:
        return {"status": "none", "version": None, "candidates": []}
    if len(details) == 1:
        return {"status": "named", "version": details[0], "candidates": []}
    return {"status": "ambiguous", "version": None, "candidates": details}


def active_plan_assemblies(
    session: Session,
    session_id: str,
    candidates: tuple[Artifact, ...],
) -> tuple[Artifact, ...]:
    """Prefer the one assembly made from the active speech plan.

    Earlier assemblies stay current as audio versions of their runs. After a
    timing-only subtitle change the plan is prepared again, keeping every take,
    and assembled without a run, so no run can name that assembly. It is the
    session's audio, and an export that names no run uses it. When the active
    plan does not single out one assembly, every candidate is kept and the
    caller's ambiguity check still applies.
    """

    if len(candidates) < 2:
        return candidates
    active_revision_id = session.scalar(
        select(GenerationPlan.active_revision_id).where(
            GenerationPlan.session_id == session_id
        )
    )
    if not active_revision_id:
        return candidates
    active_artifact_ids = {
        assembly.artifact_id
        for assembly in session.scalars(
            select(OutputAssembly).where(
                OutputAssembly.session_id == session_id,
                OutputAssembly.status == "completed",
                OutputAssembly.artifact_id.in_([item.id for item in candidates]),
            )
        )
        if isinstance(assembly.settings_json, dict)
        and assembly.settings_json.get("plan_revision_id") == active_revision_id
    }
    narrowed = tuple(item for item in candidates if item.id in active_artifact_ids)
    return narrowed if len(narrowed) == 1 else candidates


def describe_assemblies(
    db_session: Session,
    session_id: str,
    artifacts: Iterable[Artifact],
) -> list[dict[str, Any]]:
    """Describe each assembled audio artifact: run, time and plan revision.

    Newest first. Nothing here names a path, a file or any text.
    """

    artifact_list = list(artifacts)
    if not artifact_list:
        return []
    artifact_ids = [item.id for item in artifact_list]
    assemblies: dict[str, OutputAssembly] = {}
    for assembly in db_session.scalars(
        select(OutputAssembly)
        .where(
            OutputAssembly.session_id == session_id,
            OutputAssembly.status == "completed",
            OutputAssembly.artifact_id.in_(artifact_ids),
        )
        .order_by(OutputAssembly.created_at.desc(), OutputAssembly.id.desc())
    ):
        assemblies.setdefault(str(assembly.artifact_id), assembly)
    run_ids = {
        str(assembly.generation_run_id)
        for assembly in assemblies.values()
        if assembly.generation_run_id
    }
    runs = {
        run.id: run
        for run in db_session.scalars(
            select(GenerationRun).where(GenerationRun.id.in_(run_ids))
        )
    } if run_ids else {}
    revision_ids: set[str] = set()
    stored_revisions: dict[str, str] = {}
    # What active_plan_assemblies counts: only the revision the assembly stored.
    counted_revisions: dict[str, str] = {}
    for artifact_id, assembly in assemblies.items():
        settings = assembly.settings_json if isinstance(assembly.settings_json, dict) else {}
        revision_id = str(settings.get("plan_revision_id") or "")
        if revision_id:
            counted_revisions[artifact_id] = revision_id
        run = runs.get(str(assembly.generation_run_id or ""))
        if not revision_id and run is not None:
            revision_id = str(run.plan_revision_id or "")
        if revision_id:
            stored_revisions[artifact_id] = revision_id
            revision_ids.add(revision_id)
    revision_numbers = {
        revision_id: number
        for revision_id, number in db_session.execute(
            select(GenerationPlanRevision.id, GenerationPlanRevision.revision_number)
            .where(GenerationPlanRevision.id.in_(revision_ids))
        )
    } if revision_ids else {}
    active_revision_id = db_session.scalar(
        select(GenerationPlan.active_revision_id).where(
            GenerationPlan.session_id == session_id
        )
    )
    described: list[tuple[datetime, dict[str, Any]]] = []
    for artifact in artifact_list:
        assembly = assemblies.get(artifact.id)
        run = runs.get(str(assembly.generation_run_id or "")) if assembly else None
        revision_id = stored_revisions.get(artifact.id) or None
        counted_id = counted_revisions.get(artifact.id)
        moment = assembly.created_at if assembly is not None else artifact.created_at
        aware = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment
        described.append((aware, {
            "assembly_id": assembly.id if assembly is not None else None,
            "artifact_id": artifact.id,
            "generation_run_id": run.id if run is not None else None,
            "run_sequence_number": run.sequence_number if run is not None else None,
            "plan_revision_id": revision_id,
            "plan_revision_number": revision_numbers.get(revision_id) if revision_id else None,
            "active_revision": bool(
                counted_id and active_revision_id and counted_id == active_revision_id
            ),
            "assembled_at": utc_iso(moment),
        }))
    described.sort(key=lambda item: (item[0], item[1]["artifact_id"]), reverse=True)
    return [item for _, item in described]


def ambiguous_versions_message(candidates: Sequence[Mapping[str, Any]]) -> str:
    """The refusal for an export that cannot tell which assembly to use.

    The first sentence is the one callers already match on; the candidates,
    newest first, follow so a caller can pass ``generation_run_id``. Past
    ``MESSAGE_CANDIDATE_LIMIT`` the rest are counted, not listed.
    """

    if not candidates:
        return AMBIGUOUS_VERSIONS_SENTENCE
    listed = candidates[:MESSAGE_CANDIDATE_LIMIT]
    message = f"{AMBIGUOUS_VERSIONS_SENTENCE} Candidates, newest first: " + "; ".join(
        _candidate_line(item) for item in listed
    )
    remaining = len(candidates) - len(listed)
    if remaining:
        message += (
            f"; and {remaining} more, which the export plan and the Output page list"
        )
    return message + "."


def _candidate_line(item: Mapping[str, Any]) -> str:
    run_id = item.get("generation_run_id")
    if run_id:
        number = item.get("run_sequence_number")
        label = f"run {number} ({run_id})" if number is not None else f"run {run_id}"
    else:
        # No run can choose it, so name the file's artifact for removal.
        label = f"no run (artifact {item.get('artifact_id')})"
    parts = [label, f"assembled {item.get('assembled_at')}"]
    revision = item.get("plan_revision_number")
    if revision is not None:
        parts.append(f"plan revision {revision}")
    return ", ".join(parts)
