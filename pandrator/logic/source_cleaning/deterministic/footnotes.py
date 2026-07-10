from __future__ import annotations

import os
import re
import collections

# Centralized configurations for all 13 supported languages
LANGUAGE_REGISTRY = {
    "en": {
        "citation_indicators": r"ibid|op\.\s*cit|loc\.\s*cit|see\s+also|see|cf\.?",
        "page_volume_terms": {"p", "pp", "page", "pages", "vol", "vols", "ch", "chap", "trans", "ed", "eds", "press", "univ", "journal", "sec", "section"},
        "exclude_words": {"ibid", "op.cit", "loc.cit", "see", "cf", "p", "pp", "page", "pages", "vol", "vols", "ch", "chap", "trans", "ed", "eds", "press", "univ", "journal", "sec", "section"},
        "chapter_patterns": r"chapter|ch|part|section|book|prologue|epilogue|volume|vol",
        "footnote_anchors": r"fn|footnote|note|ref",
        "verbs": {"is", "are", "was", "were", "be", "been", "have", "has", "had", "do", "does", "did", "can", "could", "will", "would", "should"},
        "connectors": {"because", "although", "since", "however", "therefore", "but", "which", "that", "who", "whom", "whose"},
        "max_true_lower_ratio": 0.35,
        "is_cased": True
    },
    "pl": {
        "citation_indicators": r"tamże|tamze|tam\.?|zob\.?|por\.?|patrz\.?|vide|vid",
        "page_volume_terms": {"s", "ss", "str", "t", "tom", "wyd", "przekł", "przekl", "red"},
        "exclude_words": {"tamże", "tamze", "tam", "zob", "por", "patrz", "vide", "vid", "s", "ss", "str", "t", "tom", "wyd", "przekł", "przekl", "red"},
        "chapter_patterns": r"rozdział|rozdz|część|cz|księga|księgi|wstęp|posłowie|tom|t",
        "footnote_anchors": r"przypis|przyp|prz|przypisy|k|p",
        "verbs": {"jest", "są", "był", "była", "było", "byli", "być", "ma", "mają", "miał", "miała", "mieć"},
        "connectors": {"ponieważ", "chociaż", "jednak", "dlatego", "ale", "który", "która", "które", "że", "bo"},
        "max_true_lower_ratio": 0.50,
        "is_cased": True
    },
    "de": {
        "citation_indicators": r"ebd\.?|vgl\.?|siehe|s\.a\.?|ibid",
        "page_volume_terms": {"s", "seite", "seiten", "bd", "bde", "band", "kap", "kapitel", "u\.a"},
        "exclude_words": {"ebd", "vgl", "siehe", "ibid", "s", "seite", "seiten", "bd", "bde", "band", "kap", "kapitel"},
        "chapter_patterns": r"kapitel|kap|teil|abschnitt|buch|band|bd|prolog|epilog|vorwort|nachwort",
        "footnote_anchors": r"anm|anmerkung|fussnote|fußnote",
        "verbs": {"ist", "sind", "war", "waren", "sein", "haben", "hat", "hatte", "hatten", "wird", "werden", "wurde"},
        "connectors": {"weil", "obwohl", "da", "jedoch", "deshalb", "aber", "der", "die", "das", "dass"},
        "max_true_lower_ratio": 0.40,
        "is_cased": True
    },
    "nl": {
        "citation_indicators": r"ibidem|ibid\.?|zie|zie\s+ook|vgl\.?|cf\.?",
        "page_volume_terms": {"p", "pp", "pag", "deel", "vol", "uitg", "vert"},
        "exclude_words": {"ibidem", "ibid", "zie", "vgl", "cf", "p", "pp", "pag", "deel", "vol", "uitg", "vert"},
        "chapter_patterns": r"hoofdstuk|hst|deel|boek|inleiding|nawoord|proloog|epiloog",
        "footnote_anchors": r"voetnoot|note|ref",
        "verbs": {"is", "zijn", "was", "waren", "hebben", "heeft", "had", "hadden", "worden", "wordt", "werd"},
        "connectors": {"omdat", "hoewel", "aangezien", "echter", "daarom", "maar", "die", "dat", "wie", "wat"},
        "max_true_lower_ratio": 0.50,
        "is_cased": True
    },
    "es": {
        "citation_indicators": r"ibíd|ibídem|ibid\.?|op\.\s*cit|véase|vease|cf\.?|cfr\.?",
        "page_volume_terms": {"p", "pp", "pág", "pag", "págs", "pags", "vol", "tomo", "t", "cap", "ed", "trad"},
        "exclude_words": {"ibíd", "ibídem", "ibid", "op.cit", "véase", "vease", "cf", "cfr", "p", "pp", "pág", "pag", "págs", "pags", "vol", "tomo", "t", "cap", "ed", "trad"},
        "chapter_patterns": r"capítulo|cap|sección|seccion|parte|libro|prólogo|epílogo|introducción|tomo|t",
        "footnote_anchors": r"nota|ref",
        "verbs": {"es", "son", "era", "eran", "ser", "sido", "haber", "ha", "han", "había", "habían", "hacer", "hace"},
        "connectors": {"porque", "aunque", "ya\s+que", "sin\s+embargo", "por\s+lo\s+tanto", "pero", "que", "quien", "el\s+cual"},
        "max_true_lower_ratio": 0.50,
        "is_cased": True
    },
    "it": {
        "citation_indicators": r"ibid\.?|op\.\s*cit|loc\.\s*cit|vedi|cfr\.?",
        "page_volume_terms": {"p", "pp", "pag", "pagg", "vol", "tomo", "t", "cap", "ed", "trad"},
        "exclude_words": {"ibid", "op.cit", "loc.cit", "vedi", "cfr", "p", "pp", "pag", "pagg", "vol", "tomo", "t", "cap", "ed", "trad"},
        "chapter_patterns": r"capitolo|cap|sezione|parte|libro|prologo|epilogo|introduzione|tomo|t",
        "footnote_anchors": r"nota|ref",
        "verbs": {"è", "sono", "era", "erano", "essere", "stato", "avere", "ha", "hanno", "aveva", "avevano", "fare"},
        "connectors": {"perché", "anche\s+se", "poiché", "tuttavia", "quindi", "ma", "che", "cui", "il\s+quale"},
        "max_true_lower_ratio": 0.50,
        "is_cased": True
    },
    "fr": {
        "citation_indicators": r"ibid\.?|op\.\s*cit|loc\.\s*cit|voir|voir\s+aussi|cf\.?|sq\.?",
        "page_volume_terms": {"p", "pp", "page", "pages", "t", "tome", "vol", "ch", "chap", "éd", "ed", "trad"},
        "exclude_words": {"ibid", "op.cit", "loc.cit", "voir", "cf", "sq", "p", "pp", "page", "pages", "t", "tome", "vol", "ch", "chap", "éd", "ed", "trad"},
        "chapter_patterns": r"chapitre|chap|partie|livre|tome|t|volume|vol|prologue|épilogue|préface|avant-propos",
        "footnote_anchors": r"note|ref",
        "verbs": {"est", "sont", "était", "étaient", "être", "été", "avoir", "a", "ont", "avait", "avaient", "faire"},
        "connectors": {"parce\s+que", "bien\s+que", "puisque", "pourtant", "donc", "mais", "qui", "que", "dont", "lequel"},
        "max_true_lower_ratio": 0.50,
        "is_cased": True
    },
    "pt": {
        "citation_indicators": r"ibíd|ibídem|ibid\.?|op\.\s*cit|vide|cf\.?|cfr\.?",
        "page_volume_terms": {"p", "pp", "pág", "pag", "págs", "pags", "vol", "tomo", "t", "cap", "ed", "trad"},
        "exclude_words": {"ibíd", "ibídem", "ibid", "op.cit", "vide", "cf", "cfr", "p", "pp", "pág", "pag", "págs", "pags", "vol", "tomo", "t", "cap", "ed", "trad"},
        "chapter_patterns": r"capítulo|cap|secção|seção|parte|libro|livro|prólogo|epílogo|introdução|tomo|t",
        "footnote_anchors": r"nota|ref",
        "verbs": {"é", "são", "era", "eram", "ser", "sido", "haber", "há", "hão", "havia", "haviam", "ter", "tem"},
        "connectors": {"porque", "embora", "já\s+que", "no\s+entanto", "portanto", "mas", "que", "quem", "o\s+qual"},
        "max_true_lower_ratio": 0.50,
        "is_cased": True
    },
    "hu": {
        "citation_indicators": r"uon\.?|uo\.?|ibidem|lásd|vö\.?|cf\.?",
        "page_volume_terms": {"o", "old", "oldal", "köt", "kötet", "fej"},
        "exclude_words": {"uon", "uo", "ibidem", "lásd", "vö", "cf", "o", "old", "oldal", "köt", "kötet", "fej"},
        "chapter_patterns": r"fejezet|fej|rész|szakasz|kötet|köt|könyv|bevezetés|előszó|utószó",
        "footnote_anchors": r"jegyzet|jegy",
        "verbs": {"van", "volt", "voltak", "lesz", "lesznek", "lenni", "szól", "tartalmaz"},
        "connectors": {"mert", "bár", "mivel", "azonban", "ezért", "de", "amely", "amelyik", "hogy"},
        "max_true_lower_ratio": 0.50,
        "is_cased": True
    },
    "ru": {
        "citation_indicators": r"там\s+же|тамже|см\.?|ср\.?|указ\.\s*соч",
        "page_volume_terms": {"с", "стр", "т", "том", "вып", "изд", "ред", "пер"},
        "exclude_words": {"тамже", "см", "ср", "указ", "соч", "с", "стр", "т", "том", "вып", "изд", "ред", "пер"},
        "chapter_patterns": r"глава|гл|часть|ч|книга|кн|том|т|пролог|эпилог|введение|предисловие",
        "footnote_anchors": r"prim|primech|primechanie|сноска|прим",
        "verbs": {"является", "являются", "был", "была", "было", "были", "есть", "имеет", "имеют"},
        "connectors": {"потому\s+что", "хотя", "так\s+как", "однако", "поэтому", "но", "который", "которая", "которое", "что"},
        "max_true_lower_ratio": 0.50,
        "is_cased": True
    },
    "hi": {
        "citation_indicators": r"यथैव|पूर्ववत्|देखें|तुलना",
        "page_volume_terms": {"पृ", "पृष्ठ", "भाग", "संस्करण"},
        "exclude_words": {"यथैव", "पूर्ववत्", "देखें", "तुलना", "पृ", "पृष्ठ", "भाग", "संस्करण"},
        "chapter_patterns": r"अध्याय|भाग|खण्ड|सर्ग|भूमिका|प्रस्तावना",
        "footnote_anchors": r"टिप्पणी|टिप",
        "verbs": {"है", "हैं", "था", "थी", "थे", "होना", "होता", "होते"},
        "connectors": {"क्योंकि", "यद्यपि", "इसलिए", "लेकिन", "जो", "कि"},
        "max_true_lower_ratio": 0.0,
        "is_cased": False
    },
    "zh": {
        "citation_indicators": r"同上|见|參見|参见",
        "page_volume_terms": {"页", "頁", "卷", "册", "冊", "p", "pp"},
        "exclude_words": {"同上", "见", "參見", "参见", "页", "頁", "卷", "册", "冊"},
        "chapter_patterns": r"第?\s*(?:[0-9]+|[一二三四五六七八九十百千]+)\s*(?:章|部|节|節|卷|册|冊|页|頁)",
        "footnote_anchors": r"注|脚注|尾注",
        "verbs": set(),
        "connectors": set(),
        "max_true_lower_ratio": 0.0,
        "is_cased": False
    },
    "ja": {
        "citation_indicators": r"同上|書参照|参照",
        "page_volume_terms": {"頁", "ページ", "巻", "p", "pp"},
        "exclude_words": {"同上", "書参照", "参照", "頁", "ページ", "巻"},
        "chapter_patterns": r"第?\s*(?:[0-9]+|[一二三四五六七八九十百千]+)\s*(?:章|部|節|巻|冊|頁)|プロローグ|エピローグ",
        "footnote_anchors": r"注|脚注",
        "verbs": set(),
        "connectors": set(),
        "max_true_lower_ratio": 0.0,
        "is_cased": False
    }
}

