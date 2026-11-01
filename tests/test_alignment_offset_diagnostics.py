import json
import threading
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from pandrator.logic.dubbing.caption_alignment import (
    align_caption_cues,
    build_source_timing_offsets,
)
from pandrator.logic.media_edit import MediaCue, MediaWord
from pandrator.web.artifacts import ArtifactService
from pandrator.web.models import Artifact


def _cue(cue_id, start_ms, end_ms, text=None):
    return MediaCue(
        cue_id,
        start_ms,
        end_ms,
        text or f"transcript-{cue_id}",
    )


def _accepted(cue, start_ms, end_ms, *, timing_source="ctc_alignment"):
    word = MediaWord(cue.text, start_ms, end_ms, confidence=0.75)
    return replace(
        cue,
        start_ms=start_ms,
        end_ms=end_ms,
        words=(word,),
        timing_confidence=0.75,
        timing_source=timing_source,
    )


def _normalized_wav(path, duration_seconds=3):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 16000 * duration_seconds)
    return path


class _ArtifactRegistry:
    def __init__(self, caption_path):
        self.paths = {"caption": Path(caption_path)}

    def resolve(self, artifact_id):
        return SimpleNamespace(metadata_json={}), self.paths[artifact_id]

    def register(self, path, **kwargs):
        artifact_id = f"artifact-{len(self.paths)}"
        target = Path(path)
        self.paths[artifact_id] = target
        return SimpleNamespace(
            id=artifact_id,
            relative_path=target.name,
            metadata_json=kwargs.get("metadata") or {},
        )


def test_source_timing_offsets_report_exact_positive_and_negative_shifts():
    originals = (
        _cue("exact", 1000, 2000),
        _cue("positive", 3000, 4000),
        _cue("negative", 5000, 6000),
    )
    final = (
        _accepted(originals[0], 1000, 2000),
        _accepted(originals[1], 3100, 4100),
        _accepted(originals[2], 4900, 5900),
    )

    offsets = build_source_timing_offsets(originals, final)

    assert offsets["version"] == 1
    assert offsets["units"] == "milliseconds"
    assert offsets["semantics"] == (
        "aligned boundary minus original caption boundary; accepted cues only"
    )
    assert offsets["start"] == {
        "min_delta_ms": -100,
        "max_delta_ms": 100,
        "median_delta_ms": 0,
        "p95_absolute_delta_ms": 100,
        "maximum_absolute_delta_ms": 100,
    }
    assert offsets["end"] == offsets["start"]
    assert offsets["rejected_cue_count"] == 0
    assert offsets["outlier_cue_count"] == 0
    assert offsets["outliers"] == []


def test_source_timing_outlier_threshold_is_strict_and_checks_each_boundary():
    originals = (
        _cue("start-at-threshold", 1000, 4000),
        _cue("end-at-threshold", 5000, 8000),
        _cue("start-over-threshold", 9000, 12000),
        _cue("end-over-threshold", 13000, 16000),
    )
    final = (
        _accepted(originals[0], 3000, 4000),
        _accepted(originals[1], 5000, 6000),
        _accepted(originals[2], 11001, 12000),
        _accepted(originals[3], 13000, 13999),
    )

    offsets = build_source_timing_offsets(originals, final)

    assert offsets["outlier_threshold_ms"] == 2000
    assert offsets["outlier_cue_count"] == 2
    assert [record["cue_id"] for record in offsets["outliers"]] == [
        "start-over-threshold",
        "end-over-threshold",
    ]
    assert offsets["outliers"][0]["start_delta_ms"] == 2001
    assert offsets["outliers"][0]["end_delta_ms"] == 0
    assert offsets["outliers"][1]["start_delta_ms"] == 0
    assert offsets["outliers"][1]["end_delta_ms"] == -2001


def test_source_timing_offsets_exclude_retained_rejected_cues():
    originals = (
        _cue("first", 1000, 2000),
        _cue("rejected", 3000, 4000),
        _cue("last", 5000, 6000),
    )
    final = (
        _accepted(originals[0], 1100, 2100),
        originals[1],
        _accepted(originals[2], 4900, 5900),
    )

    offsets = build_source_timing_offsets(originals, final)

    assert offsets["rejected_cue_count"] == 1
    assert offsets["start"]["min_delta_ms"] == -100
    assert offsets["start"]["max_delta_ms"] == 100
    assert offsets["start"]["median_delta_ms"] == 0
    assert offsets["outlier_cue_count"] == 0


