"""Output assembly composition for optional project sound mixes."""

from __future__ import annotations

import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
    SessionRecord,
    SoundVariant,
)
from pandrator.web.sound_project_models import ProjectSoundCue, ProjectSoundPlan
from pandrator.web.sound_workflow_outputs import (
    finish_sound_assembly,
    has_output_sounds,
    render_output_sounds,
)
from pandrator.web.workflow_output_context import OutputWorkflowContext


@pytest.fixture(scope="module")
def case(tmp_path_factory):
    app = create_app(
        data_root=tmp_path_factory.mktemp("sound-workflow-output") / "data",
        testing=True,
        background_maintenance=False,
    )
    services = app.extensions["pandrator"]
    yield services
    services["database"].dispose()


def _write_wav(path: Path, *, frames: int = 800, rate: int = 8000, value: int = 800) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sample = value.to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(sample * frames)


def _session(case, workflow_kind: str, name: str) -> SessionRecord:
    with case["database"].immediate_session() as session:
        record = SessionRecord(name=name, workflow_kind=workflow_kind)
        session.add(record)
        session.flush()
        return record


def _artifact(
    case,
    session: SessionRecord,
    name: str,
    *,
    role: str,
    metadata: dict | None = None,
    global_artifact: bool = False,
) -> Artifact:
    paths = case["paths"]
    if global_artifact:
        target = paths.artifacts / name
        owner_id = None
    else:
        target = paths.sessions / session.storage_key / name
        owner_id = session.id
    _write_wav(target)
    return case["artifacts"].register(
        target,
        kind="audio",
        role=role,
        session_id=owner_id,
        metadata=metadata or {},
    )


def _variant(case, name: str = "workflow-effect.wav") -> str:
    record = _session(case, "voiceover", f"variant-owner-{name}")
    artifact = _artifact(case, record, name, role="sound.original", global_artifact=True)
    with case["database"].immediate_session() as session:
        variant = SoundVariant(
            artifact_id=artifact.id,
            state="ready",
            sample_rate=8000,
            channels=1,
            frame_count=800,
            duration_seconds=0.1,
        )
        session.add(variant)
        session.flush()
        return variant.id


def _add_source_time_cue(case, session_id: str, variant_id: str) -> str:
    with case["database"].immediate_session() as session:
        result = case["project_sounds"].add_in_session(
            session,
            session_id,
            {
                "expected_revision": 0,
                "variant_id": variant_id,
                "anchor": {"kind": "source_time", "time_seconds": 0.0},
                "playback": {"mode": "overlay"},
            },
        )
        return result["cue"]["logical_id"]


def _seed_segment(case, session_id: str) -> str:
    with case["database"].immediate_session() as session:
        plan = GenerationPlan(session_id=session_id)
        session.add(plan)
        session.flush()
        revision = GenerationPlanRevision(
            plan_id=plan.id,
            revision_number=1,
            content_hash=f"speech-{session_id}",
        )
        session.add(revision)
        session.flush()
        plan.active_revision_id = revision.id
        segment = GenerationSegment(plan_revision_id=revision.id, ordinal=0, text="Hello")
        session.add(segment)
        session.flush()
        return segment.id


def _add_insert_cue(case, session_id: str, segment_id: str, variant_id: str) -> str:
    with case["database"].immediate_session() as session:
        result = case["project_sounds"].add_in_session(
            session,
            session_id,
            {
                "expected_revision": 0,
                "variant_id": variant_id,
                "anchor": {"kind": "before_segment", "segment_id": segment_id},
                "playback": {"mode": "insert"},
            },
        )
        return result["cue"]["logical_id"]


