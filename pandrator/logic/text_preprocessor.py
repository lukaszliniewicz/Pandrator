import re
import unicodedata
from unidecode import unidecode
import difflib
from sentence_splitter import SentenceSplitter
import regex
import hasami
import concurrent.futures
from num2words import num2words

CHUNK_SIZE = 20000

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

def normalize_punctuation(text: str) -> str:
    """Normalizes Unicode dashes, curly quotes, and ellipses to standard equivalents."""
    # Translate fancy quotes
    text = text.translate(str.maketrans({
        '“': '"',
        '”': '"',
        '„': '"',
        '‟': '"',
        '‘': "'",
        '’': "'",
        '‚': "'",
        '‛': "'",
        '‹': "'",
        '›': "'",
        '«': '"',
        '»': '"',
    }))
    # Normalize dashes to guide prosody/pauses
    text = text.replace('—', ', ')
    text = text.replace('–', ' - ')
    # Normalize ellipsis
    text = text.replace('…', '...')
    return text

def preprocess_text(text: str, settings: dict) -> list[dict]:
    """
    Main entry point for text preprocessing. Chooses parallel or sequential.
    'settings' is a dictionary containing all necessary parameters.
    """
    if len(text) > CHUNK_SIZE:
        processed_sentences = _parallel_preprocess_text(text, settings)
    else:
        processed_sentences = _sequential_preprocess_text(text, settings)
    
    return merge_consecutive_chapters(processed_sentences)

def _parallel_preprocess_text(text: str, settings: dict) -> list[dict]:
    chunks = _split_text_into_chunks(text)
    
    processed_chunks = [None] * len(chunks)

    with concurrent.futures.ProcessPoolExecutor() as executor:
        future_to_index = {executor.submit(_process_chunk, chunk, settings): i
                           for i, chunk in enumerate(chunks)}

        for future in concurrent.futures.as_completed(future_to_index):
            index = future_to_index[future]
            processed_chunks[index] = future.result()

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
    """
    Processes a single chunk of text. This is the core logic.
    """
    pdf_preprocessed = settings.get('pdf_preprocessed', False)
    source_file = settings.get('source_file', '')
    disable_paragraph_detection = settings.get('disable_paragraph_detection', False)
    language = settings.get('language', 'en')
    max_sentence_length = settings.get('max_sentence_length', 160)
    enable_sentence_splitting = settings.get('enable_sentence_splitting', True)
    enable_sentence_appending = settings.get('enable_sentence_appending', True)
    remove_diacritics = settings.get('remove_diacritics', False)
    remove_quotation_marks = settings.get('remove_quotation_marks', False)
    tts_service = settings.get('tts_service', 'XTTS')



    chunk = re.sub(r'\r\n?', '\n', chunk)
    chunk = normalize_punctuation(chunk)
    paragraph_breaks = []

    if not disable_paragraph_detection:
        if pdf_preprocessed or source_file.endswith("_edited.txt"):
            paragraph_breaks = list(re.finditer(r'\n', chunk))
        elif source_file.endswith(".pdf"):
            chunk = preprocess_text_pdf(chunk)
        else:
            chunk = re.sub(r'(?<!\n)\n(?!\n)', ' ', chunk)
            paragraph_breaks = list(re.finditer(r'\n', chunk))

    chunk = re.sub(r'\t', ' ', chunk)

    if remove_diacritics:
        chunk = ''.join(char for char in chunk if not unicodedata.combining(char))
        chunk = unidecode(chunk)

    if remove_quotation_marks:
        chunk = strip_quotation_marks(chunk)

    chunk = re.sub(r'(^|\n+)([^\n.!?]+)(?=\n+|$)', r'\1\2.', chunk)
    sentences = split_into_sentences(chunk, language, tts_service)
    processed_sentences = []

    for sentence in sentences:
        if not sentence.strip():
            continue

        is_paragraph = any(calculate_similarity(chunk[match.start()-15:match.start()], sentence[-15:]) >= 0.8 for match in paragraph_breaks)
        
        is_chapter = "[[Chapter]]" in sentence
        if is_chapter:
            sentence = sentence.replace("[[Chapter]]", "").strip()
            is_paragraph = True

        sentence_dict = {
            "original_sentence": sentence,
            "paragraph": "yes" if is_paragraph else "no",
            "chapter": "yes" if is_chapter else "no",
            "split_part": None
        }

        if enable_sentence_splitting:
            processed_sentences.extend(split_long_sentences(sentence_dict, max_sentence_length, language))
        else:
            processed_sentences.append(sentence_dict)

    if enable_sentence_appending:
        processed_sentences = append_short_sentences(processed_sentences, max_sentence_length)

    split_sentences = []
    for sentence_dict in processed_sentences:
        split_sentences.extend(split_long_sentences_2(sentence_dict, max_sentence_length, language))

    return split_sentences

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
    normalized_language = str(language or "").strip().lower()

    if tts_service in {"XTTS", "Voxtral", "Kokoro", "Magpie", "OpenAI", "Google Gemini", "Gemini", "Custom", "OpenAI-Compatible"}:
        if normalized_language in {"zh", "zh-cn"}:
            return split_chinese_sentences(text)
        elif normalized_language.startswith("ja"):
            return hasami.segment_sentences(text)
        else:
            return _split_with_sentence_splitter(text, normalized_language)
    elif tts_service == "Silero":
        # This mapping is more robust than parsing the code string from constants
        silero_name_to_lang_code = {
            "German (v3)": "de",
            "English (v3)": "en",
            "English Indic (v3)": "en",
            "Spanish (v3)": "es",
            "French (v3)": "fr",
            "Indic (v3)": "hi",
            "Russian (v3.1)": "ru",
            "Tatar (v3)": "tt",
            "Ukrainian (v3)": "uk",
            "Uzbek (v3)": "uz",
            "Kalmyk (v3)": "ru",  # Fallback for Kalmyk to Russian
        }
        simple_lang = silero_name_to_lang_code.get(language, normalized_language or "en")
        return _split_with_sentence_splitter(text, simple_lang)

    return _split_with_sentence_splitter(text, "en")

