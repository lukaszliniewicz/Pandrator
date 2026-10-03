"""PDF role classification and layout/page continuation evidence."""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from itertools import pairwise
from typing import Any

from .models import SourceBlock, SourceDocument
from .pdf_native_layout import (
    _CHAPTER_RE,
    _MAJOR_SECTION_RE,
    _NUMBERED_HEADING_RE,
    _is_explicit_chapter_heading_text,
    _is_major_section_heading_text,
    _is_structural_heading_text,
    _normalize_space,
)

_NOTE_PREFIX_RE = re.compile(
    r"^(?:\[\d{1,3}\]|\d{1,3}[.)]|[*†‡]|[ivxlcdm]{1,8})\s+\S+",
    re.IGNORECASE,
)


_SINGLE_NOTE_MARKER_RE = re.compile(r"^[*†‡]$")


_TOC_HEADING_RE = re.compile(
    r"\b(?:table of contents|contents|spis tre[sś]ci|sommaire|inhaltsverzeichnis|indice|índice|содержание)\b",
    re.IGNORECASE,
)


_COPYRIGHT_RE = re.compile(
    r"(?:\bcopyright\b|©|\ball rights reserved\b|\bisbn\b|\blibrary of congress\b|"
    r"\bcatalog(?:ue|uing|ing)?\b|\bno part of this publication\b|\bprinted in\b|"
    r"\bfirst published\b|\bpublished by\b)",
    re.IGNORECASE,
)


_NON_NARRATIVE_HEADING_RE = re.compile(
    r"^(?:(?:select\s+)?(?:bibliography|references|works cited|index|glossary|"
    r"notes?|footnotes?|endnotes?|colophon|copyright|list of abbreviations|abbreviations)|"
    r"(?:other\s+)?works\s+(?:by|about)|"
    r"(?:wykaz|spis)\s+skr[oó]t[oó]w|ключи|.*(?:мини[-*])?словар\w*)\b",
    re.IGNORECASE,
)


_LATIN_LANGUAGE_STOPWORDS = {
    "en": {
        "the", "and", "of", "to", "in", "is", "that", "for", "with", "as", "on", "this",
        "it", "be", "by", "from", "at", "or", "an", "are", "not", "which", "but", "we",
    },
    "fr": {
        "le", "la", "les", "de", "des", "du", "et", "en", "un", "une", "que", "qui", "dans",
        "pour", "est", "pas", "sur", "avec", "par", "au", "aux", "ce", "cette", "il", "elle",
    },
    "de": {
        "der", "die", "das", "den", "dem", "des", "und", "oder", "aber", "in", "im", "ist",
        "sind", "mit", "von", "zu", "auf", "für", "nicht", "ein", "eine", "einer", "als", "auch",
    },
}


