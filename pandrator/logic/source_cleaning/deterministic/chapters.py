from __future__ import annotations

import re

NUM_PATTERN = r'^(?:[0-9]{1,3}|[IVXLCDM]{1,12})(?:$|\s*[\.\):–—-]\s*(?=\S))'
_REGEX_CACHE = {}

HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

CHAPTER_EPUB_TYPES = {
    "chapter", "doc-chapter", "part", "doc-part", "division", "volume", "book",
    "prologue", "epilogue", "appendix", "introduction", "preface",
    "foreword", "afterword",
}

NON_CHAPTER_EPUB_TYPES = {
    "toc", "doc-toc", "cover", "doc-cover", "titlepage", "halftitlepage",
    "halftitle", "copyright-page", "copyright", "doc-index", "index",
    "bibliography", "notes", "footnotes", "endnotes", "glossary", "colophon",
    "acknowledgments", "acknowledgements", "dedication",
}

NON_CHAPTER_TEXT_RE = re.compile(
    r"^(?:"
    r".*table\s+of\s+contents.*|.*next\s+chapter.*|.*previous\s+chapter.*|"
    r"contents|illustrations|list\s+of\s+illustrations|"
    r"list\s+of\s+figures|list\s+of\s+tables|title\s+page|copyright|"
    r"about\s+the\s+author|about\s+the\s+publisher|also\s+by|other\s+books|"
    r"bibliography|index|notes|footnotes|endnotes|glossary|colophon|colofon|"
    r"dedication|widmung|titelseite|titel|title|front|"
    r"praise\s+for\s+.+|.+\bpublication|.+\bpublishing|"
    r"spis\s+treści|spis\s+tresci|inhaltsverzeichnis|inhoudsopgave|inhoud|"
    r"table\s+des\s+matières|table\s+des\s+matieres|sommaire|"
    r"índice|indice|sommario|oglavlenie|soderzhanie|"
    r"оглавление|содержание"
    r")$",
    re.IGNORECASE,
)

BOILERPLATE_TEXT_RE = re.compile(
    r"(?:project\s+gutenberg|gutenberg(?:™|tm)?\s+license|"
    r"\*\*\*\s*(?:start|end)\s+of\s+(?:the|this)\s+project\s+gutenberg|"
    r"ebook\s+is\s+for\s+the\s+use\s+of\s+anyone|"
    r"all\s+rights\s+reserved|isbn\b|copyright\s+\d{4}|"
    r"published\s+by|printed\s+in|www\.gutenberg\.org)",
    re.IGNORECASE,
)

CHAPTER_CLASS_EXACT = {
    "chapter", "chap", "chapters", "chapter-title", "chaptertitle",
    "chapter-head", "chapterhead", "chapterheada", "chapter-heading",
    "chapterheading", "chapter-number", "chapternumber", "chapter-number",
    "chap-num", "chapnum", "chap_no", "chap-no", "ch-title", "chtitle",
    "chap-title", "chaptitle", "part", "part-title", "parttitle", "partno",
    "book-title", "booktitle", "volume-title", "vol-title", "stave",
    "cn", "ct", "cct", "ccn", "ctag", "ctag2",
}

CHAPTER_CLASS_RE = re.compile(
    r"(?:^|[-_\s])(?:chapter|chap|chapt|chapitre|capitulo|capítulo|capitolo|"
    r"kapitel|hoofdstuk|rozdzial|rozdział|fejezet|stave|part|book|volume|"
    r"prologue|epilogue)(?:$|[-_\s0-9])",
    re.IGNORECASE,
)

CHAPTER_ID_RE = re.compile(
    r"(?:^|[-_\s])(?:chapter|chap|chapt|ch|part|book|volume|stave|"
    r"prologue|epilogue|rozdzial|rozdział|kapitel|chapitre|capitulo|"
    r"capítulo|capitolo|hoofdstuk|fejezet)[-_\s]?[0-9ivxlcdm]*$",
    re.IGNORECASE,
)

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
    "ct", "cn", "chap", "chapters", "chtitle", "chap_no", "ch-title",
    "partno", "chapter-title", "chaptertitle", "chapterhead",
    "chapter-heading", "chapter-number", "chapternum", "stave"
}

