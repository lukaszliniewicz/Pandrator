from __future__ import annotations

import os

def is_front_boilerplate(idx: int, total_spine_files: int, size: int, href: str) -> bool:
    """
    Determines if a document is front-matter boilerplate (e.g. cover, title page, copyright).
    """
    name_lower = href.lower()
    
    # Position filter (first 5 files or first 15% of the book)
    if idx < 5 or (idx / max(1, total_spine_files)) < 0.15:
        # Size limit + keyword check
        keywords = ["cover", "title", "copyright", "copy", "contents", "nav", "fm", "halftitle"]
        if size < 3500 and any(x in name_lower for x in keywords):
            return True
            
    return False

def is_end_boilerplate(idx: int, total_spine_files: int, href: str) -> bool:
    """
    Determines if a document is end-matter boilerplate (e.g. bibliography, index, advertisements).
    """
    name_lower = href.lower()
    
    # Position filter (last 5 files or last 25% of the book)
    if idx > total_spine_files - 5 or (idx / max(1, total_spine_files)) > 0.75:
        # Calibre split-file recovery: ignore index_split pages
        if "index_split" not in name_lower:
            keywords = ["index", "biblio", "bibliography", "about", "ads", "advertisement", "colophon"]
            if any(x in name_lower for x in keywords):
                return True
                
    return False
