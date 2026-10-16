import concurrent.futures
import difflib
import re
import unicodedata

import hasami
import regex
from num2words import num2words
from sentence_splitter import SentenceSplitter
from unidecode import unidecode

from . import nemo_normalizer, sentence_segmenter
from .dialogue_flow import classify_dialogue_paragraphs
from .dubbing.languages import normalize_language_code
from .dubbing.natural_boundaries import natural_split_candidates
from .dubbing.text_units import (
    clean_text,
    contains_cjk,
    fragment_separator,
    strip_latin_diacritics,
)

CHUNK_SIZE = 20000
CHAPTER_MARKER = "[[Chapter]]"
CHAPTER_LINE_RE = re.compile(rf"(?m)^[ \t]*{re.escape(CHAPTER_MARKER)}[^\n]*$")
SCENE_GAP_MARKER = "\uE000PANDRATOR_SCENE_GAP\uE001"

DOUBLE_QUOTATION_MARKS = (
    "\""
    "\u201c\u201d\u201e\u201f"
    "\u00ab\u00bb\u2039\u203a"
    "\u300c\u300d\u300e\u300f\u300a\u300b"
    "\u301d\u301e\uff02"
)
SINGLE_QUOTATION_MARKS = "'\u2018\u2019\u201a\u201b\u0060\u00b4"

DOUBLE_QUOTATION_MARK_TRANSLATION_TABLE = str.maketrans("", "", DOUBLE_QUOTATION_MARKS)
SINGLE_QUOTATION_MARK_PATTERN = re.compile(
    rf"(?<!\w)[{re.escape(SINGLE_QUOTATION_MARKS)}]|[{re.escape(SINGLE_QUOTATION_MARKS)}](?!\w)"
)
CLOSING_SPLIT_PUNCTUATION = "\"')]}“‘”’»›」』》〟〞〃）］｝〕】〉》〘〙〚〛"

TERMINAL_PUNCTUATION = frozenset(".!?…。！？｡．‥⋯﹒﹖﹗")
CLOSING_TERMINAL_PUNCTUATION = frozenset(
    "\"'“‘”’»›」』》〟〞〃)]}）］｝〕】〉》〘〙〚〛"
)
OPENING_NARRATION_QUOTES = frozenset("‘‚‛“„‟«‹「『《〝")

SENTENCE_SPLITTER_SUPPORTED_LANGUAGES = {
    "ca",
    "cs",
    "da",
    "de",
    "el",
    "en",
    "es",
    "fi",
    "fr",
    "hu",
    "is",
    "it",
    "lt",
    "lv",
    "nl",
    "no",
    "pl",
    "pt",
    "ro",
    "ru",
    "sk",
    "sl",
    "sv",
    "tr",
}

SENTENCE_SPLITTER_LANGUAGE_ALIASES = {
    "en-us": "en",
    "en-gb": "en",
    "fr-fr": "fr",
    "pt-br": "pt",
}


def strip_quotation_marks(text: str) -> str:
    without_double_quotes = text.translate(DOUBLE_QUOTATION_MARK_TRANSLATION_TABLE)
    return SINGLE_QUOTATION_MARK_PATTERN.sub("", without_double_quotes)

def _protect_chapter_marker_lines(text: str) -> str:
    """Keep Pandrator chapter markers as standalone block boundaries."""
    if CHAPTER_MARKER not in text:
        return text

    def replace_marker(match: re.Match) -> str:
        return f"\n\n{match.group(0).strip()}\n\n"

    protected = CHAPTER_LINE_RE.sub(replace_marker, text)
    return re.sub(r"\n{3,}", "\n\n", protected)


def _mark_scene_gaps(text: str) -> tuple[str, str]:
    """Preserve three-or-more source newlines as an in-memory hard barrier."""
    marker = SCENE_GAP_MARKER
    while marker in text:
        marker += "_"
    marked = re.sub(
        r"\n(?:[ \t]*\n){2,}",
        f"\n\n{marker}\n\n",
        text,
    )
    return marked, marker


def _isolate_scene_gap_markers(text: str, marker: str) -> str:
    """Restore paragraph isolation if a format normalizer flattened newlines."""
    return re.sub(
        rf"[ \t\r\n]*{re.escape(marker)}[ \t\r\n]*",
        f"\n\n{marker}\n\n",
        text,
    )


def _split_structural_text_blocks(text: str) -> list[dict]:
    if CHAPTER_MARKER not in text:
        return [{"type": "text", "text": text}]

    blocks = []
    position = 0
    for match in CHAPTER_LINE_RE.finditer(text):
        body = text[position:match.start()]
        if body.strip():
            blocks.append({"type": "text", "text": body})

        marker_line = match.group(0).strip()
        title = marker_line[len(CHAPTER_MARKER):].strip()
        title = re.sub(r"\s+", " ", title)
        if title:
            blocks.append({"type": "chapter", "text": title})
        position = match.end()

    body = text[position:]
    if body.strip():
        blocks.append({"type": "text", "text": body})

    return blocks


def _ensure_line_terminal_punctuation(text: str) -> str:
    """Treat one newline as wrapping and two or more as a paragraph break."""
    normalized = re.sub(r"\r\n?", "\n", text)
    paragraphs = []
    for paragraph in re.split(r"\n{2,}", normalized):
        paragraph = re.sub(r"[ \t]*\n[ \t]*", " ", paragraph)
        paragraph = re.sub(r"[ \t]+", " ", paragraph).strip()
        if not paragraph:
            continue

        terminal_index = len(paragraph)
        while (
            terminal_index > 0
            and (
                paragraph[terminal_index - 1] in CLOSING_TERMINAL_PUNCTUATION
                or paragraph[terminal_index - 1].isspace()
            )
        ):
            terminal_index -= 1
        if (
            terminal_index == 0
            or paragraph[terminal_index - 1] not in TERMINAL_PUNCTUATION
        ):
            paragraph += "."
        paragraphs.append(paragraph)
    return "\n\n".join(paragraphs)


# ---------------------------------------------------------------------------
# All-caps normalization
# ---------------------------------------------------------------------------

# Words strictly longer than this are "anchor" words (stylistic caps) in body text.
# Words of this length or shorter are assumed to be acronyms/abbreviations.
_CAPS_ACRONYM_MAX_LEN: int = 4

# Lower threshold applied to all-caps-only lines (chapter titles, play-script
# speaker cues, photo captions). Catches short proper names like OLGA or ANYA.
_CAPS_HEADING_MAX_LEN: int = 3