def _annotate_structural_roles(document: SourceDocument) -> None:
    if not document.blocks:
        return
    font_samples = [
        (
            float(block.attributes.get("font_size") or 0.0),
            max(1, int(block.attributes.get("source_lines") or 1)),
        )
        for block in document.blocks
        if float(block.attributes.get("font_size") or 0.0) > 0
    ]
    body_font = _weighted_median(font_samples) if font_samples else 10.0
    page_count = max((block.page or 0 for block in document.blocks), default=1)
    blocks_by_page: dict[int, list[SourceBlock]] = defaultdict(list)
    for block in document.blocks:
        if block.page:
            blocks_by_page[block.page].append(block)
    horizontal_blocks_by_page = {
        page: sum(
            abs(float((block.attributes.get("direction") or [1.0, 0.0])[0])) >= 0.90
            and abs(float((block.attributes.get("direction") or [1.0, 0.0])[1])) <= 0.20
            for block in blocks
        )
        for page, blocks in blocks_by_page.items()
    }
    tabular_pages = _tabular_page_reasons(blocks_by_page)
    footnote_rule_boundaries = _footnote_rule_boundaries(
        document, blocks_by_page, body_font, set(tabular_pages)
    )
    large_heading_continuation_ids = _large_heading_continuation_ids(
        blocks_by_page, body_font
    )
    chapter_outline_pages = {
        page
        for page, blocks in blocks_by_page.items()
        if sum(
            len(block.text) <= 160
            and len(block.text.split()) <= 18
            and _is_explicit_chapter_heading_text(block.text)
            for block in blocks
        )
        >= 3
    }

    marginal_occurrences: dict[str, list[SourceBlock]] = defaultdict(list)
    structural_marginal_occurrences: dict[str, list[SourceBlock]] = defaultdict(list)
    for block in document.blocks:
        bbox = block.attributes.get("bbox") or [0, 0, 0, 0]
        page_size = block.attributes.get("page_size") or [1, 1]
        y0 = float(bbox[1]) / max(1.0, float(page_size[1]))
        y1 = float(bbox[3]) / max(1.0, float(page_size[1]))
        if y1 <= 0.16 or y0 >= 0.84:
            key = _normalized_marginal_key(block.text)
            font_size = float(block.attributes.get("font_size") or body_font)
            if key and not _is_structural_heading_text(block.text):
                marginal_occurrences[key].append(block)
            elif key and font_size <= body_font * 1.10:
                exact_key = re.sub(
                    r"[^\w]+", "", _normalize_space(block.text).casefold()
                )
                if exact_key:
                    structural_marginal_occurrences[exact_key].append(block)
    repeated_threshold = max(3, min(8, int(page_count * 0.15) or 3))
    repeated_ids = {
        block.block_id
        for blocks in marginal_occurrences.values()
        if len({block.page for block in blocks}) >= repeated_threshold
        for block in blocks
    }
    for blocks in structural_marginal_occurrences.values():
        previous_page = -100
        for block in sorted(
            blocks, key=lambda item: (item.page or 0, item.source_index)
        ):
            page = block.page or 0
            if page - previous_page <= 6:
                repeated_ids.add(block.block_id)
            previous_page = page

    front_limit = max(5, min(30, int(page_count * 0.2) + 1))
    toc_pages = _find_toc_pages(blocks_by_page, front_limit)
    boilerplate_pages = _find_front_boilerplate_pages(blocks_by_page, page_count)
    for block in document.blocks:
        evidence: dict[str, dict[str, Any]] = {}
        text = block.text.strip()
        bbox = block.attributes.get("bbox") or [0, 0, 0, 0]
        page_size = block.attributes.get("page_size") or [1, 1]
        x0 = float(bbox[0]) / max(1.0, float(page_size[0]))
        x1 = float(bbox[2]) / max(1.0, float(page_size[0]))
        y0 = float(bbox[1]) / max(1.0, float(page_size[1]))
        y1 = float(bbox[3]) / max(1.0, float(page_size[1]))
        direction = block.attributes.get("direction") or [1.0, 0.0]
        rotated_edge_marginal = bool(
            (abs(float(direction[0])) < 0.90 or abs(float(direction[1])) > 0.20)
            and (x1 <= 0.10 or x0 >= 0.90)
            and horizontal_blocks_by_page.get(block.page or 0, 0) >= 1
        )
        normalized_text = _normalize_space(text)
        marginal_url = bool(
            (y1 <= 0.10 or y0 >= 0.90)
            and len(normalized_text) <= 160
            and re.match(r"^(?:https?://|www\.)\S+$", normalized_text, re.IGNORECASE)
        )
        edge_glyph_artifact = bool(
            len(normalized_text) <= 2
            and not any(char.isalnum() for char in normalized_text)
            and (x1 <= 0.08 or x0 >= 0.92 or y1 <= 0.06 or y0 >= 0.94)
            and horizontal_blocks_by_page.get(block.page or 0, 0) >= 4
        )
        font_size = float(block.attributes.get("font_size") or body_font)
        font_ratio = font_size / max(1.0, body_font)
        short = len(text) <= 160 and len(text.split()) <= 18
        page_blocks = blocks_by_page.get(block.page or 0, [])
        is_toc_page = (block.page or 0) in toc_pages
        is_content_opener = _is_content_opener(block, page_blocks, body_font)
        is_title_heading = _is_title_followed_by_byline(block, page_blocks, body_font)
        is_running_header = _is_probable_running_header(
            text, short, y1, font_size, body_font
        )
        note_marker = bool(
            _NOTE_PREFIX_RE.match(text) or _SINGLE_NOTE_MARKER_RE.fullmatch(text)
        )
        bottom_plain_note_marker = bool(
            y0 >= 0.72 and re.match(r"^\d{1,3}\s+\S", text)
        )
        toc_like = bool(re.search(r"\.{3,}\s*\d{1,4}$", text)) or (
            short
            and not _is_structural_heading_text(text)
            and bool(re.search(r"\s+\d{1,4}$", text))
        )
        is_toc_candidate = bool(
            not is_toc_page
            and (block.page or 1) <= front_limit
            and (toc_like or _is_toc_heading_text(text))
        )
        footnote_reasons: list[str] = []
        footnote_score = 0.0
        if y0 >= 0.80:
            footnote_score += 0.45
            footnote_reasons.append("bottom_page_region")
        elif y0 >= 0.72:
            footnote_score += 0.30
            footnote_reasons.append("lower_page_region")
        if font_size <= body_font * 0.90:
            footnote_score += 0.3
            footnote_reasons.append("smaller_than_body_font")
        if note_marker or bottom_plain_note_marker:
            footnote_score += 0.35
            footnote_reasons.append(
                "bottom_plain_note_marker"
                if bottom_plain_note_marker and not note_marker
                else "note_marker_prefix"
            )
        likely_footnote = footnote_score >= 0.6
        footnote_rule_y = footnote_rule_boundaries.get(block.page or 0)
        if footnote_rule_y is not None and y0 >= footnote_rule_y:
            footnote_score = max(footnote_score, 0.92)
            footnote_reasons.append("below_horizontal_footnote_rule")
            likely_footnote = True

        page_number_shape = bool(
            re.fullmatch(
                r"(?:\d{1,4}|[ivxlcdm]{1,8}|[1il|]\s+\d{2,4})",
                text,
                re.IGNORECASE,
            )
        )
        # OCR and embedded subset fonts sometimes make a folio appear slightly
        # larger than the surrounding body even though it is physically tiny.
        page_number_size = font_size <= body_font * 1.35
        if page_number_shape and page_number_size and (y1 <= 0.18 or y0 >= 0.80):
            evidence["page_number"] = {
                "score": 0.99,
                "reasons": ["numeric_or_roman", "marginal_position"],
            }
        if rotated_edge_marginal:
            evidence["repeated_marginal"] = {
                "score": 0.99,
                "reasons": ["rotated_text", "outer_page_edge"],
            }
        elif marginal_url:
            evidence["repeated_marginal"] = {
                "score": 0.98,
                "reasons": ["standalone_url", "marginal_position"],
            }
        elif edge_glyph_artifact:
            evidence["repeated_marginal"] = {
                "score": 0.98,
                "reasons": ["isolated_non_alphanumeric_glyph", "outer_page_edge"],
            }
        elif block.block_id in repeated_ids:
            evidence["repeated_marginal"] = {
                "score": 0.98,
                "reasons": [
                    "normalized_text_repeats_across_pages",
                    "consistent_marginal_position",
                ],
            }
        if is_running_header:
            evidence["running_header"] = {
                "score": 0.98,
                "reasons": [
                    "top_marginal_position",
                    "smaller_than_body_font",
                    "variable_header_shape",
                ],
            }
        if is_title_heading:
            evidence["title_heading"] = {
                "score": 0.96,
                "reasons": ["large_front_matter_text", "followed_by_smaller_byline"],
            }
        if (block.page or 0) in boilerplate_pages:
            reasons = ["front_matter_page_with_multiple_publishing_signals"]
            if _COPYRIGHT_RE.search(text):
                reasons.insert(0, "explicit_publishing_or_copyright_signal")
            evidence["boilerplate"] = {"score": 0.98, "reasons": reasons}

        heading_score = 0.0
        heading_reasons: list[str] = []
        explicit_chapter = (
            short and not likely_footnote and _is_explicit_chapter_heading_text(text)
        )
        is_chapter_outline_entry = (
            explicit_chapter and (block.page or 0) in chapter_outline_pages
        )
        numbered_heading = (
            short and not likely_footnote and bool(_NUMBERED_HEADING_RE.match(text))
        )
        major_section = short and _is_major_section_heading_text(text)
        non_narrative_section = short and bool(_NON_NARRATIVE_HEADING_RE.match(text))
        if short and font_ratio >= 1.45:
            heading_score += 0.65
            heading_reasons.append("substantially_larger_than_body_font")
        elif short and font_ratio >= 1.18:
            heading_score += 0.45
            heading_reasons.append("larger_than_body_font")
        if explicit_chapter:
            heading_score += 0.75
            heading_reasons.append("explicit_numbered_chapter")
        elif numbered_heading:
            heading_score += 0.45
            heading_reasons.append("numbered_heading_with_safe_delimiter")
        if major_section:
            heading_score += 0.55
            heading_reasons.append("named_major_section")
        if short and text.isupper() and len(text) >= 4:
            heading_score += 0.20
            heading_reasons.append("all_caps")
        if short and y0 < 0.35:
            heading_score += 0.10
            heading_reasons.append("upper_page_position")
        if short and is_content_opener:
            heading_score += 0.15
            heading_reasons.append("opens_substantial_page_content")
        if numbered_heading and text.isupper() and is_content_opener:
            heading_score += 0.10
            heading_reasons.append("isolated_uppercase_numbered_heading")
        if heading_score >= 0.45:
            evidence["heading"] = {
                "score": min(0.99, heading_score),
                "reasons": heading_reasons,
            }
        if non_narrative_section:
            evidence["non_narrative_section"] = {
                "score": 0.90,
                "reasons": ["bibliographic_or_note_section_heading"],
            }
        if is_chapter_outline_entry:
            evidence["chapter_outline"] = {
                "score": 0.95,
                "reasons": ["several_chapter_labels_on_same_page"],
            }
        if (
            heading_score >= 0.85
            and not evidence.get("repeated_marginal")
            and not evidence.get("running_header")
            and not evidence.get("title_heading")
            and not evidence.get("boilerplate")
            and not evidence.get("page_number")
            and not likely_footnote
            and not is_chapter_outline_entry
            and not is_toc_page
            and not is_toc_candidate
            and not non_narrative_section
            and block.block_id not in large_heading_continuation_ids
            and (
                explicit_chapter
                or major_section
                or (
                    is_content_opener
                    and (
                        numbered_heading
                        or (
                            font_ratio >= 1.45 and len(text) >= 4 and text[:1].isupper()
                        )
                    )
                )
            )
        ):
            evidence["deterministic_chapter"] = {
                "score": min(0.96, heading_score),
                "reasons": heading_reasons + ["global_pdf_heading_policy"],
            }

        if likely_footnote and "page_number" not in evidence:
            evidence["footnote"] = {
                "score": min(0.98, footnote_score),
                "reasons": footnote_reasons,
            }

        tabular_reasons = tabular_pages.get(block.page or 0)
        if tabular_reasons:
            evidence["tabular_region"] = {
                "score": 0.90,
                "reasons": tabular_reasons,
            }

        if is_toc_page:
            evidence["toc"] = {
                "score": 0.94,
                "reasons": ["front_matter", "toc_heading_or_continuation_page"],
            }
        elif is_toc_candidate:
            evidence["toc_candidate"] = {
                "score": 0.7 if toc_like else 0.85,
                "reasons": [
                    "front_matter",
                    "toc_entry_shape" if toc_like else "toc_heading",
                ],
            }
        block.attributes["role_evidence"] = evidence
        block.role_candidates = sorted(set(block.role_candidates + list(evidence)))


