from __future__ import annotations

import re

from . import parser
from . import toc
from . import boilerplate
from . import chapters
from . import footnotes


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
        if footnotes.is_footnote_file(href, size):
            continue
        if boilerplate.is_front_boilerplate(idx, total_spine_files, size, href):
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

        ids_by_block_index = _ids_by_block_index(doc)
        chapter_candidates = []
        for idx_in_doc, block in enumerate(blocks):
            matched_title = _matched_toc_title(doc_href, block, ids_by_block_index, global_toc)
            toc_chapter = _is_usable_toc_chapter(block, matched_title, metadata, detected_lang)
            semantic_chapter = chapters.is_chapter_block(
                block,
                idx_in_doc,
                lang=detected_lang,
                allow_heading_fallback=False,
            )
            chapter_candidates.append(
                {
                    "matched_title": matched_title,
                    "toc_chapter": toc_chapter,
                    "semantic_chapter": semantic_chapter,
                }
            )

        has_toc_chapters = any(item["toc_chapter"] for item in chapter_candidates)
        has_semantic_chapters = any(item["semantic_chapter"] for item in chapter_candidates)
        recovered_title = _usable_recovered_doc_title(doc_href, global_toc, metadata, detected_lang)
        allow_heading_fallback = not has_toc_chapters and not has_semantic_chapters and not recovered_title

        formatted_blocks = []
        last_chapter_title_norm = ""

        if recovered_title and not has_toc_chapters and not has_semantic_chapters:
            formatted_blocks.append(_heading_block(doc_href, recovered_title))
            last_chapter_title_norm = _norm_title(recovered_title)

        for idx_in_doc, block in enumerate(blocks):
            block_copy = block.copy()
            candidate = chapter_candidates[idx_in_doc]
            matched_title = candidate["matched_title"]
            is_chapter = candidate["toc_chapter"] or candidate["semantic_chapter"]

            if not is_chapter and allow_heading_fallback:
                is_chapter = chapters.is_chapter_block(
                    block,
                    idx_in_doc,
                    lang=detected_lang,
                    allow_heading_fallback=True,
                )

            if is_chapter:
                title = matched_title or block.get("text", "").strip()
                if title and _norm_title(title) != last_chapter_title_norm:
                    if not title.startswith("[[Chapter]]"):
                        block_copy["text"] = f"[[Chapter]]{title}"
                        block_copy["parts"] = [{"type": "text", "content": block_copy["text"]}]
                    last_chapter_title_norm = _norm_title(title)
            elif matched_title and not chapters.is_non_chapter_heading_text(matched_title):
                matched_title_norm = _norm_title(matched_title)
                if (
                    allow_heading_fallback
                    and _is_insertable_toc_heading(matched_title, block, metadata, detected_lang)
                    and matched_title_norm != last_chapter_title_norm
                ):
                    formatted_blocks.append(_heading_block(doc_href, matched_title))
                    last_chapter_title_norm = matched_title_norm

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

    block_text = block.get("text", "").strip()
    if chapters.is_explicit_chapter_title(title, lang=detected_lang):
        return True
    if chapters.is_chapter_block(block, 0, lang=detected_lang, allow_heading_fallback=False):
        return True
    if block.get("tag", "").lower() in chapters.HEADING_TAGS:
        return True
    return False


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
    if not chapters.is_plausible_heading_text(recovered_title, max_chars=220):
        return None
    return recovered_title


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
