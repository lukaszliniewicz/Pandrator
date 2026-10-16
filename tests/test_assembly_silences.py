"""Assembly-only pause contracts, using synthetic PCM and native word timing."""

import hashlib
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydub import AudioSegment
from pydub.generators import Sine

from pandrator.logic.book_timing import AlignedBookText, BookWord
from pandrator.web.assembly_silences import frozen_silences, render_part_silences, silence_manifest
from pandrator.web.audio_assembly import (
    AudioAssemblyCancelled,
    AudioAssemblyPart,
    AudioSilenceInsertion,
    assemble_audio_plan,
    build_audio_assembly_plan,
)
from pandrator.web.generation_rendering import execute_render_parts
from pandrator.web.workflow_book_export import _assembled_timing, _FrozenTake, _native


def rendered(rate=24000):
    parts = [{"text": text, "start": index, "end": index + 1,
              "settings": {}, "silence_before_ms": index * 100}
             for index, text in enumerate("AB")]
    return execute_render_parts(parts, synthesize=lambda *_: Sine(440, sample_rate=rate).to_audio_segment(duration=100), cancelled=lambda: False)


@pytest.mark.parametrize("backend", ["streaming", "pydub"])
@pytest.mark.parametrize("rate", [24000, 44100])
def test_raw_take_is_unpadded_assembly_inserts_only_requested_boundaries(tmp_path, backend, rate):
    audio, manifest = rendered(rate)
    assert len(audio) == 200
    assert [item["start_frame"] for item in manifest] == [0, rate // 10]
    assert all(item["silence_policy"] == "assembly-v1" for item in manifest)
    source = tmp_path / "raw.wav"
    audio.export(source, format="wav").close()
    original = source.read_bytes()
    for pause, expected in [(100, 300), (300, 500)]:
        instructions = render_part_silences({"render_parts": manifest}, {"in_segment_voice_change_silence_ms": pause})
        destination = tmp_path / f"assembled-{pause}.wav"
        plan = build_audio_assembly_plan([AudioAssemblyPart(source, 200, internal_silences=instructions)],
                                         output_format="wav", sample_rate_hz=rate)
        assert plan.expected_duration_ms == expected
        result = assemble_audio_plan(plan, destination, backend=backend)
        assert result.duration_ms == expected
        assert result.part_end_frames == (round(expected * rate / 1000),)
        with wave.open(str(destination)) as reader:
            raw = reader.readframes(reader.getnframes())
        with wave.open(str(source)) as reader:
            provider = reader.readframes(reader.getnframes())
        offset = rate // 10 * 2
        assert raw[:offset] == provider[:offset]
        assert raw[offset:offset + pause * rate // 1000 * 2] == b"\0" * (pause * rate // 1000 * 2)
        assert raw[offset + pause * rate // 1000 * 2:] == provider[offset:]
    assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(original).digest()


def test_legacy_and_single_voice_takes_are_not_repadded(tmp_path):
    audio, manifest = rendered()
    assert render_part_silences({"render_parts": [{k: v for k, v in item.items() if k != "silence_policy"} for item in manifest]}, {"in_segment_voice_change_silence_ms": 300}) == ()
    assert render_part_silences({"render_parts": manifest[:1]}, {"in_segment_voice_change_silence_ms": 300}) == ()
    source = tmp_path / "legacy.wav"
    AudioSegment.silent(duration=300, frame_rate=24000).export(source, format="wav").close()
    result = assemble_audio_plan(build_audio_assembly_plan([AudioAssemblyPart(source, 300)], output_format="wav"), tmp_path / "legacy-output.wav")
    assert result.duration_ms == 300


@pytest.mark.parametrize("backend", ["streaming", "pydub"])
def test_resampling_and_paragraph_gap_apply_once(tmp_path, backend):
    audio, manifest = rendered(48000)
    source = tmp_path / "source.wav"
    audio.export(source, format="wav").close()
    instructions = render_part_silences({"render_parts": manifest})
    plan = build_audio_assembly_plan([
        AudioAssemblyPart(source, 200, silence_after_ms=50, internal_silences=instructions),
        AudioAssemblyPart(source, 200, internal_silences=instructions),
    ], output_format="wav", sample_rate_hz=24000)
    result = assemble_audio_plan(plan, tmp_path / "output.wav", backend=backend)
    assert abs(result.total_frames - 15600) <= 2  # audioop may retain one fewer frame per resampled take.
    assert result.duration_ms == 650
    with wave.open(str(tmp_path / "output.wav")) as reader:
        reader.setpos(2400)
        assert reader.readframes(2400) == b"\0" * 4800


@pytest.mark.parametrize("backend", ["streaming", "pydub"])
def test_invalid_and_cancelled_assembly_never_publishes(tmp_path, backend):
    audio, _ = rendered()
    source = tmp_path / "source.wav"
    audio.export(source, format="wav").close()
    destination = tmp_path / "output.wav"
    plan = build_audio_assembly_plan([AudioAssemblyPart(source, 200, internal_silences=(AudioSilenceInsertion(99999, 24000, 100),))], output_format="wav")
    with pytest.raises(ValueError, match="outside"):
        assemble_audio_plan(plan, destination, backend=backend)
    assert not destination.exists()
    event = threading.Event()
    event.set()
    with pytest.raises(AudioAssemblyCancelled):
        assemble_audio_plan(plan, destination, backend=backend, cancel_event=event)
    assert not destination.exists()


def test_invalid_manifest_and_instruction_coordinates_fail_closed():
    _, manifest = rendered()
    for bad in (None, -1, "2400"):
        modified = [dict(item) for item in manifest]
        modified[1]["start_frame"] = bad
        with pytest.raises(ValueError, match="coordinates"):
            render_part_silences({"render_parts": modified})
    with pytest.raises(ValueError, match="sorted"):
        AudioAssemblyPart(Path("raw.wav"), 200, internal_silences=(AudioSilenceInsertion(200, 24000, 10), AudioSilenceInsertion(100, 24000, 10)))
    with pytest.raises(ValueError):
        AudioSilenceInsertion(0, 0, 100)
    with pytest.raises(ValueError):
        frozen_silences({"internal_silences": [{}]})


def test_native_second_word_shifts_without_moving_first_word_end():
    _, manifest = rendered()
    for index, part in enumerate(manifest):
        part["speech_timing"] = AlignedBookText("AB"[index], (BookWord("AB"[index], 0, 100, 0, 1),), "native", {"duration_ms": 100}).as_dict()
    instructions = render_part_silences({"render_parts": manifest})
    take = _FrozenTake({"silence_policy": "assembly-v1", "internal_silences": silence_manifest(instructions)}, SimpleNamespace(metadata_json={"render_parts": manifest}), Path("raw.wav"), "AB", "AB", 4800, 24000)
    aligned, _ = _native(take)
    shifted = _assembled_timing(take, aligned, 24000)
    assert [(word.start_ms, word.end_ms) for word in shifted.words] == [(0, 100), (200, 300)]
    assert aligned.words[1].start_ms == 100  # cached timing remains raw.
    assert shifted.diagnostics["duration_ms"] == 300


@pytest.mark.parametrize("output_rate", [44100, 24000, 32000])
def test_native_fractional_frame_boundary_uses_cached_millisecond_domain(output_rate):
    boundary, source_rate, source_frames = 2206, 44100, 4410
    parts = []
    for index, (start, end) in enumerate(((0, boundary), (boundary, source_frames))):
        text = "AB"[index]
        duration = round((end - start) * 1000 / source_rate)
        parts.append({
            "range": [index, index + 1], "start_frame": start, "end_frame": end,
            "sample_rate_hz": source_rate, "silence_policy": "assembly-v1",
            "silence_before_ms": 300 if index else 0,
            "speech_timing": AlignedBookText(text, (BookWord(text, 0, duration, 0, 1),),
                                            "native", {"duration_ms": duration}).as_dict(),
        })
    take = _FrozenTake(
        {"silence_policy": "assembly-v1", "internal_silences": silence_manifest(
            render_part_silences({"render_parts": parts}))},
        SimpleNamespace(metadata_json={"render_parts": parts}), Path("raw.wav"),
        "AB", "AB", source_frames, source_rate,
    )
    aligned, _ = _native(take)
    assert [(word.start_ms, word.end_ms) for word in aligned.words] == [(0, 50), (50, 100)]
    shifted = _assembled_timing(take, aligned, output_rate)
    assert [(word.start_ms, word.end_ms) for word in shifted.words] == [(0, 50), (350, 400)]
    assert aligned.words[1].start_ms == 50


@pytest.mark.parametrize("marked", [True, False])
@pytest.mark.parametrize("old_tuple", [True, False])
def test_automatic_output_reads_retained_metadata_and_skips_voiceover(tmp_path, marked, old_tuple):
    from pandrator.web.artifacts import ArtifactService
    from pandrator.web.database import Database
    from pandrator.web.jobs import JobQueue
    from pandrator.web.models import AudioTake
    from pandrator.web.sessions import SessionService
    from pandrator.web.workflow_generation_automatic_output import (
        AutomaticAudioOutput,
        assemble_automatic_output,
    )
    from pandrator.web.workspace import GenerationService, WorkspaceSettingsService
    from tests.web_test_support import prepare_web_test_data_root

    paths = prepare_web_test_data_root(tmp_path / "data")
    database = Database(paths.database)
    try:
        record = SessionService(database).create("Automatic silence", workflow_kind="audiobook")
        generation = GenerationService(database, JobQueue(database), WorkspaceSettingsService(database))
        generation.create_plan(record.id, source_revision_id=None, segments=[{"text": "AB"}])
        from sqlalchemy import select

        from pandrator.web.models import GenerationSegment
        with database.session() as session:
            segment_id = session.scalar(select(GenerationSegment.id))
        source = paths.sessions / record.storage_key / "raw.wav"
        source.parent.mkdir(parents=True, exist_ok=True)
        audio, manifest = rendered()
        audio.export(source, format="wav").close()
        artifacts = ArtifactService(database, paths)
        metadata = {"render_parts": manifest} if marked else {}
        artifact = artifacts.register(source, kind="audio", role="generation_take", session_id=record.id, metadata=metadata)
        with database.session() as session:
            session.add(AudioTake(generation_segment_id=segment_id, artifact_id=artifact.id,
                                  status="completed", kind="tts", duration_ms=200))
        for allowed in (True, False):
            inputs = [(source, 200, 999)] if old_tuple else [(source, 200, 999, metadata)]
            result = assemble_automatic_output(artifacts, AutomaticAudioOutput(
                session_id=record.id, source_artifact_id=artifact.id, role="assembled_audio",
                destination=source.parent / f"output-{allowed}.wav", settings={"in_segment_voice_change_silence_ms": 300},
                assembly_inputs=inputs, take_artifact_ids=[artifact.id], segment_count=1,
                plan_revision_id="fixture", allow_internal_silences=allowed,
            ), lambda *_: None, threading.Event())
            output, _ = artifacts.resolve(result["artifact_id"])
            expected = 500 if marked and allowed else 200
            assert output.metadata_json["duration_ms"] == expected
            row = output.metadata_json["takes"][0]
            assert row["take_id"]
            assert row["end_frame"] == expected * 24
            assert row["silence_after_ms"] == 0
            assert bool(row["internal_silences"]) == (marked and allowed)
    finally:
        database.dispose()


@pytest.mark.parametrize("backend", ["streaming", "pydub"])
def test_late_cancellation_preserves_existing_destination(tmp_path, backend):
    source = tmp_path / "source.wav"
    audio, manifest = rendered()
    audio.export(source, format="wav").close()
    destination = tmp_path / "output.wav"
    destination.write_bytes(b"previous output")
    event = threading.Event()
    plan = build_audio_assembly_plan([AudioAssemblyPart(source, 200, internal_silences=render_part_silences({"render_parts": manifest}))], output_format="wav")

    def progress(*_):
        event.set()

    with pytest.raises(AudioAssemblyCancelled):
        assemble_audio_plan(plan, destination, backend=backend, cancel_event=event, progress=progress)
    assert destination.read_bytes() == b"previous output"


def test_rvc_keeps_only_duration_verified_boundaries_and_forces_alignment():
    from pandrator.web.assembly_silences import remap_render_parts_after_rvc

    _, manifest = rendered()
    manifest[0]["speech_timing"] = {"placeholder": True}
    converted = remap_render_parts_after_rvc(manifest, source_frames=4800, source_rate=24000,
                                            target_frames=9600, target_rate=48000)
    assert [part["start_frame"] for part in converted] == [0, 4800]
    assert converted[-1]["end_frame"] == 9600
    assert all(part["sample_rate_hz"] == 48000 and "speech_timing" not in part for part in converted)
    assert "speech_timing" in manifest[0]
    assert render_part_silences({"render_parts": converted}) == (AudioSilenceInsertion(4800, 48000, 100),)
    with pytest.raises(ValueError, match="changed raw take duration"):
        remap_render_parts_after_rvc(manifest, source_frames=4800, source_rate=24000,
                                    target_frames=9601, target_rate=48000)
    with pytest.raises(ValueError, match="source PCM"):
        remap_render_parts_after_rvc(manifest, source_frames=4799, source_rate=24000,
                                    target_frames=9598, target_rate=48000)
    legacy = [{key: value for key, value in part.items() if key != "silence_policy"} for part in manifest]
    converted_legacy = remap_render_parts_after_rvc(legacy, source_frames=4800, source_rate=24000,
                                                   target_frames=9601, target_rate=48000)
    assert render_part_silences({"render_parts": converted_legacy}) == ()
    assert all("speech_timing" not in part and "start_frame" not in part for part in converted_legacy)


def test_rvc_timed_workflow_discards_assembly_coordinates_but_keeps_voice_labels():
    from pandrator.web.assembly_silences import remap_render_parts_after_rvc

    _, manifest = rendered()
    manifest[0].update(voice="narrator", source="cast", speaker_labels=["Narrator"],
                       speech_timing={"placeholder": True})
    converted = remap_render_parts_after_rvc(
        manifest, source_frames=4800, source_rate=24000, target_frames=6000, target_rate=24000,
        preserve_assembly_boundaries=False,
    )
    assert converted[0]["voice"] == "narrator"
    assert converted[0]["source"] == "cast"
    assert converted[0]["speaker_labels"] == ["Narrator"]
    assert all(not ({"speech_timing", "start_frame", "end_frame", "sample_rate_hz", "silence_policy"} & part.keys())
               for part in converted)
    assert render_part_silences({"render_parts": converted}, {"in_segment_voice_change_silence_ms": 300}) == ()
    assert manifest[0]["silence_policy"] == "assembly-v1"


def test_rvc_single_book_part_maps_its_whole_converted_take_after_duration_change():
    from pandrator.web.assembly_silences import remap_render_parts_after_rvc

    _, manifest = rendered()
    single = [manifest[0]]
    single[0]["speech_timing"] = {"placeholder": True}
    converted = remap_render_parts_after_rvc(
        single, source_frames=2400, source_rate=24000, target_frames=7200, target_rate=48000,
    )
    assert converted[0]["start_frame"] == 0
    assert converted[0]["end_frame"] == 7200
    assert converted[0]["sample_rate_hz"] == 48000
    assert converted[0]["duration_ms"] == 150
    assert converted[0]["silence_policy"] == "assembly-v1"
    assert "speech_timing" not in converted[0]
    assert render_part_silences({"render_parts": converted}) == ()
    with pytest.raises(ValueError, match="source PCM"):
        remap_render_parts_after_rvc(single, source_frames=2401, source_rate=24000,
                                    target_frames=7200, target_rate=48000)


@pytest.mark.parametrize(("workflow_kind", "node_kind"), [("voiceover", "paragraph"), ("audiobook", "subtitle_cue")])
def test_run_generation_rvc_timed_workflow_guard_allows_changed_duration(workflow_kind, node_kind):
    from unittest import mock

    from sqlalchemy import select

    from pandrator.web.models import Artifact, AudioTake, GenerationRun, SessionRecord
    from tests.test_web_workflow_handlers import WebWorkflowHandlerTests

    case = WebWorkflowHandlerTests()
    case.setUp()
    try:
        revision_id, segment_ids = case.handlers._store_generation_plan(
            case.session.id, [{"text": "AB", "node_kind": node_kind}], settings={},
        )
        source = case.session_dir / "raw-rvc-source.wav"
        audio, manifest = rendered()
        manifest[0]["voice"] = "narrator"
        audio.export(source, format="wav").close()
        artifact = case.artifacts.register(source, kind="audio", role="generation_take",
                                           session_id=case.session.id,
                                           metadata={"render_parts": manifest, "synthesized_text": "AB"})
        with case.database.session() as session:
            session.get(SessionRecord, case.session.id).workflow_kind = workflow_kind
            session.add(AudioTake(generation_segment_id=segment_ids[0], artifact_id=artifact.id,
                                  kind="tts", status="completed", is_active=True, duration_ms=200))
            run = GenerationRun(session_id=case.session.id, plan_revision_id=revision_id,
                                sequence_number=1, operation="rvc", status="queued",
                                settings_snapshot_json={"tts": {"service": "Silero", "voice": "en_0"},
                                                        "rvc": {"model": "fixture-rvc"}})
            session.add(run)
            session.flush()
            run_id = run.id
        with mock.patch("pandrator.logic.rvc_handler.process_with_rvc",
                        side_effect=lambda source_audio, _settings: source_audio + AudioSegment.silent(duration=50, frame_rate=source_audio.frame_rate)):
            result = case.handlers.run_generation({"generation_run_id": run_id, "operation": "rvc"},
                                                  case.progress, threading.Event())
        assert result["generated"] == 1
        with case.database.session() as session:
            take = session.scalar(select(AudioTake).where(AudioTake.generation_run_id == run_id))
            assert take.duration_ms == 250
            converted = session.get(Artifact, take.artifact_id).metadata_json["render_parts"]
        assert converted[0]["voice"] == "narrator"
        assert all("silence_policy" not in part and "start_frame" not in part for part in converted)
        assert render_part_silences({"render_parts": converted}) == ()
    finally:
        case.tearDown()


@pytest.mark.parametrize("backend", ["streaming", "pydub"])
def test_fractional_source_boundary_is_rescaled_once_without_losing_pcm(tmp_path, backend):
    source = tmp_path / "source.wav"
    Sine(440, sample_rate=44100).to_audio_segment(duration=200).export(source, format="wav").close()
    paths = []
    insertion = AudioSilenceInsertion(2206, 44100, 13)
    for name, instructions in [("baseline", ()), ("inserted", (insertion,))]:
        destination = tmp_path / f"{name}.wav"
        plan = build_audio_assembly_plan([AudioAssemblyPart(source, 200, internal_silences=instructions)],
                                         output_format="wav", sample_rate_hz=32000)
        assemble_audio_plan(plan, destination, backend=backend)
        with wave.open(str(destination)) as reader:
            paths.append(reader.readframes(reader.getnframes()))
    baseline, inserted = paths
    offset = round(2206 * 32000 / 44100) * 2
    added = round(13 * 32000 / 1000) * 2
    assert inserted[:offset] == baseline[:offset]
    assert inserted[offset:offset + added] == b"\0" * added
    assert inserted[offset + added:] == baseline[offset:]
