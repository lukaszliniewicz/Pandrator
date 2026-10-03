"""Native PDF lines, geometry, paragraph grouping, and reading order."""

from __future__ import annotations

import re
import statistics
import unicodedata
from collections.abc import Iterable
from itertools import pairwise
from typing import Any

_CHAPTER_RE = re.compile(
    r"^(?:chapter|book|part|volume|section|chapitre|kapitel|capitulo|rozdzia[lł]|cz[eę][sś][cć]|"
    r"tom|ksi[eę]ga|prologue|epilogue|prolog|epilog|wst[eę]p|pos[lł]owie)\s+"
    r"(?:[ivxlcdm]+|\d{1,4})\b",
    re.IGNORECASE,
)


_NUMBERED_HEADING_RE = re.compile(
    # A bare ``I `` is ordinarily a sentence pronoun, and OCR often inserts
    # spaces into page numbers (``1 30``). Both were previously treated as
    # numbered headings. Permit an undelimited form only for Arabic numbers
    # followed by a word, while Roman numerals require an explicit delimiter.
    r"^(?:\d{1,4}(?:[.)]\s+|\s*[-–—]\s+|\s+(?=[^\W\d_])\S+)|"
    r"[ivxlcdm]{1,8}(?:[.)]\s+|\s*[-–—]\s+))",
    re.IGNORECASE,
)


_MAJOR_SECTION_RE = re.compile(
    r"^(?:acknowledg(?:e)?ments?|preface|foreword|introduction|prologue|epilogue|afterword|"
    r"conclusion|appendi(?:x|ces)|postscript|chapter|book|part|volume|section|act|"
    r"pr[eé]face|avant-propos|postface|chapitre|kapitel|vorwort|einleitung|nachwort|"
    r"prolog|epilog|cap[ií]tulo|introducci[oó]n|pr[oó]logo|ep[ií]logo|"
    r"wst[eę]p|przedmowa|pos[lł]owie|podzi[eę]kowania|rozdzia[lł]|cz[eę][sś][cć]|tom|ksi[eę]ga|"
    r"предисловие|введение|послесловие|глава|часть)\b",
    re.IGNORECASE,
)


def _extract_native_lines(page: Any) -> list[dict[str, Any]]:
    import pymupdf as fitz

    lines: list[dict[str, Any]] = []
    rotation_matrix = page.rotation_matrix
    for block_index, block in enumerate(page.get_text("dict").get("blocks", [])):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = _normalize_space(
                "".join(str(span.get("text", "")) for span in spans)
            )
            if not text:
                continue
            char_count = max(1, sum(len(str(span.get("text", ""))) for span in spans))
            bbox = fitz.Rect(line["bbox"])
            if int(page.rotation or 0) % 360:
                bbox *= rotation_matrix
            lines.append(
                {
                    "text": text,
                    "bbox": _round_bbox((bbox.x0, bbox.y0, bbox.x1, bbox.y1)),
                    "block_index": block_index,
                    "direction": _transform_direction(
                        line.get("dir") or (1.0, 0.0), rotation_matrix
                    ),
                    "font_size": round(
                        sum(
                            float(span.get("size", 0.0))
                            * len(str(span.get("text", "")))
                            for span in spans
                        )
                        / char_count,
                        3,
                    ),
                    "font": ",".join(
                        sorted(
                            {
                                str(span.get("font", ""))
                                for span in spans
                                if span.get("font")
                            }
                        )
                    ),
                    "confidence": None,
                    "source_lines": 1,
                }
            )
    return lines


def _extract_horizontal_rules(page: Any) -> list[list[float]]:
    """Return thin horizontal vector rules that may delimit footnotes."""
    rules: list[list[float]] = []
    rotation_matrix = page.rotation_matrix
    try:
        drawings = page.get_drawings()
    except Exception:  # noqa: BLE001 - malformed PDF drawing streams vary
        return rules
    for drawing in drawings:
        for item in drawing.get("items") or []:
            if not item or item[0] != "l" or len(item) < 3:
                continue
            start, end = item[1], item[2]
            if int(page.rotation or 0) % 360:
                start = start * rotation_matrix
                end = end * rotation_matrix
            if abs(float(start.y) - float(end.y)) > 1.0:
                continue
            x0, x1 = sorted((float(start.x), float(end.x)))
            if x1 - x0 < 8.0:
                continue
            rules.append(
                [
                    round(x0, 3),
                    round((float(start.y) + float(end.y)) / 2.0, 3),
                    round(x1, 3),
                ]
            )
    return rules


