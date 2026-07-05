from __future__ import annotations

import os
import re
from typing import Any

import ebooklib
from bs4 import BeautifulSoup
from ebooklib import epub

from .models import SourceBlock, SourceDocument


BLOCK_TAGS = {
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "blockquote",
    "li",
    "figcaption",
    "dt",
    "dd",
    "pre",
    "aside",
}

CONTAINER_TAGS = {"div", "section", "article", "main", "header", "footer"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def build_source_document(epub_path: str) -> SourceDocument:
    """Builds a structured, searchable EPUB index without changing import behavior."""
    book = epub.read_epub(epub_path)
    filename = os.path.basename(epub_path)
    metadata_candidates = _extract_metadata_candidates(book, filename)
    language = _first_metadata_value(metadata_candidates, "language")
    navigation_entries = _extract_navigation_entries(book)
    nav_titles = _dedupe([str(entry.get("title") or "") for entry in navigation_entries])

    document = SourceDocument(
        source_type="epub",
        source_path=os.path.abspath(epub_path),
        filename=filename,
        metadata_candidates=metadata_candidates,
        language=language,
        nav_titles=nav_titles,
        navigation_entries=navigation_entries,
    )

    line_number = 1
    source_index = 0

    from pandrator.logic.source_cleaning.deterministic.parser import unpack_epub_structure
    from pandrator.logic.source_cleaning.deterministic import toc, footnotes, boilerplate, chapters, illustrations

    try:
        structure = unpack_epub_structure(epub_path)
        global_toc = toc.build_global_toc_map(structure)
    except Exception:
        structure = {}
        global_toc = {}

    spine = structure.get("spine", [])
    parsed_documents = structure.get("parsed_documents", {})
    total_spine_files = len(spine)

    def norm_href(h: str) -> str:
        return str(h or "").lower().replace("\\", "/").strip()

    toc_files = set()
    footnote_files = set()
    front_bp_files = set()
    end_bp_files = set()

    # Pre-classify all documents (both in-spine and out-of-spine)
    spine_index_map = {norm_href(spine_item["href"]): spine_idx for spine_idx, spine_item in enumerate(spine)}
    all_documents = _ordered_document_items(book)

    for doc_item in all_documents:
        doc_href = doc_item.get_name()
        nh = norm_href(doc_href)
        parsed_doc = parsed_documents.get(doc_href, {})
        
        try:
            content_bytes = doc_item.get_content()
            size = len(content_bytes) if content_bytes else 0
        except Exception:
            size = parsed_doc.get("size", 0)

        spine_idx = spine_index_map.get(nh, -1)

        # 1. Check TOC
        if toc.is_toc_file(doc_href, parsed_doc, spine):
            toc_files.add(nh)
        # 2. Check Footnotes
        elif footnotes.is_footnote_file(doc_href, size):
            footnote_files.add(nh)
        # 3. Check Boilerplate (only if in spine, or using name keywords)
        else:
            if spine_idx != -1:
                if boilerplate.is_front_boilerplate(spine_idx, total_spine_files, size, doc_href):
                    front_bp_files.add(nh)
                elif boilerplate.is_end_boilerplate(spine_idx, total_spine_files, doc_href):
                    end_bp_files.add(nh)
            else:
                # Out-of-spine: check name keywords for boilerplate
                name_lower = os.path.basename(doc_href).lower()
                clean_name = re.sub(r'[^a-z0-9]', ' ', name_lower)
                tokens = set(clean_name.split())
                front_keywords = {
                    "cover", "cvi", "cvr", "title", "tp", "copyright", "cop", "cpy",
                    "dedication", "ded", "preface", "prf", "acknowledg", "ack", "foreword", "fwd",
                    "colophon", "col", "fm", "halftitle"
                }
                end_keywords = {
                    "index", "biblio", "bibliography", "bib", "about", "ads", "adc", "adv",
                    "advertisement", "colophon", "col", "copyright", "cop", "copy", "ata", "bm"
                }
                if tokens.intersection(front_keywords) or any(x in name_lower for x in ["cover", "title", "copyright", "dedicat"]):
                    front_bp_files.add(nh)
                elif tokens.intersection(end_keywords) or any(x in name_lower for x in ["index", "biblio", "copyright"]):
                    end_bp_files.add(nh)

    for item in _ordered_document_items(book):
        href = item.get_name()
        nh = norm_href(href)
        content = item.get_content().decode("utf-8", errors="ignore")
        soup = BeautifulSoup(content, "html.parser")
        item_block_count = 0
        for tag in _iter_textual_tags(soup):
            for text, attributes, raw_markup, role_candidates in _extract_tag_text_entries(tag):
                if not text:
                    continue

                source_index += 1
                block_id = f"epub:{href}:{source_index}"

                block_roles = list(role_candidates)
                if nh in toc_files:
                    block_roles.append("deterministic_toc")
                if nh in footnote_files:
                    block_roles.append("deterministic_footnote")
                if nh in front_bp_files or nh in end_bp_files:
                    block_roles.append("deterministic_boilerplate")

                block_dict = {
                    "tag": tag.name,
                    "text": text,
                    "classes": _normalize_classes(tag.get("class", [])),
                    "id": str(tag.get("id") or attributes.get("element_id") or ""),
                    "role": str(tag.get("role") or ""),
                    "epub_type": str(tag.get("epub:type") or ""),
                    "roles": _inherited_attr_values(tag, "role"),
                    "epub_types": _inherited_attr_values(tag, "epub:type"),
                    "aria_label": str(tag.get("aria-label") or ""),
                }

                if illustrations.is_visual_material_block(block_dict):
                    block_roles.append("deterministic_visual")

                # Check element ID against global TOC anchors
                elem_id = attributes.get("element_id") or tag.get("id") or ""
                is_ch_via_toc = False
                if elem_id:
                    frag_key = f"{nh}#{str(elem_id).lower()}"
                    if frag_key in global_toc:
                        is_ch_via_toc = True

                if is_ch_via_toc or chapters.is_chapter_block(block_dict, item_block_count, language or "en"):
                    block_roles.append("deterministic_chapter")

                block = SourceBlock(
                    block_id=block_id,
                    text=text,
                    line_start=line_number,
                    line_end=line_number,
                    source_index=source_index,
                    href=href,
                    tag=tag.name,
                    classes=_normalize_classes(tag.get("class", [])),
                    element_id=str(tag.get("id") or attributes.get("element_id") or "") or None,
                    dom_path=_build_dom_path(tag),
                    attributes=attributes,
                    role_candidates=_dedupe(block_roles + _infer_role_candidates(tag, text, href)),
                    raw_markup=raw_markup,
                )
                document.blocks.append(block)
                item_block_count += 1
                line_number += 1

        if item_block_count == 0:
            fallback_text = _normalize_text(soup.get_text(" ", strip=True))
            if fallback_text:
                source_index += 1
                block_roles = []
                if nh in toc_files:
                    block_roles.append("deterministic_toc")
                if nh in footnote_files:
                    block_roles.append("deterministic_footnote")
                if nh in front_bp_files or nh in end_bp_files:
                    block_roles.append("deterministic_boilerplate")
                document.blocks.append(
                    SourceBlock(
                        block_id=f"epub:{href}:{source_index}",
                        text=fallback_text,
                        line_start=line_number,
                        line_end=line_number,
                        source_index=source_index,
                        href=href,
                        tag="document",
                        role_candidates=_dedupe(block_roles),
                        raw_markup=str(soup),
                    )
                )
                document.warnings.append(
                    f"Document '{href}' had no recognized block markup; indexed its complete text as one fallback block."
                )
                line_number += 1

    if not document.blocks:
        document.warnings.append("EPUB parsing produced no text blocks.")

    return document


def _ordered_document_items(book: epub.EpubBook) -> list[Any]:
    documents = [
        item
        for item in book.get_items()
        if item.get_type() == ebooklib.ITEM_DOCUMENT
    ]
    by_id = {
        str(item.get_id() or ""): item
        for item in documents
        if str(item.get_id() or "")
    }
    ordered: list[Any] = []
    seen_names: set[str] = set()

    for spine_entry in getattr(book, "spine", []) or []:
        item_id = spine_entry[0] if isinstance(spine_entry, (list, tuple)) else spine_entry
        item = by_id.get(str(item_id or ""))
        if item is None:
            try:
                item = book.get_item_with_id(str(item_id or ""))
            except Exception:
                item = None
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        name = str(item.get_name() or "")
        if name in seen_names:
            continue
        ordered.append(item)
        seen_names.add(name)

    for item in documents:
        name = str(item.get_name() or "")
        if name in seen_names:
            continue
        ordered.append(item)
        seen_names.add(name)

    return ordered


def _extract_metadata_candidates(book: epub.EpubBook, filename: str) -> dict[str, list[dict[str, Any]]]:
    candidates: dict[str, list[dict[str, Any]]] = {}

    metadata_fields = {
        "title": ("DC", "title"),
        "author": ("DC", "creator"),
        "language": ("DC", "language"),
        "publisher": ("DC", "publisher"),
        "date": ("DC", "date"),
        "identifier": ("DC", "identifier"),
    }
    for output_key, metadata_key in metadata_fields.items():
        for value, attrs in book.get_metadata(*metadata_key):
            normalized = _normalize_text(str(value or ""))
            if not normalized:
                continue
            candidates.setdefault(output_key, []).append(
                {
                    "value": normalized,
                    "source": "epub_metadata",
                    "attributes": dict(attrs or {}),
                    "confidence": 0.95,
                }
            )

    filename_stem = os.path.splitext(filename)[0]
    filename_candidates = _metadata_from_filename(filename_stem)
    for key, values in filename_candidates.items():
        for value in values:
            candidates.setdefault(key, []).append(
                {
                    "value": value,
                    "source": "filename",
                    "confidence": 0.45,
                }
            )

    return candidates


def _metadata_from_filename(stem: str) -> dict[str, list[str]]:
    cleaned = re.sub(r"[_]+", " ", stem).strip()
    if not cleaned:
        return {}

    result: dict[str, list[str]] = {"title": [cleaned]}
    for separator in (" - ", " -- ", " by "):
        if separator not in cleaned:
            continue
        left, right = [part.strip() for part in cleaned.split(separator, 1)]
        if not left or not right:
            continue
        if separator.strip().lower() == "by":
            result = {"title": [left], "author": [right]}
        else:
            result = {
                "title": [right, cleaned],
                "author": [left],
            }
        break
    return result


def _first_metadata_value(candidates: dict[str, list[dict[str, Any]]], key: str) -> str:
    values = candidates.get(key) or []
    if not values:
        return ""
    return str(values[0].get("value") or "")


def _extract_navigation_entries(book: epub.EpubBook) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    def visit(item: Any, depth: int = 0):
        if (
            isinstance(item, tuple)
            and len(item) == 2
            and not isinstance(item[0], (list, tuple))
            and isinstance(item[1], (list, tuple))
        ):
            visit(item[0], depth)
            visit(item[1], depth + 1)
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                visit(child, depth)
            return

        title = getattr(item, "title", "")
        if title:
            href = str(getattr(item, "href", "") or "")
            href_path, separator, fragment = href.partition("#")
            entries.append(
                {
                    "order": len(entries) + 1,
                    "depth": depth,
                    "title": _normalize_text(str(title)),
                    "href": href,
                    "href_path": href_path,
                    "fragment": fragment if separator else "",
                }
            )

        subitems = getattr(item, "subitems", None)
        if subitems:
            visit(subitems, depth + 1)

    try:
        visit(book.toc)
    except Exception:
        return []

    return entries


def _iter_textual_tags(soup: BeautifulSoup):
    for tag in soup.find_all(list(BLOCK_TAGS | CONTAINER_TAGS | {"img"})):
        if tag.name == "img":
            alt_text = _normalize_text(str(tag.get("alt") or ""))
            if alt_text:
                yield tag
            continue

        if tag.name in CONTAINER_TAGS and _has_descendant_block(tag):
            continue

        if _has_ancestor_textual_tag(tag):
            continue

        text = _normalize_text(tag.get_text(" ", strip=True))
        if text:
            yield tag


def _has_descendant_block(tag) -> bool:
    return any(child.name in BLOCK_TAGS for child in tag.find_all(list(BLOCK_TAGS)))


def _has_ancestor_textual_tag(tag) -> bool:
    parent = tag.parent
    while parent is not None and getattr(parent, "name", None):
        if parent.name in BLOCK_TAGS:
            return True
        parent = parent.parent
    return False


def _extract_tag_text_entries(tag) -> list[tuple[str, dict[str, Any], str, list[str]]]:
    if tag.name == "img":
        alt = _normalize_text(str(tag.get("alt") or ""))
        if not alt:
            return []
        attributes = _safe_attributes(tag)
        return [(alt, attributes, str(tag), ["image_alt"])]

    text = _normalize_text(tag.get_text(" ", strip=True))
    if not text:
        return []

    attributes = _safe_attributes(tag)
    # Search for child anchor tags to capture nested ids and hrefs
    for a in tag.find_all("a"):
        a_id = a.get("id") or a.get("name")
        if a_id and not attributes.get("element_id"):
            attributes["element_id"] = str(a_id)
        a_href = a.get("href")
        if a_href and not attributes.get("href"):
            attributes["href"] = str(a_href)

    return [(text, attributes, str(tag), [])]


def _safe_attributes(tag) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for key, value in dict(tag.attrs or {}).items():
        if isinstance(value, list):
            attrs[str(key)] = [str(item) for item in value]
        else:
            attrs[str(key)] = str(value)
    return attrs


def _inherited_attr_values(tag, attr_name: str) -> list[str]:
    values: list[str] = []
    current = tag
    while current is not None and getattr(current, "name", None):
        raw_value = current.get(attr_name)
        if raw_value:
            values.extend(str(raw_value).split())
        current = current.parent
    return _dedupe(values)


def _infer_role_candidates(tag, text: str, href: str) -> list[str]:
    roles: list[str] = []
    lowered = " ".join(
        [
            tag.name or "",
            href or "",
            str(tag.get("id") or ""),
            " ".join(_normalize_classes(tag.get("class", []))),
            str(tag.get("role") or ""),
            str(tag.get("epub:type") or ""),
            str(tag.get("aria-label") or ""),
        ]
    ).lower()
    text_lower = text.lower()

    if tag.name in HEADING_TAGS:
        roles.append("heading")
    if tag.name in {"figcaption"} or "caption" in lowered:
        roles.append("caption")
    if tag.name in {"aside"}:
        roles.append("side_note")
    if any(token in lowered for token in ("footnote", "endnote", "note-ref", "noteref")):
        roles.append("footnote")
    
    # Check if this block contains child anchors pointing to internal links
    has_inner_links = False
    for a in tag.find_all("a"):
        a_href = a.get("href") or ""
        if a_href:
            has_inner_links = True
            if "#" in a_href:
                roles.append("toc")
                break
    if has_inner_links:
        roles.append("navigation")

    if any(token in lowered for token in ("toc", "contents", "nav")):
        roles.append("toc")
    if any(token in text_lower for token in ("copyright", "all rights reserved", "isbn")):
        roles.append("copyright")
    if _looks_like_short_heading(text):
        roles.append("heading_candidate")
    return roles


def _looks_like_short_heading(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > 140:
        return False
    if len(stripped.split()) > 16:
        return False
    if re.search(r"[.!?]\s*$", stripped) and len(stripped.split()) > 4:
        return False
    return True


def _build_dom_path(tag) -> str:
    parts: list[str] = []
    current = tag
    while current is not None and getattr(current, "name", None):
        part = current.name
        if current.get("id"):
            part += f"#{current.get('id')}"
        classes = _normalize_classes(current.get("class", []))
        if classes:
            part += "." + ".".join(classes[:3])
        parts.append(part)
        current = current.parent
    return " > ".join(reversed(parts))


def _normalize_classes(raw_classes: Any) -> list[str]:
    if isinstance(raw_classes, str):
        return [item for item in raw_classes.split() if item]
    if isinstance(raw_classes, list):
        return [str(item) for item in raw_classes if str(item).strip()]
    return []


def _normalize_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    return normalized


def _dedupe(items: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = str(item or "").strip()
        if not normalized or normalized in seen:
            continue
        deduped.append(normalized)
        seen.add(normalized)
    return deduped
