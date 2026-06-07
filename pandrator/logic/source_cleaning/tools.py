from __future__ import annotations

import os
import posixpath
import re
from collections import Counter, defaultdict
from typing import Any
from urllib.parse import unquote

from .models import SearchHit, SourceBlock, SourceDocument
from .selectors import blocks_matching_selector, selector_supported_keys


NUMBERED_HEADING_PATTERN = (
    r"^((?:chapter|ch|rozdział|rozdz|chapitre|chap|kapitel|kap|capítulo|cap|"
    r"secção|seção|hoofdstuk|hst|fejezet|fej|kapitull|part|section|sectie|część|cz|"
    r"partie|teil|abschnitt|parte|sección|rész|szakasz|pjesë|seksion|volume|vol|tom|t|"
    r"band|bd|buch|libro|livro|boek|kötet|köt|könyv|vëllim|vël|libër|prologue|epilogue|"
    r"prolog|epilog|épilogue|prólogo|epílogo|proloog|epiloog|prologus|epilogus|księga|"
    r"księgi|wstęp|posłowie|livre|tome|préface|avant-propos|vorwort|nachwort|prefacio|"
    r"introducción|prefácio|introdução|posfácio|deel|inleiding|nawoord|bevezetés|"
    r"előszó|utószó|parathënie|pasthënie)\s+)?"
    r"([ivxlcdm]+|\d{1,3}|[\u4e00-\u4e5d\u5341\u767e\u5343]+)"
    r"([\.:)\- ]|$)"
)


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
        max_blocks: int = 16,
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
        matches = self.document.blocks_in_line_range(start, end)
        limit = min(30, max(1, int(max_blocks)))
        blocks = _representative_blocks(matches, limit)
        return {
            "start_line": start,
            "end_line": end,
            "matched_blocks": len(matches),
            "returned_blocks": len(blocks),
            "tag_counts": dict(Counter(str(block.tag or "<none>") for block in matches).most_common(12)),
            "role_counts": dict(
                Counter(role for block in matches for role in block.role_candidates).most_common(12)
            ),
            "class_counts": dict(
                Counter(class_name for block in matches for class_name in block.classes).most_common(12)
            ),
            "hrefs": _dedupe_values(block.href for block in matches if block.href),
            "blocks": [self._preview_block(block) for block in blocks],
            "truncated": len(matches) > len(blocks),
        }

    def inspect_block(self, block_id: str) -> dict[str, Any]:
        block = self.document.block_by_id(block_id)
        if block is None:
            return {"error": f"Block not found: {block_id}"}
        return block.to_dict()

    def inspect_document_structure(
        self,
        max_documents: int = 30,
        scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Returns a compact, language-agnostic inventory of the parsed source."""
        scoped_blocks = self._scoped_blocks(scope)
        grouped: dict[str, list[SourceBlock]] = defaultdict(list)
        for block in scoped_blocks:
            grouped[str(block.href or "<no-href>")].append(block)

        documents: list[dict[str, Any]] = []
        for href, blocks in grouped.items():
            tag_counts = Counter(str(block.tag or "<none>") for block in blocks)
            role_counts = Counter(role for block in blocks for role in block.role_candidates)
            class_counts = Counter(class_name for block in blocks for class_name in block.classes)
            documents.append(
                {
                    "href": href,
                    "block_count": len(blocks),
                    "text_chars": sum(len(block.text) for block in blocks),
                    "first_line": blocks[0].line_start,
                    "last_line": blocks[-1].line_end,
                    "tag_counts": dict(tag_counts.most_common(12)),
                    "role_counts": dict(role_counts.most_common(12)),
                    "class_counts": dict(class_counts.most_common(12)),
                    "first_texts": [_short_text(block.text, max_chars=120) for block in blocks[:2]],
                    "last_texts": [_short_text(block.text, max_chars=120) for block in blocks[-2:]],
                }
            )

        documents.sort(key=lambda item: int(item["first_line"]))
        global_tags = Counter(str(block.tag or "<none>") for block in scoped_blocks)
        global_roles = Counter(role for block in scoped_blocks for role in block.role_candidates)
        global_classes = Counter(class_name for block in scoped_blocks for class_name in block.classes)
        heading_tag_count = sum(
            count
            for tag, count in global_tags.items()
            if tag.lower() in {"h1", "h2", "h3", "h4", "h5", "h6"}
        )
        navigation_entries = self.document.navigation_entries
        navigation_with_fragments = sum(bool(entry.get("fragment")) for entry in navigation_entries)
        diagnostics: list[str] = []
        if self.document.source_type.startswith("epub") and not heading_tag_count:
            diagnostics.append(
                "No semantic h1-h6 headings were parsed; inspect navigation, text patterns, nearby ranges, and raw markup."
            )
        if navigation_entries and not navigation_with_fragments:
            diagnostics.append(
                "Navigation entries do not contain fragment targets; use them as section hints, not exact block locations."
            )
        diagnostics.extend(self.document.warnings)

        return {
            "source_type": self.document.source_type,
            "filename": self.document.filename,
            "block_count": len(scoped_blocks),
            "text_chars": sum(len(block.text) for block in scoped_blocks),
            "document_count": len(documents),
            "navigation_entry_count": len(navigation_entries),
            "navigation_entries_with_fragments": navigation_with_fragments,
            "heading_tag_count": heading_tag_count,
            "global_tag_counts": dict(global_tags.most_common(20)),
            "global_role_counts": dict(global_roles.most_common(20)),
            "global_class_counts": dict(global_classes.most_common(20)),
            "documents": documents[: max(1, int(max_documents))],
            "documents_truncated": len(documents) > max(1, int(max_documents)),
            "diagnostics": diagnostics,
        }

    def inspect_navigation(
        self,
        max_entries: int = 80,
        max_matches_per_entry: int = 5,
    ) -> dict[str, Any]:
        """Maps navigation labels and targets to likely parsed blocks."""
        entries = self.document.navigation_entries or [
            {"order": index, "depth": 0, "title": title, "href": "", "href_path": "", "fragment": ""}
            for index, title in enumerate(self.document.nav_titles, start=1)
        ]
        normalized_blocks: list[tuple[SourceBlock, str]] = [
            (block, _normalize_for_navigation(block.text))
            for block in self.document.blocks
        ]
        blocks_by_title: dict[str, list[SourceBlock]] = defaultdict(list)
        blocks_by_href: dict[str, list[SourceBlock]] = defaultdict(list)
        blocks_by_element_id: dict[str, list[SourceBlock]] = defaultdict(list)
        normalized_by_block_id: dict[str, str] = {}
        for block, normalized in normalized_blocks:
            normalized_by_block_id[block.block_id] = normalized
            if normalized:
                blocks_by_title[normalized].append(block)
            normalized_href = _normalize_href_path(block.href)
            if normalized_href:
                blocks_by_href[normalized_href].append(block)
            normalized_element_id = unquote(str(block.element_id or ""))
            if normalized_element_id:
                blocks_by_element_id[normalized_element_id].append(block)

        rows: list[dict[str, Any]] = []
        for entry in entries[: max(1, int(max_entries))]:
            title = str(entry.get("title") or "")
            normalized_title = _normalize_for_navigation(title)
            href_path = _normalize_href_path(
                entry.get("href_path") or str(entry.get("href") or "").split("#", 1)[0]
            )
            fragment = unquote(str(entry.get("fragment") or ""))
            exact_matches = list(blocks_by_title.get(normalized_title, []))
            fragment_matches = list(blocks_by_element_id.get(fragment, []))
            same_document = list(blocks_by_href.get(href_path, []))
            title_prefix_matches = [
                block
                for block in same_document
                if _navigation_titles_overlap(
                    normalized_title,
                    normalized_by_block_id.get(block.block_id, ""),
                )
            ]
            numbered_matches: list[SourceBlock] = []
            number_match = re.search(r"\b(\d+|[ivxlcdm]+)\b", title, flags=re.IGNORECASE)
            if number_match and same_document:
                number = re.escape(number_match.group(1))
                numbered_matches = [
                    block
                    for block in same_document
                    if re.match(rf"^[\s\u200b]*{number}[\s.():\-]", block.text, flags=re.IGNORECASE)
                ]
            candidates = _dedupe_blocks(
                fragment_matches + exact_matches + title_prefix_matches + numbered_matches
            )
            rows.append(
                {
                    **dict(entry),
                    "same_document_block_count": len(same_document),
                    "match_count": len(candidates),
                    "matches": [
                        {
                            **self._preview_block(block),
                            "match_reasons": _navigation_match_reasons(
                                block,
                                normalized_title,
                                href_path,
                                fragment,
                                exact_matches,
                                title_prefix_matches,
                                numbered_matches,
                            ),
                        }
                        for block in candidates[: max(1, int(max_matches_per_entry))]
                    ],
                    "matches_truncated": len(candidates) > max(1, int(max_matches_per_entry)),
                }
            )

        entries_with_matches = sum(bool(row["match_count"]) for row in rows)
        return {
            "navigation_entry_count": len(entries),
            "returned_entry_count": len(rows),
            "returned_entries_with_matches": entries_with_matches,
            "returned_entries_without_matches": len(rows) - entries_with_matches,
            "entries": rows,
            "entries_truncated": len(entries) > max(1, int(max_entries)),
            "guidance": (
                "Navigation is evidence, not ground truth. Preview candidate ranges and markup before marking or deleting."
            ),
        }

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

    def preview_raw_markup_range(
        self,
        start_line: int,
        end_line: int,
        max_blocks: int = 30,
    ) -> dict[str, Any]:
        if self.document.source_type != "epub":
            return {"error": "Raw markup preview is only available for EPUB sources."}

        blocks = self.document.blocks_in_line_range(int(start_line), int(end_line))
        return {
            "start_line": int(start_line),
            "end_line": int(end_line),
            "matched_blocks": len(blocks),
            "blocks": [self._raw_markup_block(block) for block in blocks[:max(1, int(max_blocks))]],
            "truncated": len(blocks) > max(1, int(max_blocks)),
        }

    def list_epub_selectors(
        self,
        min_count: int = 2,
        max_items: int = 80,
    ) -> dict[str, Any]:
        grouped: dict[str, dict[str, list[SourceBlock]]] = {
            "href": defaultdict(list),
            "tag": defaultdict(list),
            "class": defaultdict(list),
            "element_id": defaultdict(list),
            "role": defaultdict(list),
        }

        for block in self.document.blocks:
            if block.href:
                grouped["href"][block.href].append(block)
            if block.tag:
                grouped["tag"][block.tag].append(block)
            if block.element_id:
                grouped["element_id"][block.element_id].append(block)
            for class_name in block.classes:
                grouped["class"][class_name].append(block)
            for role in block.role_candidates:
                grouped["role"][role].append(block)

        return {
            "supported_selector_keys": selector_supported_keys(),
            "selectors": {
                kind: self._selector_group_rows(kind, values, min_count, max_items)
                for kind, values in grouped.items()
            },
        }

    def preview_selector(
        self,
        selector: dict[str, Any],
        max_blocks: int = 30,
        include_raw_markup: bool = False,
    ) -> dict[str, Any]:
        matches = blocks_matching_selector(self.document.blocks, selector)
        requested_limit = max(1, int(max_blocks))
        effective_limit = min(requested_limit, 12 if include_raw_markup else 30)
        blocks = _representative_blocks(matches, effective_limit)
        return {
            "selector": selector,
            "matched_blocks": len(matches),
            "returned_blocks": len(blocks),
            "requested_max_blocks": requested_limit,
            "first_line": matches[0].line_start if matches else None,
            "last_line": matches[-1].line_end if matches else None,
            "hrefs": _dedupe_values(block.href for block in matches if block.href),
            "tags": _dedupe_values(block.tag for block in matches if block.tag),
            "classes": _dedupe_values(class_name for block in matches for class_name in block.classes),
            "roles": _dedupe_values(role for block in matches for role in block.role_candidates),
            "blocks": [
                self._raw_markup_block(block) if include_raw_markup else self._preview_block(block)
                for block in blocks
            ],
            "truncated": len(matches) > len(blocks),
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

    def analyze_chapter_structure(self, max_candidates: int = 60) -> dict[str, Any]:
        """Summarizes likely chapter headings and reusable selectors."""
        heading_candidates = self.find_heading_candidates(max_candidates=max(1, len(self.document.blocks)))
        normalized_nav_titles = {
            _normalize_for_repeat_detection(title)
            for title in self.document.nav_titles
            if str(title or "").strip()
        }
        likely_chapters: list[dict[str, Any]] = []
        numbered_blocks: list[SourceBlock] = []

        for candidate in heading_candidates:
            block = self.document.block_by_id(str(candidate.get("block_id") or ""))
            if block is None or _is_non_chapter_block(block):
                continue

            text = block.text.strip()
            numbered = _looks_like_numbered_heading(text)
            nav_match = _normalize_for_repeat_detection(text) in normalized_nav_titles
            explicit_heading = "heading" in block.role_candidates
            if numbered and (explicit_heading or self.document.source_type != "epub"):
                evidence = "numbered_heading"
                numbered_blocks.append(block)
            elif nav_match and explicit_heading and not _looks_like_boilerplate_heading(text):
                evidence = "epub_nav_heading"
            else:
                continue

            item = dict(candidate)
            item["chapter_evidence"] = evidence
            likely_chapters.append(item)

        likely_chapters.sort(key=lambda item: int(item["line"]))
        selector_suggestions = self._chapter_selector_suggestions(numbered_blocks)
        limit = max(1, int(max_candidates))
        return {
            "nav_title_count": len(self.document.nav_titles),
            "heading_candidate_count": len(heading_candidates),
            "likely_chapter_count": len(likely_chapters),
            "numbered_heading_count": len(numbered_blocks),
            "likely_chapters": likely_chapters[:limit],
            "likely_chapters_truncated": len(likely_chapters) > limit,
            "selector_suggestions": selector_suggestions,
            "guidance": (
                "Preview a suggested selector, then use mark_chapters_by_selector when it matches "
                "the complete narrative heading set without TOC/nav entries."
            ),
        }

    def analyze_cleanup_structure(
        self,
        max_candidates: int = 20,
        scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Summarizes structured TOC and boilerplate groups that merit inspection."""
        blocks = self._scoped_blocks(scope)
        toc_blocks = [block for block in blocks if "toc" in block.role_candidates]
        copyright_blocks = [block for block in blocks if "copyright" in block.role_candidates]
        grouped_hrefs: dict[str, list[SourceBlock]] = defaultdict(list)
        grouped_classes: dict[str, list[SourceBlock]] = defaultdict(list)
        for block in blocks:
            if block.href:
                grouped_hrefs[block.href].append(block)
            for class_name in block.classes:
                grouped_classes[class_name].append(block)

        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add_candidate(selector: dict[str, Any], matched: list[SourceBlock], reasons: list[str]):
            if len(matched) < 2:
                return
            key = repr(sorted(selector.items()))
            if key in seen:
                return
            seen.add(key)
            candidates.append(
                {
                    "selector": selector,
                    "matched_blocks": len(matched),
                    "first_line": matched[0].line_start,
                    "last_line": matched[-1].line_end,
                    "reasons": reasons,
                    "sample_texts": [_short_text(block.text) for block in matched[:5]],
                }
            )

        if len(toc_blocks) >= 4:
            add_candidate({"role": "toc"}, toc_blocks, ["structured_toc_or_navigation"])

        for class_name, class_blocks in grouped_classes.items():
            class_toc_count = sum("toc" in block.role_candidates for block in class_blocks)
            if class_toc_count >= 4 and class_toc_count >= len(class_blocks) * 0.7:
                add_candidate(
                    {"class": class_name},
                    class_blocks,
                    ["class_is_predominantly_toc"],
                )

        strong_boilerplate_blocks: set[str] = set()
        for href, href_blocks in grouped_hrefs.items():
            href_toc_count = sum("toc" in block.role_candidates for block in href_blocks)
            href_copyright_count = sum("copyright" in block.role_candidates for block in href_blocks)
            href_strong_markers = sum(_has_strong_boilerplate_marker(block.text) for block in href_blocks)
            strong_marker_ratio = href_strong_markers / len(href_blocks)
            reasons: list[str] = []
            if href_toc_count >= 4 and href_toc_count >= len(href_blocks) * 0.7:
                reasons.append("document_is_predominantly_toc")
            if href_strong_markers >= 2 and strong_marker_ratio >= 0.1:
                reasons.append("document_contains_repeated_license_or_boilerplate_markers")
            if href_copyright_count >= 3 and href_strong_markers and strong_marker_ratio >= 0.05:
                reasons.append("document_contains_many_copyright_blocks")
            if reasons:
                add_candidate({"href": href}, href_blocks, reasons)
                if any("license" in reason or "copyright" in reason or "boilerplate" in reason for reason in reasons):
                    strong_boilerplate_blocks.update(block.block_id for block in href_blocks)

        candidates.sort(key=lambda item: (-int(item["matched_blocks"]), int(item["first_line"])))
        return {
            "scoped_block_count": len(blocks),
            "toc_block_count": len(toc_blocks),
            "copyright_block_count": len(copyright_blocks),
            "likely_boilerplate_block_count": len(strong_boilerplate_blocks),
            "candidate_groups": candidates[: max(1, int(max_candidates))],
            "candidate_groups_truncated": len(candidates) > max(1, int(max_candidates)),
            "guidance": (
                "Preview each candidate group. Remove every confirmed TOC variant and the complete "
                "boilerplate/license section, not only its heading."
            ),
        }

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
        if "block_ids" in scope:
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
            "text": _short_text(block.text, max_chars=500),
            "href": block.href,
            "page": block.page,
            "tag": block.tag,
            "classes": block.classes,
            "element_id": block.element_id,
            "role_candidates": block.role_candidates,
        }

    @staticmethod
    def _raw_markup_block(block: SourceBlock) -> dict[str, Any]:
        payload = SourceCleaningTools._preview_block(block)
        payload["raw_markup"] = block.raw_markup
        payload["dom_path"] = block.dom_path
        payload["attributes"] = block.attributes
        return payload

    @staticmethod
    def _selector_group_rows(
        kind: str,
        values: dict[str, list[SourceBlock]],
        min_count: int,
        max_items: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for value, blocks in values.items():
            if len(blocks) < min_count:
                continue
            selector_key = "class" if kind == "class" else kind
            rows.append(
                {
                    "selector": {selector_key: value},
                    "value": value,
                    "count": len(blocks),
                    "first_line": blocks[0].line_start,
                    "last_line": blocks[-1].line_end,
                    "sample_texts": [block.text for block in blocks[:5]],
                    "hrefs": _dedupe_values(block.href for block in blocks if block.href),
                }
            )

        rows.sort(key=lambda item: (-int(item["count"]), str(item["value"]).lower()))
        return rows[:max(1, int(max_items))]

    def _chapter_selector_suggestions(self, numbered_blocks: list[SourceBlock]) -> list[dict[str, Any]]:
        if len(numbered_blocks) < 2:
            return []

        selectors: list[dict[str, Any]] = []
        if sum(1 for block in numbered_blocks if "heading" in block.role_candidates) >= 2:
            selectors.append({"role": "heading", "text_regex": NUMBERED_HEADING_PATTERN})

        grouped_tags: dict[str, list[SourceBlock]] = defaultdict(list)
        grouped_classes: dict[str, list[SourceBlock]] = defaultdict(list)
        grouped_id_patterns: dict[str, list[SourceBlock]] = defaultdict(list)
        for block in numbered_blocks:
            if block.tag:
                grouped_tags[block.tag].append(block)
            for class_name in block.classes:
                grouped_classes[class_name].append(block)
            if block.element_id:
                grouped_id_patterns[_numeric_id_pattern(block.element_id)].append(block)

        for tag, blocks in grouped_tags.items():
            if len(blocks) >= 2:
                selectors.append({"tag": tag, "text_regex": NUMBERED_HEADING_PATTERN})
        for class_name, blocks in grouped_classes.items():
            if len(blocks) >= 2:
                selectors.append({"class": class_name, "text_regex": NUMBERED_HEADING_PATTERN})
        for id_pattern, blocks in grouped_id_patterns.items():
            if len(blocks) >= 2:
                selectors.append({"element_id_regex": id_pattern, "text_regex": NUMBERED_HEADING_PATTERN})

        suggestions: list[dict[str, Any]] = []
        seen: set[str] = set()
        numbered_ids = {block.block_id for block in numbered_blocks}
        for selector in selectors:
            key = repr(sorted(selector.items()))
            if key in seen:
                continue
            seen.add(key)
            matches = blocks_matching_selector(self.document.blocks, selector)
            likely_matches = [block for block in matches if block.block_id in numbered_ids]
            suggestions.append(
                {
                    "selector": selector,
                    "matched_blocks": len(matches),
                    "likely_chapter_matches": len(likely_matches),
                    "sample_texts": [block.text for block in matches[:5]],
                }
            )

        suggestions.sort(
            key=lambda item: (
                -int(item["likely_chapter_matches"]),
                int(item["matched_blocks"]) - int(item["likely_chapter_matches"]),
            )
        )
        return suggestions[:10]


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


def _normalize_for_navigation(text: str) -> str:
    normalized = str(text or "").replace("\u200b", " ").casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def _normalize_href_path(value: Any) -> str:
    normalized = unquote(str(value or "").split("#", 1)[0]).replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return posixpath.normpath(normalized) if normalized else ""


def _navigation_titles_overlap(nav_title: str, block_title: str) -> bool:
    if not nav_title or not block_title or nav_title == block_title:
        return False
    shorter = min(len(nav_title), len(block_title))
    if shorter < 8:
        return False
    return nav_title.startswith(block_title + " ") or block_title.startswith(nav_title + " ")


def _dedupe_blocks(blocks: list[SourceBlock]) -> list[SourceBlock]:
    deduped: list[SourceBlock] = []
    seen: set[str] = set()
    for block in blocks:
        if block.block_id in seen:
            continue
        deduped.append(block)
        seen.add(block.block_id)
    return deduped


def _navigation_match_reasons(
    block: SourceBlock,
    normalized_title: str,
    href_path: str,
    fragment: str,
    exact_matches: list[SourceBlock],
    title_prefix_matches: list[SourceBlock],
    numbered_matches: list[SourceBlock],
) -> list[str]:
    reasons: list[str] = []
    if fragment and unquote(str(block.element_id or "")) == fragment:
        reasons.append("fragment_target")
    if normalized_title and block in exact_matches:
        reasons.append("normalized_title_match")
    if block in title_prefix_matches:
        reasons.append("normalized_title_prefix_match")
    if block in numbered_matches:
        reasons.append("numbered_prefix_in_target_document")
    if href_path and _normalize_href_path(block.href) == href_path:
        reasons.append("same_document")
    return reasons


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


def _short_text(text: str, max_chars: int = 180) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3] + "..."