def _tabular_page_reasons(
    blocks_by_page: dict[int, list[SourceBlock]],
) -> dict[int, list[str]]:
    """Identify dense tables, dictionaries, indexes, and multi-column notes.

    The role is preservation-only: it blocks speculative paragraph reflow but
    never deletes content.
    """
    result: dict[int, list[str]] = {}
    for page, blocks in blocks_by_page.items():
        substantive = [block for block in blocks if block.text.strip()]
        if len(substantive) < 12:
            continue
        word_counts = [len(block.text.split()) for block in substantive]
        short_fraction = sum(count <= 8 for count in word_counts) / len(word_counts)
        source_lines = [
            max(1, int(block.attributes.get("source_lines") or 1))
            for block in substantive
        ]
        wide_fraction = sum(
            (
                float((block.attributes.get("bbox") or [0, 0, 0, 0])[2])
                - float((block.attributes.get("bbox") or [0, 0, 0, 0])[0])
            )
            / max(
                1.0,
                float((block.attributes.get("page_size") or [1.0, 1.0])[0]),
            )
            >= 0.45
            for block in substantive
        ) / len(substantive)
        left_slot_counts = Counter(
            round(
                float((block.attributes.get("bbox") or [0, 0, 0, 0])[0])
                / max(
                    1.0,
                    float(
                        (block.attributes.get("page_size") or [1.0, 1.0])[0]
                    ),
                ),
                2,
            )
            for block in substantive
        )
        left_slots = set(left_slot_counts)
        repeated_left_slots = sum(count >= 3 for count in left_slot_counts.values())
        reasons: list[str] = []
        if any(
            block.attributes.get("reading_order") == "multi_columns"
            for block in substantive
        ):
            reasons.append("three_or_more_text_columns")
        if (
            len(substantive) >= 18
            and short_fraction >= 0.62
            and statistics.median(source_lines) <= 2
            and repeated_left_slots >= 2
            and wide_fraction <= 0.55
        ):
            reasons.append("dense_short_aligned_records")
        if (
            len(substantive) >= 24
            and short_fraction >= 0.50
            and len(left_slots) >= 4
            and wide_fraction <= 0.55
        ):
            reasons.append("many_repeated_record_columns")
        if reasons:
            result[page] = reasons
    return result


