"""Manual subtitle timing model and persistence for native dubbing."""

from __future__ import annotations

import json
import math
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from .models import SubtitleSegment
from .srt_utils import compose_srt, parse_srt

MIN_SEGMENT_DURATION_SECONDS = 0.05
MIN_GAP_BETWEEN_SEGMENTS_SECONDS = 0.02


def build_segment_preview_command(
    audio_file: str | os.PathLike[str],
    output_file: str | os.PathLike[str],
    *,
    start_ms: int,
    end_ms: int,
    ffmpeg_executable: str = "ffmpeg",
) -> list[str]:
    """Builds a fast input-seeking FFmpeg command for a short PCM preview."""

    normalized_start_ms = max(0, int(start_ms))
    normalized_end_ms = max(normalized_start_ms + 50, int(end_ms))
    duration_ms = normalized_end_ms - normalized_start_ms
    return [
        str(ffmpeg_executable or "ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{normalized_start_ms / 1000.0:.3f}",
        "-i",
        str(audio_file),
        "-t",
        f"{duration_ms / 1000.0:.3f}",
        "-vn",
        "-acodec",
        "pcm_s16le",
        str(output_file),
    ]


def srt_to_timing_segments(srt_content: str) -> list[dict[str, Any]]:
    return [
        {
            "start": segment.start_ms / 1000.0,
            "end": segment.end_ms / 1000.0,
            "text": segment.text,
        }
        for segment in parse_srt(srt_content)
    ]


def timing_segments_to_srt(segments: list[dict[str, Any]]) -> str:
    subtitle_segments: list[SubtitleSegment] = []
    for segment in segments:
        start = float(segment.get("start") or 0.0)
        end = float(segment.get("end") or 0.0)
        if end <= start:
            end = start + MIN_SEGMENT_DURATION_SECONDS
        subtitle_segments.append(
            SubtitleSegment(
                index=len(subtitle_segments) + 1,
                start_ms=int(round(start * 1000)),
                end_ms=int(round(end * 1000)),
                text=str(segment.get("text") or "").strip(),
            )
        )
    return compose_srt(subtitle_segments)


def load_srt_timing_segments(srt_file: str | os.PathLike[str]) -> list[dict[str, Any]]:
    with Path(srt_file).open("r", encoding="utf-8-sig") as handle:
        return srt_to_timing_segments(handle.read())


class BoundaryEditorModel:
    def __init__(self, segments: list[dict[str, Any]] | None = None, corrections: list[dict[str, Any]] | None = None):
        self.segments: list[dict[str, Any]] = []
        self.corrections: list[dict[str, Any]] = []
        self.original_segments: list[dict[str, Any]] = []
        self.pre_correction_segments: list[dict[str, Any]] = []
        self.load(segments or [], corrections or [])

    def load(self, segments: list[dict[str, Any]], corrections: list[dict[str, Any]]) -> None:
        self.segments = [deepcopy(segment) for segment in segments]
        self.corrections = [deepcopy(correction) for correction in corrections]
        self.original_segments = [deepcopy(segment) for segment in self.segments]
        self.pre_correction_segments = self.reconstruct_pre_correction_segments(self.segments, self.corrections)

    def get_row_view(self, segment_index: int) -> dict[str, Any] | None:
        if segment_index < 0 or segment_index >= len(self.segments):
            return None
        segment = self.segments[segment_index]
        pre_corr = self.pre_correction_segments[segment_index] if segment_index < len(self.pre_correction_segments) else segment
        original = self.original_segments[segment_index] if segment_index < len(self.original_segments) else segment
        was_modified = (
            segment.get("end") != pre_corr.get("end")
            or segment.get("start") != pre_corr.get("start")
            or segment.get("end") != original.get("end")
            or segment.get("start") != original.get("start")
            or segment.get("text") != original.get("text")
        )
        gap_text = ""
        if segment_index < len(self.segments) - 1:
            next_segment = self.segments[segment_index + 1]
            gap_text = f"{(float(next_segment['start']) - float(segment['end'])) * 1000:.1f}ms"
        return {"segment": segment, "gap_text": gap_text, "was_modified": was_modified}

    def split_segment(self, segment_index: int, split_position: int, split_time: float) -> bool:
        if segment_index < 0 or segment_index >= len(self.segments):
            return False
        segment = self.segments[segment_index]
        words = str(segment.get("text") or "").strip().split()
        if len(words) < 2 or split_position < 0 or split_position >= len(words) - 1:
            return False
        if not isinstance(split_time, (int, float)) or not math.isfinite(split_time):
            return False

        segment_start = float(segment.get("start") or 0.0)
        segment_end = float(segment.get("end") or 0.0)
        if split_time <= segment_start + MIN_SEGMENT_DURATION_SECONDS:
            return False
        if split_time >= segment_end - MIN_SEGMENT_DURATION_SECONDS:
            return False

        segment1 = {
            "start": segment_start,
            "end": float(split_time),
            "text": " ".join(words[: split_position + 1]),
        }
        segment2 = {
            "start": float(split_time),
            "end": segment_end,
            "text": " ".join(words[split_position + 1:]),
        }
        if "words" in segment and segment["words"]:
            words_data = list(segment["words"])
            word_split_index = min(split_position + 1, len(words_data))
            segment1["words"] = words_data[:word_split_index]
            segment2["words"] = words_data[word_split_index:]

        self.segments[segment_index] = segment1
        self.segments.insert(segment_index + 1, segment2)
        original = deepcopy(self.original_segments[segment_index])
        self.original_segments[segment_index] = deepcopy(original)
        self.original_segments.insert(segment_index + 1, deepcopy(original))
        if segment_index < len(self.pre_correction_segments):
            pre_correction = deepcopy(self.pre_correction_segments[segment_index])
            self.pre_correction_segments[segment_index] = deepcopy(pre_correction)
            self.pre_correction_segments.insert(segment_index + 1, deepcopy(pre_correction))

        for correction in self.corrections:
            correction_index = correction.get("segment_index")
            if isinstance(correction_index, int) and correction_index >= segment_index:
                correction["segment_index"] = correction_index + 1
        return True

    def merge_up(self, segment_index: int) -> bool:
        if segment_index <= 0 or segment_index >= len(self.segments):
            return False
        return self._merge_pair(segment_index - 1, segment_index)

    def merge_down(self, segment_index: int) -> bool:
        if segment_index < 0 or segment_index >= len(self.segments) - 1:
            return False
        return self._merge_pair(segment_index, segment_index + 1)

    def _merge_pair(self, first_index: int, second_index: int) -> bool:
        first = self.segments[first_index]
        second = self.segments[second_index]
        merged = {
            "start": first["start"],
            "end": second["end"],
            "text": f"{first.get('text', '')} {second.get('text', '')}".strip(),
        }
        if "words" in first or "words" in second:
            merged["words"] = list(first.get("words") or []) + list(second.get("words") or [])

        self.segments[first_index] = merged
        del self.segments[second_index]
        del self.original_segments[second_index]
        if second_index < len(self.pre_correction_segments):
            del self.pre_correction_segments[second_index]

        updated_corrections = []
        for correction in self.corrections:
            correction_index = correction.get("segment_index")
            if not isinstance(correction_index, int):
                updated_corrections.append(correction)
            elif correction_index > second_index:
                updated = dict(correction)
                updated["segment_index"] = correction_index - 1
                updated_corrections.append(updated)
            elif correction_index < second_index:
                updated_corrections.append(correction)
        self.corrections = updated_corrections
        return True

    def set_segment_text(self, segment_index: int, new_text: str) -> bool:
        if segment_index < 0 or segment_index >= len(self.segments):
            return False
        self.segments[segment_index]["text"] = str(new_text)
        self.segments[segment_index].pop("words", None)
        return True

    def set_boundary_time(
        self,
        segment_index: int,
        boundary_type: str,
        new_time: float,
        allow_adjacent_shift: bool = False,
    ) -> bool:
        if segment_index < 0 or segment_index >= len(self.segments):
            return False
        if boundary_type not in {"start", "end"}:
            return False
        if not isinstance(new_time, (int, float)) or not math.isfinite(new_time):
            return False
        if boundary_type == "start":
            return self._set_start_boundary_time(segment_index, float(new_time), allow_adjacent_shift)
        return self._set_end_boundary_time(segment_index, float(new_time), allow_adjacent_shift)

    def _set_start_boundary_time(self, segment_index: int, new_time: float, allow_adjacent_shift: bool) -> bool:
        segment = self.segments[segment_index]
        upper_bound = float(segment["end"]) - MIN_SEGMENT_DURATION_SECONDS
        target_start = min(max(new_time, 0.0), upper_bound)
        if segment_index == 0:
            segment["start"] = target_start
            return True

        previous = self.segments[segment_index - 1]
        lower_bound_without_shift = float(previous["end"]) + MIN_GAP_BETWEEN_SEGMENTS_SECONDS
        if not allow_adjacent_shift or target_start >= lower_bound_without_shift:
            if lower_bound_without_shift > upper_bound:
                return False
            segment["start"] = min(max(target_start, lower_bound_without_shift), upper_bound)
            return True

        previous_end_lower_bound = float(previous["start"]) + MIN_SEGMENT_DURATION_SECONDS
        adjusted_previous_end = max(target_start - MIN_GAP_BETWEEN_SEGMENTS_SECONDS, previous_end_lower_bound)
        adjusted_start = adjusted_previous_end + MIN_GAP_BETWEEN_SEGMENTS_SECONDS
        if adjusted_start > upper_bound:
            return False
        previous["end"] = adjusted_previous_end
        segment["start"] = adjusted_start
        return True

    def _set_end_boundary_time(self, segment_index: int, new_time: float, allow_adjacent_shift: bool) -> bool:
        segment = self.segments[segment_index]
        lower_bound = float(segment["start"]) + MIN_SEGMENT_DURATION_SECONDS
        target_end = max(new_time, lower_bound)
        if segment_index == len(self.segments) - 1:
            segment["end"] = target_end
            return True

        next_segment = self.segments[segment_index + 1]
        upper_bound_without_shift = float(next_segment["start"]) - MIN_GAP_BETWEEN_SEGMENTS_SECONDS
        if not allow_adjacent_shift or target_end <= upper_bound_without_shift:
            if lower_bound > upper_bound_without_shift:
                return False
            segment["end"] = min(max(target_end, lower_bound), upper_bound_without_shift)
            return True

        next_start_upper_bound = float(next_segment["end"]) - MIN_SEGMENT_DURATION_SECONDS
        adjusted_next_start = min(target_end + MIN_GAP_BETWEEN_SEGMENTS_SECONDS, next_start_upper_bound)
        adjusted_end = adjusted_next_start - MIN_GAP_BETWEEN_SEGMENTS_SECONDS
        if adjusted_end < lower_bound:
            return False
        next_segment["start"] = adjusted_next_start
        segment["end"] = adjusted_end
        return True

    def build_manual_corrections(self) -> list[dict[str, Any]]:
        corrections: list[dict[str, Any]] = []
        for index, (segment, original) in enumerate(zip(self.segments, self.original_segments)):
            if segment.get("end") != original.get("end"):
                corrections.append(
                    {
                        "type": "manual_edit",
                        "segment_index": index,
                        "old_end": original.get("end"),
                        "new_end": segment.get("end"),
                    }
                )
            if segment.get("start") != original.get("start"):
                corrections.append(
                    {
                        "type": "manual_edit",
                        "segment_index": index,
                        "old_start": original.get("start"),
                        "new_start": segment.get("start"),
                    }
                )
            if segment.get("text") != original.get("text"):
                corrections.append(
                    {
                        "type": "text_edit",
                        "segment_index": index,
                        "old_text": original.get("text"),
                        "new_text": segment.get("text"),
                    }
                )
        return corrections

    @staticmethod
    def reconstruct_pre_correction_segments(
        segments: list[dict[str, Any]],
        corrections: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        pre_correction = [deepcopy(segment) for segment in segments]
        for correction in corrections:
            if correction.get("type") not in {"energy_boundary", "overlap"}:
                continue
            segment_index = correction.get("segment_index")
            if not isinstance(segment_index, int) or segment_index < 0 or segment_index >= len(pre_correction):
                continue
            if "old_end" in correction:
                pre_correction[segment_index]["end"] = correction["old_end"]
            if "old_start" in correction:
                pre_correction[segment_index]["start"] = correction["old_start"]
        return pre_correction


class BoundaryEditorPersistence:
    def __init__(
        self,
        save_folder: str | os.PathLike[str] | None = None,
        json_path: str | os.PathLike[str] | None = None,
        default_srt_output_path: str | os.PathLike[str] | None = None,
    ):
        self.save_folder = str(save_folder or "")
        self.json_path = str(json_path or "")
        self.default_srt_output_path = str(default_srt_output_path or "")

    def resolve_default_srt_path(self) -> str:
        if self.default_srt_output_path:
            return self.default_srt_output_path
        if self.save_folder:
            stem = Path(self.json_path).stem if self.json_path else "corrected"
            return str(Path(self.save_folder) / f"{stem}.srt")
        return ""

    def resolve_default_json_path(self) -> str:
        if self.save_folder:
            stem = Path(self.json_path).stem if self.json_path else "corrected"
            return str(Path(self.save_folder) / f"{stem}_corrected.json")
        if self.json_path:
            return str(Path(self.json_path).with_name(f"{Path(self.json_path).stem}_corrected.json"))
        return ""

    def write_srt(self, segments: list[dict[str, Any]], file_path: str | os.PathLike[str] | None = None) -> str:
        output_target = str(file_path or self.resolve_default_srt_path()).strip()
        if not output_target:
            raise ValueError("No output path provided for SRT export.")
        output_path = Path(output_target)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(timing_segments_to_srt(segments), encoding="utf-8")
        return str(output_path)

    def write_json(
        self,
        segments: list[dict[str, Any]],
        corrections: list[dict[str, Any]],
        file_path: str | os.PathLike[str] | None = None,
    ) -> str:
        output_target = str(file_path or self.resolve_default_json_path()).strip()
        if not output_target:
            raise ValueError("No output path provided for JSON export.")
        output_path = Path(output_target)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"segments": segments, "corrections": corrections}
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(output_path)


class BoundaryEditorController:
    def __init__(self, model: BoundaryEditorModel, persistence: BoundaryEditorPersistence):
        self.model = model
        self.persistence = persistence

    @property
    def segments(self) -> list[dict[str, Any]]:
        return self.model.segments

    @property
    def corrections(self) -> list[dict[str, Any]]:
        return self.model.corrections

    @property
    def original_segments(self) -> list[dict[str, Any]]:
        return self.model.original_segments

    @property
    def pre_correction_segments(self) -> list[dict[str, Any]]:
        return self.model.pre_correction_segments

    def load_data(
        self,
        segments: list[dict[str, Any]],
        corrections: list[dict[str, Any]],
        json_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.model.load(segments, corrections)
        if json_path is not None:
            self.persistence.json_path = str(json_path)

    def get_row_view(self, segment_index: int) -> dict[str, Any] | None:
        return self.model.get_row_view(segment_index)

    def split_subtitle(self, segment_index: int, split_position: int, split_time: float) -> bool:
        return self.model.split_segment(segment_index, split_position, split_time)

    def merge_up(self, segment_index: int) -> bool:
        return self.model.merge_up(segment_index)

    def merge_down(self, segment_index: int) -> bool:
        return self.model.merge_down(segment_index)

    def update_text(self, segment_index: int, new_text: str) -> bool:
        return self.model.set_segment_text(segment_index, new_text)

    def update_boundary(
        self,
        segment_index: int,
        boundary_type: str,
        new_time: float,
        allow_adjacent_shift: bool = True,
    ) -> bool:
        return self.model.set_boundary_time(
            segment_index,
            boundary_type,
            new_time,
            allow_adjacent_shift=allow_adjacent_shift,
        )

    def build_manual_corrections(self) -> list[dict[str, Any]]:
        return self.model.build_manual_corrections()

    def save_srt(self, file_path: str | os.PathLike[str] | None = None) -> str:
        return self.persistence.write_srt(self.model.segments, file_path)

    def save_json(self, file_path: str | os.PathLike[str] | None = None) -> str:
        return self.persistence.write_json(self.model.segments, self.build_manual_corrections(), file_path)