def _looks_like_numbered_heading(text: str) -> bool:
    stripped = text.strip()
    if re.match(NUMBERED_HEADING_PATTERN, stripped, flags=re.IGNORECASE):
        return True
    return bool(
        re.match(
            r"^(?:プロローグ|エピローグ|楔子|前言|序幕|尾声|尾聲|序|跋|序章|終章|まえがき|あとがき|后记|後記|引言|绪论|緒論)$",
            stripped,
            flags=re.IGNORECASE,
        )
    )


def _is_non_chapter_block(block: SourceBlock) -> bool:
    excluded_roles = {
        "toc",
        "copyright",
        "page_number",
        "caption",
        "image_alt",
        "footnote",
        "footnote_candidate",
        "side_note",
    }
    return bool(excluded_roles.intersection(block.role_candidates))


def _looks_like_boilerplate_heading(text: str) -> bool:
    lowered = _normalize_for_repeat_detection(text)
    return any(
        marker in lowered
        for marker in (
            "contents",
            "table of contents",
            "copyright",
            "license",
            "project gutenberg",
            "the end",
        )
    )


def _has_strong_boilerplate_marker(text: str) -> bool:
    lowered = _normalize_for_repeat_detection(text)
    return any(
        marker in lowered
        for marker in (
            "project gutenberg",
            "full license",
            "terms of use",
            "redistributing project gutenberg",
            "all rights reserved",
            "isbn",
        )
    )


def _numeric_id_pattern(value: str) -> str:
    parts = re.split(r"(\d+)", str(value or ""))
    return "^" + "".join(r"\d+" if part.isdigit() else re.escape(part) for part in parts) + "$"


def _dedupe_values(values) -> list[Any]:
    deduped: list[Any] = []
    seen: set[Any] = set()
    for value in values:
        if value in seen:
            continue
        deduped.append(value)
        seen.add(value)
    return deduped


def _representative_blocks(blocks: list[SourceBlock], limit: int) -> list[SourceBlock]:
    if len(blocks) <= limit:
        return blocks
    head_count = (limit + 1) // 2
    tail_count = limit - head_count
    return blocks[:head_count] + blocks[-tail_count:] if tail_count else blocks[:head_count]