def _footnote_rule_boundaries(
    document: SourceDocument,
    blocks_by_page: dict[int, list[SourceBlock]],
    body_font: float,
    tabular_pages: set[int],
) -> dict[int, float]:
    """Locate conventional short rules separating lower-page footnote areas."""
    page_records = {
        int(record.get("page") or 0): record
        for record in document.attributes.get("pdf_ingestion", {}).get("pages", [])
        if isinstance(record, dict)
    }
    result: dict[int, float] = {}
    for page, blocks in blocks_by_page.items():
        if page in tabular_pages or not blocks:
            continue
        page_size = blocks[0].attributes.get("page_size") or [1.0, 1.0]
        width = max(1.0, float(page_size[0]))
        height = max(1.0, float(page_size[1]))
        candidates: list[float] = []
        for rule in page_records.get(page, {}).get("horizontal_rules") or []:
            if not isinstance(rule, list) or len(rule) < 3:
                continue
            x0, y, x1 = map(float, rule[:3])
            width_ratio = (x1 - x0) / width
            y_ratio = y / height
            if not (0.06 <= width_ratio <= 0.38 and 0.45 <= y_ratio <= 0.90):
                continue
            if x0 / width > 0.42:
                continue
            below = [
                block
                for block in blocks
                if float((block.attributes.get("bbox") or [0, 0, 0, 0])[1])
                >= y + 1.0
            ]
            if not below:
                continue
            below_fonts = [
                float(block.attributes.get("font_size") or 0.0)
                for block in below
                if float(block.attributes.get("font_size") or 0.0) > 0
            ]
            if below_fonts and statistics.median(below_fonts) > body_font * 1.08:
                continue
            candidates.append(y_ratio)
        if candidates:
            result[page] = min(candidates)
    return result


def _annotate_layout_continuations(document: SourceDocument) -> None:
    """Persist auditable same-page paragraph seams before cleanup is applied."""
    blocks_by_page: dict[int, list[SourceBlock]] = defaultdict(list)
    for block in document.blocks:
        if block.page:
            blocks_by_page[block.page].append(block)
    non_narrative_pages = _non_narrative_page_span(blocks_by_page)
    for page, blocks in blocks_by_page.items():
        previous: SourceBlock | None = None
        for current in blocks:
            if _is_ignorable_layout_separator(current):
                continue
            if previous is not None and page not in non_narrative_pages:
                continuation = _layout_continuation_details(previous, current)
                if continuation is not None:
                    mode, score, reasons = continuation
                    evidence = current.attributes.setdefault("role_evidence", {})
                    evidence["layout_continuation"] = {
                        "score": score,
                        "reasons": reasons,
                    }
                    current.attributes["layout_continuation_from_block_id"] = (
                        previous.block_id
                    )
                    current.attributes["layout_continuation_join"] = mode
                    current.role_candidates = sorted(
                        set(current.role_candidates + ["layout_continuation"])
                    )
            previous = current


def _is_ignorable_layout_separator(block: SourceBlock) -> bool:
    return any(
        block.role_score(role) >= score
        for role, score in (
            ("repeated_marginal", 0.95),
            ("running_header", 0.98),
            ("page_number", 0.98),
        )
    )


def _layout_continuation_details(
    previous: SourceBlock, current: SourceBlock
) -> tuple[str, float, list[str]] | None:
    if previous.page != current.page:
        return None
    if previous.attributes.get("source_method") != current.attributes.get(
        "source_method"
    ):
        return None
    if previous.attributes.get("reading_order") != current.attributes.get(
        "reading_order"
    ):
        return None
    if not _matching_horizontal_directions(previous, current):
        return None
    if _has_layout_separator_role(previous) or _has_layout_separator_role(current):
        return None

    same_native_container = _share_native_text_container(previous, current)
    tabular = (
        previous.role_score("tabular_region") >= 0.90
        or current.role_score("tabular_region") >= 0.90
    )
    if tabular:
        return None

    previous_box = previous.attributes.get("bbox")
    current_box = current.attributes.get("bbox")
    previous_line = previous.attributes.get("last_line_bbox") or previous_box
    current_line = current.attributes.get("first_line_bbox") or current_box
    previous_size = previous.attributes.get("page_size")
    current_size = current.attributes.get("page_size")
    if (
        previous_box is None
        or current_box is None
        or previous_line is None
        or current_line is None
        or previous_size is None
        or current_size is None
    ):
        return None

    page_width = max(float(previous_size[0]), float(current_size[0]), 1.0)
    previous_font = float(previous.attributes.get("font_size") or 0.0)
    current_font = float(current.attributes.get("font_size") or 0.0)
    if previous_font and current_font:
        font_ratio = max(previous_font, current_font) / max(
            0.1, min(previous_font, current_font)
        )
        if font_ratio > 1.25:
            return None
    line_height = max(previous_font, current_font, 5.0) * 1.35
    vertical_gap = float(current_line[1]) - float(previous_line[3])
    if vertical_gap < -line_height * 0.25 or vertical_gap > line_height * 1.80:
        return None
    if not same_native_container and vertical_gap > line_height * 0.55:
        return None
    left_delta = abs(float(current_line[0]) - float(previous_box[0]))
    if left_delta > max(page_width * 0.08, line_height * 2.5):
        return None
    if _horizontal_overlap(previous_box, current_box) < 0.55:
        return None

    current_first_indent = float(current_line[0]) - float(current_box[0])
    if current_first_indent > max(page_width * 0.008, current_font * 0.55, 4.0):
        return None
    if (
        not same_native_container
        and (float(current_box[2]) - float(current_box[0])) / page_width >= 0.80
    ):
        return None

    left = previous.text.rstrip()
    right = current.text.lstrip()
    if not left or not right or _ends_with_sentence_terminal(left):
        return None
    if _is_layout_structural_singleton(previous) or _is_layout_structural_singleton(
        current
    ):
        return None
    hyphenated = _ends_with_split_hyphen(left) and _starts_with_letter(right)
    lowercase = _starts_with_lowercase_continuation(right)
    full_previous_line = float(previous_line[2]) >= float(previous_box[2]) - max(
        line_height * 2.5, page_width * 0.03
    )
    strong_geometry = bool(
        full_previous_line
        and vertical_gap <= line_height * 1.30
        and len(left.split()) >= 8
        and not _starts_with_list_or_entry_marker(right)
    )
    if not hyphenated and not lowercase and not (same_native_container and strong_geometry):
        return None
    if (
        not hyphenated
        and not same_native_container
        and int(previous.attributes.get("source_lines") or 1) <= 2
        and int(current.attributes.get("source_lines") or 1) <= 2
        and len(left.split()) <= 12
        and len(right.split()) <= 12
    ):
        return None
    if (
        not hyphenated
        and not same_native_container
        and int(previous.attributes.get("source_lines") or 1) <= 1
        and len(left.split()) < 8
    ):
        return None

    reasons = [
        "same_page",
        "matching_source_method_layout_and_direction",
        "adjacent_visual_lines",
        "compatible_typography_and_margins",
    ]
    if same_native_container:
        reasons.append("shared_native_text_container")
    if hyphenated:
        reasons.append("hyphenated_seam")
    elif lowercase:
        reasons.append("lowercase_continuation")
    else:
        reasons.append("full_line_geometric_continuation")
    mode = _continuation_join_mode(left, same_native_container)
    return mode, (0.96 if same_native_container else 0.93), reasons