def test_source_timing_offsets_use_final_fallback_cues_with_words():
    originals = (
        _cue("ctc", 1000, 2000),
        _cue("fallback", 3000, 5000),
        _cue("still-rejected", 6000, 7000),
    )
    final = (
        _accepted(originals[0], 1100, 1900),
        _accepted(
            originals[1],
            5501,
            7501,
            timing_source="asr_word_projection",
        ),
        originals[2],
    )

    offsets = build_source_timing_offsets(originals, final)

    assert offsets["rejected_cue_count"] == 1
    assert offsets["outlier_cue_count"] == 1
    assert offsets["outliers"][0] == {
        "cue_id": "fallback",
        "source_start_ms": 3000,
        "source_end_ms": 5000,
        "aligned_start_ms": 5501,
        "aligned_end_ms": 7501,
        "start_delta_ms": 2501,
        "end_delta_ms": 2501,
    }


def test_source_timing_offsets_use_odd_even_medians_and_nearest_rank_p95():
    odd_originals = tuple(
        _cue(f"odd-{index}", 10000 + index * 5000, 12000 + index * 5000)
        for index in range(3)
    )
    odd_deltas = (-30, 10, 20)
    odd_final = tuple(
        _accepted(cue, cue.start_ms + delta, cue.end_ms + delta)
        for cue, delta in zip(odd_originals, odd_deltas, strict=True)
    )
    even_originals = tuple(
        _cue(f"even-{index}", 30000 + index * 5000, 32000 + index * 5000)
        for index in range(4)
    )
    even_deltas = (-30, 10, 20, 40)
    even_final = tuple(
        _accepted(cue, cue.start_ms + delta, cue.end_ms + delta)
        for cue, delta in zip(even_originals, even_deltas, strict=True)
    )

    odd = build_source_timing_offsets(odd_originals, odd_final)
    even = build_source_timing_offsets(even_originals, even_final)

    assert odd["start"]["median_delta_ms"] == 10
    assert odd["start"]["p95_absolute_delta_ms"] == 30
    assert odd["end"]["p95_absolute_delta_ms"] == 30
    assert even["start"]["median_delta_ms"] == 15
    assert even["start"]["p95_absolute_delta_ms"] == 40
    assert even["end"]["maximum_absolute_delta_ms"] == 40


def test_source_timing_offsets_empty_and_all_rejected_have_null_statistics():
    empty = build_source_timing_offsets((), ())
    originals = (_cue("a", 1000, 2000), _cue("b", 3000, 4000))
    all_rejected = build_source_timing_offsets(originals, originals)

    for offsets in (empty, all_rejected):
        assert offsets["start"] == {
            "min_delta_ms": None,
            "max_delta_ms": None,
            "median_delta_ms": None,
            "p95_absolute_delta_ms": None,
            "maximum_absolute_delta_ms": None,
        }
        assert offsets["end"] == offsets["start"]
        assert offsets["outlier_cue_count"] == 0
        assert offsets["outliers"] == []
    assert empty["rejected_cue_count"] == 0
    assert all_rejected["rejected_cue_count"] == 2


def test_source_timing_outlier_list_is_bounded_sorted_and_contains_no_text():
    originals = tuple(
        _cue(f"cue-{index:02d}", 10000 + index * 10000, 11000 + index * 10000)
        for index in range(25)
    )
    shifts = tuple(2101 + index % 5 for index in range(25))
    final = tuple(
        _accepted(cue, cue.start_ms + shift, cue.end_ms + shift)
        for cue, shift in zip(originals, shifts, strict=True)
    )

    offsets = build_source_timing_offsets(originals, final)
    expected_indices = sorted(range(25), key=lambda index: (-shifts[index], index))[:20]

    assert offsets["outlier_cue_count"] == 25
    assert offsets["outliers_truncated"] is True
    assert len(offsets["outliers"]) == 20
    assert [record["cue_id"] for record in offsets["outliers"]] == [
        f"cue-{index:02d}" for index in expected_indices
    ]
    assert "text" not in offsets["outliers"][0]
    assert "transcript-" not in json.dumps(offsets)


def test_mocked_alignment_exposes_offsets_without_changing_timing_metrics(tmp_path):
    normalized = _normalized_wav(tmp_path / "normalized.wav")
    original = _cue("cue", 1000, 2000, "Caption")

    result = align_caption_cues(
        normalized,
        (original,),
        ctc_runner=lambda *_args: [
            {"word": "Caption", "start": 1.1, "end": 1.4}
        ],
    )

    assert len(result.cues) == 1
    assert (result.cues[0].start_ms, result.cues[0].end_ms) == (1100, 1400)
    assert [(word.start_ms, word.end_ms) for word in result.cues[0].words] == [
        (1100, 1400)
    ]
    assert result.diagnostics.alignment_coverage == 1.0
    assert result.diagnostics.alignment_confidence == 0.75
    assert result.metrics["source_timing_offsets"]["start"]["median_delta_ms"] == 100
    assert result.metrics["source_timing_offsets"]["end"]["median_delta_ms"] == -600
    assert result.metrics["source_timing_offsets"]["rejected_cue_count"] == 0


