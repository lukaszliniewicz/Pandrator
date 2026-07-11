from __future__ import annotations

import re

from . import parser
from . import toc
from . import boilerplate
from . import chapters
from . import footnotes
from . import illustrations


def extract_clean_epub(epub_path: str, remove_footnotes: bool = False, filter_citations: bool = True) -> str:
    """
    Extracts, filters, and formats text from an EPUB file.
    Performs deterministic removal of TOCs, front/end boilerplate,
    multilingual chapter heading markings, and inline footnote repositioning.
    """
    structure = parser.unpack_epub_structure(epub_path)
    spine = structure["spine"]
    parsed_docs = structure["parsed_documents"]
    metadata = structure.get("metadata", {})

    detected_lang = footnotes.detect_book_language(metadata, parsed_docs)

    total_spine_files = len(spine)
    content_docs = []

    for idx, item in enumerate(spine):
        href = item["href"]
        if href not in parsed_docs:
            continue

        doc = parsed_docs[href]
        size = doc["size"]

        if toc.is_toc_file(href, doc, spine):
            continue
        if footnotes.is_footnote_file(href, size, parsed_doc=doc, spine_item=item):
            continue
        if boilerplate.is_front_boilerplate(idx, total_spine_files, size, href, blocks=doc.get("blocks", [])):
            continue
        if boilerplate.is_end_boilerplate(idx, total_spine_files, href):
            continue

        content_docs.append(href)

    repositioned_block_ids = set()
    extracted_chapters = []

    backlink_map = footnotes.build_backlink_map(parsed_docs)
    global_toc = toc.build_global_toc_map(structure)

    for doc_href in content_docs:
        doc = parsed_docs[doc_href]
        blocks = _strip_block_boilerplate(
            list(doc["blocks"]),
            metadata=metadata,
            detected_lang=detected_lang,
            doc_href=doc_href,
        )
        if not blocks:
            continue

        # Preserve likely structural headings while removing visual-only blocks.
        # Navigation links are deliberately not used here: a page-list or nested
        # TOC entry must never create an audiobook chapter by itself.
        visual_indexes = illustrations.visual_block_indexes(
            blocks,
            protected_indexes={
                idx
                for idx, block in enumerate(blocks)
                if (
                    chapters.has_direct_chapter_semantics(block)
                    or chapters.is_chapter_block(
                        block,
                        idx,
                        lang=detected_lang,
                        allow_heading_fallback=False,
                    )
                )
            },
        )
        if visual_indexes:
            blocks = [
                block
                for idx, block in enumerate(blocks)
                if idx not in visual_indexes
            ]

        ids_by_block_index = _ids_by_block_index(doc)
        chapter_titles = _direct_chapter_titles(
            blocks,
            doc_href,
            ids_by_block_index,
            global_toc,
            metadata,
            detected_lang,
        )
        recovered_title = None
        allow_heading_fallback = False

        # Direct structural signals are authoritative over weak heading and
        # navigation fallbacks. Explicit chapter text is still additive: a
        # publisher can legitimately put a Part wrapper and ``1. Title`` in
        # the same document.
        for idx, block in enumerate(blocks):
            if idx in chapter_titles:
                continue
            text = block.get("text", "").strip()
            if chapters.is_chapter_block(
                block,
                idx,
                lang=detected_lang,
                allow_heading_fallback=False,
            ):
                chapter_titles[idx] = text

        # TOC entries validate matching headings when no stronger evidence is
        # available. A strongly formatted all-caps TOC heading can also be an
        # additive title inside a document containing another direct chapter
        # boundary (common in older Gutenberg EPUBs).
        for idx, block in enumerate(blocks):
            if idx in chapter_titles:
                continue
            text = block.get("text", "").strip()
            if chapter_titles and not _is_all_caps_heading(text):
                continue
            matched_title = _matched_toc_title(
                doc_href,
                block,
                ids_by_block_index,
                global_toc,
            )
            if _is_toc_validated_heading(
                block,
                matched_title,
                metadata,
                detected_lang,
            ):
                chapter_titles[idx] = text

        if not chapter_titles and _has_meaningful_text(blocks):
            raw_recovered_title = global_toc.get(doc_href.lower())
            recovered_title = _usable_recovered_doc_title(
                doc_href,
                global_toc,
                metadata,
                detected_lang,
            )
            # A navigation target still tells us that this document belongs to
            # a larger hierarchy.  If its title is a map item, page label, or
            # other unusable value, do not fall back to every heading in the
            # document; that turns guidebook lists into hundreds of chapters.
            allow_heading_fallback = not recovered_title and not raw_recovered_title

        chapter_titles = _dedupe_numbered_chapter_titles(chapter_titles)

        formatted_blocks = []
        last_chapter_title_norm = ""

        if recovered_title:
            formatted_blocks.append(_heading_block(doc_href, recovered_title))
            last_chapter_title_norm = _norm_title(recovered_title)

        for idx_in_doc, block in enumerate(blocks):
            block_copy = block.copy()
            title = chapter_titles.get(idx_in_doc, "")
            if not title and allow_heading_fallback and chapters.is_chapter_block(
                block,
                idx_in_doc,
                lang=detected_lang,
                allow_heading_fallback=True,
            ):
                title = block.get("text", "").strip()

            if title:
                if title and _norm_title(title) != last_chapter_title_norm:
                    if not title.startswith("[[Chapter]]"):
                        block_copy["text"] = f"[[Chapter]]{title}"
                        block_copy["parts"] = [{"type": "text", "content": block_copy["text"]}]
                    last_chapter_title_norm = _norm_title(title)

            formatted_blocks.append(block_copy)

        doc_lines = footnotes.reposition_footnotes_in_document(
            doc_href,
            formatted_blocks,
            parsed_docs,
            repositioned_block_ids,
            remove_footnotes=remove_footnotes,
            filter_citations=filter_citations,
            detected_lang=detected_lang,
            backlink_map=backlink_map,
        )

        chapter_text = "\n\n".join(doc_lines).strip()
        if chapter_text:
            extracted_chapters.append(chapter_text)

    full_text = "\n\n".join(extracted_chapters)

    start_match = boilerplate.PROJECT_GUTENBERG_START_RE.search(full_text)
    if start_match:
        full_text = full_text[start_match.end():].strip()

    end_match = boilerplate.PROJECT_GUTENBERG_END_RE.search(full_text)
    if end_match:
        full_text = full_text[:end_match.start()].strip()

    return full_text