def _has_layout_separator_role(block: SourceBlock) -> bool:
    return any(
        block.role_score(role) >= score
        for role, score in (
            ("deterministic_chapter", 0.85),
            ("footnote", 0.60),
            ("heading", 0.45),
            ("title_heading", 0.95),
            ("toc", 0.92),
            ("toc_candidate", 0.70),
            ("non_narrative_section", 0.85),
            ("metadata", 0.90),
            ("boilerplate", 0.98),
        )
    )


def _is_layout_structural_singleton(block: SourceBlock) -> bool:
    """Reject terse metadata and unrecognized headings at a paragraph seam."""
    text = _normalize_space(block.text)
    if not text:
        return True
    source_lines = int(block.attributes.get("source_lines") or 1)
    bbox = block.attributes.get("bbox") or [0, 0, 0, 0]
    page_size = block.attributes.get("page_size") or [1, 1]
    top = float(bbox[1]) / max(1.0, float(page_size[1]))
    if top <= 0.09 and source_lines <= 2 and len(text.split()) <= 5:
        return True
    if source_lines > 1:
        return False
    if "@" in text and re.fullmatch(r"\S+@\S+", text):
        return True
    letters = [char for char in text if char.isalpha()]
    return bool(
        letters
        and len(text.split()) <= 14
        and all(char.isupper() for char in letters)
    )


def _matching_horizontal_directions(
    previous: SourceBlock, current: SourceBlock
) -> bool:
    previous_direction = previous.attributes.get("direction") or [1.0, 0.0]
    current_direction = current.attributes.get("direction") or [1.0, 0.0]
    return bool(
        abs(float(previous_direction[0])) >= 0.90
        and abs(float(previous_direction[1])) <= 0.20
        and all(
            abs(float(left) - float(right)) <= 0.05
            for left, right in zip(previous_direction, current_direction, strict=False)
        )
    )


def _share_native_text_container(
    previous: SourceBlock, current: SourceBlock
) -> bool:
    previous_indexes = {
        int(value) for value in previous.attributes.get("native_block_indexes") or []
    }
    current_indexes = {
        int(value) for value in current.attributes.get("native_block_indexes") or []
    }
    return bool(previous_indexes and current_indexes and previous_indexes & current_indexes)


def _horizontal_overlap(previous_box: list[Any], current_box: list[Any]) -> float:
    previous_width = max(1.0, float(previous_box[2]) - float(previous_box[0]))
    current_width = max(1.0, float(current_box[2]) - float(current_box[0]))
    overlap = max(
        0.0,
        min(float(previous_box[2]), float(current_box[2]))
        - max(float(previous_box[0]), float(current_box[0])),
    )
    return overlap / min(previous_width, current_width)


def _starts_with_list_or_entry_marker(text: str) -> bool:
    return bool(
        re.match(
            r"^[\s\"'“‘(\[]*(?:[-–—•▪◦*†‡]|\d{1,4}[.)]|[a-z][.)]\s)",
            text,
            re.IGNORECASE,
        )
    )


def _continuation_join_mode(text: str, remove_printed_hyphen: bool) -> str:
    stripped = text.rstrip()
    if stripped.endswith("\u00ad"):
        return "remove_hyphen"
    if stripped.endswith("-"):
        return "remove_hyphen" if remove_printed_hyphen else "keep_hyphen"
    return "space"


def _annotate_page_continuations(document: SourceDocument) -> None:
    """Mark only high-confidence narrative continuations across adjacent pages.

    PDF layout normally creates a new extraction block at every page boundary.
    Keeping the source blocks separate preserves page-level provenance for review,
    while the annotation lets the cleaned-text writer reflow safe continuations.
    """
    blocks_by_page: dict[int, list[SourceBlock]] = defaultdict(list)
    for block in document.blocks:
        if block.page:
            blocks_by_page[block.page].append(block)

    non_narrative_pages = _non_narrative_page_span(blocks_by_page)
    pages = sorted(blocks_by_page)
    for previous_page, current_page in pairwise(pages):
        if current_page != previous_page + 1:
            continue
        if previous_page in non_narrative_pages or current_page in non_narrative_pages:
            continue
        previous = _boundary_narrative_block(blocks_by_page[previous_page], reverse=True)
        current = _boundary_narrative_block(blocks_by_page[current_page])
        if previous is None or current is None:
            continue
        if _has_structural_separator_after(blocks_by_page[previous_page], previous):
            continue
        if _has_structural_separator_before(blocks_by_page[current_page], current):
            continue
        continuation = _page_continuation_details(previous, current)
        if continuation is None:
            continue
        mode, reasons = continuation
        evidence = current.attributes.setdefault("role_evidence", {})
        evidence["page_continuation"] = {"score": 0.96, "reasons": reasons}
        current.attributes["continuation_from_block_id"] = previous.block_id
        current.attributes["continuation_join"] = mode
        current.role_candidates = sorted(set(current.role_candidates + ["page_continuation"]))