# Known acronyms that must never be title-cased regardless of length.
_CAPS_KNOWN_ACRONYMS: frozenset = frozenset({
    # Countries / intergovernmental
    "USA", "USSR", "UK", "EU", "UN", "NATO", "WHO", "IMF", "WTO",
    "UNESCO", "UNICEF", "UNHCR", "UNRWA", "IAEA", "OECD", "OPEC", "NAFTA",
    "INTERPOL",
    # Intelligence / law enforcement
    "FBI", "CIA", "NSA", "MI5", "MI6", "KGB", "CDC", "FDA", "EPA",
    "IRS", "DOD", "DOJ",
    # Tech / web
    "HTML", "HTTP", "HTTPS", "JSON", "XML", "API", "URL", "ISBN", "PDF",
    "CPU", "GPU", "RAM", "ROM", "USB", "TCP", "IP", "DNS", "FTP", "SSH",
    # Medical / science
    "PTSD", "ADHD", "AIDS", "HIV", "DNA", "RNA", "MRI", "ICU",
    # Physics / engineering (5-char words that would otherwise become anchors)
    "LASER", "RADAR", "SONAR", "LIDAR",
    # Common initialisms
    "ASAP", "RSVP", "FAQ", "OK", "AM", "PM", "BC", "AD", "VS",
    "AKA", "ETA", "ETC", "BTW", "FYI", "DIY", "ESP", "IQ",
    # Military
    "HQ", "POW", "MIA", "KIA", "AWOL", "NCO",
    # Religious / other
    "YHWH",
})

# Connector / article words that are lowercased when inside a normalized run.
_CAPS_CONNECTORS: frozenset = frozenset({
    "A", "AN", "THE", "AND", "OR", "NOR", "BUT", "FOR", "SO", "YET",
    "OF", "IN", "ON", "AT", "TO", "BY", "AS", "UP", "IT", "IS",
    "WITH", "FROM", "INTO", "ONTO", "UPON", "OVER", "UNDER", "ABOUT",
    "BETWEEN", "AMONG", "THROUGH", "DURING", "BEFORE", "AFTER",
    "ABOVE", "BELOW", "AGAINST",
    "DO",  # Polish preposition "do" (to / of)
})

# Roman numerals — standalone matches only.
_CAPS_ROMAN_RE = re.compile(
    r"^M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})$",
    re.IGNORECASE,
)


def _is_all_caps_context(line: str) -> bool:
    """Return True if every alphabetic word in *line* is all-uppercase.

    Used to detect headings / speaker-cue lines where the lower threshold is safe.
    """
    alpha_words = [w for w in line.split() if w.isalpha()]
    return bool(alpha_words) and all(w.isupper() for w in alpha_words)


def _normalize_caps_run(words: list, max_len: int) -> tuple:
    """Decide how to normalize a contiguous run of ALL-CAPS tokens.

    An *anchor* word is one whose length exceeds *max_len*, is not a known
    acronym, and is not a Roman numeral.  If the run contains at least one
    anchor the whole run is eligible for normalization:

    * Anchor words            → title-cased (capitalize()).
    * Connector words         → lowercased.
    * Short non-connector words adjacent to an anchor → title-cased.
    * Known acronyms / Roman numerals → always left unchanged.

    Returns (new_words, changed_flag).
    """
    anchor_positions: set = set()
    for i, w in enumerate(words):
        if (
            len(w) > max_len
            and w not in _CAPS_KNOWN_ACRONYMS
            and not _CAPS_ROMAN_RE.fullmatch(w)
        ):
            anchor_positions.add(i)

    if not anchor_positions:
        return words, False

    result: list = []
    changed = False

    for i, w in enumerate(words):
        if w in _CAPS_KNOWN_ACRONYMS:
            result.append(w)
            continue
        if _CAPS_ROMAN_RE.fullmatch(w):
            result.append(w)
            continue
        if i in anchor_positions:
            tc = w.capitalize()
            result.append(tc)
            if tc != w:
                changed = True
            continue
        # Short word
        if w in _CAPS_CONNECTORS:
            lc = w.lower()
            result.append(lc)
            if lc != w:
                changed = True
        elif (i - 1) in anchor_positions or (i + 1) in anchor_positions:
            tc = w.capitalize()
            result.append(tc)
            if tc != w:
                changed = True
        else:
            result.append(w)

    return result, changed


def _normalize_caps_line(line: str, max_len: int) -> str:
    """Find and normalize runs of ALL-CAPS tokens in a single line.

    A *run* is a maximal sequence of [A-Z]{2,} tokens separated only by
    whitespace.  Non-whitespace characters break the run.
    """
    segments: list = []
    pos = 0
    for m in re.finditer(r"[A-Z]{2,}", line):
        if m.start() > pos:
            segments.append((pos, m.start(), False))
        segments.append((m.start(), m.end(), True))
        pos = m.end()
    if pos < len(line):
        segments.append((pos, len(line), False))

    if not segments:
        return line

    # Group caps tokens into runs; pure-whitespace gaps are allowed inside a run.
    runs: list = []
    current_run: list = []
    for seg_start, seg_end, is_caps in segments:
        if is_caps:
            current_run.append((seg_start, seg_end))
        elif line[seg_start:seg_end].strip() == "":
            pass  # whitespace gap — run continues
        else:
            if current_run:
                runs.append(current_run)
                current_run = []
    if current_run:
        runs.append(current_run)

    if not runs:
        return line

    # Apply replacements in reverse order to keep earlier offsets valid.
    replacements: list = []
    for run in runs:
        words = [line[s:e] for s, e in run]
        new_words, changed = _normalize_caps_run(words, max_len)
        if changed:
            new_run_text = ""
            for i, (new_word, (s, _e)) in enumerate(
                zip(new_words, run, strict=False)
            ):
                if i > 0:
                    new_run_text += line[run[i - 1][1]:s]
                new_run_text += new_word
            replacements.append((run[0][0], run[-1][1], new_run_text))

    rebuilt = line
    for start, end, replacement in sorted(replacements, reverse=True):
        rebuilt = rebuilt[:start] + replacement + rebuilt[end:]
    return rebuilt


def normalize_all_caps_words(text: str) -> str:
    """Normalize stylistic ALL-CAPS words in *text* to title-case.

    Operates line-by-line.  For lines that consist entirely of uppercase words
    (chapter titles, speaker cues, photo labels) a lower word-length threshold
    is applied, catching short proper names like OLGA or ANYA.  For lines with
    mixed-case context the conservative threshold protects common short acronyms
    (NATO, FIFA, YMCA …).

    Known acronyms and Roman numerals are always left unchanged.
    """
    result_lines = []
    for line in text.split("\n"):
        # Strip the chapter marker before context detection.
        check_line = line
        if check_line.startswith("[[Chapter]]"):
            check_line = check_line[len("[[Chapter]]"):].strip()
        max_len = _CAPS_HEADING_MAX_LEN if _is_all_caps_context(check_line) else _CAPS_ACRONYM_MAX_LEN
        result_lines.append(_normalize_caps_line(line, max_len))
    return "\n".join(result_lines)


