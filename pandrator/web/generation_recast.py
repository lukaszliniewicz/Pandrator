"""Read-only cast impact and literal, whole-block scoped recast preparation.

Only active recordings and their executor lineage are read. Historical intent
comes from the executor snapshot, never from today's character ledger.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import select

from pandrator.logic.speech_markup import parse_speech_markup

from . import models as m
from .generation_audio_identity import IDENTITY_KEY, AudioIdentityContext, plan_audio_identities
from .generation_cast_runtime import (
    _VOICE_IDENTITY_KEYS,
    _binding_key,
    apply_resolved_binding,
    freeze_cast_snapshot,
    resolve_binding,
    segment_voice_binding,
)
from .generation_controls import get_generation_controls, resolve_cast_voice
from .generation_performance_snapshot import performance_runtime_settings
from .generation_rendering import is_strict_single_voice
from .generation_selection import _resolve_takes, collect_row_state
from .generation_start_preparation import snapshot_guard
from .jobs import JobQueue
from .settings_policy import RevisionConflict, stable_hash
from .speech_plan_workspace import freeze_speech_snapshot

_BASE_KEYS = ("voice", "speaker", *_VOICE_IDENTITY_KEYS)
_CATEGORIES = ("male", "female", "androgynous", "unspecified")


def collect_recast_job_guard(session, session_id: str, revision_id: str) -> list[list[str]]:
    """Light admission guard, including failed runs with a live worker job."""
    return [
        list(row)
        for row in session.execute(
            select(m.Job.id, m.Job.status, m.GenerationRun.id)
            .join(
                m.GenerationRun,
                (m.GenerationRun.job_id == m.Job.id)
                | (m.Job.payload_json["generation_run_id"].as_string() == m.GenerationRun.id),
            )
            .where(
                m.Job.session_id == session_id,
                m.Job.kind == "generation.run",
                m.Job.status.not_in(sorted(JobQueue.GENERATION_JOB_TERMINAL_STATUS)),
                m.GenerationRun.session_id == session_id,
                m.GenerationRun.plan_revision_id == revision_id,
            )
            .distinct()
            .order_by(m.Job.id, m.GenerationRun.id)
        )
    ]


def _load(session, model, ids):
    result = {}
    ids = sorted(set(ids))
    for offset in range(0, len(ids), 500):
        for row in session.scalars(select(model).where(model.id.in_(ids[offset : offset + 500]))):
            result[row.id] = row
    return result


def _recorded_snapshots(session, session_id, recorded):
    """Find the actual executor, including copied-take ancestry, in bounded batches."""
    takes = _load(session, m.AudioTake, [take["id"] for take in recorded.values()])
    frontier = list(takes.values())
    while frontier:
        parents = [take.parent_take_id for take in frontier if take.parent_take_id not in takes]
        frontier = list(_load(session, m.AudioTake, [value for value in parents if value]).values())
        takes.update({take.id: take for take in frontier})
    artifacts = _load(
        session, m.Artifact, [take.artifact_id for take in takes.values() if take.artifact_id]
    )
    executor_ids = {}
    for take in takes.values():
        artifact = artifacts.get(take.artifact_id)
        metadata = (artifact.metadata_json or {}) if artifact else {}
        executor_ids[take.id] = metadata.get("generation_task_run_id") or take.generation_run_id
    runs = _load(session, m.GenerationRun, [value for value in executor_ids.values() if value])
    frontier = list(runs.values())
    while frontier:
        parents = [
            run.source_generation_run_id
            for run in frontier
            if run.source_generation_run_id not in runs
        ]
        frontier = list(
            _load(session, m.GenerationRun, [value for value in parents if value]).values()
        )
        runs.update({run.id: run for run in frontier})
    result = {}
    for segment_id, selected in recorded.items():
        take = takes.get(selected["id"])
        seen = set()
        while take and take.id not in seen:
            seen.add(take.id)
            run = runs.get(executor_ids.get(take.id))
            seen_runs = set()
            while run and run.id not in seen_runs and run.session_id == session_id:
                seen_runs.add(run.id)
                snapshot = run.settings_snapshot_json or {}
                frozen = snapshot.get("generation_control_snapshot") or {}
                entries = frozen.get("segments") or {}
                entry_id = segment_id if segment_id in entries else take.generation_segment_id
                entry = entries.get(entry_id)
                if entry and frozen.get("schema_version") == 1:
                    artifact = artifacts.get(take.artifact_id)
                    identity = (
                        (artifact.metadata_json or {}).get(IDENTITY_KEY) if artifact else None
                    )
                    result[segment_id] = (snapshot, entry, run.id, entry_id, identity)
                    break
                run = runs.get(run.source_generation_run_id)
            if segment_id in result:
                break
            take = takes.get(take.parent_take_id)
    return result


def _selectors(controls, segments):
    result = {"narrator": "Narrator"}
    result.update(
        {
            f"character:{item['id']}": item.get("display_name") or item["id"]
            for item in controls["characters"]
        }
    )
    result.update(
        {f"category:{category}": category.capitalize() + " dialogue" for category in _CATEGORIES}
    )
    sources = set((controls.get("cast") or {}).get("source_speakers") or {})
    sources.update(segment.speaker for segment in segments if segment.speaker)
    result.update({f"source:{label}": label for label in sorted(sources)})
    return result


def _scopes(segment, entry, controls):
    """Return literal speaker scopes and selectors whose effective cast reaches them."""
    text = segment.optimized_text or segment.text
    xml = entry.get("speech_xml")
    if xml:
        spans = parse_speech_markup(
            xml,
            expected_segment_id=segment.id,
            expected_text=text,
            characters=controls["characters"],
        ).spans
    else:
        spans = [None]
    result = []
    for span in spans:
        if span is not None and not any(character.isalnum() for character in span.text):
            continue
        narrator = bool(span and span.narrator)
        speaker = span.speaker_id if span and not narrator else None
        source = (
            segment.speaker
            if not narrator and (not xml or span and span.dialogue and not speaker)
            else None
        )
        dialogue = bool(span.dialogue if span and not narrator else source)
        raw_category = span.voice_category if span and not narrator else "unspecified"
        category = raw_category
        if speaker and category == "unspecified":
            category = next(
                (
                    item.get("voice_category") or category
                    for item in controls["characters"]
                    if item["id"] == speaker
                ),
                category,
            )
        scope = (
            f"character:{speaker}"
            if speaker
            else f"source:{source}"
            if source
            else f"category:{category}"
            if dialogue
            else "narrator"
        )
        resolution = resolve_cast_voice(
            controls,
            speaker_id=speaker,
            source_speaker=source,
            dialogue=dialogue,
            voice_category=category,
            span_voice=span.voice if span else None,
        )
        keys = {scope}
        if resolution["source"] == "category":
            keys.add(f"category:{category}")
        elif dialogue and resolution["source"] in {"narrator", "inherited"}:
            keys.add(f"category:{category}")
        if resolution["source"] in {"narrator", "inherited"}:
            keys.add("narrator")
        if resolution["source"] == "source_speaker" and source:
            keys.add(f"source:{source}")
        overridden = bool(entry.get("voice_binding") or resolution["source"] == "span")
        result.append(
            {
                "scope": scope,
                "keys": keys,
                "speaker": speaker,
                "source": source,
                "dialogue": dialogue,
                "category": raw_category,
                "binding": resolution["binding"],
                "overridden": overridden,
            }
        )
    return result


def _effective(session, context, binding, base, resolutions=None):
    resolved = None
    if binding:
        key = _binding_key(binding)
        resolved = (resolutions or {}).get(key)
        if resolved is None:
            resolved = resolve_binding(
                session, binding, base, context._service_config_cache, context._registry
            )
        settings = apply_resolved_binding(binding, base, {key: resolved})
    else:
        settings = base
    voice = str(settings.get("voice") or settings.get("speaker") or "")
    refs = list(context.voices.get(voice.strip().casefold().removesuffix(".wav"), []))
    managed = settings.get("_cast_voice_id")
    if managed:
        refs.extend(context.voices.get(str(managed).casefold(), []))
    # Provider/model/delivery changes are deliberately excluded from cast impact.
    return stable_hash(
        {
            "voice": voice,
            "provider_voice": settings.get("elevenlabs_voice_id"),
            "description": (binding or {}).get("voice_description"),
            "inline_reference": [
                settings.get(key)
                for key in (
                    "audio_cpp_voice_ref",
                    "audio_cpp_voice_ref_hash",
                    "audio_cpp_reference_text",
                )
            ],
            "references": resolved.get(
                "_recast_reference_fingerprint", sorted({stable_hash(ref) for ref in refs})
            )
            if resolved
            else sorted({stable_hash(ref) for ref in refs}),
        }
    )


def _binding_for_scope(controls, scope):
    return resolve_cast_voice(
        controls,
        speaker_id=scope["speaker"],
        source_speaker=scope["source"],
        dialogue=scope["dialogue"],
        voice_category=scope["category"],
    )["binding"]


def _base_binding(base):
    if base.get("_cast_voice_id"):
        return {"voice_id": base["_cast_voice_id"]}
    voice = base.get("voice") or base.get("speaker")
    if not voice:
        raise ValueError(
            "The recorded fallback voice cannot be represented safely for this speaker."
        )
    return {"voice": voice}


def _patch_scope(controls, scope, binding):
    cast = controls.setdefault("cast", {})
    key = scope["scope"]
    if key == "narrator":
        cast["narrator"] = deepcopy(binding)
    else:
        prefix, identifier = key.split(":", 1)
        field = {"character": "characters", "category": "categories", "source": "source_speakers"}[
            prefix
        ]
        cast.setdefault(field, {})[identifier] = deepcopy(binding)


def prepare_recast(
    database,
    settings_service,
    session_id: str,
    *,
    selector_keys: list[str] | None = None,
    speech_plan_revision_id: str | None = None,
    force: bool = False,
    include_missing: bool = False,
) -> dict[str, Any]:
    """Compile current settings and scoped assignments without persisting anything."""
    with database.snapshot_session() as session:
        raw, resolved_hash = settings_service.resolve_in_session(session, session_id)
        raw = deepcopy(raw)
        snapshot = deepcopy(raw)
        controls = get_generation_controls(session, session_id)
        plan = session.scalar(
            select(m.GenerationPlan).where(m.GenerationPlan.session_id == session_id)
        )
        revision_id = speech_plan_revision_id or (plan.active_revision_id if plan else None)
        revision = session.get(m.GenerationPlanRevision, revision_id) if revision_id else None
        if (
            plan is None
            or revision is None
            or revision.plan_id != plan.id
            or plan.active_revision_id != revision_id
        ):
            raise RevisionConflict(
                "Select the current speech plan before recasting its recordings."
            )
        revision_id = revision.id
        row_state = collect_row_state(session, revision_id)
        active_job_guard = collect_recast_job_guard(session, session_id, revision_id)
        _, _, recorded = _resolve_takes(row_state)
        segments = list(
            session.scalars(
                select(m.GenerationSegment)
                .where(
                    m.GenerationSegment.plan_revision_id == revision_id,
                    m.GenerationSegment.removed.is_(False),
                )
                .order_by(m.GenerationSegment.ordinal)
            )
        )
        labels = _selectors(controls, segments)
        if selector_keys is not None and (
            not isinstance(selector_keys, list)
            or any(not isinstance(key, str) or key not in labels for key in selector_keys)
        ):
            raise ValueError(
                "A recast selector does not belong to this session's current speech plan."
            )
        selected = set(selector_keys or [])
        blocked = None
        current_settings = performance_runtime_settings(snapshot)
        if is_strict_single_voice(current_settings) or not current_settings.get("casting_enabled"):
            blocked = "Enable multiple voices before recasting individual speakers."
        if row_state["active_runs"] or active_job_guard:
            blocked = "Finish or cancel the active generation run before recasting."
        guard = snapshot_guard(session, session_id, revision_id)
        snapshot["speech_plan_revision_id"] = revision_id
        freeze_error = None
        if not blocked:
            try:
                freeze_speech_snapshot(
                    session, revision_id, snapshot, explicit=True, cast_segment_ids=[]
                )
            except ValueError as error:
                freeze_error = str(error)
                blocked = freeze_error
        frozen = snapshot.get("generation_control_snapshot") or {}
        entries = frozen.get("segments") or {}
        provenance = _recorded_snapshots(session, session_id, recorded)
        recorded_segments = _load(
            session, m.GenerationSegment, [value[3] for value in provenance.values()]
        )
        context = AudioIdentityContext(session, snapshot)
        counts = {
            key: {
                "key": key,
                "label": label,
                "affected_count": 0,
                "changed_count": 0,
                "missing_count": 0,
                "unverified_count": 0,
                "overridden_count": 0,
            }
            for key, label in labels.items()
        }
        rows = []
        evidence_hash = []
        historical_contexts = {}
        historical_snapshot_hashes = {}
        for segment in segments:
            entry = entries.get(segment.id) or {
                "speech_xml": (segment.speech_plan_json or {}).get("speech_xml"),
                "voice_binding": segment_voice_binding(segment),
            }
            scopes = _scopes(segment, entry, controls)
            baseline = provenance.get(segment.id)
            old_snapshot, old_entry, executor, recorded_segment_id, actual_identity = (
                baseline if baseline else ({}, {}, None, None, None)
            )
            old_frozen = old_snapshot.get("generation_control_snapshot") or {}
            old_controls = old_entry.get("controls", old_frozen.get("controls"))
            old_base = performance_runtime_settings(old_snapshot)
            for key, value in ((old_controls or {}).get("_base_voice_settings") or {}).items():
                if value is None:
                    old_base.pop(key, None)
                else:
                    old_base[key] = value
            changed, unknown, affected, overrides = set(), set(), set(), set()
            missing = segment.id not in recorded
            reference_verified = True
            if baseline:
                if executor not in historical_contexts:
                    historical_contexts[executor] = AudioIdentityContext(session, old_snapshot)
                    historical_snapshot_hashes[executor] = stable_hash(old_snapshot)
                old_identity = actual_identity
                recorded_segment = recorded_segments.get(recorded_segment_id)
                current_old_identity = (
                    historical_contexts[executor].for_segment(recorded_segment)
                    if recorded_segment
                    else {}
                )
                reference_verified = bool(
                    old_identity
                    and old_identity.get("voice_reference_hash")
                    == current_old_identity.get("voice_reference_hash")
                )
            for scope in scopes:
                keys = scope["keys"] & labels.keys()
                if scope["overridden"]:
                    overrides.update(keys)
                    continue
                affected.update(keys)
                try:
                    new_fp = _effective(
                        session,
                        context,
                        scope["binding"],
                        current_settings,
                        frozen.get("resolved_bindings"),
                    )
                    if missing:
                        continue
                    if not old_controls:
                        unknown.update(keys)
                        continue
                    old_binding = _binding_for_scope(old_controls, scope)
                    old_fp = _effective(
                        session, context, old_binding, old_base, old_frozen.get("resolved_bindings")
                    )
                    if old_fp != new_fp:
                        changed.update(keys)
                    elif not reference_verified and (
                        (old_binding or {}).get("voice_id")
                        or any(
                            context.voices.get(str(old_base.get(key) or "").casefold(), [])
                            for key in ("voice", "speaker")
                        )
                    ):
                        unknown.update(keys)
                    evidence_hash.append(
                        [
                            segment.id,
                            scope["scope"],
                            old_fp,
                            new_fp,
                            reference_verified,
                            executor,
                            actual_identity,
                        ]
                    )
                except ValueError as error:
                    unknown.update(keys)
                    if "Unknown character ID" not in str(error):
                        scope["error"] = str(error)
                        for key in keys:
                            counts[key].setdefault("error", str(error))
            for key in affected:
                counts[key]["affected_count"] += 1
                counts[key]["missing_count"] += int(missing)
                counts[key]["changed_count"] += int(key in changed)
                counts[key]["unverified_count"] += int(key in unknown)
            for key in overrides:
                counts[key]["overridden_count"] += 1
            rows.append(
                (segment, scopes, affected, changed, unknown, missing, baseline, reference_verified)
            )
        if selector_keys is None:
            selected = {key for key, count in counts.items() if count["changed_count"]}
        for _, scopes, *_ in rows:
            for scope in scopes:
                if scope.get("error") and scope["keys"] & selected:
                    blocked = blocked or scope["error"]
        targets, scoped, preview_segments = [], {}, []
        shared = 0
        missing_count = 0
        for (
            segment,
            scopes,
            affected,
            changed,
            _unknown,
            missing,
            baseline,
            reference_verified,
        ) in rows:
            chosen = affected & selected
            generate = bool(
                chosen
                and (
                    (missing and include_missing) or (not missing and (force or changed & selected))
                )
            )
            if not generate:
                continue
            targets.append(segment.id)
            missing_count += int(missing)
            active_scopes = [scope for scope in scopes if not scope["overridden"]]
            selected_scopes = [scope for scope in active_scopes if scope["keys"] & selected]
            shared += int(len(selected_scopes) < len(active_scopes))
            if missing:
                scoped_controls = deepcopy(controls)
            elif baseline:
                old_snapshot, old_entry, _, _, _ = baseline
                old_frozen = old_snapshot["generation_control_snapshot"]
                scoped_controls = deepcopy(old_entry.get("controls", old_frozen["controls"]))
                recorded_controls = deepcopy(scoped_controls)
                old_base = performance_runtime_settings(old_snapshot)
                for key, value in (scoped_controls.get("_base_voice_settings") or {}).items():
                    if value is None:
                        old_base.pop(key, None)
                    else:
                        old_base[key] = value
                scoped_controls["_base_voice_settings"] = {
                    key: deepcopy(old_base.get(key)) for key in _BASE_KEYS
                }
                # Explicitly pin each unselected identity's recorded effective
                # fallback, so a selected narrator/category cannot spill over.
                try:
                    for scope in active_scopes:
                        is_selected = bool(scope["keys"] & selected)
                        source_controls = controls if is_selected else recorded_controls
                        binding = _binding_for_scope(source_controls, scope)
                        historical_binding = binding or (
                            {"voice_id": old_base["_cast_voice_id"]}
                            if old_base.get("_cast_voice_id")
                            else None
                        )
                        if (
                            not is_selected
                            and historical_binding is not None
                            and historical_binding.get("voice_id")
                        ):
                            recorded_resolution = (old_frozen.get("resolved_bindings") or {}).get(
                                _binding_key(historical_binding)
                            ) or {}
                            recorded_references = recorded_resolution.get(
                                "_recast_reference_fingerprint"
                            )
                            preserved_reference = reference_verified
                            if recorded_references is not None:
                                current_resolution = resolve_binding(
                                    session,
                                    historical_binding,
                                    current_settings,
                                    context._service_config_cache,
                                    context._registry,
                                )
                                current_references = sorted(
                                    {
                                        stable_hash(reference)
                                        for name in (
                                            current_resolution.get("voice"),
                                            current_resolution.get("voice_id"),
                                        )
                                        if name
                                        for reference in context.voices.get(
                                            str(name).strip().casefold().removesuffix(".wav"), []
                                        )
                                    }
                                )
                                preserved_reference = recorded_references == current_references
                            if not preserved_reference:
                                raise ValueError(
                                    "An unselected speaker’s recorded reference changed. Include that speaker or restore its earlier reference before recasting this shared segment."
                                )
                        if scope["scope"] != "narrator":
                            binding = binding or _base_binding(
                                current_settings if is_selected else old_base
                            )
                        _patch_scope(scoped_controls, scope, binding)
                    if any(
                        scope["scope"] == "narrator" and scope["keys"] & selected
                        for scope in active_scopes
                    ):
                        scoped_controls["_base_voice_settings"] = {
                            key: deepcopy(current_settings.get(key)) for key in _BASE_KEYS
                        }
                except ValueError as error:
                    blocked = blocked or str(error)
            elif len(selected_scopes) == len(active_scopes):
                scoped_controls = deepcopy(controls)
            else:
                blocked = (
                    blocked
                    or "This mixed recording has no verified executor cast snapshot. Select every speaker in the block to force a recast."
                )
                continue
            scoped[segment.id] = scoped_controls
            if len(preview_segments) < 50:
                preview_segments.append(
                    {
                        "id": segment.id,
                        "ordinal": segment.ordinal,
                        "speakers": sorted({scope["scope"] for scope in active_scopes}),
                        "reason": "missing_audio"
                        if missing
                        else "requested"
                        if force
                        else "voice_changed",
                    }
                )
        if targets and not blocked:
            try:
                freeze_cast_snapshot(
                    session,
                    revision_id,
                    snapshot,
                    current_settings,
                    controls_by_segment=scoped,
                    validate_segment_ids=targets,
                )
                snapshot["generation_audio_identities"] = plan_audio_identities(
                    session, revision_id, snapshot, segment_ids=targets
                )
                errors = [
                    identity.get("performance_error")
                    for identity in snapshot["generation_audio_identities"].values()
                    if identity.get("performance_error")
                ]
                if errors:
                    blocked = errors[0]
            except ValueError as error:
                blocked = str(error)
        blocked = blocked or (
            "No recordings need the selected voice changes." if not targets else None
        )
        preview = {
            "speech_plan_revision_id": revision_id,
            "controls_revision": controls["revision"],
            "selection_hash": stable_hash(
                {
                    "snapshot": snapshot,
                    "guard": guard,
                    "settings": raw,
                    "rows": row_state,
                    "active_jobs": active_job_guard,
                    "selected": sorted(selected),
                    "force": force,
                    "include_missing": include_missing,
                    "evidence": evidence_hash,
                    "provenance": historical_snapshot_hashes,
                    "targets": targets,
                }
            ),
            "settings_summary": {
                "service": str(
                    current_settings.get("service") or current_settings.get("tts_service") or ""
                ),
                "model": str(
                    current_settings.get("model") or current_settings.get("xtts_model") or ""
                ),
                "language": str(current_settings.get("language") or ""),
            },
            "selectors": list(counts.values()),
            "selected_keys": sorted(selected),
            "generate_count": len(targets),
            "keep_count": len(segments) - len(targets),
            "missing_count": missing_count,
            "shared_segment_count": shared,
            "blocked_reason": blocked,
            "segments": preview_segments,
        }
        return {
            "preview": preview,
            "frozen_snapshot": snapshot,
            "baseline_take_ids": {key: value["id"] for key, value in recorded.items()},
            "target_segment_ids": targets,
            "snapshot_guard": guard,
            "row_state": row_state,
            "active_job_guard": active_job_guard,
            "resolved_settings_hash": resolved_hash,
            "selector_records": [{"key": key, "label": labels[key]} for key in sorted(selected)],
            "raw_settings_snapshot": raw,
        }