def _non_narrative_page_span(blocks_by_page: dict[int, list[SourceBlock]]) -> set[int]:
    """Track end-matter/list sections so their entries are never reflowed as prose."""
    active = False
    pages: set[int] = set()
    for page in sorted(blocks_by_page):
        for block in blocks_by_page[page]:
            if block.role_score("toc") >= 0.92:
                continue
            if block.role_score("deterministic_chapter") >= 0.85:
                active = False
            if block.role_score("non_narrative_section") >= 0.85:
                active = True
        if active:
            pages.add(page)
    return pages


def _boundary_narrative_block(blocks: list[SourceBlock], reverse: bool = False) -> SourceBlock | None:
    ordered = list(reversed(blocks)) if reverse else blocks
    for index, block in enumerate(ordered):
        if _is_narrative_boundary_block(block):
            if not reverse and _is_probable_page_boundary_header(
                block, ordered[index + 1 :]
            ):
                continue
            return block
    return None


def _is_narrative_boundary_block(block: SourceBlock) -> bool:
    if len(block.text.strip()) < 3:
        return False
    excluded_roles = (
        ("repeated_marginal", 0.95),
        ("running_header", 0.98),
        ("page_number", 0.98),
        ("boilerplate", 0.98),
        ("toc", 0.92),
        ("toc_candidate", 0.70),
        ("footnote", 0.60),
        ("heading", 0.45),
        ("non_narrative_section", 0.85),
    )
    return not any(
        block.role_score(role) >= score for role, score in excluded_roles
    )


def _is_probable_page_boundary_header(
    block: SourceBlock, following_blocks: list[SourceBlock]
) -> bool:
    """Skip a terse top line only when substantial page content follows it."""
    bbox = block.attributes.get("bbox") or [0, 0, 0, 0]
    page_size = block.attributes.get("page_size") or [1, 1]
    top = float(bbox[1]) / max(1.0, float(page_size[1]))
    if not (
        top <= 0.09
        and int(block.attributes.get("source_lines") or 1) <= 2
        and len(block.text.split()) <= 5
        and not _is_major_section_heading_text(block.text)
    ):
        return False
    return any(
        len(candidate.text) >= 40
        and int(candidate.attributes.get("source_lines") or 1) >= 2
        and float(
            (candidate.attributes.get("bbox") or [0, 0, 0, 0])[1]
        )
        > float(bbox[3])
        for candidate in following_blocks
    )


def _has_structural_separator_after(blocks: list[SourceBlock], candidate: SourceBlock) -> bool:
    try:
        candidate_index = next(
            index for index, block in enumerate(blocks) if block.block_id == candidate.block_id
        )
    except StopIteration:
        return True
    return any(_is_structural_page_separator(block) for block in blocks[candidate_index + 1:])


def _has_structural_separator_before(blocks: list[SourceBlock], candidate: SourceBlock) -> bool:
    try:
        candidate_index = next(
            index for index, block in enumerate(blocks) if block.block_id == candidate.block_id
        )
    except StopIteration:
        return True
    return any(_is_structural_page_separator(block) for block in blocks[:candidate_index])


def _is_structural_page_separator(block: SourceBlock) -> bool:
    if any(
        block.role_score(role) >= score
        for role, score in (
            ("repeated_marginal", 0.95),
            ("running_header", 0.98),
            ("page_number", 0.98),
        )
    ):
        return False
    if re.match(
        r"^(?:[ivxlcdm]+\s+)?acknowledg(?:e)?ments?\b",
        _normalize_space(block.text),
        re.IGNORECASE,
    ):
        return True
    return any(
        block.role_score(role) >= score
        for role, score in (
            ("deterministic_chapter", 0.85),
            ("heading", 0.45),
            ("non_narrative_section", 0.85),
            ("toc", 0.92),
            ("boilerplate", 0.98),
        )
    )


