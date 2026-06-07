from __future__ import annotations

import re

# Compiled regular expressions for multilingual chapter detection
WORD_PATTERN = (
    r'^(?:chapter|ch|rozdział|rozdz|chapitre|chap|kapitel|kap|capítulo|cap|'
    r'secção|seção|hoofdstuk|hst|fejezet|fej|kapitull|part|section|sectie|część|cz|'
    r'partie|teil|abschnitt|parte|sección|rész|szakasz|pjesë|seksion|volume|'
    r'vol|tom|t|band|bd|buch|libro|livro|boek|kötet|köt|könyv|vëllim|vël|'
    r'libër|prologue|epilogue|prolog|epilog|épilogue|prólogo|epílogo|proloog|'
    r'epiloog|prologus|epilogus|księga|księgi|wstęp|posłowie|livre|tome|'
    r'préface|avant-propos|vorwort|nachwort|prefacio|introducción|prefácio|'
    r'introdução|posfácio|deel|inleiding|nawoord|bevezetés|előszó|utószó|'
    r'parathënie|pasthënie)\b'
)

CJK_PATTERN = (
    r'^(?:第?\s*(?:[0-9]+|[一二三四五六七八九十百千]+)\s*(?:章|部|节|節|卷|巻|册|冊|页|頁)|'
    r'プロローグ|エピローグ|楔子|前言|序幕|尾声|尾聲|序|跋|序章|終章|まえがき|あとがき|后记|後記|引言|绪论|緒論)'
)

NUM_PATTERN = r'^(?:[0-9]{1,3}|[I|V|X|L|C|D|M]+)\b'

COMBINED_REGEX = re.compile(f"{WORD_PATTERN}|{CJK_PATTERN}|{NUM_PATTERN}", re.IGNORECASE)

KNOWN_CHAPTER_CLASSES = {
    "ct", "cn", "chap", "chtitle", "chap_no", "ch-title", "partno", "chapter-title",
    "h1", "h2", "h3", "heading", "chapter", "title", "header", "chapter-number", "chapternum"
}

def is_chapter_block(block: dict, idx_in_doc: int) -> bool:
    """
    Checks if a block is a chapter heading.
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
        if COMBINED_REGEX.match(text):
            return True
            
    return False