def preprocess_text(text: str, settings: dict, progress_callback=None) -> list[dict]:
    """
    Main entry point for text preprocessing. Chooses parallel or sequential.
    'settings' is a dictionary containing all necessary parameters.
    """
    if settings.get("enable_nemo_normalization", True):
        if progress_callback:
            progress_callback(0.05, "Normalizing narration text")
        text = nemo_normalizer.normalize_text_for_tts(text, settings.get("language", "en"))
    if progress_callback:
        progress_callback(0.2, "Splitting narration into processing chunks")

    preserve_dialogue_context = (
        settings.get("audiobook_preparation") is True
        and settings.get("audiobook_dialogue_flow") == "auto"
    )
    if (
        len(text) > CHUNK_SIZE
        and not sentence_segmenter.is_available()
        and not preserve_dialogue_context
    ):
        processed_sentences = _parallel_preprocess_text(
            text,
            settings,
            progress_callback=progress_callback,
        )
    else:
        processed_sentences = _sequential_preprocess_text(text, settings)
        if progress_callback:
            progress_callback(0.9, "Narration chunk processed")
    
    if progress_callback:
        progress_callback(0.95, "Finalizing narration boundaries")
    processed_sentences = merge_consecutive_chapters(
        processed_sentences,
        max_sentence_length=settings.get('max_sentence_length', 160),
    )
    for i, sentence in enumerate(processed_sentences, start=1):
        sentence['sentence_number'] = str(i)
    if progress_callback:
        progress_callback(1.0, f"Prepared {len(processed_sentences)} narration segments")
    return processed_sentences

def _parallel_preprocess_text(text: str, settings: dict, progress_callback=None) -> list[dict]:
    chunks = _split_text_into_chunks(text)
    
    processed_chunks: list[list[dict] | None] = [None] * len(chunks)

    with concurrent.futures.ProcessPoolExecutor() as executor:
        future_to_index = {executor.submit(_process_chunk, chunk, settings): i
                           for i, chunk in enumerate(chunks)}

        completed = 0
        for future in concurrent.futures.as_completed(future_to_index):
            index = future_to_index[future]
            processed_chunks[index] = future.result()
            completed += 1
            if progress_callback:
                progress_callback(
                    0.2 + 0.7 * (completed / max(1, len(chunks))),
                    f"Processed narration chunk {completed} of {len(chunks)}",
                )

    all_processed_sentences = [sentence for chunk in processed_chunks if chunk for sentence in chunk]

    for i, sentence in enumerate(all_processed_sentences, start=1):
        sentence['sentence_number'] = str(i)
        
    return all_processed_sentences

def _sequential_preprocess_text(text: str, settings: dict) -> list[dict]:
    processed = _process_chunk(text, settings)
    for i, sentence in enumerate(processed, start=1):
        sentence['sentence_number'] = str(i)
    return processed

def _process_chunk(chunk: str, settings: dict) -> list[dict]:
    if settings.get("audiobook_preparation") is True:
        return _process_audiobook_chunk(chunk, settings)
    return _process_chunk_legacy(chunk, settings)


def _process_chunk_legacy(chunk: str, settings: dict) -> list[dict]:
    """Retain the existing segmentation policy for non-audiobook callers."""
    pdf_preprocessed = settings.get('pdf_preprocessed', False)
    source_file = settings.get('source_file', '')
    disable_paragraph_detection = settings.get('disable_paragraph_detection', False)
    language = normalize_language_code(settings.get('language', 'en'))
    max_sentence_length = settings.get('max_sentence_length', 160)
    enable_sentence_splitting = settings.get('enable_sentence_splitting', True)
    enable_sentence_appending = settings.get('enable_sentence_appending', True)
    remove_diacritics = settings.get('remove_diacritics', False)
    remove_quotation_marks = settings.get('remove_quotation_marks', False)
    normalize_all_caps = settings.get('normalize_all_caps', False)
    tts_service = settings.get('tts_service', 'XTTS')

    chunk = re.sub(r'\r\n?', '\n', chunk)
    chunk = _protect_chapter_marker_lines(chunk)

    if not disable_paragraph_detection:
        if pdf_preprocessed or source_file.endswith("_edited.txt"):
            pass
        elif source_file.endswith(".pdf"):
            chunk = preprocess_text_pdf(chunk)
        else:
            chunk = '\n\n'.join(clean_text(paragraph) for paragraph in re.split(r'\n{2,}', chunk))

    chunk = re.sub(r'\t', ' ', chunk)

    if remove_diacritics:
        if contains_cjk(chunk):
            chunk = strip_latin_diacritics(chunk)
        else:
            chunk = ''.join(char for char in chunk if not unicodedata.combining(char))
            chunk = unidecode(chunk)

    if remove_quotation_marks:
        chunk = strip_quotation_marks(chunk)

    if normalize_all_caps:
        chunk = normalize_all_caps_words(chunk)

    processed_sentences = []

    for block in _split_structural_text_blocks(chunk):
        if block["type"] == "chapter":
            if processed_sentences and processed_sentences[-1].get("chapter") != "yes":
                processed_sentences[-1]["paragraph"] = "yes"

            title = block["text"].strip()
            if not title:
                continue
            processed_sentences.append(
                {
                    "original_sentence": title,
                    "paragraph": "yes",
                    "chapter": "yes",
                    "split_part": None,
                    "sentence_continues_after": False,
                }
            )
            continue

        for paragraph_text in re.split(r"\n{2,}", block["text"]):
            body_text = _ensure_line_terminal_punctuation(paragraph_text.strip())
            if not body_text:
                continue

            sentences = [
                sentence for sentence in split_into_sentences(
                    body_text, language, tts_service
                ) if sentence.strip()
            ]

            for sentence_index, sentence in enumerate(sentences):
                sentence_dict = {
                    "original_sentence": sentence,
                    "paragraph": "yes" if sentence_index == len(sentences) - 1 else "no",
                    "chapter": "no",
                    "split_part": None,
                    "sentence_continues_after": False,
                }

                if enable_sentence_splitting:
                    processed_sentences.extend(
                        split_long_sentences(sentence_dict, max_sentence_length, language)
                    )
                else:
                    processed_sentences.append(sentence_dict)

    if enable_sentence_appending:
        processed_sentences = append_short_sentences(
            processed_sentences, max_sentence_length
        )

    split_sentences = []
    for sentence_dict in processed_sentences:
        if sentence_dict.get("chapter") == "yes":
            split_sentences.append(sentence_dict)
        else:
            split_sentences.extend(
                split_long_sentences_2(sentence_dict, max_sentence_length, language)
            )

    max_words = settings.get("max_segment_words")
    if isinstance(max_words, int) and not isinstance(max_words, bool) and max_words > 0:
        split_sentences = [
            part
            for segment in split_sentences
            for part in _limit_segment_words(segment, max_words, language)
        ]
    byte_budget = settings.get("max_segment_tokenizer_bytes")
    if isinstance(byte_budget, int) and not isinstance(byte_budget, bool) and byte_budget > 0:
        split_sentences = [
            part
            for segment in split_sentences
            for part in _limit_segment_tokenizer_bytes(segment, byte_budget, language)
        ]
    return split_sentences