def _page_continuation_details(
    previous: SourceBlock, current: SourceBlock
) -> tuple[str, list[str]] | None:
    if (
        previous.role_score("tabular_region") >= 0.90
        or current.role_score("tabular_region") >= 0.90
    ):
        return None
    previous_box = previous.attributes.get("bbox") or [0, 0, 0, 0]
    current_box = current.attributes.get("bbox") or [0, 0, 0, 0]
    previous_size = previous.attributes.get("page_size") or [1, 1]
    current_size = current.attributes.get("page_size") or [1, 1]
    previous_bottom = float(previous_box[3]) / max(1.0, float(previous_size[1]))
    current_top = float(current_box[1]) / max(1.0, float(current_size[1]))
    if previous_bottom < 0.64 or current_top > 0.32:
        return None
    if previous.attributes.get("source_method") != current.attributes.get(
        "source_method"
    ):
        return None
    if previous.attributes.get("reading_order") != current.attributes.get(
        "reading_order"
    ):
        return None
    if not _matching_horizontal_directions(previous, current):
        return None

    previous_font = float(previous.attributes.get("font_size") or 0.0)
    current_font = float(current.attributes.get("font_size") or 0.0)
    if previous_font and current_font:
        font_ratio = current_font / previous_font
        if not 0.78 <= font_ratio <= 1.28:
            return None
    previous_left = float(previous_box[0]) / max(1.0, float(previous_size[0]))
    current_left = float(current_box[0]) / max(1.0, float(current_size[0]))
    if abs(previous_left - current_left) > 0.14:
        return None

    previous_text = previous.text.rstrip()
    current_text = current.text.lstrip()
    if not previous_text or not current_text:
        return None
    previous_language = _dominant_latin_language(previous_text)
    current_language = _dominant_latin_language(current_text)
    if previous_language and current_language and previous_language != current_language:
        return None
    reasons = [
        "adjacent_pages",
        "body_blocks_touch_page_boundary",
        "matching_source_method_and_layout",
        "matching_typography_and_left_margin",
    ]
    if _ends_with_split_hyphen(previous_text) and _starts_with_letter(current_text):
        return _continuation_join_mode(
            previous_text, remove_printed_hyphen=False
        ), reasons + ["hyphenated_word_continues"]
    previous_method = str(previous.attributes.get("source_method") or "")
    if (
        previous_method != "ocr"
        and int(previous.attributes.get("source_lines") or 1) <= 1
        and len(previous_text.split()) < 8
    ):
        return None
    if (
        int(current.attributes.get("source_lines") or 1) <= 1
        and len(current_text.split()) < 4
    ):
        return None
    if _ends_with_sentence_terminal(
        previous_text
    ) or not _starts_with_lowercase_continuation(current_text):
        return None
    return "space", reasons + ["unfinished_sentence_with_lowercase_continuation"]


def _ends_with_split_hyphen(text: str) -> bool:
    stripped = text.rstrip()
    return bool(
        len(stripped) >= 2
        and stripped[-1] in {"-", "\u00ad"}
        and stripped[-2].isalpha()
    )


def _starts_with_letter(text: str) -> bool:
    return bool(_first_letter(text))


def _starts_with_lowercase_continuation(text: str) -> bool:
    first_letter = _first_letter(text)
    return bool(first_letter and first_letter.islower())


def _first_letter(text: str) -> str:
    for char in str(text or "").lstrip(" \t\"'“”‘’([{<"):
        if char.isalpha():
            return char
        if not char.isspace() and char not in "\"'“”‘’([{<":
            return ""
    return ""


def _ends_with_sentence_terminal(text: str) -> bool:
    return str(text or "").rstrip().endswith((".", "!", "?", "…", ":", ";"))


def _dominant_latin_language(text: str) -> str:
    """Return a language only when simple stop-word evidence is decisive.

    It is intentionally limited to English/French/German: those can share the same
    page geometry in bilingual source editions, while a weak guess must never
    suppress a valid continuation.
    """
    tokens = re.findall(r"[^\W\d_]+", str(text or "").casefold())
    if not tokens:
        return ""
    scores = {
        language: sum(token in words for token in tokens)
        for language, words in _LATIN_LANGUAGE_STOPWORDS.items()
    }
    language, score = max(scores.items(), key=lambda item: item[1])
    other_score = max(value for other, value in scores.items() if other != language)
    return language if score >= 2 and score >= other_score + 2 else ""


def _find_toc_pages(
    blocks_by_page: dict[int, list[SourceBlock]], front_limit: int
) -> set[int]:
    """Find an anchored TOC and its short, numbered continuation pages."""
    anchors = {
        page
        for page, blocks in blocks_by_page.items()
        if page <= front_limit
        and any(_is_toc_heading_text(block.text) for block in blocks)
    }
    toc_pages = set(anchors)
    for anchor in anchors:
        for page in range(anchor + 1, min(front_limit, anchor + 5) + 1):
            if not _looks_like_toc_continuation(blocks_by_page.get(page, [])):
                break
            toc_pages.add(page)
    return toc_pages


def _is_toc_heading_text(text: str) -> bool:
    """Accept an actual TOC heading, not prose that merely mentions one."""
    normalized = _normalize_space(text).strip()
    normalized = re.sub(r"\s*[:.\-\u2013\u2014]+\s*$", "", normalized).strip()
    return bool(
        normalized
        and len(normalized) <= 80
        and len(normalized.split()) <= 8
        and _TOC_HEADING_RE.fullmatch(normalized)
    )


def _looks_like_toc_continuation(blocks: list[SourceBlock]) -> bool:
    if not blocks:
        return False
    texts = [_normalize_space(block.text) for block in blocks if _normalize_space(block.text)]
    if not texts:
        return False
    long_narration = any(len(text) > 300 or len(text.split()) > 50 for text in texts)
    if long_narration:
        return False
    entry_count = sum(
        len(re.findall(r"(?:\.{3,}\s*|\s+)\d{1,4}(?=\s|$)", text)) for text in texts
    )
    page_number_column = any(
        re.fullmatch(r"(?:\d{1,4}\s+){3,}\d{1,4}", text) is not None for text in texts
    )
    return page_number_column or entry_count >= 2


def _find_front_boilerplate_pages(
    blocks_by_page: dict[int, list[SourceBlock]], page_count: int
) -> set[int]:
    front_limit = min(12, max(5, int(page_count * 0.05)))
    return {
        page
        for page, blocks in blocks_by_page.items()
        if page <= front_limit and sum(bool(_COPYRIGHT_RE.search(block.text)) for block in blocks) >= 2
    }


def _is_content_opener(
    block: SourceBlock, page_blocks: list[SourceBlock], body_font: float
) -> bool:
    bbox = block.attributes.get("bbox") or [0, 0, 0, 0]
    page_size = block.attributes.get("page_size") or [1, 1]
    y0 = float(bbox[1]) / max(1.0, float(page_size[1]))
    if y0 > 0.42:
        return False
    bottom = float(bbox[3])
    for other in page_blocks:
        if other.block_id == block.block_id or len(other.text) < 140:
            continue
        other_bbox = other.attributes.get("bbox") or [0, 0, 0, 0]
        other_font = float(other.attributes.get("font_size") or body_font)
        if float(other_bbox[1]) >= bottom - 1.0 and other_font <= body_font * 1.15:
            return True
    return False


