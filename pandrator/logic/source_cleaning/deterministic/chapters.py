from __future__ import annotations

import re

NUM_PATTERN = r'^(?:[0-9]{1,3}|[I|V|X|L|C|D|M]+)\b'
_REGEX_CACHE = {}

def get_chapter_regex(lang: str) -> re.Pattern:
    if lang not in _REGEX_CACHE:
        from .footnotes import LANGUAGE_REGISTRY
        en_pattern = LANGUAGE_REGISTRY["en"]["chapter_patterns"]
        
        if lang != "en" and lang in LANGUAGE_REGISTRY:
            lang_pattern = LANGUAGE_REGISTRY[lang]["chapter_patterns"]
            if lang in ("zh", "ja"):
                # CJK: lang_pattern doesn't need \b, but en_pattern DOES!
                pat = f"^(?:(?:{en_pattern})\\b|{lang_pattern})|{NUM_PATTERN}"
            else:
                # Both alphabetical, both can use \b
                pat = f"^(?:{en_pattern}|{lang_pattern})\\b|{NUM_PATTERN}"
        else:
            pat = f"^(?:{en_pattern})\\b|{NUM_PATTERN}"
            
        _REGEX_CACHE[lang] = re.compile(pat, re.IGNORECASE)
    return _REGEX_CACHE[lang]

KNOWN_CHAPTER_CLASSES = {
    "ct", "cn", "chap", "chtitle", "chap_no", "ch-title", "partno", "chapter-title",
    "h1", "h2", "h3", "heading", "chapter", "title", "header", "chapter-number", "chapternum"
}

def is_chapter_block(block: dict, idx_in_doc: int, lang: str = "en") -> bool:
    """
    Checks if a block is a chapter heading, tailored to the book's language.
    """
    tag = block.get("tag", "").lower()
    text = block.get("text", "").strip()
    classes = block.get("classes", [])
    
    if not text:
        return False
        
    # 1. Standard HTML headings (h1 - h6)
    if tag in ["h1", "h2", "h3", "h4", "h5", "h6"]:
        return True
        
    # 2. Custom chapter-like CSS classes
    if any(cls.lower() in KNOWN_CHAPTER_CLASSES for cls in classes):
        return True
        
    # 3. First-3-paragraphs text scanner matching chapter prefixes
    if idx_in_doc < 3 and len(text) < 60:
        regex = get_chapter_regex(lang)
        if regex.match(text):
            return True
            
    return False