def detect_book_language(metadata: dict, parsed_documents: dict) -> str:
    """Detects the language of the book from metadata with a robust stopword fallback."""
    lang = (metadata.get("language") or "").strip().lower()
    
    # 1. Normalize metadata language code
    if lang:
        lang_map = {
            "en": "en", "eng": "en",
            "de": "de", "deu": "de", "ger": "de",
            "nl": "nl", "nld": "nl", "dut": "nl",
            "pl": "pl", "pol": "pl",
            "es": "es", "spa": "es",
            "it": "it", "ita": "it",
            "pt": "pt", "por": "pt",
            "fr": "fr", "fra": "fr", "fre": "fr",
            "zh": "zh", "zho": "zh", "chi": "zh",
            "ja": "ja", "jpn": "ja",
            "hu": "hu", "hun": "hu",
            "ru": "ru", "rus": "ru",
            "hi": "hi", "hin": "hi"
        }
        for k, v in lang_map.items():
            if lang.startswith(k):
                return v

    # 2. Text-based detection fallback
    raw_sample = ""
    for doc in parsed_documents.values():
        for block in doc.get("blocks", []):
            text = block.get("text", "")
            if text:
                raw_sample += text + " "
                if len(raw_sample) > 5000:
                    break
        if len(raw_sample) > 5000:
            break
            
    if not raw_sample.strip():
        return "en" # Fallback default
        
    # Check CJK / non-Latin scripts
    if re.search(r'[\u3040-\u309F\u30A0-\u30FF]', raw_sample):
        return "ja"
    if re.search(r'[\u4E00-\u9FFF]', raw_sample):
        return "zh"
    if re.search(r'[\u0400-\u04FF]', raw_sample):
        return "ru"
    if re.search(r'[\u0900-\u097F]', raw_sample):
        return "hi"

    # Count stop words
    words = re.findall(r'\b\w+\b', raw_sample.lower())
    word_counts = collections.Counter(words)
    
    scores = {}
    for lang_code, config in LANGUAGE_REGISTRY.items():
        words_set = config.get("detection_words", set())
        if words_set:
            score = sum(word_counts[w] for w in words_set)
            scores[lang_code] = score
            
    if scores:
        best_lang = max(scores, key=scores.get)
        if scores[best_lang] > 10:
            return best_lang
            
    return "en"

