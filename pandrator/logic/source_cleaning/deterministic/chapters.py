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

# Expanded explicit chapter patterns with numbers/words for deep checks
EXPLICIT_CHAPTER_WORDS = (
    r"chapter|ch|part|section|book|volume|vol|rozdział|rozdz|część|cz|księga|tom|"
    r"kapitel|kap|teil|abschnitt|band|hoofdstuk|hst|deel|capítulo|cap|sección|sezione|"
    r"chapitre|chap|tome|secção|seção|livro|fejezet|fej|rész|szakasz|kötet|глава|гл|часть|ч|книга|кн|том|т|अध्याय|खण्ड|सर्ग|appendix"
)
NUMERIC_WORDS = (
    r"[0-9]+|[I|V|X|L|C|D|M]+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth|"
    r"jeden|dwa|trzy|cztery|pięć|sześć|siedem|osiem|dziewięć|dziesięć|pierwszy|drugi|trzeci|czwarty|piąty|"
    r"ein|zwei|drei|vier|fünf|sechs|sieben|acht|neun|zehn|erste|zweite|dritte|vierte|fünfte|"
    r"een|twee|drie|vier|vijf|zes|zeven|acht|negen|tien|eerste|tweede|derde|vierde|vijfde|"
    r"uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|primo|segundo|tercero|cuarto|quinto|"
    r"un|deux|trois|quatre|cinq|six|sept|huit|neuf|dix|premier|second|troisième|quatrième|cinquième|"
    r"egy|két|három|négy|öt|hat|hét|nyolc|kilenc|tíz|első|második|harmadik|negyedik|ötödik|"
    r"один|два|три|четыре|пять|шесть|семь|восемь|девять|десять|первый|второй|третий|четвертый|пятый"
)
STANDALONE_CHAPTER_WORDS = (
    r"prologue|epilogue|preface|foreword|introduction|afterword|acknowledgements|acknowledgment|contents|table\s+of\s+contents|"
    r"wstęp|posłowie|podziękowania|spis\s+treści|prolog|epilog|vorwort|nachwort|danksagung|einleitung|inhaltsverzeichnis|"
    r"inleiding|nawoord|dankwoord|inhoud|inhoudsopgave|prólogo|epílogo|introducción|prefacio|agradecimientos|contenido|indice|"
    r"introduzione|prefazione|ringraziamenti|préface|avant-propos|remerciements|table\s+des\s+matières|sommaire|"
    r"bevezetés|előszó|utószó|köszönetnyilvánítás|tartalom|введение|предисловие|послесловие|благодарности|"
    r"содержание|оглавление|भूमिका|प्रस्तावना|आभार|विषय-सूची"
)

EXPLICIT_CHAPTER_RE = re.compile(r"^(?:(?:" + EXPLICIT_CHAPTER_WORDS + r")\s+(?:" + NUMERIC_WORDS + r"))\b", re.IGNORECASE)
STANDALONE_CHAPTER_RE = re.compile(r"^(?:" + STANDALONE_CHAPTER_WORDS + r")\b", re.IGNORECASE)

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
        
    # 3. Text scanner matching chapter prefixes (safe anywhere for CJK, first-3-paragraphs for alphabetical)
    if (lang in ("zh", "ja") or idx_in_doc < 3) and len(text) < 60:
        regex = get_chapter_regex(lang)
        if regex.match(text):
            return True
            
    # 4. Deep document chapter keyword checks (safe anywhere in the document if short and explicit)
    if len(text) < 80:
        if EXPLICIT_CHAPTER_RE.match(text):
            return True
        if STANDALONE_CHAPTER_RE.match(text):
            return True
            
    return False
