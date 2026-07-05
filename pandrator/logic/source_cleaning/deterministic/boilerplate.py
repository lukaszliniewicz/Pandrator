from __future__ import annotations

import os
import re

PROJECT_GUTENBERG_START_RE = re.compile(
    r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*",
    re.IGNORECASE,
)
PROJECT_GUTENBERG_END_RE = re.compile(
    r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*",
    re.IGNORECASE,
)

INLINE_TOC_HEADING_RE = re.compile(
    r"^(?:"
    r"table\s+of\s+contents|contents|illustrations|list\s+of\s+illustrations|"
    r"list\s+of\s+figures|list\s+of\s+tables|"
    r"spis\s+treści|spis\s+tresci|inhaltsverzeichnis|inhoudsopgave|inhoud|"
    r"table\s+des\s+matières|table\s+des\s+matieres|sommaire|"
    r"índice|indice|sommario|оглавление|содержание"
    r")$",
    re.IGNORECASE,
)

BOILERPLATE_TEXT_RE = re.compile(
    r"(?:the\s+project\s+gutenberg\s+ebook|project\s+gutenberg|"
    r"gutenberg(?:™|tm)?\s+license|"
    r"this\s+ebook\s+is\s+for\s+the\s+use\s+of\s+anyone|"
    r"release\s+date:\s+|language:\s+|credits:\s+|"
    r"www\.gutenberg\.org|gutenberg\.org/ebooks|"
    r"all\s+rights\s+reserved|isbn\b)",
    re.IGNORECASE,
)

TITLEPAGE_TEXT_RE = re.compile(
    r"^(?:by\s+.+|illustrated\s+by\s+.+|translator:\s+.+|illustrator:\s+.+|"
    r"author:\s+.+|title:\s+.+|publisher:\s+.+|original\s+publication:\s+.+|"
    r"copyright\b.*|\(c\)\s*.*|©\s*.*|new\s+york\b.*|london\b.*)$",
    re.IGNORECASE,
)

def is_front_boilerplate(idx: int, total_spine_files: int, size: int, href: str) -> bool:
    """
    Determines if a document is front-matter boilerplate (e.g. cover, title page, copyright).
    """
    name_lower = os.path.basename(href).lower()
    
    # Tokenize filename to split by non-alphanumeric characters
    clean_name = re.sub(r'[^a-z0-9]', ' ', name_lower)
    tokens = set(clean_name.split())
    
    # Position filter (first 5 files or first 15% of the book)
    if idx < 5 or (idx / max(1, total_spine_files)) < 0.15:
        # Size limit + keyword check
        if size < 3500:
            front_keywords = {
                "cover", "cvi", "cvr", "title", "tp", "copyright", "cop", "cpy",
                "dedication", "ded", "preface", "prf", "acknowledg", "ack", "foreword", "fwd",
                "colophon", "col", "fm", "halftitle"
            }
            if tokens.intersection(front_keywords):
                return True
            substring_keywords = ["cover", "title", "copyright", "dedicat", "prefac", "acknowledg", "colophon", "halftitle"]
            if any(x in name_lower for x in substring_keywords):
                return True
                
    return False

def is_end_boilerplate(idx: int, total_spine_files: int, href: str) -> bool:
    """
    Determines if a document is end-matter boilerplate (e.g. bibliography, index, advertisements).
    """
    name_lower = os.path.basename(href).lower()
    
    clean_name = re.sub(r'[^a-z0-9]', ' ', name_lower)
    tokens = set(clean_name.split())
    
    # Position filter (last 5 files or last 25% of the book)
    if idx > total_spine_files - 5 or (idx / max(1, total_spine_files)) > 0.75:
        # Calibre split-file recovery: ignore index_split pages
        if "index_split" not in name_lower:
            end_keywords = {
                "index", "biblio", "bibliography", "bib", "about", "ads", "adc", "adv",
                "advertisement", "colophon", "col", "copyright", "cop", "copy", "ata", "bm"
            }
            if tokens.intersection(end_keywords):
                return True
            substring_keywords = ["index", "biblio", "advertis", "colophon", "copyright", "backmatter"]
            if any(x in name_lower for x in substring_keywords):
                return True
                
    return False


def is_project_gutenberg_start(text: str) -> bool:
    return bool(PROJECT_GUTENBERG_START_RE.search(text or ""))


def is_project_gutenberg_end(text: str) -> bool:
    return bool(PROJECT_GUTENBERG_END_RE.search(text or ""))


def is_inline_toc_heading(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    return bool(INLINE_TOC_HEADING_RE.match(normalized))


def is_boilerplate_text(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return False
    return bool(
        BOILERPLATE_TEXT_RE.search(normalized)
        or is_project_gutenberg_start(normalized)
        or is_project_gutenberg_end(normalized)
    )


def normalized_metadata_values(metadata: dict) -> set[str]:
    values = set()
    for key in ("title", "creator", "author"):
        value = metadata.get(key)
        if not value:
            continue
        normalized = re.sub(r"\s+", " ", str(value)).strip().lower()
        if normalized:
            values.add(normalized)
    return values


def is_titlepage_metadata_text(text: str, metadata: dict) -> bool:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return False
    lowered = normalized.lower()
    if lowered in normalized_metadata_values(metadata):
        return True
    for value in normalized_metadata_values(metadata):
        if value and lowered in {f"by {value}", f"author: {value}", f"title: {value}"}:
            return True
    return bool(TITLEPAGE_TEXT_RE.match(normalized))


def looks_like_toc_entry(block: dict) -> bool:
    text = re.sub(r"\s+", " ", block.get("text", "") or "").strip()
    if not text:
        return False
    words = text.split()
    if len(text) <= 140 and len(words) <= 18 and not re.search(r"[.!?]\s*$", text):
        return True
    anchor_count = sum(1 for part in block.get("parts", []) if part.get("type") == "anchor")
    return anchor_count > 0 and len(words) <= 40