def _native_diagnostics(page: Any, lines: list[dict[str, Any]]) -> dict[str, Any]:
    text = "\n".join(line["text"] for line in lines)
    compact = "".join(text.split())
    alpha_numeric = sum(char.isalnum() for char in compact)
    bad_chars = sum(char == "\ufffd" or unicodedata.category(char) == "Cc" for char in compact)
    one_token_lines = sum(len(line["text"].split()) <= 1 for line in lines)
    auto_ocr = len(compact) < 40 or alpha_numeric < 20
    reasons: list[str] = []
    if auto_ocr:
        reasons.append("too_little_native_text")
    if compact and bad_chars / len(compact) > 0.02:
        auto_ocr = True
        reasons.append("invalid_character_ratio")
    if len(lines) >= 20 and one_token_lines / len(lines) > 0.65:
        auto_ocr = True
        reasons.append("fragmented_native_text")
    image_area = sum(_bbox_area(info.get("bbox", (0, 0, 0, 0))) for info in page.get_image_info())
    page_area = max(1.0, float(page.rect.width * page.rect.height))
    return {
        "chars": len(text),
        "line_count": len(lines),
        "alpha_numeric_ratio": round(alpha_numeric / max(1, len(compact)), 4),
        "bad_character_ratio": round(bad_chars / max(1, len(compact)), 4),
        "image_coverage": round(min(1.0, image_area / page_area), 4),
        "auto_ocr": auto_ocr,
        "decision_reasons": reasons or ["native_text_is_plausible"],
    }


def _lines_to_blocks(
    lines: list[dict[str, Any]], page_rect: Any, source_method: str
) -> list[dict[str, Any]]:
    if not lines:
        return []
    lines = _coalesce_native_line_fragments(lines)
    ordered, reading_order = _geometry_order(lines, float(page_rect.width))
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for line in ordered:
        if current and not _same_paragraph(current[-1], line):
            groups.append(current)
            current = []
        current.append(line)
    if current:
        groups.append(current)
    blocks: list[dict[str, Any]] = []
    for group in groups:
        text = _join_lines(group)
        if not text:
            continue
        confidences = [
            float(line["confidence"])
            for line in group
            if line.get("confidence") is not None
        ]
        blocks.append(
            {
                "text": text,
                "bbox": _combined_bbox(group),
                "font_size": round(
                    statistics.fmean(
                        float(line.get("font_size") or 0.0) for line in group
                    ),
                    3,
                ),
                "fonts": sorted(
                    {str(line.get("font") or "") for line in group if line.get("font")}
                ),
                "confidence": round(statistics.fmean(confidences), 4)
                if confidences
                else None,
                "source_lines": sum(
                    max(1, int(line.get("source_lines") or 1)) for line in group
                ),
                "reading_order": reading_order,
                "source_method": source_method,
                "direction": list(group[0].get("direction") or [1.0, 0.0]),
                "native_block_indexes": sorted(
                    {
                        int(line["block_index"])
                        for line in group
                        if line.get("block_index") is not None
                    }
                ),
                "first_line_bbox": list(group[0]["bbox"]),
                "last_line_bbox": list(group[-1]["bbox"]),
            }
        )
    return blocks


