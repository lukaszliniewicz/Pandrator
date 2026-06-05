from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .models import CleaningResult, SourceDocument


@dataclass
class SourceCleaningValidationReport:
    warnings: list[str] = field(default_factory=list)
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
    original_blocks = len(document.plain_lines())
    cleaned_blocks = len([line for line in result.cleaned_text.splitlines() if line.strip()])
    deleted_blocks = len(result.deleted_block_ids)
    chapter_count = result.report.get("chapter_count", 0)
    nav_title_count = len(document.nav_titles)
    deletion_ratio = (deleted_blocks / original_blocks) if original_blocks else 0.0

    report.stats.update(
        {
            "original_blocks": original_blocks,
            "cleaned_nonempty_lines": cleaned_blocks,
            "deleted_blocks": deleted_blocks,
            "deletion_ratio": round(deletion_ratio, 4),
            "chapter_count": chapter_count,
            "nav_title_count": nav_title_count,
            "applied_operation_count": len(result.applied_operations),
            "skipped_operation_count": len(result.skipped_operations),
        }
    )

    if original_blocks and deletion_ratio > 0.45:
        report.warnings.append(
            f"High deletion ratio ({deletion_ratio:.1%}); review the diff before accepting."
        )
    elif original_blocks and deletion_ratio > 0.30:
        report.warnings.append(
            f"Moderate deletion ratio ({deletion_ratio:.1%}); spot-check removed sections."
        )

    if original_blocks >= 40 and not chapter_count:
        report.warnings.append("No chapter markers were added for a book-length source.")

    if original_blocks >= 300 and chapter_count == 1:
        report.warnings.append(
            "Only one chapter marker was added for a long source; review heading candidates before accepting."
        )

    if nav_title_count >= 4 and chapter_count < min(3, nav_title_count):
        report.warnings.append(
            f"Only {chapter_count} chapter marker(s) were added despite {nav_title_count} EPUB navigation title(s)."
        )

    if _contains_toc_like_section(result.cleaned_text):
        report.warnings.append("Cleaned text may still contain a table-of-contents-like section.")

    if remove_footnotes and _contains_footnote_like_lines(result.cleaned_text):
        report.warnings.append("Footnote-like lines may remain even though footnote removal was requested.")

    if result.skipped_operations:
        report.warnings.append(
            f"{len(result.skipped_operations)} cleaning operation(s) were skipped by deterministic guards."
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
    return False


def _looks_like_toc_entry(line: str) -> bool:
    stripped = line.strip()
    if re.search(r"\.{3,}\s*\d+$", stripped):
        return True
    if re.search(r"\s+\d{1,4}$", stripped) and len(stripped.split()) <= 12:
        return True
    return False


def _contains_footnote_like_lines(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    matches = 0
    for line in lines:
        if re.match(r"^(\[\d+\]|\d{1,3}[\.)]|[*†‡])\s+\S+", line) and len(line) <= 400:
            matches += 1
    return matches >= 2