def _strip_block_boilerplate(
    blocks: list[dict],
    metadata: dict,
    detected_lang: str,
    doc_href: str,
) -> list[dict]:
    to_remove = set()

    start_indexes = [
        idx
        for idx, block in enumerate(blocks)
        if boilerplate.is_project_gutenberg_start(block.get("text", ""))
    ]
    if start_indexes:
        to_remove.update(range(0, start_indexes[-1] + 1))

    end_indexes = [
        idx
        for idx, block in enumerate(blocks)
        if boilerplate.is_project_gutenberg_end(block.get("text", ""))
    ]
    if end_indexes:
        to_remove.update(range(end_indexes[0], len(blocks)))

    if len(blocks) <= 2:
        href_lower = doc_href.lower()
        for idx, block in enumerate(blocks):
            if block.get("text", "").strip().lower() in {"cover", "back"} and ".wrap" in href_lower:
                to_remove.add(idx)

    for idx, block in enumerate(blocks):
        text = block.get("text", "").strip()
        if boilerplate.is_boilerplate_text(text):
            to_remove.add(idx)

    to_remove.update(_inline_toc_indexes(blocks, detected_lang))
    to_remove.update(_titlepage_run_indexes(blocks, metadata, detected_lang, already_removed=to_remove))

    if not to_remove:
        return blocks
    return [block for idx, block in enumerate(blocks) if idx not in to_remove]