def is_footnote_ref(anchor: dict, lang: str = "en") -> bool:
    """
    Checks if an anchor block part is a footnote reference, using language-specific configurations.
    Always includes English anchor patterns (fn, footnote, note, ref) as a baseline.
    """
    href = anchor.get("href", "")
    if not href or "#" not in href:
        return False
    frag = href.split("#")[-1].lower()
    cls = (anchor.get("class") or "").lower()
    id_val = (anchor.get("id") or "").lower()
    epub_type = (anchor.get("epub_type") or "").lower()
    
    # Standard EPUB3 indicators
    if "noteref" in epub_type or "footnote" in epub_type:
        return True
        
    en_pattern = LANGUAGE_REGISTRY["en"]["footnote_anchors"]
    if lang != "en" and lang in LANGUAGE_REGISTRY:
        lang_pattern = LANGUAGE_REGISTRY[lang]["footnote_anchors"]
        keywords_pattern = f"{en_pattern}|{lang_pattern}"
    else:
        keywords_pattern = en_pattern
    
    # 1. Target file name check (e.g. przypisy.html, note.html)
    target_file = os.path.basename(href.split("#")[0]).lower()
    file_match_pat = r"note|fn|przypis|anm|jegy|prim|сноск"
    if re.search(file_match_pat, target_file) and re.search(r"\d+", frag):
        return True
        
    # 2. Fragment or ID patterns
    if re.search(r"(?:^|_|-)(?:" + keywords_pattern + r")[-_a-zA-Z]*?\d+", frag):
        return True
    if re.search(r"(?:^|_|-)(?:" + keywords_pattern + r")[-_a-zA-Z]*?\d+", id_val):
        return True
        
    # Universal fallback
    if "footnote" in frag or "footnote" in id_val:
        return True
        
    # 3. Class/ID keyword matching
    if re.search(r"\b(?:" + keywords_pattern + r")\b", cls):
        return True
    if re.search(r"\b(?:" + keywords_pattern + r")\b", id_val):
        return True
        
    return False