def split_chinese_sentences(text):
    end_punctuation = '。！？…'
    segments = re.split(f'([{end_punctuation}])', text)
    return [''.join(segments[i:i+2]).strip() for i in range(0, len(segments), 2) if segments[i]]

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
    first_part_dict.update({"original_sentence": first_part, "split_part": 0, "paragraph": "no"})
    
    second_part_dict = sentence_dict.copy()
    second_part_dict.update({"original_sentence": second_part, "split_part": 1})

    return [first_part_dict, second_part_dict]

def find_best_split_index(sentence, language, max_sentence_length):
    punctuation_marks = ['，', '；', '：', '。', '！', '？'] if language == "zh-cn" else [',', ':', ';', '–']
    conjunction_marks = [' and ', ' or ', 'which'] if language != "zh-cn" else []
    min_distance = 10 if language == "zh-cn" else 30
    best_split_index, min_diff = None, float('inf')

    for mark in punctuation_marks:
        for match in re.finditer(re.escape(mark), sentence):
            index = match.start()
            if min_distance <= index <= len(sentence) - min_distance and index + 1 <= max_sentence_length:
                if not (mark == ',' and sentence[index-1:index].isdigit() and sentence[index+1:index+2].isdigit()):
                    diff = abs(index - len(sentence) // 2)
                    if diff < min_diff:
                        min_diff, best_split_index = diff, index + 1

    if best_split_index is None:
        for mark in conjunction_marks:
            index = sentence.find(mark)
            if min_distance <= index <= len(sentence) - min_distance and index <= max_sentence_length:
                return index
    
    return best_split_index

def split_long_sentences_2(sentence_dict, max_sentence_length, language: str):
    sentence = sentence_dict["original_sentence"]
    if len(sentence) <= max_sentence_length:
        return [sentence_dict]

    best_split_index = find_best_split_index(sentence, language, max_sentence_length)
    if best_split_index is None:
        potential_split = sentence.rfind(' ', 0, max_sentence_length)
        best_split_index = potential_split + 1 if potential_split > 0 else max_sentence_length

    first_part, second_part = sentence[:best_split_index].strip(), sentence[best_split_index:].strip()
    split_part_prefix = "0" if sentence_dict.get("split_part") is None else str(sentence_dict["split_part"])
    
    first_part_dict = sentence_dict.copy()
    first_part_dict.update({"original_sentence": first_part, "split_part": split_part_prefix + "a", "paragraph": "no"})
    
    split_sentences = [first_part_dict]

    if second_part:
        second_part_initial_dict = sentence_dict.copy()
        second_part_initial_dict.update({"original_sentence": second_part, "split_part": split_part_prefix + "b"})
        
        further_splits = split_long_sentences_2(second_part_initial_dict, max_sentence_length, language)
        if further_splits:
            for part in further_splits[:-1]:
                part["paragraph"] = "no"
            # The last part inherits the original paragraph status
            further_splits[-1]["paragraph"] = sentence_dict["paragraph"]
        split_sentences.extend(further_splits)

    return split_sentences

def append_short_sentences(sentence_dicts, max_sentence_length):
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
                    combined_text = prev_sentence_dict["original_sentence"] + ' ' + current_sentence_dict["original_sentence"]
                    if len(combined_text) <= max_sentence_length:
                        # Update the previous sentence and mark it as a paragraph
                        prev_sentence_dict["original_sentence"] = combined_text
                        prev_sentence_dict["paragraph"] = "yes"
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
                combined_text = prev_sentence_dict["original_sentence"] + ' ' + current_sentence_dict["original_sentence"]
                if len(combined_text) <= max_sentence_length:
                    prev_sentence_dict["original_sentence"] = combined_text
                    # Paragraph status of prev_sentence_dict remains "no"
                    i += 1
                    continue

        # Try to prepend current non-paragraph, non-chapter sentence to the next non-chapter, non-paragraph sentence
        if i + 1 < len(sentence_dicts):
            next_sentence_dict = sentence_dicts[i + 1]
            if (next_sentence_dict.get("chapter") != "yes" and
                next_sentence_dict.get("paragraph") != "yes"):
                combined_text = current_sentence_dict["original_sentence"] + ' ' + next_sentence_dict["original_sentence"]
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

def merge_consecutive_chapters(sentences):
    merged, i = [], 0
    while i < len(sentences):
        current = sentences[i]
        if current.get("chapter") == "yes":
            text = current["original_sentence"].strip()
            j = i + 1
            while j < len(sentences) and sentences[j].get("chapter") == "yes":
                next_text = sentences[j]["original_sentence"].strip()
                if text and not re.search(r'[.!?]$', text):
                    text += "."
                text += " " + next_text
                j += 1
            if text and not re.search(r'[.!?]$', text):
                 text += "."
            merged.append({
                "original_sentence": text.strip(),
                "paragraph": "yes", "chapter": "yes", "split_part": None,
                "sentence_number": current.get("sentence_number")
            })
            i = j
        else:
            merged.append(current)
            i += 1
    return merged