def _inline_toc_indexes(blocks: list[dict], detected_lang: str) -> set[int]:
    to_remove = set()
    idx = 0
    while idx < len(blocks):
        text = blocks[idx].get("text", "").strip()
        if not boilerplate.is_inline_toc_heading(text):
            idx += 1
            continue

        to_remove.add(idx)
        idx += 1
        while idx < len(blocks):
            next_block = blocks[idx]
            next_text = next_block.get("text", "").strip()
            if not next_text:
                to_remove.add(idx)
                idx += 1
                continue
            if boilerplate.is_inline_toc_heading(next_text):
                to_remove.add(idx)
                idx += 1
                continue
            if chapters.is_explicit_chapter_title(next_text, lang=detected_lang):
                if _is_inline_toc_item(next_block):
                    to_remove.add(idx)
                    idx += 1
                    continue
                break
            if next_block.get("tag", "").lower() in chapters.HEADING_TAGS:
                break
            if boilerplate.looks_like_toc_entry(next_block) or chapters.is_plausible_heading_text(next_text):
                to_remove.add(idx)
                idx += 1
                continue
            break
    return to_remove


def _is_inline_toc_item(block: dict) -> bool:
    tag = block.get("tag", "").lower()
    has_anchor = any(part.get("type") == "anchor" for part in block.get("parts", []))
    return has_anchor or tag in {"li", "p", "div", "dd", "dt"}


def _titlepage_run_indexes(
    blocks: list[dict],
    metadata: dict,
    detected_lang: str,
    already_removed: set[int],
) -> set[int]:
    to_remove = set()
    for idx, block in enumerate(blocks):
        if idx in already_removed or idx in to_remove:
            continue
        text = block.get("text", "").strip()
        if not boilerplate.is_titlepage_metadata_text(text, metadata):
            continue

        run = [idx]
        j = idx + 1
        while j < len(blocks):
            if j in already_removed:
                j += 1
                continue
            next_text = blocks[j].get("text", "").strip()
            if not next_text:
                run.append(j)
                j += 1
                continue
            if chapters.is_explicit_chapter_title(next_text, lang=detected_lang):
                break
            if boilerplate.is_titlepage_metadata_text(next_text, metadata):
                run.append(j)
                j += 1
                continue
            if chapters.is_plausible_heading_text(next_text, max_chars=120):
                run.append(j)
                j += 1
                continue
            break

        if len([item for item in run if item not in already_removed]) >= 2 or idx < 12:
            to_remove.update(run)

    return to_remove


def _is_standalone_chapter_number(text: str) -> bool:
    return chapters.is_standalone_chapter_number(text)


def _dedupe_numbered_chapter_titles(chapter_titles: dict[int, str]) -> dict[int, str]:
    """Keep the cleaner of adjacent duplicate chapter-number headings."""
    selected: dict[str, tuple[int, str]] = {}
    unkeyed: list[tuple[int, str]] = []
    for index, title in chapter_titles.items():
        normalized = re.sub(r"\s+", " ", title or "").strip()
        match = re.search(r"\b(?:chapter|ch)\s+(\d{1,4})\b", normalized, re.IGNORECASE)
        if not match:
            unkeyed.append((index, title))
            continue

        key = f"chapter:{match.group(1)}"
        current = selected.get(key)
        candidate_score = (
            int(bool(re.match(r"^(?:chapter|ch)\s+\d{1,4}\b", normalized, re.IGNORECASE))),
            -len(normalized),
        )
        if current is None:
            selected[key] = (index, title)
            continue

        current_title = re.sub(r"\s+", " ", current[1] or "").strip()
        current_score = (
            int(bool(re.match(r"^(?:chapter|ch)\s+\d{1,4}\b", current_title, re.IGNORECASE))),
            -len(current_title),
        )
        if candidate_score > current_score:
            selected[key] = (index, title)

    return dict(sorted(unkeyed + list(selected.values())))


def _is_direct_title_candidate(block: dict) -> bool:
    """Whether a block can supply the spoken title for a chapter container."""
    text = re.sub(r"\s+", " ", block.get("text", "") or "").strip()
    if (
        not text
        or _is_standalone_chapter_number(text)
        or chapters.is_navigation_label(text)
        or chapters.is_heading_fragment(text)
        or chapters.is_non_chapter_heading_text(text)
        or not chapters.is_plausible_heading_text(text, max_chars=220)
    ):
        return False

    tag = str(block.get("tag", "")).lower()
    return tag in chapters.HEADING_TAGS or chapters.has_direct_chapter_semantics(block)