def clean_footnote_text(text: str) -> str:
    """
    Cleans leading markers (e.g. [1], 1., *, etc.) from footnote text for natural TTS flow.
    """
    cleaned = text.strip()
    cleaned = re.sub(r'^(?:\[(?:fn)?\d+\]|\d+[\.)]|[*†‡])\s*', '', cleaned)
    return cleaned.strip()

def split_sentences_regex(text: str) -> list[str]:
    """
    Fast sentence splitter that handles common abbreviations without look-behind errors.
    """
    pattern = re.compile(r'([.!?])\s+(?=[A-Z\u0100-\u017f\u0400-\u04ff\u4e00-\u9fff])')
    abbreviations = {
        "mr", "st", "dr", "mrs", "ms", "jr", "sr", "prof", "gen", "capt", "col", "lt", 
        "m", "mlle", "mme", "vs", "ca", "eg", "ie", "etc", "rozdzia", "rozdz"
    }
    
    sentences = []
    last_idx = 0
    
    for match in pattern.finditer(text):
        split_idx = match.start(1) + 1
        prev_text = text[max(0, split_idx-15):split_idx-1]
        words = re.findall(r'\b\w+\b$', prev_text)
        if words:
            prev_word = words[0].lower()
            if prev_word in abbreviations:
                continue
                
        sentences.append(text[last_idx:match.end()].strip())
        last_idx = match.end()
        
    if last_idx < len(text):
        sentences.append(text[last_idx:].strip())
        
    return [s for s in sentences if s]

