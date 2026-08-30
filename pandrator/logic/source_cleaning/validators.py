from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from .models import CleaningResult, SourceBlock, SourceDocument
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
    cleaned_blocks = len(
        [line for line in result.cleaned_text.splitlines() if line.strip()]
    )
    deleted_blocks = len(result.deleted_block_ids)
    chapter_count = result.report.get("chapter_count", 0)
    nav_title_count = len(document.nav_titles)
    chapter_structure = SourceCleaningTools(document).analyze_chapter_structure(
        max_candidates=1
    )
    likely_chapter_count = int(chapter_structure.get("likely_chapter_count") or 0)
    numbered_heading_count = int(chapter_structure.get("numbered_heading_count") or 0)
    section_heading_count = int(chapter_structure.get("section_heading_count") or 0)
    deleted_ids = set(result.deleted_block_ids)
    retained_ids = [
        block.block_id for block in document.blocks if block.block_id not in deleted_ids
    ]
    cleanup_structure = SourceCleaningTools(document).analyze_cleanup_structure(
        max_candidates=10,
        scope={"block_ids": retained_ids},
    )
    remaining_toc_blocks = int(cleanup_structure.get("toc_block_count") or 0)
    remaining_boilerplate_blocks = int(
        cleanup_structure.get("likely_boilerplate_block_count") or 0
    )
    title_heading_blocks = _narrative_title_heading_blocks(document)
    retained_title_heading_count = sum(
        block.block_id not in deleted_ids for block in title_heading_blocks
    )
    closing_marker_blocks = _narrative_closing_marker_blocks(document)
    retained_closing_marker_count = sum(
        block.block_id not in deleted_ids for block in closing_marker_blocks
    )
    deletion_ratio = (deleted_blocks / original_blocks) if original_blocks else 0.0
    expected_deletion_ids = {
        block.block_id
        for block in document.blocks
        if _is_expected_non_narrative_deletion(block, remove_footnotes=remove_footnotes)
    }
    substantive_original_blocks = max(0, original_blocks - len(expected_deletion_ids))
    substantive_deleted_blocks = len(deleted_ids - expected_deletion_ids)
    substantive_deletion_ratio = (
        substantive_deleted_blocks / substantive_original_blocks
        if substantive_original_blocks
        else 0.0
    )

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
            "section_heading_count": section_heading_count,
            "remaining_toc_blocks": remaining_toc_blocks,
            "remaining_likely_boilerplate_blocks": remaining_boilerplate_blocks,
            "retained_title_heading_count": retained_title_heading_count,
            "retained_closing_marker_count": retained_closing_marker_count,
            "applied_operation_count": len(result.applied_operations),
            "skipped_operation_count": len(result.skipped_operations),
            "substantive_deleted_blocks": substantive_deleted_blocks,
            "substantive_deletion_ratio": round(substantive_deletion_ratio, 4),
        }
    )

    if document.source_type == "pdf_structured":
        page_blocks = Counter(block.page for block in document.blocks if block.page)
        deleted_page_blocks = Counter(
            block.page
            for block in document.blocks
            if block.block_id in deleted_ids and block.page
        )
        fully_deleted_pages = sorted(
            page
            for page, count in page_blocks.items()
            if deleted_page_blocks.get(page, 0) >= count
        )
        expected_fully_deleted_pages = [
            page
            for page in fully_deleted_pages
            if all(
                _is_expected_non_narrative_deletion(
                    block,
                    remove_footnotes=remove_footnotes,
                )
                for block in document.blocks
                if block.page == page
            )
        ]
        relocated_fully_deleted_pages = [
            page
            for page in fully_deleted_pages
            if page not in expected_fully_deleted_pages
            and _page_content_preserved_after_repair(
                [block for block in document.blocks if block.page == page],
                result.cleaned_text,
                remove_footnotes=remove_footnotes,
            )
        ]
        substantive_fully_deleted_pages = [
            page
            for page in fully_deleted_pages
            if page not in expected_fully_deleted_pages
            and page not in relocated_fully_deleted_pages
        ]
        ingestion_pages = document.attributes.get("pdf_ingestion", {}).get("pages", [])
        low_confidence_ocr_pages = [
            int(page.get("page") or 0)
            for page in ingestion_pages
            if page.get("source_method") == "ocr"
            and isinstance(page.get("ocr"), dict)
            and float(page["ocr"].get("mean_confidence") or 0.0) < 0.75
        ]
        report.stats["fully_deleted_pdf_pages"] = fully_deleted_pages
        report.stats["expected_fully_deleted_pdf_pages"] = expected_fully_deleted_pages
        report.stats["relocated_fully_deleted_pdf_pages"] = (
            relocated_fully_deleted_pages
        )
        report.stats["substantive_fully_deleted_pdf_pages"] = (
            substantive_fully_deleted_pages
        )
        report.stats["low_confidence_ocr_pages"] = low_confidence_ocr_pages
        if substantive_fully_deleted_pages:
            add_warning(
                "All substantive extracted text was removed from PDF page(s): "
                f"{substantive_fully_deleted_pages[:12]}.",
                blocking=len(substantive_fully_deleted_pages) >= 2,
            )
        if low_confidence_ocr_pages:
            add_warning(
                f"Low-confidence OCR was retained on PDF page(s): {low_confidence_ocr_pages[:12]}."
            )

    if substantive_original_blocks and substantive_deletion_ratio > 0.45:
        add_warning(
            "High substantive deletion ratio "
            f"({substantive_deletion_ratio:.1%}); review the diff before accepting."
        )
    elif substantive_original_blocks and substantive_deletion_ratio > 0.30:
        add_warning(
            "Moderate substantive deletion ratio "
            f"({substantive_deletion_ratio:.1%}); spot-check removed sections."
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

    if _contains_toc_like_section(
        result.cleaned_text,
        document=document,
        deleted_block_ids=deleted_ids,
    ):
        add_warning(
            "Cleaned text may still contain a table-of-contents-like section.",
            blocking=True,
        )

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
        add_warning(
            "The book title heading appears to have been removed.", blocking=True
        )

    if closing_marker_blocks and not retained_closing_marker_count:
        add_warning(
            "The narrative closing marker appears to have been removed.", blocking=True
        )

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


def _is_expected_non_narrative_deletion(
    block: SourceBlock,
    *,
    remove_footnotes: bool,
) -> bool:
    roles = set(block.role_candidates)
    expected_roles = {
        "boilerplate",
        "copyright",
        "page_number",
        "repeated_marginal",
        "running_header",
        "toc",
    }
    if remove_footnotes:
        expected_roles.update({"footnote", "footnote_candidate"})
    return bool(roles & expected_roles)


def _page_content_preserved_after_repair(
    blocks: list[SourceBlock],
    cleaned_text: str,
    *,
    remove_footnotes: bool,
) -> bool:
    """Recognize page text deliberately moved into reviewed replacement blocks.

    Block IDs cannot survive a replacement that repairs column order or joins a
    page seam.  Eight-word phrases provide stronger evidence than global token
    overlap, while short blocks still require their entire normalized phrase.
    """

    cleaned_words = _validation_words(cleaned_text)
    if not cleaned_words:
        return False
    cleaned = " ".join(cleaned_words)
    phrases: list[str] = []
    for block in blocks:
        if _is_expected_non_narrative_deletion(
            block,
            remove_footnotes=remove_footnotes,
        ):
            continue
        words = _validation_words(block.text)
        if not words:
            continue
        if len(words) <= 8:
            phrases.append(" ".join(words))
            continue
        starts = list(range(0, len(words) - 7, 8))
        final_start = len(words) - 8
        if final_start not in starts:
            starts.append(final_start)
        phrases.extend(" ".join(words[start : start + 8]) for start in starts)
    if not phrases:
        return False
    matched = sum(phrase in cleaned for phrase in phrases)
    return matched / len(phrases) >= 0.75


def _validation_words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+(?:[’'][^\W_]+)?", str(text or "").casefold())


def _contains_toc_like_section(
    text: str,
    *,
    document: SourceDocument | None = None,
    deleted_block_ids: set[str] | None = None,
) -> bool:
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
    front_items: list[Any] = list(lines[:120])
    if document is not None:
        deleted = deleted_block_ids or set()
        front_items = [
            block
            for block in document.blocks
            if block.block_id not in deleted and block.text.strip()
        ][:120]
    for index in range(max(1, len(front_items) - 7)):
        window = front_items[index : index + 8]
        if (
            sum(
                _looks_like_chapter_heading(_toc_candidate_text(item))
                and not _is_non_narrative_toc_candidate(item)
                for item in window
            )
            >= 5
        ):
            return True
    return False


def _toc_candidate_text(item: Any) -> str:
    return str(getattr(item, "text", item) or "")


def _is_non_narrative_toc_candidate(item: Any) -> bool:
    roles = set(getattr(item, "role_candidates", []) or [])
    return bool(
        roles
        & {
            "footnote",
            "deterministic_footnote",
            "page_number",
            "running_header",
            "repeated_marginal",
        }
    )


def _looks_like_toc_entry(line: str) -> bool:
    stripped = line.strip()
    if re.search(r"\.{3,}\s*\d+$", stripped):
        return True
    return bool(re.search(r"\s+\d{1,4}$", stripped) and len(stripped.split()) <= 12)


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
