from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .models import CleaningResult, SourceDocument
from .tools import SourceCleaningTools


@dataclass
class SourceCleaningValidationReport:
    warnings: list[str] = field(default_factory=list)
    blocking_warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_cleaning_result(
    document: SourceDocument,
    result: CleaningResult,
    remove_footnotes: bool = False,
) -> SourceCleaningValidationReport:
    report = SourceCleaningValidationReport()

    def add_warning(message: str, *, blocking: bool = False):
        report.warnings.append(message)
        if blocking:
            report.blocking_warnings.append(message)

    original_blocks = len(document.plain_lines())
    cleaned_blocks = len([line for line in result.cleaned_text.splitlines() if line.strip()])
    deleted_blocks = len(result.deleted_block_ids)
    chapter_count = result.report.get("chapter_count", 0)
    nav_title_count = len(document.nav_titles)
    chapter_structure = SourceCleaningTools(document).analyze_chapter_structure(max_candidates=1)
    likely_chapter_count = int(chapter_structure.get("likely_chapter_count") or 0)
    numbered_heading_count = int(chapter_structure.get("numbered_heading_count") or 0)
    deleted_ids = set(result.deleted_block_ids)
    retained_ids = [
        block.block_id
        for block in document.blocks
        if block.block_id not in deleted_ids
    ]
    cleanup_structure = SourceCleaningTools(document).analyze_cleanup_structure(
        max_candidates=10,
        scope={"block_ids": retained_ids},
    )
    remaining_toc_blocks = int(cleanup_structure.get("toc_block_count") or 0)
    remaining_boilerplate_blocks = int(cleanup_structure.get("likely_boilerplate_block_count") or 0)
    title_heading_blocks = _narrative_title_heading_blocks(document)
    retained_title_heading_count = sum(block.block_id not in deleted_ids for block in title_heading_blocks)
    closing_marker_blocks = _narrative_closing_marker_blocks(document)
    retained_closing_marker_count = sum(block.block_id not in deleted_ids for block in closing_marker_blocks)
    deletion_ratio = (deleted_blocks / original_blocks) if original_blocks else 0.0

    report.stats.update(
        {
            "original_blocks": original_blocks,
            "cleaned_nonempty_lines": cleaned_blocks,
            "deleted_blocks": deleted_blocks,
            "deletion_ratio": round(deletion_ratio, 4),
            "chapter_count": chapter_count,
            "nav_title_count": nav_title_count,
            "likely_chapter_count": likely_chapter_count,
            "numbered_heading_count": numbered_heading_count,
            "remaining_toc_blocks": remaining_toc_blocks,
            "remaining_likely_boilerplate_blocks": remaining_boilerplate_blocks,
            "retained_title_heading_count": retained_title_heading_count,
            "retained_closing_marker_count": retained_closing_marker_count,
            "applied_operation_count": len(result.applied_operations),
            "skipped_operation_count": len(result.skipped_operations),
        }
    )

    if original_blocks and deletion_ratio > 0.45:
        add_warning(
            f"High deletion ratio ({deletion_ratio:.1%}); review the diff before accepting."
        )
    elif original_blocks and deletion_ratio > 0.30:
        add_warning(
            f"Moderate deletion ratio ({deletion_ratio:.1%}); spot-check removed sections."
        )

    if original_blocks >= 40 and not chapter_count:
        add_warning("No chapter markers were added for a book-length source.")

    if original_blocks >= 300 and chapter_count == 1:
        add_warning(
            "Only one chapter marker was added for a long source; review heading candidates before accepting."
        )

    if nav_title_count >= 4 and chapter_count < min(3, nav_title_count):
        add_warning(
            f"Only {chapter_count} chapter marker(s) were added despite {nav_title_count} EPUB navigation title(s)."
        )

    expected_chapter_count = max(likely_chapter_count, numbered_heading_count)
    completeness_floor = max(2, int(expected_chapter_count * 0.6))
    if expected_chapter_count >= 4 and chapter_count < completeness_floor:
        add_warning(
            f"Only {chapter_count} chapter marker(s) were added despite "
            f"{expected_chapter_count} likely narrative heading(s).",
            blocking=True,
        )

    if _contains_toc_like_section(result.cleaned_text):
        add_warning("Cleaned text may still contain a table-of-contents-like section.", blocking=True)

    if remaining_toc_blocks >= 4:
        add_warning(
            f"Cleaned text still contains {remaining_toc_blocks} structured TOC/navigation block(s).",
            blocking=True,
        )

    if remaining_boilerplate_blocks >= 3:
        add_warning(
            f"Cleaned text still contains {remaining_boilerplate_blocks} likely boilerplate/license block(s).",
            blocking=True,
        )

    if _contains_boilerplate_like_section(result.cleaned_text):
        add_warning(
            "Cleaned text may still contain Project Gutenberg or license boilerplate.",
            blocking=True,
        )

    if title_heading_blocks and not retained_title_heading_count:
        add_warning("The book title heading appears to have been removed.", blocking=True)

    if closing_marker_blocks and not retained_closing_marker_count:
        add_warning("The narrative closing marker appears to have been removed.", blocking=True)

    if remove_footnotes and _contains_footnote_like_lines(result.cleaned_text):
        add_warning(
            "Footnote-like lines may remain even though footnote removal was requested.",
            blocking=True,
        )

    if result.skipped_operations:
        add_warning(
            f"{len(result.skipped_operations)} cleaning operation(s) were skipped by deterministic guards.",
            blocking=True,
        )

    if not result.cleaned_text.strip():
        report.errors.append("Cleaning produced empty text.")

    return report