def test_mocked_workflow_aggregates_offsets_after_asr_fallback(tmp_path):
    from pandrator.web.workflow_caption_alignment import (
        CaptionAlignmentContext,
        transcribe_media_edit_with_ctc,
    )

    operation_dir = tmp_path / "operation"
    caption_path = tmp_path / "captions.srt"
    caption_path.write_text(
        "1\n00:00:01,000 --> 00:00:08,000\nHello\n",
        encoding="utf-8",
    )
    artifacts = _ArtifactRegistry(caption_path)
    context = CaptionAlignmentContext(
        artifacts=cast(ArtifactService, artifacts),
        _operation_dir=lambda *_args: operation_dir,
        _store_srt_document=lambda *_args, **_kwargs: ("document", "revision"),
        _store_timed_words=lambda *_args, **_kwargs: 1,
        _media_edit_token_count=lambda *_args: 1,
    )

    def extract_audio(_source, target_dir, _source_name, **_kwargs):
        target_dir.mkdir(parents=True, exist_ok=True)
        return str(_normalized_wav(target_dir / "normalized.wav", duration_seconds=12))

    def fallback_asr(target_dir, *_args, **_kwargs):
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        srt_path = target_dir / "fallback.srt"
        srt_path.write_text(
            "1\n00:00:04,100 --> 00:00:04,400\nHello\n",
            encoding="utf-8",
        )
        words_path = target_dir / "fallback.json"
        words_path.write_text(
            json.dumps(
                {
                    "schema": "pandrator.transcript.v1",
                    "segments": [
                        {
                            "id": "fallback",
                            "start_ms": 4100,
                            "end_ms": 4400,
                            "text": "Hello",
                            "words": [
                                {"text": "Hello", "start_ms": 4100, "end_ms": 4400}
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            srt_path=str(srt_path),
            word_timestamps_path=str(words_path),
            engine="mock-asr",
            compute_backend="cpu",
        )

    with (
        patch(
            "pandrator.logic.dubbing.transcription.extract_audio",
            side_effect=extract_audio,
        ),
        patch(
            "pandrator.logic.dubbing.transcription.apply_vocal_isolation",
            side_effect=lambda audio, *_args, **_kwargs: (audio, None),
        ),
        patch(
            "pandrator.logic.dubbing.crispasr.run_ctc_alignment",
            return_value=[{"word": "Wrong", "start": 4.1, "end": 4.4}],
        ),
        patch(
            "pandrator.logic.dubbing.transcription.transcribe_source_file_with_metadata",
            side_effect=fallback_asr,
        ),
    ):
        result = transcribe_media_edit_with_ctc(
            context,
            session_id="session",
            source_artifact=cast(Artifact, SimpleNamespace(id="source")),
            source_path=tmp_path / "source.mp4",
            caption_artifact=cast(Artifact, SimpleNamespace(id="caption")),
            submitted_settings={
                "caption_alignment_method": "ctc_asr_fallback",
                "caption_alignment_padding_ms": 5000,
            },
            runtime_settings={
                "caption_alignment_method": "ctc_asr_fallback",
                "caption_alignment_padding_ms": 5000,
                "crispasr_vad_enabled": False,
            },
            ffmpeg_executable="mock-ffmpeg",
            crispasr_executable="mock-crispasr",
            progress=lambda *_args, **_kwargs: None,
            cancel_event=threading.Event(),
        )

    diagnostics_path = artifacts.paths[result["alignment_diagnostics_artifact_id"]]
    diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
    offsets = diagnostics["source_timing_offsets"]
    assert result["fallback_triggered"] is True
    assert result["fallback_filled_cue_count"] == 1
    assert offsets == result["source_timing_offsets"]
    assert offsets["rejected_cue_count"] == 0
    assert offsets["outlier_cue_count"] == 1
    assert offsets["outliers"][0]["source_start_ms"] == 1000
    assert offsets["outliers"][0]["aligned_start_ms"] == 4100
    assert offsets["outliers"][0]["start_delta_ms"] == 3100
    assert offsets["outliers"][0]["end_delta_ms"] == -3600