def _direct_title_after_container(blocks: list[dict], start_idx: int) -> tuple[int, str] | None:
    """Find a nearby title for an empty semantic chapter wrapper.

    Several commercial EPUBs encode a chapter as an empty ``section`` followed
    by a page number, a chapter number, then the actual heading.  We preserve
    the chapter number only when it is itself structurally tagged and join it
    to the first meaningful title.
    """
    number_prefix = ""
    for idx in range(start_idx, min(len(blocks), start_idx + 8)):
        block = blocks[idx]
        text = re.sub(r"\s+", " ", block.get("text", "") or "").strip()

        if text and _is_standalone_chapter_number(text):
            if chapters.has_direct_chapter_semantics(block):
                number_prefix = text.rstrip(".)")
            continue

        if _is_direct_title_candidate(block):
            if number_prefix and not re.match(rf"^{re.escape(number_prefix)}(?:[.)]|\s|:)", text):
                text = f"{number_prefix}. {text}"
            return idx, text

        # A normal paragraph before a title indicates that this container has
        # no separately marked chapter title.
        if text and not chapters.is_plausible_heading_text(text, max_chars=220):
            break

    return None


def _direct_chapter_titles(
    blocks: list[dict],
    doc_href: str,
    ids_by_block_index: dict[int, list[str]],
    global_toc: dict[str, str],
    metadata: dict,
    detected_lang: str,
) -> dict[int, str]:
    """Return title-bearing blocks selected from direct chapter structure.

    Navigation is used only as a title fallback for a direct semantic element;
    it cannot independently add a chapter boundary.  This avoids promoting
    page-list targets and nested TOC entries to M4B chapters.
    """
    titles: dict[int, str] = {}
    seen_container_ids: set[str] = set()

    for idx, block in enumerate(blocks):
        if not chapters.has_direct_chapter_semantics(block):
            continue

        container_id = str(block.get("id") or "").strip().lower()
        if container_id and container_id in seen_container_ids:
            continue
        if container_id:
            seen_container_ids.add(container_id)

        resolved = _direct_title_after_container(blocks, idx)
        if resolved:
            title_idx, title = resolved
            titles.setdefault(title_idx, title)
            continue

        matched_title = _matched_toc_title(doc_href, block, ids_by_block_index, global_toc)
        if _is_usable_toc_chapter(block, matched_title, metadata, detected_lang):
            title = re.sub(r"\s+", " ", matched_title or "").strip()
            if title and not _is_standalone_chapter_number(title):
                titles.setdefault(idx, title)

    return titles


def _ids_by_block_index(doc: dict) -> dict[int, list[str]]:
    ids_by_block: dict[int, list[str]] = {}
    for nested_id, block_idx in doc.get("ids", {}).items():
        ids_by_block.setdefault(block_idx, []).append(nested_id)
    return ids_by_block


def _matched_toc_title(
    doc_href: str,
    block: dict,
    ids_by_block_index: dict[int, list[str]],
    global_toc: dict[str, str],
) -> str | None:
    block_ids = []
    if block.get("id"):
        block_ids.append(block["id"])
    block_ids.extend(ids_by_block_index.get(block.get("block_index"), []))

    for block_id in block_ids:
        frag_key = f"{doc_href.lower()}#{str(block_id).lower()}"
        if frag_key in global_toc:
            return global_toc[frag_key]
    return None


def _is_usable_toc_chapter(
    block: dict,
    matched_title: str | None,
    metadata: dict,
    detected_lang: str,
) -> bool:
    if not matched_title:
        return False
    title = re.sub(r"\s+", " ", matched_title).strip()
    if not title:
        return False
    if chapters.is_non_chapter_heading_text(title):
        return False
    if boilerplate.is_titlepage_metadata_text(title, metadata):
        return False
    if not chapters.is_plausible_heading_text(title, max_chars=220):
        return False
    if _is_standalone_chapter_number(title):
        return False
    if chapters.is_navigation_label(title):
        return False
    if chapters.is_heading_fragment(title):
        return False

    block_text = block.get("text", "").strip()
    if chapters.is_explicit_chapter_title(title, lang=detected_lang):
        return True
    if chapters.is_chapter_block(block, 0, lang=detected_lang, allow_heading_fallback=False):
        return True
    if block.get("tag", "").lower() in chapters.HEADING_TAGS:
        return True
    return False