def _contains_toc_like_section(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False

    toc_heading_pattern = re.compile(
        r"\b(contents|table of contents|toc|spis tre[śs]ci|indice|índice|inhaltsverzeichnis|sommaire)\b",
        flags=re.IGNORECASE,
    )
    for index, line in enumerate(lines[:120]):
        if not toc_heading_pattern.search(line):
            continue
        following = lines[index + 1 : index + 18]
        if sum(1 for item in following if _looks_like_toc_entry(item)) >= 4:
            return True
    front_lines = lines[:120]
    for index in range(0, max(1, len(front_lines) - 7)):
        window = front_lines[index : index + 8]
        if sum(_looks_like_chapter_heading(item) for item in window) >= 5:
            return True
    return False


def _looks_like_toc_entry(line: str) -> bool:
    stripped = line.strip()
    if re.search(r"\.{3,}\s*\d+$", stripped):
        return True
    if re.search(r"\s+\d{1,4}$", stripped) and len(stripped.split()) <= 12:
        return True
    return False


def _looks_like_chapter_heading(line: str) -> bool:
    return bool(
        re.match(
            r"^((chapter|part|book|section)\s+)?([ivxlcdm]+|\d+)([\.:)\- ]|$)",
            line.strip(),
            flags=re.IGNORECASE,
        )
    )


def _contains_boilerplate_like_section(text: str) -> bool:
    lowered = str(text or "").lower()
    if "the full project gutenberg" in lowered or "start: full license" in lowered:
        return True
    return lowered.count("project gutenberg") >= 3


def _contains_footnote_like_lines(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    matches = 0
    for line in lines:
        if re.match(r"^(\[\d+\]|\d{1,3}[\.)]|[*†‡])\s+\S+", line) and len(line) <= 400:
            matches += 1
    return matches >= 2


def _narrative_title_heading_blocks(document: SourceDocument):
    title_values = {
        _normalize_text(candidate.get("value"))
        for candidate in document.metadata_candidates.get("title", [])
        if isinstance(candidate, dict) and candidate.get("value")
    }
    if not title_values:
        return []
    return [
        block
        for block in document.blocks
        if _normalize_text(block.text) in title_values
        and "heading" in block.role_candidates
        and "toc" not in block.role_candidates
        and "copyright" not in block.role_candidates
    ]


def _narrative_closing_marker_blocks(document: SourceDocument):
    closing_markers = {
        "the end",
        "end",
        "fin",
        "finis",
        "fine",
        "koniec",
        "ende",
    }
    return [
        block
        for block in document.blocks
        if _normalize_text(block.text) in closing_markers
        and "heading" in block.role_candidates
        and "toc" not in block.role_candidates
    ]


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()