def build_backlink_map(parsed_documents: dict) -> dict[tuple[str, str], dict]:
    """
    Builds a pre-indexed backlink map for the entire EPUB.
    Maps (referencing_doc_basename, number_string) -> target_block (footnote text block)
    """
    backlink_map = {}
    for doc_href, doc in parsed_documents.items():
        for block in doc.get("blocks", []):
            for part in block.get("parts", []):
                if part.get("type") == "anchor":
                    href = part.get("href", "")
                    if "#" in href:
                        target_file, target_frag = href.split("#", 1)
                        target_file_basename = os.path.basename(target_file).lower()
                        target_frag_lower = target_frag.lower()
                        
                        num_match = re.search(r"\d+", target_frag_lower)
                        if num_match:
                            num = num_match.group(0)
                            if "fnanchor" in target_frag_lower or any(kw in target_frag_lower for kw in ["ref", "anchor", "back"]):
                                target_basename = target_file_basename if target_file_basename else os.path.basename(doc_href).lower()
                                backlink_map[(target_basename, num)] = block
    return backlink_map

def find_gutenberg_fallback_footnote(referencing_href: str, frag_id: str, backlink_map: dict) -> dict | None:
    """
    O(1) fallback lookup using the pre-built backlink map.
    """
    num_match = re.search(r"\d+", frag_id)
    if not num_match:
        return None
    num = num_match.group(0)
    ref_basename = os.path.basename(referencing_href).lower()
    return backlink_map.get((ref_basename, num))

