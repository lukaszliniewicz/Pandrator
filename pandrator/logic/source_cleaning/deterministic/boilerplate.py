from __future__ import annotations

import os
import re

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
