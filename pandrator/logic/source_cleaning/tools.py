from __future__ import annotations

import os
import re
from collections import defaultdict
from typing import Any

from .models import SearchHit, SourceBlock, SourceDocument


class SourceCleaningTools:
    """Deterministic inspection helpers intended for an LLM tool loop."""

    def __init__(self, document: SourceDocument):
        self.document = document
        self._last_hits: dict[str, SearchHit] = {}

    def search(
        self,
        query: str,
        mode: str = "plain",
        case_sensitive: bool = False,
        scope: dict[str, Any] | None = None,
        max_hits: int = 50,
    ) -> list[dict[str, Any]]:
        if mode == "regex":
            return self.regex_search(
                query,
                flags="" if case_sensitive else "i",
                scope=scope,
                max_hits=max_hits,
            )

        terms = _parse_plain_query(query)
        if not terms:
            return []

        hits: list[SearchHit] = []
        for block in self._scoped_blocks(scope):
            haystack = block.text if case_sensitive else block.text.lower()
            matched_term = ""
            for term in terms:
                needle = term if case_sensitive else term.lower()
                if needle and needle in haystack:
                    matched_term = term
                    break
            if not matched_term:
                continue
            hits.append(self._make_hit(block, matched_term, len(hits) + 1))
            if len(hits) >= max_hits:
                break

        return self._store_hits(hits)

    def regex_search(
        self,
        pattern: str,
        flags: str = "i",
        scope: dict[str, Any] | None = None,
        max_hits: int = 50,
    ) -> list[dict[str, Any]]:
        re_flags = re.MULTILINE
        if "i" in str(flags or "").lower():
            re_flags |= re.IGNORECASE
        if "s" in str(flags or "").lower():
            re_flags |= re.DOTALL

        compiled = re.compile(pattern, re_flags)
        hits: list[SearchHit] = []
        for block in self._scoped_blocks(scope):
            match = compiled.search(block.text)
            if not match:
                continue
            hits.append(self._make_hit(block, match.group(0), len(hits) + 1))
            if len(hits) >= max_hits:
                break

        return self._store_hits(hits)

    def preview(
        self,
        start_line: int | None = None,
        end_line: int | None = None,
        before: int | None = None,
        after: int | None = None,
        around_hit_id: str | None = None,
    ) -> dict[str, Any]:
        if around_hit_id:
            hit = self._last_hits.get(str(around_hit_id))
            if hit is None:
                return {"blocks": [], "warning": f"Unknown hit id: {around_hit_id}"}
            anchor_start = hit.line_start
            anchor_end = hit.line_end
            start = anchor_start - max(0, int(before if before is not None else 5))
            end = anchor_end + max(0, int(after if after is not None else 5))
        elif start_line is not None or end_line is not None:
            start = int(start_line if start_line is not None else end_line)
            end = int(end_line if end_line is not None else start_line)
        elif before is not None:
            start = 1
            end = int(before)
        elif after is not None:
            start = int(after)
            end = max((block.line_end for block in self.document.blocks), default=start)
        else:
            start = 1
            end = min(20, max((block.line_end for block in self.document.blocks), default=1))

        start = max(1, start)
        end = max(start, end)
        blocks = self.document.blocks_in_line_range(start, end)
        return {
            "start_line": start,
            "end_line": end,
            "blocks": [self._preview_block(block) for block in blocks],
        }

    def inspect_block(self, block_id: str) -> dict[str, Any]:
        block = self.document.block_by_id(block_id)
        if block is None:
            return {"error": f"Block not found: {block_id}"}
        return block.to_dict()

    def get_epub_markup_for_text(
        self,
        text: str,
        occurrence: int = 1,
        context_blocks: int = 2,
    ) -> dict[str, Any]:
        if self.document.source_type != "epub":
            return {"error": "Raw markup lookup is only available for EPUB sources."}

        needle = str(text or "").strip().lower()
        if not needle:
            return {"matches": []}

        matches: list[SourceBlock] = []
        for block in self.document.blocks:
            if needle in block.text.lower():
                matches.append(block)

        if not matches:
            return {"matches": []}

        index = max(0, min(len(matches) - 1, int(occurrence) - 1))
        target = matches[index]
        all_blocks = self.document.blocks
        target_index = all_blocks.index(target)
        start = max(0, target_index - max(0, int(context_blocks)))
        end = min(len(all_blocks), target_index + max(0, int(context_blocks)) + 1)
        return {
            "match_count": len(matches),
            "selected_occurrence": index + 1,
            "target": target.to_dict(),
            "context": [block.to_dict() for block in all_blocks[start:end]],
        }

    def list_repeated_lines(self, min_repeats: int = 3, max_length: int = 120) -> list[dict[str, Any]]:
        grouped: dict[str, list[SourceBlock]] = defaultdict(list)
        for block in self.document.blocks:
            normalized = _normalize_for_repeat_detection(block.text)
            if not normalized or len(normalized) > max_length:
                continue
            grouped[normalized].append(block)

        repeated: list[dict[str, Any]] = []
        for normalized, blocks in grouped.items():
            if len(blocks) < min_repeats:
                continue
            repeated.append(
                {
                    "text": blocks[0].text,
                    "normalized": normalized,
                    "count": len(blocks),
                    "line_numbers": [block.line_start for block in blocks],
                    "pages": _dedupe_values(block.page for block in blocks if block.page is not None),
                    "hrefs": _dedupe_values(block.href for block in blocks if block.href),
                    "block_ids": [block.block_id for block in blocks],
                }
            )

        return sorted(repeated, key=lambda item: (-int(item["count"]), str(item["text"]).lower()))

    def find_heading_candidates(self, max_candidates: int = 100) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        nav_titles = {title.lower() for title in self.document.nav_titles}
        for block in self.document.blocks:
            score = 0.0
            reasons: list[str] = []
            text = block.text.strip()
            if not text:
                continue
            word_count = len(text.split())
            if block.tag and block.tag.lower() in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                score += 0.45
                reasons.append("heading_tag")
            if "heading_candidate" in block.role_candidates:
                score += 0.15
                reasons.append("short_isolated_text")
            if text.lower() in nav_titles:
                score += 0.35
                reasons.append("epub_nav_title")
            if _looks_like_numbered_heading(text):
                score += 0.25
                reasons.append("numbered_or_roman_heading")
            if 1 <= word_count <= 12 and len(text) <= 100:
                score += 0.15
                reasons.append("brief")
            if text.endswith((".", "!", "?")) and word_count > 4:
                score -= 0.25
                reasons.append("sentence_like")
            if score <= 0:
                continue
            candidates.append(
                {
                    "block_id": block.block_id,
                    "line": block.line_start,
                    "text": text,
                    "score": round(score, 3),
                    "reasons": reasons,
                    "href": block.href,
                    "page": block.page,
                    "tag": block.tag,
                    "classes": block.classes,
                    "element_id": block.element_id,
                }
            )

        candidates.sort(key=lambda item: (-float(item["score"]), int(item["line"])))
        return candidates[:max_candidates]

    def find_footnote_candidates(self, max_candidates: int = 100) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for block in self.document.blocks:
            reasons: list[str] = []
            evidence = " ".join(
                [
                    block.tag or "",
                    block.href or "",
                    block.element_id or "",
                    " ".join(block.classes),
                    " ".join(block.role_candidates),
                    block.dom_path or "",
                ]
            ).lower()
            if any(token in evidence for token in ("footnote", "endnote", "noteref", "note-ref")):
                reasons.append("markup_or_role_mentions_note")
            if block.tag == "aside":
                reasons.append("aside_tag")
            if re.match(r"^(\[\d+\]|\d{1,3}[\.)]|[*†‡])\s+\S+", block.text.strip()):
                reasons.append("note_marker_prefix")
            if len(block.text) <= 350 and reasons:
                candidates.append(
                    {
                        "block_id": block.block_id,
                        "line": block.line_start,
                        "text": block.text,
                        "reasons": reasons,
                        "href": block.href,
                        "page": block.page,
                        "tag": block.tag,
                        "classes": block.classes,
                        "element_id": block.element_id,
                    }
                )

        return candidates[:max_candidates]

    def find_metadata_candidates(self) -> dict[str, Any]:
        return {
            "filename": os.path.basename(self.document.filename),
            "language": self.document.language,
            "metadata_candidates": self.document.metadata_candidates,
            "front_matter_preview": [
                self._preview_block(block)
                for block in self.document.blocks[:25]
            ],
        }

    def _scoped_blocks(self, scope: dict[str, Any] | None) -> list[SourceBlock]:
        if not scope:
            return self.document.blocks

        blocks = self.document.blocks
        start_line = scope.get("start_line")
        end_line = scope.get("end_line")
        if start_line is not None or end_line is not None:
            start = int(start_line if start_line is not None else 1)
            end = int(end_line if end_line is not None else max(block.line_end for block in blocks))
            blocks = self.document.blocks_in_line_range(start, end)

        href = scope.get("href")
        if href:
            blocks = [block for block in blocks if block.href == href]

        page = scope.get("page")
        if page is not None:
            blocks = [block for block in blocks if block.page == int(page)]

        block_ids = set(scope.get("block_ids") or [])
        if block_ids:
            blocks = [block for block in blocks if block.block_id in block_ids]

        return blocks

    def _make_hit(self, block: SourceBlock, match_text: str, number: int) -> SearchHit:
        return SearchHit(
            hit_id=f"hit:{number}",
            block_id=block.block_id,
            line_start=block.line_start,
            line_end=block.line_end,
            snippet=_snippet(block.text, match_text),
            match_text=match_text,
            href=block.href,
            page=block.page,
        )

    def _store_hits(self, hits: list[SearchHit]) -> list[dict[str, Any]]:
        self._last_hits = {hit.hit_id: hit for hit in hits}
        return [hit.to_dict() for hit in hits]

    @staticmethod
    def _preview_block(block: SourceBlock) -> dict[str, Any]:
        return {
            "block_id": block.block_id,
            "line_start": block.line_start,
            "line_end": block.line_end,
            "text": block.text,
            "href": block.href,
            "page": block.page,
            "tag": block.tag,
            "classes": block.classes,
            "element_id": block.element_id,
            "role_candidates": block.role_candidates,
        }