def classify_footnote_improved(text: str, lang: str) -> dict:
    """Language-aware footnote citation classifier that protects quotes and grammatical clauses."""
    text_clean = text.strip()
    words = text_clean.split()
    word_count = len(words)
    if word_count == 0:
        return {"class": "reference", "reason": "empty"}
    lower_text = text_clean.lower()
    
    config = LANGUAGE_REGISTRY.get(lang, LANGUAGE_REGISTRY["en"])
    
    # 1. Quoted Text Override (Quotes containing 3+ words are considered narrative quotes)
    quote_matches = re.findall(r'["“«„]([^"”»“„]{10,})["”»“„]', text_clean)
    for qm in quote_matches:
        q_words = qm.strip().split()
        if len(q_words) >= 3:
            return {"class": "narrative", "reason": "quoted_text_override"}

    # CJK Script check
    has_cjk = bool(re.search(r'[\u4e00-\u9fff\u3040-\u309F\u30A0-\u30FF]', text_clean))
    
    # Translation / Explanation keyword markers
    explanation_keywords = [
        r'\bmeans\b', r'\bliterally\b', r'\btranslates\s+to\b', r'\btranslates\s+as\b', 
        r'\bstands\s+for\b', r'\bsignifies\b', r'\bdenotes\b', r'\bliteral\s+meaning\b',
        r'\brefers\s+to\s+the\b', r'\bmeaning\b', r'\brepresent\b', r'\brepresents\b',
        r'\btranslation\b', r'\bfrom\s+\w+\s+meaning\b',
        r'\boznacza\b', r'\bznaczy\b', r'\bdosłownie\b', r'\bprzekład\b', r'\btłumaczenie\b', r'\bczyli\b',
        r'\bbedeutet\b', r'\bwörtlich\b', r'\bheißt\b', r'\bübersetzt\b'
    ]
    has_explanation = any(re.search(pat, lower_text) for pat in explanation_keywords)
    
    if has_cjk or has_explanation:
        return {"class": "narrative", "reason": "explanation_override"}
        
    # Check for strong citation indicators
    strong_cite_pat = config["citation_indicators"]
    latin_cite_pat = r"ibid|ibidem|op\.\s*cit|loc\.\s*cit|cf|cfr|sq|sqq|vide|vid"
    combined_cite_pat = f"(?:{strong_cite_pat})|(?:{latin_cite_pat})"
    
    has_strong_cite = bool(re.search(r'\b(?:' + combined_cite_pat + r')\b', lower_text))
    
    # If it has no digits and no strong citation indicators, it is narrative
    has_digits = any(c.isdigit() for c in text_clean)
    if not has_digits and not has_strong_cite:
        return {"class": "narrative", "reason": "no_digit_no_cite_override"}

    # Calculate True Lowercase Word Ratio
    clean_words = [re.sub(r'^\W+|\W+$', '', w) for w in words]
    clean_words = [w for w in clean_words if w and not w.isdigit()]
    
    exclude_words = config.get("exclude_words", set())
    
    true_lower_words = []
    for w in clean_words:
        if w[0].islower() and w.lower() not in exclude_words:
            true_lower_words.append(w)
            
    true_lower_ratio = len(true_lower_words) / len(clean_words) if clean_words else 0.0
    
    # Verbs and connectors checks
    verb_hits = sum(1 for v in config.get("verbs", set()) if re.search(r'\b' + v + r'\b', lower_text))
    connector_hits = sum(1 for c in config.get("connectors", set()) if re.search(r'\b' + c + r'\b', lower_text))
    narrative_score = verb_hits + connector_hits
    
    # Grammar clause override
    if verb_hits >= 1 and connector_hits >= 1:
        return {"class": "narrative", "reason": "grammar_check_override"}

    if config["is_cased"]:
        max_ratio = config.get("max_true_lower_ratio", 0.35)
        if true_lower_ratio >= max_ratio:
            return {"class": "narrative", "reason": "true_lowercase_ratio"}
            
        cap_words = [w for w in clean_words if w[0].isupper()]
        cap_word_ratio = len(cap_words) / len(clean_words) if clean_words else 0.0
        
        if cap_word_ratio >= 0.45 and narrative_score <= 1 and word_count <= 22:
            return {"class": "reference", "reason": "high_cap_low_narr"}

    # Size limits
    if word_count > 30:
        return {"class": "narrative", "reason": "too_long"}
    if has_strong_cite and word_count <= 8:
        return {"class": "reference", "reason": "short_strong_cite"}
        
    page_regex = r'\b(?:p\.|pp\.|page|pages|s\.|ss\.)\s*\d+'
    has_page_ref = bool(re.search(page_regex, lower_text))
    years = re.findall(r'\b(1[789]\d{2}|20\d{2})\b', text_clean)
    has_year = len(years) > 0
    
    if (has_page_ref or has_year) and narrative_score == 0 and word_count <= 22:
        return {"class": "reference", "reason": "no_narrative_cite"}
        
    digit_chars = sum(c.isdigit() for c in text_clean)
    if word_count <= 12 and digit_chars > 0 and narrative_score == 0:
        return {"class": "reference", "reason": "short_digits_no_narr"}
        
    return {"class": "narrative", "reason": "default_narrative"}

def is_backlink_fragment(frag: str) -> bool:
    frag = frag.lower()
    if any(k in frag for k in ["backlink", "back", "fnanchor", "refanchor", "fnref"]):
        return True
    if frag.endswith("-ref") or frag.endswith("_ref") or frag.endswith("ref"):
        if re.search(r'\w+-ref\d*$', frag) or re.search(r'\w+_ref\d*$', frag):
            return True
        if frag.endswith("-ref") or frag.endswith("_ref"):
            return True
    if re.match(r'^r(?:fn|note|ref|appfn|memfn|pgfn|pgixfn|pgvi|bmfn|chfn|ch_fn|intfn|prefacefn)\d*', frag):
        return True
    return False