def _large_heading_continuation_ids(
    blocks_by_page: dict[int, list[SourceBlock]], body_font: float
) -> set[str]:
    """Identify later visual lines of one large, centered heading.

    Native PDF blocks can split a multiline title at font or editing boundaries,
    and tiny superscript/marginal records may occur between its lines. Those
    lines remain separately inspectable, but only the first substantive line
    should become a chapter marker.
    """
    continuation_ids: set[str] = set()
    for blocks in blocks_by_page.values():
        candidates: list[SourceBlock] = []
        for block in blocks:
            bbox = block.attributes.get("bbox") or [0, 0, 0, 0]
            page_size = block.attributes.get("page_size") or [1, 1]
            y0 = float(bbox[1]) / max(1.0, float(page_size[1]))
            font_size = float(block.attributes.get("font_size") or 0.0)
            text = block.text.strip()
            if (
                len(text) >= 4
                and len(text) <= 160
                and len(text.split()) <= 18
                and y0 <= 0.42
                and font_size >= body_font * 1.45
            ):
                candidates.append(block)
        previous: SourceBlock | None = None
        for block in sorted(
            candidates,
            key=lambda item: (
                float((item.attributes.get("bbox") or [0, 0, 0, 0])[1]),
                float((item.attributes.get("bbox") or [0, 0, 0, 0])[0]),
            ),
        ):
            if previous is not None and _large_heading_blocks_are_contiguous(
                previous, block, body_font
            ):
                continuation_ids.add(block.block_id)
            else:
                previous = block
                continue
            previous = block
    return continuation_ids


def _large_heading_blocks_are_contiguous(
    previous: SourceBlock, current: SourceBlock, body_font: float
) -> bool:
    previous_box = previous.attributes.get("bbox") or [0, 0, 0, 0]
    current_box = current.attributes.get("bbox") or [0, 0, 0, 0]
    previous_font = float(previous.attributes.get("font_size") or body_font)
    current_font = float(current.attributes.get("font_size") or body_font)
    if (
        max(previous_font, current_font) / max(0.1, min(previous_font, current_font))
        > 1.18
    ):
        return False
    height = max(
        1.0,
        float(previous_box[3]) - float(previous_box[1]),
        float(current_box[3]) - float(current_box[1]),
    )
    vertical_gap = float(current_box[1]) - float(previous_box[3])
    if not -height * 0.10 <= vertical_gap <= max(height * 0.65, body_font * 1.5):
        return False
    previous_center = (float(previous_box[0]) + float(previous_box[2])) / 2.0
    current_center = (float(current_box[0]) + float(current_box[2])) / 2.0
    return abs(previous_center - current_center) <= max(height * 1.75, body_font * 3.0)


def _is_title_followed_by_byline(
    block: SourceBlock, page_blocks: list[SourceBlock], body_font: float
) -> bool:
    """Recognize a front-matter title/byline pair without relying on its words.

    This prevents a large article or book title from becoming an audiobook
    chapter simply because it precedes substantial prose.
    """
    text = block.text.strip()
    if not text or len(text) > 180 or len(text.split()) > 16:
        return False
    bbox = block.attributes.get("bbox") or [0, 0, 0, 0]
    page_size = block.attributes.get("page_size") or [1, 1]
    y0 = float(bbox[1]) / max(1.0, float(page_size[1]))
    font_size = float(block.attributes.get("font_size") or body_font)
    if y0 > 0.35 or font_size < body_font * 1.30:
        return False
    try:
        index = next(i for i, candidate in enumerate(page_blocks) if candidate.block_id == block.block_id)
    except StopIteration:
        return False
    following = page_blocks[index + 1 : index + 3]
    for candidate in following:
        candidate_text = candidate.text.strip()
        candidate_box = candidate.attributes.get("bbox") or [0, 0, 0, 0]
        candidate_font = float(candidate.attributes.get("font_size") or body_font)
        vertical_distance = float(candidate_box[1]) - float(bbox[3])
        if vertical_distance < -2.0 or vertical_distance > float(page_size[1]) * 0.14:
            continue
        if (
            _looks_like_byline(candidate_text)
            and len(candidate_text) <= 90
            and candidate_font <= font_size * 0.88
            and not _CHAPTER_RE.match(candidate_text)
            and not _NUMBERED_HEADING_RE.match(candidate_text)
            and not _MAJOR_SECTION_RE.match(candidate_text)
        ):
            return True
    return False


def _looks_like_byline(text: str) -> bool:
    normalized = _normalize_space(text)
    if normalized.endswith((".", "!", "?", ";", ":")):
        return False
    words = re.findall(r"[^\W\d_]+", normalized)
    if not 2 <= len(words) <= 8:
        return False
    capitalized = sum(word[0].isupper() for word in words if word)
    # Permit a connector such as "and", "de", or "van" in an otherwise
    # name-like byline, but avoid treating a short sentence as an author line.
    return capitalized >= len(words) - 1


def _is_probable_running_header(
    text: str, short: bool, y1: float, font_size: float, body_font: float
) -> bool:
    """Catch section/page headers whose wording varies too much to repeat exactly."""
    return bool(
        short
        and y1 <= 0.14
        and font_size <= body_font * 1.10
        and text.isupper()
        and re.search(r"[^\W\d_]", text)
        and not _is_major_section_heading_text(text)
    )


def _normalized_marginal_key(text: str) -> str:
    normalized = _normalize_space(text).casefold()
    normalized = re.sub(r"\d+", "#", normalized)
    return re.sub(r"[^\w#]+", "", normalized)


def _weighted_median(samples: list[tuple[float, int]]) -> float:
    """Return the median value when each sample represents several source lines."""
    ordered = sorted((value, max(1, int(weight))) for value, weight in samples)
    threshold = sum(weight for _, weight in ordered) / 2
    cumulative = 0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return ordered[-1][0]