def _context(case, callback=None) -> OutputWorkflowContext:
    def resolve_input(artifact_id: str):
        with case["database"].session() as session:
            artifact = session.get(Artifact, artifact_id)
            assert artifact is not None
            return artifact, case["paths"].managed_path(artifact.relative_path)

    def session_record(session_id: str):
        with case["database"].session() as session:
            record = session.get(SessionRecord, session_id)
            assert record is not None
            return record

    return OutputWorkflowContext(
        database=case["database"],
        paths=case["paths"],
        artifacts=case["artifacts"],
        _resolve_input=resolve_input,
        _session_dir=lambda session_id: (
            case["paths"].sessions / session_record(session_id).storage_key
        ),
        _session_record=session_record,
        _operation_dir=lambda session_id, operation: (
            case["paths"].sessions / session_record(session_id).storage_key / operation
        ),
        _render_sound_mix=callback,
    )


def test_output_sound_gate_uses_latest_included_cue_without_writes(case):
    session = _session(case, "voiceover", "Sound output gate")
    variant_id = _variant(case, "output-gate-effect.wav")
    _add_source_time_cue(case, session.id, variant_id)
    context = _context(case, callback=lambda *_args, **_kwargs: None)
    with case["database"].session() as db:
        counts_before = (
            db.scalar(select(func.count()).select_from(ProjectSoundPlan)),
            db.scalar(select(func.count()).select_from(ProjectSoundCue)),
        )
    assert has_output_sounds(context, session.id, {}) is True
    assert has_output_sounds(context, session.id, {"include_sounds": False}) is False
    context_without_callback = _context(case)
    assert has_output_sounds(context_without_callback, session.id, {}) is False

    with case["database"].immediate_session() as db:
        latest = db.scalar(
            select(ProjectSoundPlan)
            .where(ProjectSoundPlan.session_id == session.id)
            .order_by(ProjectSoundPlan.revision.desc())
        )
        cue = db.scalar(select(ProjectSoundCue).where(ProjectSoundCue.plan_id == latest.id))
        db.add(
            ProjectSoundPlan(
                session_id=session.id,
                revision=latest.revision + 1,
                speech_plan_revision_id=latest.speech_plan_revision_id,
            )
        )
        db.flush()
        newest = db.scalar(
            select(ProjectSoundPlan)
            .where(ProjectSoundPlan.session_id == session.id)
            .order_by(ProjectSoundPlan.revision.desc())
        )
        db.add(
            ProjectSoundCue(
                logical_id=cue.logical_id,
                plan_id=newest.id,
                variant_id=cue.variant_id,
                anchor_json=cue.anchor_json,
                playback_json=cue.playback_json,
                included=False,
                placement_status=cue.placement_status,
            )
        )
    assert has_output_sounds(context, session.id, {}) is False
    with case["database"].session() as db:
        assert (
            db.scalar(select(func.count()).select_from(ProjectSoundPlan)),
            db.scalar(select(func.count()).select_from(ProjectSoundCue)),
        ) == (counts_before[0] + 1, counts_before[1] + 1)