def get_footnote_text_multi_block(blocks: list[dict], start_idx: int, doc_href: str, resolved_set: set) -> tuple[str, list[int]]:
    start_block = blocks[start_idx]
    start_text = start_block["text"].strip()
    consumed_indices = [start_idx]
    
    if start_text:
        return start_text, consumed_indices
        
    texts = []
    curr_idx = start_idx + 1
    while curr_idx < len(blocks):
        b = blocks[curr_idx]
        if b["id"]:
            break
        if b["text"].strip():
            texts.append(b["text"])
        consumed_indices.append(curr_idx)
        curr_idx += 1
        
    return " ".join(texts), consumed_indices

def reposition_footnotes_in_document(
    doc_href: str,
    blocks: list[dict],
    parsed_documents: dict,
    repositioned_block_ids: set[tuple[str, int]],
    remove_footnotes: bool = False,
    filter_citations: bool = True,
    detected_lang: str = "en",
    backlink_map: dict | None = None
) -> list[str]:
    """
    Repositions footnotes in a document inline, immediately following their marker sentences.
    Modifies repositioned_block_ids to track which blocks have been consumed.
    If remove_footnotes is True, resolves and consumes footnotes without injecting them.
    If filter_citations is True, bibliographic citation references are stripped (skipped) deterministically.
    """
    cleaned_lines = []
    
    for block in blocks:
        block_idx = block["block_index"]
        if (doc_href, block_idx) in repositioned_block_ids:
            continue
            
        parts = block.get("parts", [])
        
        # Filter anchors to reject backlinks and cross-references (xref)
        anchors = []
        for p in parts:
            if p["type"] == "anchor" and is_footnote_ref(p, detected_lang):
                cls_val = (p.get("class") or "").lower()
                id_val = (p.get("id") or "").lower()
                if "xref" in cls_val or "reference" in cls_val or "xref" in id_val:
                    continue
                href_val = p.get("href", "")
                frag_val = href_val.split("#")[-1] if "#" in href_val else ""
                if is_backlink_fragment(frag_val):
                    continue
                anchors.append(p)
        
        if not anchors:
            cleaned_lines.append(block["text"])
            continue
            
        plain_text = block["text"]
        
        # Resolve target footnote blocks
        resolved_footnotes = []
        for anchor in anchors:
            href = anchor.get("href", "")
            target_file_rel, frag_id = href.split("#", 1) if "#" in href else ("", "")
            
            if not target_file_rel:
                target_href = doc_href
            else:
                target_href = target_file_rel
                
            matched_doc_key = None
            for key in parsed_documents:
                if os.path.basename(key).lower() == os.path.basename(target_href).lower():
                    matched_doc_key = key
                    break
                    
            target_block = None
            consumed_idxs = []
            target_doc_key_resolved = ""
            
            if matched_doc_key:
                target_doc = parsed_documents[matched_doc_key]
                if frag_id in target_doc.get("ids", {}):
                    block_idx_target = target_doc["ids"][frag_id]
                    target_block_text, consumed_idxs = get_footnote_text_multi_block(target_doc["blocks"], block_idx_target, matched_doc_key, repositioned_block_ids)
                    target_block = {
                        "text": target_block_text,
                        "href": matched_doc_key,
                        "block_index": block_idx_target
                    }
                    target_doc_key_resolved = matched_doc_key
                else:
                    if backlink_map is None:
                        backlink_map = build_backlink_map(parsed_documents)
                    fallback = find_gutenberg_fallback_footnote(doc_href, frag_id, backlink_map)
                    if fallback:
                        target_block = fallback
                        consumed_idxs = [fallback["block_index"]]
                        target_doc_key_resolved = fallback["href"]
                        
            if target_block:
                fn_text = clean_footnote_text(target_block["text"])
                if fn_text and not remove_footnotes:
                    # Apply bibliographic citation filter
                    if filter_citations:
                        res = classify_footnote_improved(fn_text, detected_lang)
                        if res["class"] == "reference":
                            # Consume the blocks anyway to prevent backlink loops, but do not inject them
                            for c_idx in consumed_idxs:
                                repositioned_block_ids.add((target_doc_key_resolved, c_idx))
                            continue
                            
                    resolved_footnotes.append((anchor, fn_text))
                    for c_idx in consumed_idxs:
                        repositioned_block_ids.add((target_doc_key_resolved, c_idx))
                    
        if not resolved_footnotes:
            cleaned_lines.append(plain_text)
            continue
            
        # Split block text into sentences and map footnote text blocks to them
        sentences = split_sentences_regex(plain_text)
        sentence_footnotes = collections.defaultdict(list)
        
        search_start = 0
        for anchor, fn_text in resolved_footnotes:
            anchor_content = anchor.get("content", "").strip()
            if not anchor_content:
                sentence_footnotes[len(sentences) - 1].append(fn_text)
                continue
                
            pos = plain_text.find(anchor_content, search_start)
            if pos == -1:
                clean_content = re.sub(r'\D+', '', anchor_content)
                if clean_content:
                    pos = plain_text.find(clean_content, search_start)
                    
            if pos == -1:
                sentence_footnotes[len(sentences) - 1].append(fn_text)
                continue
                
            search_start = pos + len(anchor_content)
            
            # Find the matching sentence index
            curr_len = 0
            found_idx = len(sentences) - 1
            for s_idx, s in enumerate(sentences):
                s_pos = plain_text.find(s, curr_len)
                if s_pos != -1:
                    s_end = s_pos + len(s)
                    if s_pos <= pos <= s_end:
                        found_idx = s_idx
                        curr_len = s_end
                        break
                    curr_len = s_end
            
            sentence_footnotes[found_idx].append(fn_text)
            
        # Rebuild the sentences with inline footnotes
        final_sentences = []
        for s_idx, s in enumerate(sentences):
            final_sentences.append(s)
            if s_idx in sentence_footnotes:
                for fn in sentence_footnotes[s_idx]:
                    final_sentences.append(f" [Footnote: {fn}]")
                    
        cleaned_lines.append("".join(final_sentences))
        
    return cleaned_lines