def _process_audiobook_chunk(chunk: str, settings: dict) -> list[dict]:
    """
    Processes a single chunk of text. This is the core logic.
    """
    pdf_preprocessed = settings.get('pdf_preprocessed', False)
    source_file = settings.get('source_file', '')
    disable_paragraph_detection = settings.get('disable_paragraph_detection', False)
    language = normalize_language_code(settings.get('language', 'en'))
    max_sentence_length = settings.get('max_sentence_length', 160)
    enable_sentence_appending = settings.get('enable_sentence_appending', True)
    remove_diacritics = settings.get('remove_diacritics', False)
    remove_quotation_marks = settings.get('remove_quotation_marks', False)
    normalize_all_caps = settings.get('normalize_all_caps', False)
    tts_service = settings.get('tts_service', 'XTTS')



    chunk = re.sub(r'\r\n?', '\n', chunk)
    chunk, scene_gap_marker = _mark_scene_gaps(chunk)
    # Prepared text is also reader-facing. Preserve its typography unless an
    # explicit processing option below requests a change; provider-specific
    # pronunciation changes belong at the speech boundary.
    chunk = _protect_chapter_marker_lines(chunk)

    if not disable_paragraph_detection:
        if pdf_preprocessed or source_file.endswith("_edited.txt"):
            pass
        elif source_file.endswith(".pdf"):
            chunk = preprocess_text_pdf(chunk)
        else:
            chunk = '\n\n'.join(clean_text(paragraph) for paragraph in re.split(r'\n{2,}', chunk))

    chunk = re.sub(r'\t', ' ', chunk)
    if source_file.endswith(".pdf") and not pdf_preprocessed:
        chunk = _isolate_scene_gap_markers(chunk, scene_gap_marker)

    chunk_contains_cjk = contains_cjk(chunk)

    processed_sentences = []

    for block in _split_structural_text_blocks(chunk):
        if block["type"] == "chapter":
            if processed_sentences and processed_sentences[-1].get("chapter") != "yes":
                processed_sentences[-1]["paragraph"] = "yes"

            title = block["text"].strip()
            if not title:
                continue
            if remove_diacritics:
                if chunk_contains_cjk:
                    title = strip_latin_diacritics(title)
                else:
                    title = ''.join(char for char in title if not unicodedata.combining(char))
                    title = unidecode(title)
            if remove_quotation_marks:
                title = strip_quotation_marks(title)
            if normalize_all_caps:
                title = normalize_all_caps_words(title)
            processed_sentences.append(
                {
                    "original_sentence": title,
                    "paragraph": "yes",
                    "chapter": "yes",
                    "split_part": None,
                    "sentence_continues_after": False,
                }
            )
            continue

        # Classify the authored paragraph before optional quote stripping and
        # sentence splitting.  A three-newline scene marker and chapter block
        # both interrupt adjacency.
        paragraph_texts = re.split(r"\n{2,}", block["text"])
        dialogue_annotations = (
            classify_dialogue_paragraphs(paragraph_texts, language)
            if settings.get("audiobook_dialogue_flow") == "auto"
            else [None] * len(paragraph_texts)
        )

        # Segment each source paragraph independently.  A sentence segmenter
        # can return a single list for text spanning blank lines, which makes
        # the boundary impossible to recover reliably after sentence appending.
        for paragraph_index, paragraph_text in enumerate(paragraph_texts):
            if paragraph_text.strip() == scene_gap_marker:
                continue

            if remove_diacritics:
                if chunk_contains_cjk:
                    paragraph_text = strip_latin_diacritics(paragraph_text)
                else:
                    paragraph_text = ''.join(
                        char for char in paragraph_text if not unicodedata.combining(char)
                    )
                    paragraph_text = unidecode(paragraph_text)
            if remove_quotation_marks:
                paragraph_text = strip_quotation_marks(paragraph_text)
            if normalize_all_caps:
                paragraph_text = normalize_all_caps_words(paragraph_text)

            body_text = _ensure_line_terminal_punctuation(paragraph_text.strip())
            if not body_text:
                continue

            sentences = [
                sentence for sentence in split_into_sentences(
                    body_text, language, tts_service
                ) if sentence.strip()
            ]

            dialogue_annotation = dialogue_annotations[paragraph_index]
            for sentence_index, sentence in enumerate(sentences):
                sentence_dict = {
                    "original_sentence": sentence,
                    "paragraph": "yes" if sentence_index == len(sentences) - 1 else "no",
                    "chapter": "no",
                    "split_part": None,
                    "sentence_continues_after": False,
                }
                if (
                    dialogue_annotation is not None
                    and sentence_index == len(sentences) - 1
                ):
                    sentence_dict["dialogue_flow"] = {
                        "source": dialogue_annotation["source"],
                        "paragraph_count": dialogue_annotation["paragraph_count"],
                        "rule_language": dialogue_annotation["rule_language"],
                    }
                    sentence_dict["speech_boundary_after"] = dialogue_annotation[
                        "speech_boundary_after"
                    ]
                processed_sentences.append(sentence_dict)

    # Split only oversized source sentences, and do so before greedy packing.
    # This prevents a short final part of one sentence from pulling words out
    # of the next complete sentence merely to fill a block.
    split_sentences = []
    for sentence_dict in processed_sentences:
        if sentence_dict.get("chapter") == "yes":
            split_sentences.append(sentence_dict)
        else:
            split_sentences.extend(
                split_long_sentences_2(
                    sentence_dict,
                    max_sentence_length,
                    language,
                    audiobook_preparation=True,
                )
            )

    max_words = settings.get("max_segment_words")
    if isinstance(max_words, int) and not isinstance(max_words, bool) and max_words > 0:
        split_sentences = [
            part
            for segment in split_sentences
            for part in _limit_segment_words(
                segment, max_words, language, audiobook_preparation=True
            )
        ]
    byte_budget = settings.get("max_segment_tokenizer_bytes")
    if isinstance(byte_budget, int) and not isinstance(byte_budget, bool) and byte_budget > 0:
        split_sentences = [
            part
            for segment in split_sentences
            for part in _limit_segment_tokenizer_bytes(
                segment, byte_budget, language, audiobook_preparation=True
            )
        ]

    if enable_sentence_appending:
        split_sentences = append_short_sentences(
            split_sentences,
            max_sentence_length,
            max_segment_words=max_words,
            max_segment_tokenizer_bytes=byte_budget,
            dialogue_merge_allowed=(
                settings.get("audiobook_dialogue_merge_allowed") is True
            ),
            audiobook_preparation=True,
        )
    return split_sentences