def _parse_plain_query(query: str) -> list[str]:
    raw = str(query or "").strip()
    if not raw:
        return []
    parts = re.split(r"\s+\bOR\b\s+", raw, flags=re.IGNORECASE)
    terms: list[str] = []
    for part in parts:
        term = part.strip().strip("\"'")
        if term:
            terms.append(term)
    return terms


def _normalize_for_repeat_detection(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return normalized


def _snippet(text: str, match_text: str, radius: int = 80) -> str:
    lowered = text.lower()
    match_lowered = str(match_text or "").lower()
    index = lowered.find(match_lowered)
    if index < 0:
        return text[: radius * 2]
    start = max(0, index - radius)
    end = min(len(text), index + len(match_text) + radius)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


def _looks_like_numbered_heading(text: str) -> bool:
    stripped = text.strip()
    return bool(
        re.match(
            r"^((chapter|part|book|section)\s+)?([ivxlcdm]+|\d+|[一二三四五六七八九十百千]+)([\.:)\- ]|$)",
            stripped,
            flags=re.IGNORECASE,
        )
    )


def _dedupe_values(values) -> list[Any]:
    deduped: list[Any] = []
    seen: set[Any] = set()
    for value in values:
        if value in seen:
            continue
        deduped.append(value)
        seen.add(value)
    return deduped