def _looks_like_dedicated_note_document(parsed_doc: dict | None) -> bool:
    """Require local note evidence before trusting a generic ``notes`` name."""
    if not parsed_doc:
        return False

    evidence = 0
    note_id_re = re.compile(
        r"(?:^|[-_:.])(?:fn|footnote|endnote|note|noteref|przypis|nts)[-_:.]?\d*\b",
        re.IGNORECASE,
    )
    for block in parsed_doc.get("blocks", []):
        values = [block.get("id", ""), *block.get("nested_ids", [])]
        if any(note_id_re.search(str(value or "")) for value in values):
            evidence += 1
        for part in block.get("parts", []):
            epub_type = str(part.get("epub_type", "")).lower()
            href_value = str(part.get("href", "")).lower()
            if "footnote" in epub_type or "noteref" in epub_type or "backlink" in href_value:
                evidence += 1
        if evidence >= 2:
            return True
    return False


def is_footnote_file(
    href: str,
    size: int,
    parsed_doc: dict | None = None,
    spine_item: dict | None = None,
) -> bool:
    """
    Determines if a file in the spine is a dedicated footnote/endnote container file.
    """
    properties = {str(value).lower() for value in (spine_item or {}).get("properties", [])}
    if properties.intersection({"footnotes", "endnotes", "doc-footnotes", "doc-endnotes"}):
        return True

    base_name = os.path.splitext(os.path.basename(href).lower())[0]
    # Specific note names are useful filename signals. Deliberately exclude
    # generic ``notes`` here: a title like ``Notes_Off_The_Cuff`` is ordinary
    # narrative content.
    if re.search(
        r"(?:^|[_\-.])(?:fns?|footnotes?|endnotes?|przypisy?|nts)(?:\d+)?(?:$|[_\-.])",
        base_name,
    ):
        return True

    if re.fullmatch(r"(?:notes?|endnotes?|przypisy?|przypis)(?:[_\-.]?\d+)?", base_name):
        return _looks_like_dedicated_note_document(parsed_doc)

    return False