def _limit_segment_words(
    segment: dict,
    maximum: int,
    language: str,
    *,
    audiobook_preparation: bool = False,
) -> list[dict]:
    """Apply a word recommendation without discarding paragraph boundaries."""
    if segment.get("chapter") == "yes":
        return [segment]
    words = list(re.finditer(r"\S+", segment["original_sentence"]))
    if len(words) <= maximum:
        return [segment]
    parts = split_long_sentences_2(
        segment,
        words[maximum].start(),
        language,
        audiobook_preparation=audiobook_preparation,
    )
    return [
        child
        for part in parts
        for child in _limit_segment_words(
            part, maximum, language, audiobook_preparation=audiobook_preparation
        )
    ]


def _limit_segment_tokenizer_bytes(
    segment: dict,
    maximum: int,
    language: str,
    *,
    audiobook_preparation: bool = False,
) -> list[dict]:
    """Reserve Irodori's normalized UTF-8 window before native tokenization."""
    text = segment["original_sentence"]
    used = 0
    for index, character in enumerate(text):
        used += 3 if character == " " else len(character.encode("utf-8"))
        if used > maximum:
            if index == 0:
                raise ValueError("A character exceeds the model's text-token budget.")
            parts = split_long_sentences_2(
                segment,
                index,
                language,
                audiobook_preparation=audiobook_preparation,
            )
            return [
                child
                for part in parts
                for child in _limit_segment_tokenizer_bytes(
                    part, maximum, language, audiobook_preparation=audiobook_preparation
                )
            ]
    return [segment]

def _split_text_into_chunks(text: str) -> list[str]:
    chunks = []
    total_length = len(text)
    if total_length == 0:
        return []
        
    # Aim for roughly 4 chunks for parallel processing
    target_chunk_size = total_length // 4
    if target_chunk_size == 0:
        return [text]
        
    start = 0
    while start < total_length:
        end = start + target_chunk_size
        if end >= total_length:
            chunks.append(text[start:])
            break

        # Find the next paragraph break to avoid splitting mid-paragraph
        next_para_break = text.find('\n\n', end)

        if next_para_break == -1:
            chunks.append(text[start:])
            break

        # Find the last sentence end before the paragraph break
        last_sentence_end = max(
            text.rfind('. ', start, next_para_break),
            text.rfind('! ', start, next_para_break),
            text.rfind('? ', start, next_para_break)
        )

        if last_sentence_end == -1 or last_sentence_end < start:
            # If no sentence end found, split at the paragraph break
            end = next_para_break + 2
        else:
            # Split after the last full sentence
            end = last_sentence_end + 2
        
        chunks.append(text[start:end])
        start = end
        
    return [c for c in chunks if c.strip()]

def preprocess_text_pdf(text, remove_double_newlines=False):
    text = regex.sub(r'\r\n|\r', '\n', text)
    text = regex.sub(r'[\x00-\x09\x0B-\x1F\x7F]', '', text)
    if remove_double_newlines:
        text = regex.sub(r'(?<![.!?])\n\n', ' ', text)
    else:
        text = regex.sub(r'\n$(?<!\n[ \t]*\n)|(?<!\n[ \t]*)\n(?![ \t]*\n)', ' ', text)
    text = regex.sub(r'[ \t]*\n[ \t]*\n[ \t]*(?:\n[ \t]*){0,2}', '\n', text)
    text = regex.sub(r' {2,}', ' ', text)
    text = regex.sub(r'(?m)^[ \t]+', '', text)
    return text


def _normalize_sentence_splitter_language(language: str) -> str:
    normalized = str(language or "").strip().lower()
    if not normalized:
        return "en"

    normalized = SENTENCE_SPLITTER_LANGUAGE_ALIASES.get(normalized, normalized)

    if normalized in SENTENCE_SPLITTER_SUPPORTED_LANGUAGES:
        return normalized

    if "-" in normalized:
        base_language = normalized.split("-", 1)[0]
        if base_language in SENTENCE_SPLITTER_SUPPORTED_LANGUAGES:
            return base_language

    return "en"


def _split_with_sentence_splitter(text: str, language: str) -> list[str]:
    splitter_language = _normalize_sentence_splitter_language(language)
    try:
        splitter = SentenceSplitter(language=splitter_language)
        return splitter.split(text)
    except Exception:
        if splitter_language != "en":
            splitter = SentenceSplitter(language="en")
            return splitter.split(text)
        raise

def split_into_sentences(text, language, tts_service):
    normalized_language = normalize_language_code(language)
    wtpsplit_sentences = sentence_segmenter.split_text(text)
    if wtpsplit_sentences is not None:
        return wtpsplit_sentences

    # Native punctuation is language-specific, not provider-specific. This
    # also covers audio.cpp/Qwen/Fish providers and offline Sat fallback.
    if normalized_language.startswith("zh"):
        return split_chinese_sentences(text)
    if normalized_language == "ja":
        return hasami.segment_sentences(text)

    if tts_service in {"XTTS", "Voxtral", "Kokoro", "Magpie", "OpenAI", "Google Gemini", "Gemini", "Custom", "OpenAI-Compatible"}:
        if normalized_language in {"zh", "zh-cn"}:
            return split_chinese_sentences(text)
        elif normalized_language.startswith("ja"):
            return hasami.segment_sentences(text)
        else:
            return _split_with_sentence_splitter(text, normalized_language)
    elif tts_service == "Silero":
        from .tts_handler import normalize_silero_language_code

        normalized_silero = normalize_silero_language_code(language)
        sentence_splitter_aliases = {
            "ukr": "uk", "tat": "ru", "uzb": "ru", "xal": "ru",
            "hye": "hy", "kat": "ka", "kir": "ky", "bel": "be",
        }
        simple_lang = sentence_splitter_aliases.get(
            normalized_silero,
            normalized_silero or normalized_language or "en",
        )
        return _split_with_sentence_splitter(text, simple_lang)

    return _split_with_sentence_splitter(text, "en")

def split_chinese_sentences(text):
    positions = sorted({offset for offset, kind in natural_split_candidates(text, "zh") if kind == "sentence"})
    boundaries = [0, *positions, len(text)]
    return [
        text[start:end].strip()
        for start, end in zip(boundaries, boundaries[1:], strict=False)
        if text[start:end].strip()
    ]


def calculate_similarity(str1, str2):
    return difflib.SequenceMatcher(None, str1, str2).ratio()

def split_long_sentences(sentence_dict, max_sentence_length, language: str):
    sentence = sentence_dict["original_sentence"]
    if len(sentence) <= max_sentence_length:
        return [sentence_dict.copy()]

    best_split_index = find_best_split_index(sentence, language, max_sentence_length)
    if best_split_index is None:
        return [sentence_dict.copy()]

    first_part = sentence[:best_split_index].strip()
    second_part = sentence[best_split_index:].strip()

    first_part_dict = sentence_dict.copy()
    first_part_dict.update({
        "original_sentence": first_part,
        "split_part": 0,
        "paragraph": "no",
        "sentence_continues_after": True,
    })
    
    second_part_dict = sentence_dict.copy()
    second_part_dict.update({"original_sentence": second_part, "split_part": 1})

    return [first_part_dict, second_part_dict]