def _coalesce_native_line_fragments(
    lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rebuild visual rows split into word-sized records by a PDF text layer.

    Some embedded text layers expose every word as a separate ``line`` within
    one native text block. Treating those fragments as independent geometry can
    turn an ordinary justified paragraph into two fictitious columns. Native
    block identity and orientation let us reassemble those rows without joining
    separate real columns. OCR lines deliberately lack block identity and pass
    through unchanged.
    """
    native = [line for line in lines if line.get("block_index") is not None]
    if not native:
        return lines

    rebuilt: list[dict[str, Any]] = []
    passthrough = [line for line in lines if line.get("block_index") is None]
    keys = {
        (int(line["block_index"]), tuple(line.get("direction") or [1.0, 0.0]))
        for line in native
    }
    for block_index, direction in sorted(keys):
        block_lines = [
            line
            for line in native
            if int(line["block_index"]) == block_index
            and tuple(line.get("direction") or [1.0, 0.0]) == direction
        ]
        rows: list[list[dict[str, Any]]] = []
        for line in sorted(block_lines, key=_visual_line_sort_key):
            center = _line_vertical_center(line)
            height = _line_height(line)
            matching_row: list[dict[str, Any]] | None = None
            for row in reversed(rows[-3:]):
                row_center = statistics.fmean(
                    _line_vertical_center(item) for item in row
                )
                row_height = statistics.fmean(_line_height(item) for item in row)
                if abs(center - row_center) <= max(height, row_height) * 0.58:
                    matching_row = row
                    break
            if matching_row is None:
                rows.append([line])
            else:
                matching_row.append(line)

        for row in rows:
            run: list[dict[str, Any]] = []
            for line in sorted(row, key=lambda item: item["bbox"][0]):
                if run:
                    gap = float(line["bbox"][0]) - float(run[-1]["bbox"][2])
                    if gap > max(_line_height(line), _line_height(run[-1])) * 3.0:
                        rebuilt.append(_merge_line_fragments(run))
                        run = []
                run.append(line)
            if run:
                rebuilt.append(_merge_line_fragments(run))
    return rebuilt + passthrough


def _merge_line_fragments(lines: list[dict[str, Any]]) -> dict[str, Any]:
    if len(lines) == 1:
        return dict(lines[0])
    char_counts = [max(1, len(str(line.get("text") or ""))) for line in lines]
    total_chars = sum(char_counts)
    confidences = [
        float(line["confidence"])
        for line in lines
        if line.get("confidence") is not None
    ]
    merged = dict(lines[0])
    merged.update(
        {
            "text": " ".join(
                _normalize_space(line.get("text") or "") for line in lines
            ).strip(),
            "bbox": _combined_bbox(lines),
            "font_size": round(
                sum(
                    float(line.get("font_size") or 0.0) * count
                    for line, count in zip(lines, char_counts, strict=False)
                )
                / max(1, total_chars),
                3,
            ),
            "font": ",".join(
                sorted(
                    {
                        font
                        for line in lines
                        for font in str(line.get("font") or "").split(",")
                        if font
                    }
                )
            ),
            "confidence": round(statistics.fmean(confidences), 4)
            if confidences
            else None,
            "source_lines": sum(
                max(1, int(line.get("source_lines") or 1)) for line in lines
            ),
        }
    )
    return merged


def _geometry_order(
    lines: list[dict[str, Any]], page_width: float
) -> tuple[list[dict[str, Any]], str]:
    horizontal = [line for line in lines if _is_horizontal_line(line)]
    rotated = [line for line in lines if line not in horizontal]
    if len(horizontal) < 8:
        return _sort_lines_by_visual_rows(horizontal) + _sort_lines_by_visual_rows(
            rotated
        ), "top_to_bottom"
    separators = _column_separators(horizontal, page_width)
    if separators:
        lanes, spanning = _partition_column_lines(horizontal, separators, page_width)
        if (
            all(len(lane) >= 4 for lane in lanes)
            and all(
                _aligned_column_row_count(left, right) >= 3
                for left, right in pairwise(lanes)
            )
            and len(spanning) <= len(horizontal) * 0.45
        ):
            ordered = _order_column_regions(lanes, spanning, page_width)
            ordered += _sort_lines_by_visual_rows(rotated)
            label = "two_columns" if len(lanes) == 2 else "multi_columns"
            return ordered, label
    return _sort_lines_by_visual_rows(horizontal) + _sort_lines_by_visual_rows(
        rotated
    ), "top_to_bottom"


def _column_separators(
    lines: list[dict[str, Any]], page_width: float
) -> list[float]:
    """Find persistent vertical gutters without assuming exactly two columns."""
    tolerance = max(1.0, page_width * 0.006)
    crossing_limit = max(2, int(len(lines) * 0.10))
    samples: list[tuple[float, int]] = []
    for step in range(8, 93):
        position = page_width * step / 100.0
        crossing = sum(
            float(line["bbox"][0]) + tolerance
            < position
            < float(line["bbox"][2]) - tolerance
            for line in lines
        )
        left = sum(float(line["bbox"][2]) <= position + tolerance for line in lines)
        right = sum(float(line["bbox"][0]) >= position - tolerance for line in lines)
        if crossing <= crossing_limit and left >= 4 and right >= 4:
            samples.append((position, crossing))
    if not samples:
        return []

    bands: list[list[tuple[float, int]]] = []
    for sample in samples:
        if bands and sample[0] - bands[-1][-1][0] <= page_width * 0.011:
            bands[-1].append(sample)
        else:
            bands.append([sample])

    candidates: list[tuple[float, int, float]] = []
    for band in bands:
        minimum = min(value for _, value in band)
        best = [position for position, value in band if value == minimum]
        position = statistics.median(best)
        candidates.append((float(position), minimum, band[-1][0] - band[0][0]))

    selected: list[float] = []
    for position, _, _ in sorted(candidates, key=lambda item: item[0]):
        if selected and position - selected[-1] < page_width * 0.09:
            continue
        selected.append(position)
    return selected[:5]


def _partition_column_lines(
    lines: list[dict[str, Any]], separators: list[float], page_width: float
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    tolerance = max(1.0, page_width * 0.012)
    lanes: list[list[dict[str, Any]]] = [[] for _ in range(len(separators) + 1)]
    spanning: list[dict[str, Any]] = []
    for line in lines:
        x0 = float(line["bbox"][0])
        x1 = float(line["bbox"][2])
        crossed = [
            separator
            for separator in separators
            if x0 + tolerance < separator < x1 - tolerance
        ]
        if crossed:
            spanning.append(line)
            continue
        center = (x0 + x1) / 2.0
        lane_index = sum(center > separator for separator in separators)
        lanes[lane_index].append(line)
    return lanes, spanning


def _order_column_regions(
    lanes: list[list[dict[str, Any]]],
    spanning: list[dict[str, Any]],
    page_width: float,
) -> list[dict[str, Any]]:
    """Read column bands around full-width titles and section separators."""
    anchors = [
        line
        for line in spanning
        if (
            float(line["bbox"][2]) - float(line["bbox"][0]) >= page_width * 0.42
            or _is_heading_like_line(line)
        )
    ]
    non_anchors = [line for line in spanning if line not in anchors]
    if non_anchors:
        lane_centers = [
            statistics.median(
                (float(line["bbox"][0]) + float(line["bbox"][2])) / 2.0
                for line in lane
            )
            for lane in lanes
        ]
        for line in non_anchors:
            center = (float(line["bbox"][0]) + float(line["bbox"][2])) / 2.0
            nearest = min(
                range(len(lanes)), key=lambda index: abs(center - lane_centers[index])
            )
            lanes[nearest].append(line)

    remaining = [list(_sort_lines_by_visual_rows(lane)) for lane in lanes]
    ordered: list[dict[str, Any]] = []
    for anchor in _sort_lines_by_visual_rows(anchors):
        anchor_top = float(anchor["bbox"][1])
        for lane in remaining:
            before = [line for line in lane if float(line["bbox"][1]) < anchor_top]
            ordered.extend(before)
            del lane[: len(before)]
        ordered.append(anchor)
    for lane in remaining:
        ordered.extend(lane)
    return ordered


def _aligned_column_row_count(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> int:
    matches = 0
    unused = set(range(len(right)))
    for left_line in sorted(left, key=_visual_line_sort_key):
        left_center = _line_vertical_center(left_line)
        left_height = _line_height(left_line)
        candidate = next(
            (
                index
                for index in sorted(unused)
                if abs(left_center - _line_vertical_center(right[index]))
                <= max(left_height, _line_height(right[index])) * 0.60
            ),
            None,
        )
        if candidate is not None:
            matches += 1
            unused.remove(candidate)
    return matches


def _sort_lines_by_visual_rows(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order fragments on the same visual baseline from left to right.

    Native PDF text dictionaries sometimes represent one printed heading as
    several line records with tiny baseline differences.  Sorting solely by y
    can scramble those fragments before paragraph grouping sees them.
    """
    rows: list[list[dict[str, Any]]] = []
    for line in sorted(lines, key=_visual_line_sort_key):
        center = _line_vertical_center(line)
        height = _line_height(line)
        if rows:
            row = rows[-1]
            row_center = statistics.fmean(_line_vertical_center(item) for item in row)
            row_height = statistics.fmean(_line_height(item) for item in row)
            if (
                _same_native_block(row[-1], line)
                and _same_line_direction(row[-1], line)
                and abs(center - row_center) <= max(height, row_height) * 0.35
            ):
                row.append(line)
                continue
        rows.append([line])
    return [
        line for row in rows for line in sorted(row, key=lambda item: item["bbox"][0])
    ]


def _same_paragraph(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    if not _same_line_direction(previous, current):
        return False
    if not _same_native_block(previous, current):
        return _same_large_heading_run(previous, current)
    if _same_visual_row(previous, current):
        return True
    prev_box = previous["bbox"]
    box = current["bbox"]
    height = max(5.0, prev_box[3] - prev_box[1], box[3] - box[1])
    vertical_gap = box[1] - prev_box[3]
    left_gap = abs(box[0] - prev_box[0])
    if _is_heading_like_line(previous) or _is_heading_like_line(current):
        return False
    previous_font = float(previous.get("font_size") or 0.0)
    current_font = float(current.get("font_size") or 0.0)
    if previous_font and current_font:
        font_ratio = max(previous_font, current_font) / max(
            0.1, min(previous_font, current_font)
        )
        if font_ratio >= 1.18 and vertical_gap > height * 0.45:
            return False
    if box[1] < prev_box[1] - height:
        return False
    return vertical_gap <= height * 1.15 and left_gap <= height * 2.5


def _same_visual_row(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    if not _same_native_block(previous, current) or not _same_line_direction(
        previous, current
    ):
        return False
    previous_box = previous["bbox"]
    current_box = current["bbox"]
    previous_height = max(1.0, previous_box[3] - previous_box[1])
    current_height = max(1.0, current_box[3] - current_box[1])
    previous_center = (previous_box[1] + previous_box[3]) / 2.0
    current_center = (current_box[1] + current_box[3]) / 2.0
    horizontally_adjacent = (
        current_box[0] <= previous_box[2] + max(previous_height, current_height) * 2.5
    )
    return (
        horizontally_adjacent
        and abs(previous_center - current_center)
        <= max(previous_height, current_height) * 0.35
    )


def _same_native_block(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_index = previous.get("block_index")
    current_index = current.get("block_index")
    return (
        previous_index is None
        or current_index is None
        or previous_index == current_index
    )


def _same_large_heading_run(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_text = _normalize_space(previous.get("text") or "")
    current_text = _normalize_space(current.get("text") or "")
    if (
        not previous_text
        or not current_text
        or len(previous_text) > 160
        or len(current_text) > 160
        or len(previous_text.split()) > 18
        or len(current_text.split()) > 18
    ):
        return False
    previous_font = float(previous.get("font_size") or 0.0)
    current_font = float(current.get("font_size") or 0.0)
    if min(previous_font, current_font) < 14.0:
        return False
    if (
        max(previous_font, current_font) / max(0.1, min(previous_font, current_font))
        > 1.18
    ):
        return False
    previous_box = previous["bbox"]
    current_box = current["bbox"]
    height = max(_line_height(previous), _line_height(current))
    vertical_gap = float(current_box[1]) - float(previous_box[3])
    if not -height * 0.10 <= vertical_gap <= height * 0.55:
        return False
    previous_center = (float(previous_box[0]) + float(previous_box[2])) / 2.0
    current_center = (float(current_box[0]) + float(current_box[2])) / 2.0
    return abs(previous_center - current_center) <= height * 1.75


def _same_line_direction(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_direction = tuple(previous.get("direction") or [1.0, 0.0])
    current_direction = tuple(current.get("direction") or [1.0, 0.0])
    return all(
        abs(float(left) - float(right)) <= 0.05
        for left, right in zip(previous_direction, current_direction, strict=False)
    )


def _is_horizontal_line(line: dict[str, Any]) -> bool:
    direction = line.get("direction") or [1.0, 0.0]
    return abs(float(direction[0])) >= 0.90 and abs(float(direction[1])) <= 0.20


def _visual_line_sort_key(line: dict[str, Any]) -> tuple[float, float]:
    return (_line_vertical_center(line), float(line["bbox"][0]))


def _line_vertical_center(line: dict[str, Any]) -> float:
    return (float(line["bbox"][1]) + float(line["bbox"][3])) / 2.0


def _line_height(line: dict[str, Any]) -> float:
    return max(1.0, float(line["bbox"][3]) - float(line["bbox"][1]))


def _round_direction(direction: Iterable[float]) -> list[float]:
    values = list(direction)
    if len(values) < 2:
        return [1.0, 0.0]
    return [round(float(values[0]), 4), round(float(values[1]), 4)]


def _transform_direction(direction: Iterable[float], matrix: Any) -> list[float]:
    values = list(direction)
    if len(values) < 2:
        values = [1.0, 0.0]
    transformed_x = float(matrix.a) * float(values[0]) + float(matrix.c) * float(
        values[1]
    )
    transformed_y = float(matrix.b) * float(values[0]) + float(matrix.d) * float(
        values[1]
    )
    length = max(1e-9, (transformed_x**2 + transformed_y**2) ** 0.5)
    return _round_direction((transformed_x / length, transformed_y / length))


def _is_heading_like_line(line: dict[str, Any]) -> bool:
    if _is_structural_heading_text(_normalize_space(line.get("text") or "")):
        return True
    text = _normalize_space(line.get("text") or "")
    if not text or len(text) > 180 or len(text.split()) > 18:
        return False
    return bool(
        _is_explicit_chapter_heading_text(text)
        or _NUMBERED_HEADING_RE.match(text)
        or _is_major_section_heading_text(text)
        or (len(text) >= 4 and text.isupper() and re.search(r"[^\W\d_]", text))
    )


def _is_structural_heading_text(text: str) -> bool:
    normalized = _normalize_space(text)
    if not normalized or len(normalized) > 180 or len(normalized.split()) > 18:
        return False
    return bool(
        _is_explicit_chapter_heading_text(normalized)
        or _NUMBERED_HEADING_RE.match(normalized)
        or _is_major_section_heading_text(normalized)
    )


def _is_explicit_chapter_heading_text(text: str) -> bool:
    """Accept a chapter label, not an ordinary sentence that cites a chapter."""
    normalized = _normalize_space(text)
    match = _CHAPTER_RE.match(normalized)
    if not match:
        return False
    remainder = normalized[match.end() :].strip()
    return bool(
        not remainder
        or remainder[0] in ".:;,-–—"
        or normalized.isupper()
    )


def _is_major_section_heading_text(text: str) -> bool:
    normalized = _normalize_space(text)
    if not normalized or not normalized[0].isupper():
        return False
    match = _MAJOR_SECTION_RE.match(normalized)
    if not match:
        return False
    if _CHAPTER_RE.match(normalized):
        return _is_explicit_chapter_heading_text(normalized)
    remainder = normalized[match.end() :].strip()
    return bool(
        not remainder
        or remainder[0] in ".:;,-–—"
        or normalized.isupper()
    )


def _join_lines(lines: list[dict[str, Any]]) -> str:
    text = ""
    for line in lines:
        current = _normalize_space(line["text"])
        if not current:
            continue
        if text and re.search(r"[\w\u00c0-\u024f]-$", text) and re.match(r"^[a-z\u00df-\u024f]", current):
            text = text[:-1] + current
        else:
            text = f"{text} {current}".strip()
    return text


def _combined_bbox(lines: list[dict[str, Any]]) -> list[float]:
    return _round_bbox(
        [
            min(line["bbox"][0] for line in lines),
            min(line["bbox"][1] for line in lines),
            max(line["bbox"][2] for line in lines),
            max(line["bbox"][3] for line in lines),
        ]
    )


def _round_bbox(value: Iterable[float]) -> list[float]:
    return [round(float(item), 3) for item in value]


def _bbox_area(value: Iterable[float]) -> float:
    x0, y0, x1, y1 = value
    return max(0.0, float(x1) - float(x0)) * max(0.0, float(y1) - float(y0))


def _normalize_space(text: str) -> str:
    without_controls = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text or ""))
    return re.sub(r"\s+", " ", without_controls).strip()