def test_render_output_sounds_mixes_a_tiny_real_wav_and_short_circuits(case):
    variant_id = _variant(case, "real-output-effect.wav")
    session = _session(case, "voiceover", "Timed sound output")
    base = _artifact(
        case,
        session,
        "speech-master.wav",
        role="assembled_audio",
        metadata={"audio_timeline": {"version": 1, "sample_rate_hz": 8000, "total_frames": 800}},
    )
    _add_source_time_cue(case, session.id, variant_id)
    mix_service = case["services"].sound_mixes
    calls = []

    def render_mix(session_id, base_artifact_id, **kwargs):
        calls.append((session_id, base_artifact_id))
        return mix_service.render_for_artifact(session_id, base_artifact_id, **kwargs)

    context = _context(case, callback=render_mix)
    settings = {"include_sounds": False}
    skipped = render_output_sounds(
        context,
        base,
        session_id=session.id,
        settings=settings,
        progress=lambda *_args: None,
        cancel_event=threading.Event(),
    )
    assert skipped.id == base.id and calls == []

    mixed = render_output_sounds(
        context,
        base,
        session_id=session.id,
        settings={"include_sounds": True},
        progress=lambda *_args: None,
        cancel_event=threading.Event(),
    )
    assert mixed.id != base.id
    assert (mixed.role, mixed.kind, mixed.state) == ("sound_mix.audio", "audio", "current")
    assert calls == [(session.id, base.id)]
    with case["database"].session() as db:
        assert db.scalar(select(func.count()).select_from(AudioTake)) == 0

    empty = _session(case, "voiceover", "No sound cues")
    empty_base = _artifact(
        case,
        empty,
        "empty-speech.wav",
        role="assembled_audio",
        metadata={"audio_timeline": {"version": 1, "sample_rate_hz": 8000, "total_frames": 800}},
    )
    assert (
        render_output_sounds(
            context,
            empty_base,
            session_id=empty.id,
            settings={},
            progress=lambda *_args: None,
            cancel_event=threading.Event(),
        ).id
        == empty_base.id
    )
    subtitles = _session(case, "subtitles", "Subtitle-only output")
    subtitle_base = _artifact(case, subtitles, "subtitle-output.wav", role="assembled_audio")
    assert (
        render_output_sounds(
            context,
            subtitle_base,
            session_id=subtitles.id,
            settings={},
            progress=lambda *_args: None,
            cancel_event=threading.Event(),
        ).id
        == subtitle_base.id
    )
    assert calls == [(session.id, base.id)]


def test_book_assembly_keeps_dry_receipt_and_reports_pending_mix(case):
    variant_id = _variant(case, "book-output-effect.wav")
    session = _session(case, "audiobook", "Book output result")
    segment_id = _seed_segment(case, session.id)
    base = _artifact(case, session, "dry-book.wav", role="assembled_audio")
    _add_insert_cue(case, session.id, segment_id, variant_id)
    context = _context(case, callback=lambda *_args, **_kwargs: SimpleNamespace(id="mixed-book"))
    dry_result = {"artifact_id": base.id, "paths": [base.relative_path], "takes": 2}
    mixed = finish_sound_assembly(
        context,
        dry_result,
        {"settings": {"include_sounds": True}},
        progress=lambda *_args: None,
        cancel_event=threading.Event(),
    )
    assert mixed["artifact_id"] == base.id
    assert mixed["sound_mix_artifact_id"] == "mixed-book"
    assert mixed["sound_mix_status"] == "ready"
    assert mixed["paths"] == dry_result["paths"]

    pending_context = _context(
        case,
        callback=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("Sound cues still need placement.")
        ),
    )
    pending = finish_sound_assembly(
        pending_context,
        dry_result,
        {"settings": {"include_sounds": True}},
        progress=lambda *_args: None,
        cancel_event=threading.Event(),
    )
    assert pending["artifact_id"] == base.id
    assert pending["sound_mix_status"] == "not_rendered"
    assert pending["sound_mix_error"] == "Sound cues still need placement."
    assert pending["paths"] == dry_result["paths"]

    subtitle_session = _session(case, "subtitles", "Subtitle-only assembly")
    subtitle_base = _artifact(case, subtitle_session, "dry-subtitle.wav", role="assembled_audio")
    callback_calls = []
    subtitle_context = _context(
        case,
        callback=lambda *_args, **_kwargs: callback_calls.append("unexpected"),
    )
    subtitle_result = {"artifact_id": subtitle_base.id, "paths": [subtitle_base.relative_path]}
    assert (
        finish_sound_assembly(
            subtitle_context,
            subtitle_result,
            {"settings": {"include_sounds": True}},
            progress=lambda *_args: None,
            cancel_event=threading.Event(),
        )
        == subtitle_result
    )
    assert callback_calls == []
    no_callback = finish_sound_assembly(
        _context(case),
        dry_result,
        {"settings": {"include_sounds": True}},
        progress=lambda *_args: None,
        cancel_event=threading.Event(),
    )
    assert no_callback == dry_result
