"""Export subtitle diagnostics that name their cues, and optional overlap clamping."""

from pathlib import Path
from types import SimpleNamespace

from pandrator.logic.dubbing.subtitle_finalization import (
    diagnose_srt_content,
    finalize_srt_file,
)
from pandrator.logic.dubbing.subtitle_integrity import subtitle_text_integrity
from pandrator.web.export_subtitles import (
    subtitle_export_diagnostics,
    subtitle_issue_summary,
)
from pandrator.web.models import Job
from pandrator.web.work import WorkService


def _srt(cues):
    blocks = []
    for index, (start_ms, end_ms, text) in enumerate(cues, start=1):
        blocks.append(
            f"{index}\n{_timestamp(start_ms)} --> {_timestamp(end_ms)}\n{text}\n"
        )
    return "\n".join(blocks)


def _timestamp(value_ms: int) -> str:
    hours, remainder = divmod(int(value_ms), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _overlapping_cues(count: int = 15):
    """Cues that each overlap the next one by 200 to 900 ms, with room to trim."""

    cues = []
    for index in range(count):
        start = index * 3_000
        end = start + 3_400
        cues.append((start, end, f"Line {index + 1} of the track."))
    cues.append((count * 3_000 - 1_200, count * 3_000 + 5_000, "Closing line."))
    return cues


class _StubRedactor:
    def redact(self, value):
        return value

    def redact_value(self, value):
        return value


def _project_export_result(result: dict) -> dict:
    job = Job(
        id="job-export-1",
        kind="export.create",
        session_id="session-1",
        status="succeeded",
        progress=1.0,
        attempts=1,
        max_attempts=3,
        result_json=result,
    )
    service = WorkService(SimpleNamespace(database=None), _StubRedactor())
    view = service._project(job)
    return view.result_summary


def test_subtitle_issues_survive_the_work_projection():
    settings = {"subtitle_language": "en"}
    source = _srt(_overlapping_cues())
    diagnostics = subtitle_export_diagnostics(
        source,
        source,
        settings,
        None,
        language="en",
        language_origin="test",
    )
    tracks = [
        {
            "artifact_id": "artifact-1",
            "language": "en",
            "subtitle_diagnostics": diagnostics,
        }
    ]
    issues = subtitle_issue_summary(tracks)
    assert issues is not None
    result = {
        "artifact_ids": ["artifact-1"],
        "paths": ["subtitles/track.srt"],
        "subtitle_diagnostics": tracks,
        "subtitle_issues": issues,
    }

    projected = _project_export_result(result)

    assert projected["subtitle_issues"]["overlapping_cues"] == 15
    assert projected["subtitle_issues"]["issues_total"] >= 15
    first_overlap = projected["subtitle_issues"]["overlaps"][0]
    assert first_overlap[0] == 1
    assert first_overlap[1] == 2
    assert isinstance(first_overlap[2], int) and first_overlap[2] > 0
    first_issue = projected["subtitle_issues"]["issues"][0]
    assert first_issue[0] == 2
    assert isinstance(first_issue[1], int)
    assert "overlap" in first_issue[3]
    # The nested diagnostics are still truncation-shaped; the flat block is the
    # evidence a model can actually read.
    nested = projected["subtitle_diagnostics"][0]["subtitle_diagnostics"]["final"][
        "issues"
    ]
    assert "[TRUNCATED]" in str(nested)


def test_overlap_rows_carry_ordinals_and_milliseconds():
    settings = {"subtitle_language": "en"}
    source = _srt(_overlapping_cues())

    diagnostics = subtitle_export_diagnostics(
        source, source, settings, None, language="en", language_origin="test"
    )

    rows = diagnostics["final"]["overlaps"]
    assert len(rows) == 15
    assert all(len(row) == 3 for row in rows)
    assert [row[0] for row in rows] == list(range(1, 16))
    assert [row[1] for row in rows] == list(range(2, 17))


def test_clamp_overlaps_removes_every_overlap_and_keeps_the_text(tmp_path: Path):
    cues = _overlapping_cues()
    source = tmp_path / "source.srt"
    destination = tmp_path / "final.srt"
    source.write_text(_srt(cues), encoding="utf-8")
    settings = {"subtitle_language": "en", "clamp_overlaps": True}
    report: dict[str, int] = {}

    finalize_srt_file(source, destination, settings, report=report)

    finalized = destination.read_text(encoding="utf-8")
    self_check = diagnose_srt_content(finalized, settings)
    assert self_check["overlapping_cues"] == 0
    assert report["overlaps_clamped"] == 15
    assert report["overlaps_unresolved"] == 0
    assert subtitle_text_integrity(source.read_text(encoding="utf-8"), finalized)[
        "matches"
    ]
    assert (
        subtitle_text_integrity(source.read_text(encoding="utf-8"), finalized)[
            "final_sha256"
        ]
        == subtitle_text_integrity(
            source.read_text(encoding="utf-8"), source.read_text(encoding="utf-8")
        )["source_sha256"]
    )
    # Each trimmed end leaves the configured minimum gap.
    assert "00:00:02,920 --> 00:00:03,000" not in finalized
    assert "00:00:02,920" in finalized


def test_clamp_leaves_a_cue_that_cannot_keep_its_minimum_duration(tmp_path: Path):
    cues = [
        (0, 900, "First line here."),
        (300, 4_000, "Second line here."),
    ]
    source = tmp_path / "source.srt"
    destination = tmp_path / "final.srt"
    source.write_text(_srt(cues), encoding="utf-8")
    settings = {
        "subtitle_language": "en",
        "clamp_overlaps": True,
        "subtitle_min_duration_ms": 833,
    }
    report: dict[str, int] = {}

    finalize_srt_file(source, destination, settings, report=report)

    assert report["overlaps_clamped"] == 0
    assert report["overlaps_unresolved"] == 1
    assert diagnose_srt_content(
        destination.read_text(encoding="utf-8"), settings
    )["overlapping_cues"] == 1


def test_clamp_is_off_by_default(tmp_path: Path):
    source = tmp_path / "source.srt"
    destination = tmp_path / "final.srt"
    source.write_text(_srt(_overlapping_cues()), encoding="utf-8")
    settings = {"subtitle_language": "en"}
    report: dict[str, int] = {}

    finalize_srt_file(source, destination, settings, report=report)

    assert report == {"overlaps_clamped": 0, "overlaps_unresolved": 0}
    assert diagnose_srt_content(
        destination.read_text(encoding="utf-8"), settings
    )["overlapping_cues"] == 15