def find_best_split_index(
    sentence, language, max_sentence_length, *, audiobook_preparation: bool = False
):
    if audiobook_preparation:
        return _find_audiobook_split_index(sentence, language, max_sentence_length)

    code = normalize_language_code(language)
    if code.split("-")[0] in {"ja", "zh", "ko"} or contains_cjk(sentence):
        target = min(len(sentence) // 2, max_sentence_length)
        candidates = [offset for offset, _kind in natural_split_candidates(sentence, code)
                      if 4 <= offset <= max_sentence_length and len(sentence[offset:].strip()) >= 4]
        return min(candidates, key=lambda offset: abs(offset - target)) if candidates else None
    punctuation_marks = (
        ["\uff0c", "\uff1b", "\uff1a", "\u3002", "\uff01", "\uff1f"]
        if language == "zh-cn"
        else [",", ":", ";", "\u2013", "\u2014", "â€“", "â€”"]
    )
    conjunction_marks = [' and ', ' or ', ' but ', ' because ', ' which '] if language != "zh-cn" else []
    min_distance = 10 if language == "zh-cn" else 30
    best_split_index, min_diff = None, float('inf')
    target_index = min(len(sentence) // 2, max_sentence_length)

    for mark in punctuation_marks:
        for match in re.finditer(re.escape(mark), sentence):
            index = match.start()
            split_index = _extend_split_index_past_closing_punctuation(sentence, index + len(mark))
            if min_distance <= index <= len(sentence) - min_distance and split_index <= max_sentence_length:
                if not (mark == ',' and sentence[index-1:index].isdigit() and sentence[index+1:index+2].isdigit()):
                    diff = abs(split_index - target_index)
                    if diff < min_diff:
                        min_diff, best_split_index = diff, split_index

    if best_split_index is None:
        min_diff = float('inf')
        for mark in conjunction_marks:
            for match in re.finditer(re.escape(mark), sentence, flags=re.IGNORECASE):
                index = match.start()
                if min_distance <= index <= len(sentence) - min_distance and index <= max_sentence_length:
                    diff = abs(index - target_index)
                    if diff < min_diff:
                        min_diff, best_split_index = diff, index

    return best_split_index


def _find_audiobook_split_index(sentence: str, language: str, max_length: int) -> int | None:
    """Choose a punctuation-first split near a balanced target length."""
    if not sentence or max_length <= 0:
        return None
    code = normalize_language_code(language)
    target = _balanced_split_target(len(sentence), max_length)
    natural_candidates = natural_split_candidates(sentence, code)

    punctuation = [
        offset
        for offset, kind in natural_candidates
        if kind != "conjunction"
        and _valid_split_offset(sentence, offset, max_length)
        and not _is_numeric_comma_boundary(sentence, offset, kind)
    ]
    if punctuation:
        return min(punctuation, key=lambda offset: (abs(offset - target), -offset))

    conjunctions = [
        offset
        for offset, kind in natural_candidates
        if kind == "conjunction" and _valid_split_offset(sentence, offset, max_length)
    ]
    if conjunctions:
        return min(conjunctions, key=lambda offset: (abs(offset - target), -offset))
    return None


def _balanced_split_target(text_length: int, max_length: int) -> int:
    if text_length <= 0 or max_length <= 0:
        return 0
    piece_count = (text_length + max_length - 1) // max_length
    return max(1, round(text_length / piece_count))


def _valid_split_offset(text: str, offset: int, max_length: int) -> bool:
    return (
        0 < offset <= max_length
        and offset < len(text)
        and bool(text[:offset].strip())
        and bool(text[offset:].strip())
    )


def _is_numeric_comma_boundary(text: str, offset: int, kind: str) -> bool:
    if kind != "comma" or offset <= 0 or offset >= len(text):
        return False
    comma_index = offset - 1
    while comma_index >= 0 and text[comma_index] in CLOSING_SPLIT_PUNCTUATION:
        comma_index -= 1
    return (
        comma_index > 0
        and comma_index + 1 < len(text)
        and text[comma_index] == ","
        and text[comma_index - 1].isdigit()
        and text[comma_index + 1].isdigit()
    )


def _extend_split_index_past_closing_punctuation(sentence: str, split_index: int) -> int:
    while split_index < len(sentence) and sentence[split_index] in CLOSING_SPLIT_PUNCTUATION:
        split_index += 1
    return split_index


def split_long_sentences_2(
    sentence_dict,
    max_sentence_length,
    language: str,
    *,
    audiobook_preparation: bool = False,
):
    sentence = sentence_dict["original_sentence"]
    if len(sentence) <= max_sentence_length:
        return [sentence_dict.copy() if audiobook_preparation else sentence_dict]
    if audiobook_preparation and max_sentence_length <= 0:
        raise ValueError("The narration character limit must be greater than zero.")

    best_split_index = find_best_split_index(
        sentence,
        language,
        max_sentence_length,
        audiobook_preparation=audiobook_preparation,
    )
    if best_split_index is None:
        if contains_cjk(sentence):
            raise ValueError(
                f"CJK narration exceeds the {max_sentence_length}-character speech limit "
                "without a natural sentence or clause boundary. Add suitable punctuation "
                "or increase max_sentence_length; subtitle character wrapping is not a safe speech split."
            )
        if audiobook_preparation:
            best_split_index = _find_balanced_whitespace_split(sentence, max_sentence_length)
            if best_split_index is None:
                if re.search(r"\s", sentence.strip()):
                    raise ValueError(
                        "Unable to split oversized narration at a safe whitespace "
                        "boundary within the character limit."
                    )
                best_split_index = max_sentence_length
        else:
            potential_split = sentence.rfind(' ', 0, max_sentence_length)
            best_split_index = potential_split + 1 if potential_split > 0 else max_sentence_length

    first_part, second_part = sentence[:best_split_index].strip(), sentence[best_split_index:].strip()
    if audiobook_preparation and (
        not first_part or not second_part or len(first_part) > max_sentence_length
    ):
        raise ValueError("Unable to split oversized narration within the character limit.")
    split_part_prefix = "0" if sentence_dict.get("split_part") is None else str(sentence_dict["split_part"])
    
    first_part_dict = sentence_dict.copy()
    first_part_dict.update({
        "original_sentence": first_part,
        "split_part": split_part_prefix + "a",
        "paragraph": "no",
        "sentence_continues_after": True,
    })
    if audiobook_preparation:
        first_part_dict.pop("speech_boundary_after", None)
        first_part_dict.pop("dialogue_flow", None)
    
    split_sentences = [first_part_dict]

    if second_part:
        second_part_initial_dict = sentence_dict.copy()
        second_part_initial_dict.update({"original_sentence": second_part, "split_part": split_part_prefix + "b"})
        
        further_splits = split_long_sentences_2(
            second_part_initial_dict,
            max_sentence_length,
            language,
            audiobook_preparation=audiobook_preparation,
        )
        if further_splits:
            for part in further_splits[:-1]:
                part["paragraph"] = "no"
                if audiobook_preparation:
                    part.pop("speech_boundary_after", None)
            # The last part inherits the original paragraph status
            further_splits[-1]["paragraph"] = sentence_dict["paragraph"]
        split_sentences.extend(further_splits)

    if audiobook_preparation and split_sentences:
        for part in split_sentences[:-1]:
            part["paragraph"] = "no"
            part["sentence_continues_after"] = True
            part.pop("speech_boundary_after", None)
            part.pop("dialogue_flow", None)
        split_sentences[-1]["paragraph"] = sentence_dict["paragraph"]
        split_sentences[-1]["sentence_continues_after"] = bool(
            sentence_dict.get("sentence_continues_after", False)
        )

    return split_sentences


def _find_balanced_whitespace_split(text: str, max_length: int) -> int | None:
    target = _balanced_split_target(len(text), max_length)
    candidates = []
    for match in re.finditer(r"\s+", text):
        offset = match.end()
        if _valid_split_offset(text, offset, max_length):
            candidates.append(offset)
    if candidates:
        return min(candidates, key=lambda offset: (abs(offset - target), -offset))
    return None


def _is_opening_narration_quote(text: str) -> bool:
    if not text:
        return False

    first = text[0]
    if first in OPENING_NARRATION_QUOTES:
        return True
    if first not in {'"', "'"}:
        return False

    remainder = text[1:].lstrip()
    while remainder and remainder[0] in DOUBLE_QUOTATION_MARKS:
        remainder = remainder[1:].lstrip()
    if not remainder or remainder[0] in TERMINAL_PUNCTUATION:
        return False

    # An ASCII apostrophe followed by a lower-case word is a contraction such
    # as 'tis or 'em, rather than an opening dialogue quote.
    return first == '"' or not remainder[0].islower()


def _join_narration_fragments(values) -> str:
    """Join narration fragments while keeping dialogue quote seams readable."""
    result = ""
    for value in values:
        fragment = clean_text(value)
        if not fragment:
            continue

        separator = fragment_separator(result, fragment) if result else ""
        if (
            result
            and not result[-1].isspace()
            and _is_opening_narration_quote(fragment)
            and not contains_cjk(result)
            and not contains_cjk(fragment)
        ):
            separator = " "
        result += separator + fragment
    return result.strip(" \t\r\n")


def append_short_sentences(
    sentence_dicts,
    max_sentence_length,
    *,
    max_segment_words=None,
    max_segment_tokenizer_bytes=None,
    dialogue_merge_allowed=False,
    audiobook_preparation=False,
):
    if audiobook_preparation:
        return _append_audiobook_sentences(
            sentence_dicts,
            max_sentence_length,
            max_segment_words=max_segment_words,
            max_segment_tokenizer_bytes=max_segment_tokenizer_bytes,
            dialogue_merge_allowed=dialogue_merge_allowed,
        )

    appended_sentences = []
    i = 0
    while i < len(sentence_dicts):
        current_sentence_dict = sentence_dicts[i].copy()

        # Chapter sentences are never modified
        if current_sentence_dict.get("chapter") == "yes":
            appended_sentences.append(current_sentence_dict)
            i += 1
            continue

        # Paragraph sentences: attempt to append to the previous non-chapter/non-paragraph sentence
        if current_sentence_dict.get("paragraph") == "yes":
            if appended_sentences: # Check if there's a previous sentence to append to
                prev_sentence_dict = appended_sentences[-1]
                if prev_sentence_dict.get("chapter") != "yes" and prev_sentence_dict.get("paragraph") != "yes":
                    combined_text = _join_narration_fragments(
                        (prev_sentence_dict["original_sentence"], current_sentence_dict["original_sentence"])
                    )
                    if len(combined_text) <= max_sentence_length:
                        # Update the previous sentence and mark it as a paragraph
                        prev_sentence_dict["original_sentence"] = combined_text
                        prev_sentence_dict["paragraph"] = "yes"
                        prev_sentence_dict["sentence_continues_after"] = bool(
                            current_sentence_dict.get("sentence_continues_after", False)
                        )
                        # Do not add current_sentence_dict, it's merged
                        i += 1
                        continue
            # If not appended, add current sentence as is
            appended_sentences.append(current_sentence_dict)
            i += 1
            continue

        # Try to append current non-paragraph, non-chapter sentence to the previous non-chapter, non-paragraph sentence
        if appended_sentences:
            prev_sentence_dict = appended_sentences[-1]
            if (prev_sentence_dict.get("chapter") != "yes" and
                prev_sentence_dict.get("paragraph") != "yes"):
                combined_text = _join_narration_fragments(
                    (prev_sentence_dict["original_sentence"], current_sentence_dict["original_sentence"])
                )
                if len(combined_text) <= max_sentence_length:
                    prev_sentence_dict["original_sentence"] = combined_text
                    prev_sentence_dict["sentence_continues_after"] = bool(
                        current_sentence_dict.get("sentence_continues_after", False)
                    )
                    # Paragraph status of prev_sentence_dict remains "no"
                    i += 1
                    continue

        # Try to prepend current non-paragraph, non-chapter sentence to the next non-chapter, non-paragraph sentence
        if i + 1 < len(sentence_dicts):
            next_sentence_dict = sentence_dicts[i + 1]
            if (next_sentence_dict.get("chapter") != "yes" and
                next_sentence_dict.get("paragraph") != "yes"):
                    combined_text = _join_narration_fragments(
                        (current_sentence_dict["original_sentence"], next_sentence_dict["original_sentence"])
                    )
                    if len(combined_text) <= max_sentence_length:
                        # Create a new merged sentence dictionary
                        merged_dict = next_sentence_dict.copy() # Start with next sentence's properties
                        merged_dict["original_sentence"] = combined_text
                        merged_dict["paragraph"] = "no" # Result of merging non-paragraphs is non-paragraph
                        merged_dict["split_part"] = None # Or determine appropriate merged split_part logic

                        appended_sentences.append(merged_dict)
                        i += 2 # Skip current and next as they are merged
                        continue

        # If no appending or prepending occurred, add the current sentence as is
        appended_sentences.append(current_sentence_dict)
        i += 1

    return appended_sentences


def _append_audiobook_sentences(
    sentence_dicts,
    max_sentence_length,
    *,
    max_segment_words=None,
    max_segment_tokenizer_bytes=None,
    dialogue_merge_allowed=False,
):
    """Greedily pack complete source sentences without crossing paragraphs."""
    groups = []
    current_paragraph = []
    for sentence_dict in sentence_dicts:
        copied = sentence_dict.copy()
        if copied.get("chapter") == "yes":
            if current_paragraph:
                groups.append(("paragraph", current_paragraph))
                current_paragraph = []
            groups.append(("chapter", copied))
            continue

        current_paragraph.append(copied)
        if copied.get("paragraph") == "yes":
            groups.append(("paragraph", current_paragraph))
            current_paragraph = []
    if current_paragraph:
        groups.append(("paragraph", current_paragraph))

    packed_groups = []
    for group_type, group in groups:
        if group_type == "chapter":
            packed_groups.append((group_type, [group]))
            continue

        packed_groups.append((
            group_type,
            _pack_audiobook_paragraph(
                group,
                max_sentence_length,
                max_segment_words=max_segment_words,
                max_segment_tokenizer_bytes=max_segment_tokenizer_bytes,
            ),
        ))

    result = []
    previous_group_type = None
    for group_type, packed in packed_groups:
        if group_type == "chapter":
            result.extend(packed)
            previous_group_type = "chapter"
            continue

        if (
            dialogue_merge_allowed is True
            and previous_group_type == "paragraph"
            and result
            and result[-1].get("paragraph") == "yes"
            and len(packed) == 1
            and _can_merge_dialogue_turns(
                result[-1],
                packed[0],
                max_sentence_length,
                max_segment_words,
                max_segment_tokenizer_bytes,
            )
        ):
            left = result[-1]
            right = packed[0]
            combined_text = left["original_sentence"].rstrip() + "\n\n" + right[
                "original_sentence"
            ].lstrip()
            left_flow = left["dialogue_flow"]
            right_flow = right["dialogue_flow"]
            merged = left.copy()
            merged.update(right)
            merged["original_sentence"] = combined_text
            merged["dialogue_flow"] = right_flow.copy()
            merged["dialogue_flow"]["paragraph_count"] = (
                left_flow.get("paragraph_count", 1)
                + right_flow.get("paragraph_count", 1)
            )
            result[-1] = merged
        else:
            result.extend(packed)
        previous_group_type = "paragraph"

    return result


def _pack_audiobook_paragraph(
    rows,
    max_sentence_length,
    *,
    max_segment_words=None,
    max_segment_tokenizer_bytes=None,
):
    packed = []
    current = None
    for row in rows:
        if current is None:
            current = row.copy()
            continue

        candidate_text = _join_narration_fragments(
            (current["original_sentence"], row["original_sentence"])
        )
        if _audiobook_candidate_fits(
            candidate_text,
            max_sentence_length,
            max_segment_words,
            max_segment_tokenizer_bytes,
        ):
            current.update(row)
            current["original_sentence"] = candidate_text
        else:
            packed.append(current)
            current = row.copy()

    if current is not None:
        packed.append(current)
    return packed


def _can_merge_dialogue_turns(
    left,
    right,
    max_sentence_length,
    max_segment_words,
    max_segment_tokenizer_bytes,
):
    left_flow = left.get("dialogue_flow")
    right_flow = right.get("dialogue_flow")
    if (
        left.get("speech_boundary_after") != "dialogue_turn"
        or not isinstance(left_flow, dict)
        or not isinstance(right_flow, dict)
        or left_flow.get("source") != "punctuation"
        or right_flow.get("source") != "punctuation"
    ):
        return False
    candidate = left["original_sentence"].rstrip() + "\n\n" + right[
        "original_sentence"
    ].lstrip()
    return _audiobook_candidate_fits(
        candidate,
        max_sentence_length,
        max_segment_words,
        max_segment_tokenizer_bytes,
    )


def _audiobook_candidate_fits(text, max_chars, max_words, max_tokenizer_bytes):
    if len(text) > max_chars:
        return False
    if (
        isinstance(max_words, int)
        and not isinstance(max_words, bool)
        and max_words > 0
        and len(re.findall(r"\S+", text)) > max_words
    ):
        return False
    if (
        isinstance(max_tokenizer_bytes, int)
        and not isinstance(max_tokenizer_bytes, bool)
        and max_tokenizer_bytes > 0
    ):
        byte_cost = sum(
            3 if character == " " else len(character.encode("utf-8"))
            for character in text
        )
        if byte_cost > max_tokenizer_bytes:
            return False
    return True

def convert_digits_to_words(sentence: str, language_code: str):
    def replace_numbers(match):
        number = match.group(0)
        try:
            # Mapping from Silero names and XTTS codes to num2words language codes
            lang_to_num2words_code = {
                # Silero
                "German (v3)": "de", "English (v3)": "en", "English Indic (v3)": "en",
                "Spanish (v3)": "es", "French (v3)": "fr", "Indic (v3)": "hi",
                "Russian (v3.1)": "ru", "Tatar (v3)": "tt", "Ukrainian (v3)": "uk",
                "Uzbek (v3)": "uz", "Kalmyk (v3)": "ru", # Fallback to Russian
                # XTTS - most are direct mappings
                "en": "en", "es": "es", "fr": "fr", "de": "de", "it": "it",
                "pt": "pt", "pl": "pl", "tr": "tr", "ru": "ru", "nl": "nl",
                "cs": "cs", "ar": "ar", "zh-cn": "zh", "ja": "ja", "hu": "hu", 
                "ko": "ko", "hi": "hi"
            }
            num2words_lang = lang_to_num2words_code.get(language_code, "en")
            return num2words(int(number), lang=num2words_lang)
        except (ValueError, NotImplementedError):
            return number
    return re.sub(r'\d+', replace_numbers, sentence)

def _join_chapter_titles(titles: list[str]) -> str:
    text = ""
    for title in titles:
        next_text = title.strip()
        if not next_text:
            continue
        if text and not re.search(r'[.!?]$', text):
            text += "."
        text += (" " if text else "") + next_text
    if text and not re.search(r'[.!?]$', text):
        text += "."
    return text.strip()


def merge_consecutive_chapters(sentences, max_sentence_length: int | None = None):
    merged, i = [], 0
    while i < len(sentences):
        current = sentences[i]
        if current.get("chapter") == "yes":
            titles = []
            text = current["original_sentence"].strip()
            if text:
                titles.append(text)
            j = i + 1
            while j < len(sentences) and sentences[j].get("chapter") == "yes":
                next_text = sentences[j]["original_sentence"].strip()
                if not next_text:
                    j += 1
                    continue
                candidate = _join_chapter_titles(titles + [next_text])
                if max_sentence_length and titles and len(candidate) > max_sentence_length:
                    break
                titles.append(next_text)
                j += 1
            text = _join_chapter_titles(titles)
            if text:
                merged.append({
                    "original_sentence": text,
                    "paragraph": "yes", "chapter": "yes", "split_part": None,
                    "sentence_continues_after": False,
                    "sentence_number": current.get("sentence_number")
                })
            i = j
        else:
            merged.append(current)
            i += 1
    return merged