def _has_meaningful_text(blocks: list[dict]) -> bool:
    """Whether a navigation target has text, rather than only images/anchors."""
    for block in blocks:
        text = re.sub(r"\s+", " ", block.get("text", "") or "").strip()
        if not text or _is_standalone_chapter_number(text):
            continue
        return True
    return False


def _is_all_caps_heading(text: str) -> bool:
    letters = [character for character in str(text or "") if character.isalpha()]
    return bool(letters) and all(character.isupper() for character in letters)


def _is_toc_validated_heading(
    block: dict,
    matched_title: str | None,
    metadata: dict,
    detected_lang: str,
) -> bool:
    """Use TOC metadata only to validate an already-present heading element."""
    if str(block.get("tag", "")).lower() not in chapters.HEADING_TAGS:
        return False
    if not _is_usable_toc_chapter(block, matched_title, metadata, detected_lang):
        return False
    block_text = re.sub(r"\s+", " ", block.get("text", "") or "").strip()
    toc_text = re.sub(r"\s+", " ", matched_title or "").strip()
    return bool(block_text and _norm_title(block_text) == _norm_title(toc_text))


def _is_insertable_toc_heading(
    matched_title: str,
    block: dict,
    metadata: dict,
    detected_lang: str,
) -> bool:
    title = re.sub(r"\s+", " ", matched_title or "").strip()
    if not title:
        return False
    if chapters.is_non_chapter_heading_text(title):
        return False
    if boilerplate.is_titlepage_metadata_text(title, metadata):
        return False
    if not chapters.is_plausible_heading_text(title, max_chars=180):
        return False
    if chapters.is_explicit_chapter_title(title, lang=detected_lang):
        return True
    block_text = re.sub(r"\s+", " ", block.get("text", "") or "").strip()
    if block_text and _norm_title(block_text) == _norm_title(title):
        return False
    if re.search(r"[.!?]\s*$", title) and len(title.split()) > 3:
        return False
    return True


def _usable_recovered_doc_title(
    doc_href: str,
    global_toc: dict[str, str],
    metadata: dict,
    detected_lang: str,
) -> str | None:
    recovered_title = global_toc.get(doc_href.lower())
    if not recovered_title:
        return None
    if chapters.is_non_chapter_heading_text(recovered_title):
        return None
    if boilerplate.is_titlepage_metadata_text(recovered_title, metadata):
        return None
    if _is_standalone_chapter_number(recovered_title):
        return None
    if chapters.is_navigation_label(recovered_title):
        return None
    if _looks_like_navigation_noise(recovered_title):
        return None
    if not chapters.is_plausible_heading_text(recovered_title, max_chars=220):
        return None
    return recovered_title


def _looks_like_navigation_noise(title: str) -> bool:
    """Reject page-list and map/POI labels masquerading as document titles."""
    normalized = re.sub(r"\s+", " ", title or "").strip()
    lowered = normalized.lower()
    if re.match(r"^\d+(?:[.,]\d+)?\s+(?:million|thousand|billion)\b", lowered):
        return True
    if re.match(r"^\d+\s*(?:\.{2,}|…)", normalized):
        return True
    if re.match(r"^\d{1,4}\s*[-–]\s*\d{1,4}$", normalized):
        return True
    if re.match(r"^\d{2,4}\s+", normalized) and not re.match(r"^\d{1,4}\s*[.)•:–—-]", normalized):
        return True
    if normalized.startswith("♦"):
        return True
    if re.match(r"^(?:early|mid|late)[ -]?\d+(?:st|nd|rd|th)\s+century$", lowered):
        return True
    if re.search(r"\b(?:hostel|hotel|guesthouse|temple|museum)\W*$", lowered):
        return True
    if re.search(r"[A-Z]\d$", normalized):
        return True
    return lowered in {"book designers", "our writers"}


def _heading_block(doc_href: str, title: str) -> dict:
    return {
        "tag": "h1",
        "id": "",
        "classes": ["chapter"],
        "role": "doc-chapter",
        "epub_type": "chapter",
        "roles": ["doc-chapter"],
        "epub_types": ["chapter"],
        "parts": [{"type": "text", "content": title}],
        "text": f"[[Chapter]]{title}",
        "href": doc_href,
        "block_index": -1,
    }


def _norm_title(title: str) -> str:
    return re.sub(r"\s+", "", title or "").lower()