# Expanded explicit chapter patterns with numbers/words for deep checks
EXPLICIT_CHAPTER_WORDS = (
    r"chapter|ch|part|section|book|volume|vol|stave|rozdział|rozdz|część|cz|księga|tom|"
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

def is_non_chapter_heading_text(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return True
    compact_alpha = re.sub(r"[^a-z]", "", normalized.lower())
    if compact_alpha.endswith(("publishing", "press", "publisher")):
        return True
    if re.match(r"^for\s+.{2,80}[.!?]$", normalized, re.IGNORECASE) and len(normalized.split()) <= 10:
        return True
    return bool(NON_CHAPTER_TEXT_RE.match(normalized) or BOILERPLATE_TEXT_RE.search(normalized))


def is_plausible_heading_text(text: str, max_chars: int = 180) -> bool:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized or len(normalized) > max_chars:
        return False
    words = normalized.split()
    if len(words) > 22:
        return False
    if re.search(r"https?://|www\.|@", normalized, re.IGNORECASE):
        return False
    if "[footnote:" in normalized.lower():
        return False
    if re.search(r"[.!?]\s*$", normalized) and len(words) > 5:
        return False
    return True


def is_explicit_chapter_title(text: str, lang: str = "en") -> bool:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized or is_non_chapter_heading_text(normalized):
        return False
    if len(normalized) >= 100:
        return False

    if get_chapter_regex(lang).match(normalized):
        return True

    match = EXPLICIT_CHAPTER_RE.match(normalized)
    if match:
        remainder = normalized[match.end():].strip()
        if not remainder:
            return True
        first_char = remainder[0]
        if first_char in ".:;,-—~" or first_char.isupper() or first_char.isdigit():
            return True

    if STANDALONE_CHAPTER_RE.match(normalized):
        return True

    return False


def _norm_values(values) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        raw_values = values.split()
    else:
        raw_values = values
    return [str(value or "").strip().lower() for value in raw_values if str(value or "").strip()]


def _identity_values(block: dict) -> list[str]:
    values = []
    values.extend(_norm_values(block.get("classes", [])))
    values.extend(_norm_values(block.get("id", "")))
    values.extend(_norm_values(block.get("nested_ids", [])))
    values.extend(_norm_values(block.get("roles", [])))
    values.extend(_norm_values(block.get("role", "")))
    values.extend(_norm_values(block.get("epub_types", [])))
    values.extend(_norm_values(block.get("epub_type", "")))
    aria_label = block.get("aria_label", "")
    if aria_label:
        values.append(str(aria_label).strip().lower())
    return values


def _direct_semantic_values(block: dict) -> list[str]:
    values = []
    values.extend(_norm_values(block.get("role", "")))
    values.extend(_norm_values(block.get("epub_type", "")))
    return values


def _class_id_values(block: dict) -> list[str]:
    values = []
    values.extend(_norm_values(block.get("classes", [])))
    values.extend(_norm_values(block.get("id", "")))
    values.extend(_norm_values(block.get("nested_ids", [])))
    return values


def _has_non_chapter_semantics(block: dict) -> bool:
    values = _identity_values(block)
    for value in values:
        parts = set(re.split(r"[^0-9a-zA-Z]+", value))
        if value in NON_CHAPTER_EPUB_TYPES or parts.intersection(NON_CHAPTER_EPUB_TYPES):
            return True
        if any(
            token in value
            for token in (
                "toc", "contents", "nav", "cover", "titlepage", "copyright",
                "license", "gutenberg", "index", "bibliography", "footnote",
                "endnote", "notes", "illustration", "figure-list"
            )
        ):
            return True
    return False


def _has_strong_semantics(block: dict) -> bool:
    direct_values = _direct_semantic_values(block)
    inherited_values = _identity_values(block)
    class_id_values = _class_id_values(block)

    for value in direct_values:
        parts = set(re.split(r"[^0-9a-zA-Z]+", value))
        if value in CHAPTER_EPUB_TYPES or parts.intersection(CHAPTER_EPUB_TYPES):
            return True

    for value in class_id_values:
        parts = set(re.split(r"[^0-9a-zA-Z]+", value))
        if value in CHAPTER_CLASS_EXACT or parts.intersection(CHAPTER_CLASS_EXACT):
            return True
        if CHAPTER_CLASS_RE.search(value):
            return True
        if CHAPTER_ID_RE.search(value):
            return True

    tag = str(block.get("tag", "")).lower()
    if tag in HEADING_TAGS:
        for value in inherited_values:
            parts = set(re.split(r"[^0-9a-zA-Z]+", value))
            if value in CHAPTER_EPUB_TYPES or parts.intersection(CHAPTER_EPUB_TYPES):
                return True

    return False


def is_chapter_block(
    block: dict,
    idx_in_doc: int,
    lang: str = "en",
    allow_heading_fallback: bool = True,
) -> bool:
    """
    Checks if a block is a chapter heading, tailored to the book's language.
    """
    tag = block.get("tag", "").lower()
    text = block.get("text", "").strip()
    
    if not text:
        return False

    if is_non_chapter_heading_text(text) or _has_non_chapter_semantics(block):
        return False

    plausible = is_plausible_heading_text(text)

    # 1. Explicit EPUB semantics and known real-world chapter IDs/classes.
    if plausible and _has_strong_semantics(block):
        return True
        
    # 2. Text scanner matching chapter prefixes. CJK headings are safe anywhere;
    # alphabetical first paragraphs are accepted for legacy single-file books.
    if plausible and (lang in ("zh", "ja") or idx_in_doc < 3):
        if get_chapter_regex(lang).match(text):
            return True
            
    # 3. Deep chapter keyword checks are safe anywhere if short and explicit.
    if plausible and is_explicit_chapter_title(text, lang=lang):
        return True

    # 4. Bare heading tags are useful only when the caller has no stronger
    # navigation or semantic signal to rely on.
    if allow_heading_fallback and plausible and tag in HEADING_TAGS:
        return True
            
    return False
