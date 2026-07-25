"""Validated speech plans that keep display text separate from TTS delivery text."""

from __future__ import annotations

import collections
import difflib
import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

from pandrator.logic.llm_handler import ChatCompletionResult, chat_completion_with_metadata

from .pronunciations import RESPELLING_RE, render_respelling


ALLOWED_ACTIONS = {"pronounce", "verbalize", "spell_letters", "keep", "uncertain"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
PLACEHOLDER_RE = re.compile(r"\{\{[KPN]\d+\}\}")
WORD_RE = re.compile(
    r"[^\W\d_]+(?:[’'][^\W\d_]+)*(?:-[^\W\d_]+)*",
    flags=re.UNICODE,
)
TOKEN_RE = re.compile(
    r"\{\{[KPN]\d+\}\}|"
    r"[^\W\d_]+(?:[’'][^\W\d_]+)*(?:-[^\W\d_]+)*|"
    r"\d+(?:[.,:/]\d+)*|"
    r"[^\s]",
    flags=re.UNICODE,
)
ROMAN_RE = re.compile(r"\b[IVXLCDM]{2,}\b")
ALL_CAPS_RE = re.compile(r"\b[A-Z]{2,8}\b")
ABBREVIATION_RE = re.compile(
    r"\b(?:(?:[A-Za-z]\.){2,}|Dr\.|Prof\.|Capt\.|Mr\.|Mrs\.|Ms\.|"
    r"Ch\.|No\.|Vol\.|pp\.|lat\.|long\.|q\.i\.d\.|p\.m\.|a\.m\.|[A-Za-z]\.)",
    flags=re.IGNORECASE,
)
URL_OR_EMAIL_RE = re.compile(
    r"\b(?:https?://|www\.)\S+|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b",
    flags=re.IGNORECASE,
)
RESIDUAL_NUMBER_RE = re.compile(
    r"(?<!\w)(?:\d+(?:[.,:/]\d+)*(?:st|nd|rd|th)?|[¼½¾⅓⅔⅛⅜⅝⅞])(?!\w)",
    flags=re.IGNORECASE,
)
SYMBOL_RE = re.compile(r"[§£€¥%&@°′″]")
REPEATED_WORD_RE = re.compile(
    r"\b([^\W\d_]+)(?:\s+\1)\b",
    flags=re.IGNORECASE | re.UNICODE,
)
NUMBER_WORDS = frozenset(
    {
        "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
        "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
        "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
        "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred",
        "thousand",
    }
)
COMMON_TITLECASE_WORDS = frozenset(
    {
        "a", "an", "and", "as", "at", "but", "by", "chapter", "doctor", "for",
        "from", "he", "her", "his", "i", "in", "it", "its", "miss", "mister",
        "mrs", "no", "not", "of", "on", "or", "professor", "section", "she",
        "that", "the", "their", "then", "they", "this", "to", "we", "when",
        "where", "which", "who", "with", "you",
    }
)


GUARDED_SYSTEM_PROMPT = """You are a constrained speech-planning component.
You receive one display sentence, its deterministic normalization result, stable tokens,
reviewed pronunciations, and unresolved candidate spans.

Decide every unresolved span exactly once. Required spans cannot use keep. Actions:
- pronounce: lowercase ASCII syllables separated by ASCII hyphens, e.g. ee-mah-oh-kah
- verbalize: complete words that should be spoken
- spell_letters: letters separated by spaces
- keep: written text is already suitable
- uncertain: retain the text because changing it would be unsafe

Do not rewrite the sentence and do not decide reviewed pronunciations. Audit the whole
sentence for a missed abbreviation, number, symbol, repeated word, broken range, or OCR
debris. Report a new issue only as a discovery using exact token IDs and exact source text.
Never duplicate a supplied span. Prosody is normally empty and may contain at most two
items. Return JSON only:
{"case_id":"same ID","decisions":[{"span_id":"P1","action":"pronounce|verbalize|spell_letters|keep|uncertain","spoken":"string or null","confidence":"high|medium|low"}],"discoveries":[{"start_token_id":"T1","end_token_id":"T1","source_text":"exact text","action":"pronounce|verbalize|spell_letters","spoken":"string","confidence":"high|medium|low"}],"prosody":[{"after_token_id":"T1","kind":"short_pause|sentence_break"}]}
Use empty arrays where appropriate. Never add explanations."""


FLEXIBLE_SYSTEM_PROMPT = """You are a contextual speech-text editor.
You receive one display sentence, deterministic text, a protected speech template, stable
tokens, reviewed pronunciations, and unresolved spans.

Return a complete speech_template for natural delivery. You may fix missed verbalization,
speech-only punctuation, and phrasing, but may not paraphrase, add facts, remove facts, or
change meaning. Every {{K1}}, {{P1}}, or {{N1}} placeholder in the protected template must
appear in your template exactly the same number of times and with identical spelling. Never
replace or invent placeholders; the host substitutes them later.

Decide every unresolved span exactly once. Required spans cannot use keep. Actions:
- pronounce: lowercase ASCII syllables separated by ASCII hyphens, e.g. ee-mah-oh-kah
- verbalize: complete words to speak
- spell_letters: letters separated by spaces
- keep: retain the written text
- uncertain: retain the text because changing it would be unsafe

Do not decide reviewed pronunciations. Any additional fix must also be reported as a
discovery against exact token IDs and source_text. Never duplicate a supplied span.
Prosody is normally empty and may contain at most two items. Return JSON only:
{"case_id":"same ID","speech_template":"complete template with placeholders","decisions":[{"span_id":"P1","action":"pronounce|verbalize|spell_letters|keep|uncertain","spoken":"string or null","confidence":"high|medium|low"}],"discoveries":[{"start_token_id":"T1","end_token_id":"T1","source_text":"exact text","action":"pronounce|verbalize|spell_letters","spoken":"string","confidence":"high|medium|low"}],"prosody":[{"after_token_id":"T1","kind":"short_pause|sentence_break"}]}
Use empty arrays where appropriate. Never add explanations."""


@dataclass(slots=True)
class SpeechPlanAttempt:
    mode: str
    response: ChatCompletionResult | None = None
    parsed: dict[str, Any] | None = None
    validation: dict[str, Any] = field(default_factory=dict)
    raw_content: str = ""


@dataclass(slots=True)
class SpeechPlanResult:
    text: str
    plan: dict[str, Any]
    responses: list[ChatCompletionResult] = field(default_factory=list)


def _comparison_key(value: object) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def _bounded_pattern(value: str) -> re.Pattern[str]:
    prefix = r"(?<!\w)" if value and value[0].isalnum() else ""
    suffix = r"(?!\w)" if value and value[-1].isalnum() else ""
    return re.compile(prefix + re.escape(value) + suffix, flags=re.IGNORECASE)


def tokenize(text: str) -> list[dict[str, Any]]:
    return [
        {
            "id": f"T{index}",
            "text": match.group(0),
            "start": match.start(),
            "end": match.end(),
        }
        for index, match in enumerate(TOKEN_RE.finditer(text), start=1)
    ]


def _hunspell_unknown_words(text: str, dictionary: str) -> set[str]:
    executable = shutil.which("hunspell")
    if not executable:
        return set()
    try:
        completed = subprocess.run(
            [executable, "-d", dictionary, "-l"],
            input=text + "\n",
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if completed.returncode not in (0, 1):
        return set()
    return {line.strip().casefold() for line in completed.stdout.splitlines() if line.strip()}


def _dictionary_for(language: str) -> str:
    base = str(language or "en").replace("-", "_")
    mapping = {
        "en": "en_US", "en_us": "en_US", "en_gb": "en_GB", "pl": "pl_PL",
        "de": "de_DE", "fr": "fr_FR", "es": "es_ES", "it": "it_IT",
        "pt": "pt_PT", "pt_br": "pt_BR", "nl": "nl_NL",
    }
    return mapping.get(base.lower(), base)


def _foreign_name_shape(word: str) -> bool:
    lowered = word.casefold()
    vowel_count = sum(character in "aeiouy" for character in lowered)
    adjacent_vowels = bool(re.search(r"[aeiouy]{2,}", lowered))
    internal_upper = any(character.isupper() for character in word[1:])
    non_ascii = any(ord(character) > 127 for character in word)
    return internal_upper or non_ascii or (len(word) >= 5 and vowel_count >= 3 and adjacent_vowels)


def _overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return int(left["start"]) < int(right["end"]) and int(right["start"]) < int(left["end"])


def detect_candidates(
    text: str,
    *,
    language: str,
    known_pronunciations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    known_spans: list[dict[str, Any]] = []
    for entry in sorted(
        known_pronunciations,
        key=lambda item: len(str(item.get("source_form") or "")),
        reverse=True,
    ):
        term = str(entry.get("source_form") or "").strip()
        if not term:
            continue
        for match in _bounded_pattern(term).finditer(text):
            span = {
                "start": match.start(),
                "end": match.end(),
                "text": match.group(0),
                "spoken": str(entry.get("phonetic") or ""),
                "entry_id": entry.get("id"),
                "entry_revision": entry.get("revision"),
                "status": "reviewed",
                "task": "pronunciation",
            }
            if not any(_overlaps(span, existing) for existing in known_spans):
                known_spans.append(span)

    detected: list[dict[str, Any]] = []

    def add(
        start: int,
        end: int,
        *,
        task: str,
        signals: list[str],
        required: bool = False,
    ) -> None:
        span = {
            "start": start,
            "end": end,
            "text": text[start:end],
            "task": task,
            "signals": list(dict.fromkeys(signals)),
            "resolution_policy": "required" if required else "review",
        }
        if not span["text"].strip():
            return
        if any(_overlaps(span, item) for item in [*known_spans, *detected]):
            return
        detected.append(span)

    residuals: list[tuple[re.Pattern[str], str, list[str], bool]] = [
        (URL_OR_EMAIL_RE, "verbalize", ["url_or_email", "survived_normalization"], True),
        (ABBREVIATION_RE, "verbalize", ["abbreviation", "survived_normalization"], True),
        (ROMAN_RE, "verbalize", ["roman_numeral", "survived_normalization"], True),
        (RESIDUAL_NUMBER_RE, "verbalize", ["number", "survived_normalization"], True),
        (ALL_CAPS_RE, "review", ["all_caps", "acronym_or_word"], False),
        (SYMBOL_RE, "verbalize", ["symbol", "survived_normalization"], True),
        (REPEATED_WORD_RE, "verbalize", ["adjacent_repeated_word"], True),
    ]
    for pattern, task, signals, required in residuals:
        for match in pattern.finditer(text):
            add(match.start(), match.end(), task=task, signals=signals, required=required)

    words = list(WORD_RE.finditer(text))
    unknown = _hunspell_unknown_words(text, _dictionary_for(language))
    for match in words:
        word = match.group(0)
        if not word or word.casefold() in COMMON_TITLECASE_WORDS:
            continue
        titlecase = word[0].isupper() and not word.isupper()
        preceding = text[: match.start()].rstrip()
        sentence_initial = not preceding or preceding.endswith((".", "!", "?", ":", "…"))
        signals: list[str] = []
        if word.casefold() in unknown:
            signals.append("dictionary_oov")
        if titlecase and not sentence_initial:
            signals.append("proper_name_like")
        if titlecase and _foreign_name_shape(word):
            signals.append("foreign_name_shape")
        if signals:
            add(
                match.start(),
                match.end(),
                task="pronunciation",
                signals=signals,
            )

    for dash in re.finditer(r"[–-]", text):
        previous = next((word for word in reversed(words) if word.end() <= dash.start()), None)
        following = next((word for word in words if word.start() >= dash.end()), None)
        if (
            previous
            and following
            and previous.group(0).casefold() in NUMBER_WORDS
            and following.group(0).casefold() in NUMBER_WORDS
        ):
            add(
                dash.start(),
                dash.end(),
                task="verbalize",
                signals=["numeric_range_separator"],
                required=True,
            )

    counters: collections.defaultdict[str, int] = collections.defaultdict(int)
    for span in sorted(detected, key=lambda item: (item["start"], item["end"])):
        prefix = "P" if span["task"] == "pronunciation" else "N"
        counters[prefix] += 1
        span["id"] = f"{prefix}{counters[prefix]}"
    for index, span in enumerate(sorted(known_spans, key=lambda item: item["start"]), start=1):
        span["id"] = f"K{index}"
    return (
        sorted(detected, key=lambda item: item["start"]),
        sorted(known_spans, key=lambda item: item["start"]),
    )


def build_protected_template(
    text: str,
    candidates: list[dict[str, Any]],
    known: list[dict[str, Any]],
) -> tuple[str, dict[str, int]]:
    entries = sorted([*candidates, *known], key=lambda item: item["start"], reverse=True)
    template = text
    counts: dict[str, int] = {}
    for entry in entries:
        placeholder = "{{" + str(entry["id"]) + "}}"
        template = template[: int(entry["start"])] + placeholder + template[int(entry["end"]) :]
        counts[placeholder] = counts.get(placeholder, 0) + 1
    return template, counts


def _extract_json(value: object) -> tuple[dict[str, Any] | None, str]:
    raw = str(value or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload, ""
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, character in enumerate(raw):
        if character != "{":
            continue
        try:
            payload, _end = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload, "Recovered JSON from surrounding text."
    return None, "No valid JSON object was returned."


def _lexical_tokens(text: str) -> list[str]:
    return [
        token.casefold()
        for token in TOKEN_RE.findall(str(text or ""))
        if PLACEHOLDER_RE.fullmatch(token) or any(character.isalnum() for character in token)
    ]


def validate_plan(
    parsed: dict[str, Any] | None,
    *,
    mode: str,
    case_id: str,
    deterministic_text: str,
    tokens: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    known: list[dict[str, Any]],
    protected_template: str,
    placeholder_counts: dict[str, int],
    min_retention: float,
    parse_note: str = "",
) -> dict[str, Any]:
    errors: list[str] = []
    warnings = [parse_note] if parse_note else []
    if parsed is None:
        return {"valid": False, "errors": ["Response was not parseable JSON."], "warnings": warnings}
    if parsed.get("case_id") != case_id:
        errors.append("case_id does not match.")
    decisions = parsed.get("decisions")
    discoveries = parsed.get("discoveries")
    prosody = parsed.get("prosody")
    if not isinstance(decisions, list):
        decisions = []
        errors.append("decisions must be a list.")
    if not isinstance(discoveries, list):
        discoveries = []
        errors.append("discoveries must be a list.")
    if not isinstance(prosody, list):
        prosody = []
        errors.append("prosody must be a list.")

    candidates_by_id = {item["id"]: item for item in candidates}
    returned: list[str] = []
    for index, raw in enumerate(decisions):
        if not isinstance(raw, dict):
            errors.append(f"Decision {index} is not an object.")
            continue
        span_id = str(raw.get("span_id") or "")
        returned.append(span_id)
        candidate = candidates_by_id.get(span_id)
        if candidate is None:
            errors.append(f"Decision references unknown span {span_id!r}.")
            continue
        action = str(raw.get("action") or "")
        spoken = raw.get("spoken")
        if action not in ALLOWED_ACTIONS:
            errors.append(f"Decision {span_id} has an invalid action.")
        if candidate["resolution_policy"] == "required" and action == "keep":
            errors.append(f"Required span {span_id} cannot use keep.")
        if str(raw.get("confidence") or "") not in ALLOWED_CONFIDENCE:
            errors.append(f"Decision {span_id} has invalid confidence.")
        if action in {"pronounce", "verbalize", "spell_letters"}:
            if not isinstance(spoken, str) or not spoken.strip():
                errors.append(f"Decision {span_id} requires spoken text.")
            elif action == "pronounce":
                if not RESPELLING_RE.fullmatch(spoken):
                    errors.append(f"Decision {span_id} has an invalid pronunciation format.")
                elif _comparison_key(spoken) == _comparison_key(candidate["text"]):
                    errors.append(f"Decision {span_id} repeats the written form.")
        elif spoken not in (None, ""):
            warnings.append(f"Decision {span_id} supplied unused spoken text.")
    expected = set(candidates_by_id)
    duplicate = [item for item, count in collections.Counter(returned).items() if count > 1]
    if duplicate:
        errors.append("Duplicate decisions: " + ", ".join(sorted(duplicate)))
    missing = expected - set(returned)
    if missing:
        errors.append("Missing decisions: " + ", ".join(sorted(missing)))

    token_lookup = {item["id"]: (index, item) for index, item in enumerate(tokens)}
    protected_ranges = [(int(item["start"]), int(item["end"])) for item in [*candidates, *known]]
    valid_discoveries: list[dict[str, Any]] = []
    for index, raw in enumerate(discoveries):
        if not isinstance(raw, dict):
            errors.append(f"Discovery {index} is not an object.")
            continue
        start_entry = token_lookup.get(str(raw.get("start_token_id") or ""))
        end_entry = token_lookup.get(str(raw.get("end_token_id") or ""))
        if not start_entry or not end_entry or start_entry[0] > end_entry[0]:
            errors.append(f"Discovery {index} has invalid token IDs.")
            continue
        start = int(start_entry[1]["start"])
        end = int(end_entry[1]["end"])
        source_text = deterministic_text[start:end]
        if str(raw.get("source_text") or "") != source_text:
            errors.append(f"Discovery {index} source_text does not match its token range.")
            continue
        if any(start < protected_end and protected_start < end for protected_start, protected_end in protected_ranges):
            warnings.append(f"Discovery {index} duplicates a protected span and was ignored.")
            continue
        action = str(raw.get("action") or "")
        spoken = str(raw.get("spoken") or "").strip()
        if action not in {"pronounce", "verbalize", "spell_letters"} or not spoken:
            errors.append(f"Discovery {index} has an invalid action or empty spoken value.")
            continue
        if action == "pronounce" and not RESPELLING_RE.fullmatch(spoken):
            errors.append(f"Discovery {index} has an invalid pronunciation format.")
            continue
        if str(raw.get("confidence") or "") not in ALLOWED_CONFIDENCE:
            errors.append(f"Discovery {index} has invalid confidence.")
            continue
        valid_discoveries.append({**raw, "start": start, "end": end})
    if len(prosody) > 2:
        errors.append("prosody may contain at most two entries.")
    for index, raw in enumerate(prosody[:2]):
        if not isinstance(raw, dict):
            errors.append(f"Prosody item {index} is not an object.")
            continue
        if str(raw.get("after_token_id") or "") not in token_lookup:
            errors.append(f"Prosody item {index} has an invalid token.")
        if raw.get("kind") not in {"short_pause", "sentence_break"}:
            errors.append(f"Prosody item {index} has an invalid kind.")

    retention = None
    placeholder_integrity = None
    if mode == "flexible":
        template = parsed.get("speech_template")
        if not isinstance(template, str) or not template.strip():
            errors.append("Flexible plan has no speech_template.")
        else:
            actual = collections.Counter(PLACEHOLDER_RE.findall(template))
            expected_counts = collections.Counter(placeholder_counts)
            placeholder_integrity = actual == expected_counts
            if not placeholder_integrity:
                errors.append("speech_template changed or invented protected placeholders.")
            retention = difflib.SequenceMatcher(
                a=_lexical_tokens(protected_template),
                b=_lexical_tokens(template),
                autojunk=False,
            ).ratio()
            if retention < min_retention:
                errors.append(
                    f"speech_template lexical retention {retention:.3f} is below {min_retention:.3f}."
                )
    elif "speech_template" in parsed:
        warnings.append("Guarded response returned an unused speech_template.")
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "valid_discoveries": valid_discoveries,
        "placeholder_integrity": placeholder_integrity,
        "retention_ratio": retention,
    }


def _render_decision(candidate: dict[str, Any], decision: dict[str, Any] | None) -> str:
    if not decision:
        return str(candidate["text"])
    action = str(decision.get("action") or "")
    spoken = str(decision.get("spoken") or "")
    if action == "pronounce" and spoken:
        return render_respelling(spoken)
    if action in {"verbalize", "spell_letters"} and spoken:
        return spoken
    return str(candidate["text"])


def _render_known(entry: dict[str, Any]) -> str:
    try:
        return render_respelling(entry.get("spoken"))
    except ValueError:
        return str(entry.get("text") or "")


def compile_plan(
    parsed: dict[str, Any],
    *,
    mode: str,
    deterministic_text: str,
    candidates: list[dict[str, Any]],
    known: list[dict[str, Any]],
    protected_template: str,
    validation: dict[str, Any],
) -> str:
    decisions = {
        str(item.get("span_id")): item
        for item in parsed.get("decisions", [])
        if isinstance(item, dict) and item.get("span_id")
    }
    if mode == "flexible":
        speech = str(parsed.get("speech_template") or protected_template)
        replacements = {
            "{{" + item["id"] + "}}": _render_known(item) for item in known
        }
        replacements.update(
            {
                "{{" + item["id"] + "}}": _render_decision(
                    item, decisions.get(item["id"])
                )
                for item in candidates
            }
        )
        for placeholder, replacement in replacements.items():
            speech = speech.replace(placeholder, replacement)
        return " ".join(speech.split())

    replacements: list[dict[str, Any]] = []
    for item in known:
        replacements.append(
            {
                "start": item["start"],
                "end": item["end"],
                "spoken": _render_known(item),
                "kind": "known",
            }
        )
    for item in candidates:
        replacements.append(
            {
                "start": item["start"],
                "end": item["end"],
                "spoken": _render_decision(item, decisions.get(item["id"])),
                "kind": "decision",
            }
        )
    for item in validation.get("valid_discoveries", []):
        spoken = str(item.get("spoken") or "")
        if item.get("action") == "pronounce":
            spoken = render_respelling(spoken)
        replacements.append(
            {
                "start": item["start"],
                "end": item["end"],
                "spoken": spoken,
                "kind": "discovery",
            }
        )
    accepted: list[dict[str, Any]] = []
    for item in sorted(replacements, key=lambda value: (value["start"], value["end"])):
        if any(_overlaps(item, existing) for existing in accepted):
            continue
        accepted.append(item)
    speech = deterministic_text
    for item in sorted(accepted, key=lambda value: value["start"], reverse=True):
        start, end = int(item["start"]), int(item["end"])
        spoken = str(item["spoken"])
        left = speech[start - 1] if start else ""
        right = speech[end] if end < len(speech) else ""
        if left.isalnum() and spoken and spoken[0].isalnum():
            spoken = " " + spoken
        if right.isalnum() and spoken and spoken[-1].isalnum():
            spoken += " "
        speech = speech[:start] + spoken + speech[end:]
    return " ".join(speech.split())


def _prompt_payload(
    *,
    case_id: str,
    language: str,
    voice_language: str,
    display_text: str,
    deterministic_text: str,
    tokens: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    known: list[dict[str, Any]],
    protected_template: str,
    placeholder_counts: dict[str, int],
    mode: str,
) -> dict[str, Any]:
    payload = {
        "case_id": case_id,
        "language": language,
        "voice_language": voice_language,
        "display_text": display_text,
        "deterministic_text": deterministic_text,
        "token_anchors": " ".join(f"{item['id']}={item['text']}" for item in tokens),
        "reviewed_pronunciations": [
            {
                "id": item["id"],
                "text": item["text"],
                "spoken": item["spoken"],
            }
            for item in known
        ],
        "unresolved_candidates": [
            {
                "id": item["id"],
                "text": item["text"],
                "suggested_task": item["task"],
                "signals": item["signals"],
                "resolution_policy": item["resolution_policy"],
            }
            for item in candidates
        ],
    }
    if mode == "flexible":
        payload["protected_speech_template"] = protected_template
        payload["required_placeholder_counts"] = placeholder_counts
    return payload


def plan_speech_text(
    text: str,
    *,
    language: str,
    voice_language: str,
    mode: str,
    model_name: str,
    llm_settings: Any,
    known_pronunciations: list[dict[str, Any]] | None = None,
    cancel_event: Any | None = None,
    min_retention: float = 0.9,
    completion_func: Callable[..., Any] | None = None,
) -> SpeechPlanResult:
    """Create one validated plan; flexible mode falls back to the guarded protocol."""
    display_text = " ".join(str(text or "").split())
    deterministic_text = display_text
    case_id = hashlib.sha256(
        f"{language}\0{voice_language}\0{display_text}".encode("utf-8")
    ).hexdigest()[:16]
    candidates, known = detect_candidates(
        deterministic_text,
        language=language,
        known_pronunciations=list(known_pronunciations or []),
    )
    tokens = tokenize(deterministic_text)
    protected_template, placeholder_counts = build_protected_template(
        deterministic_text,
        candidates,
        known,
    )
    completion = completion_func or chat_completion_with_metadata
    requested_mode = "flexible" if mode == "flexible" else "guarded"
    modes = [requested_mode, "guarded"] if requested_mode == "flexible" else ["guarded"]
    attempts: list[SpeechPlanAttempt] = []
    responses: list[ChatCompletionResult] = []
    accepted: SpeechPlanAttempt | None = None

    for attempted_mode in modes:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Speech planning was canceled.")
        payload = _prompt_payload(
            case_id=case_id,
            language=language,
            voice_language=voice_language,
            display_text=display_text,
            deterministic_text=deterministic_text,
            tokens=tokens,
            candidates=candidates,
            known=known,
            protected_template=protected_template,
            placeholder_counts=placeholder_counts,
            mode=attempted_mode,
        )
        kwargs = {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        FLEXIBLE_SYSTEM_PROMPT
                        if attempted_mode == "flexible"
                        else GUARDED_SYSTEM_PROMPT
                    ),
                },
                {
                    "role": "user",
                    "content": "Plan this single speech sentence:\n"
                    + json.dumps(payload, ensure_ascii=False, indent=2),
                },
            ],
            "model_name": model_name,
            "llm_settings": llm_settings,
            "max_tokens": 1100 if attempted_mode == "flexible" else 850,
        }
        if completion_func is None:
            kwargs["cancel_event"] = cancel_event
        response = completion(**kwargs)
        if isinstance(response, str):
            raw = response
            normalized_response = None
        else:
            raw = str(getattr(response, "content", "") or "")
            normalized_response = response
            responses.append(response)
        parsed, parse_note = _extract_json(raw)
        validation = validate_plan(
            parsed,
            mode=attempted_mode,
            case_id=case_id,
            deterministic_text=deterministic_text,
            tokens=tokens,
            candidates=candidates,
            known=known,
            protected_template=protected_template,
            placeholder_counts=placeholder_counts,
            min_retention=max(0.75, min(float(min_retention), 1.0)),
            parse_note=parse_note,
        )
        attempt = SpeechPlanAttempt(
            mode=attempted_mode,
            response=normalized_response,
            parsed=parsed,
            validation=validation,
            raw_content=raw,
        )
        attempts.append(attempt)
        if validation["valid"]:
            accepted = attempt
            break

    if accepted is not None and accepted.parsed is not None:
        compiled = compile_plan(
            accepted.parsed,
            mode=accepted.mode,
            deterministic_text=deterministic_text,
            candidates=candidates,
            known=known,
            protected_template=protected_template,
            validation=accepted.validation,
        )
        decisions = accepted.parsed.get("decisions", [])
        status = "valid"
        mode_used = accepted.mode
        discoveries = accepted.parsed.get("discoveries", [])
        prosody = accepted.parsed.get("prosody", [])
        validation = {
            key: value
            for key, value in accepted.validation.items()
            if key != "valid_discoveries"
        }
    else:
        fallback_replacements = [
            {
                "start": item["start"],
                "end": item["end"],
                "spoken": _render_known(item),
            }
            for item in known
        ]
        compiled = deterministic_text
        for item in sorted(fallback_replacements, key=lambda value: value["start"], reverse=True):
            compiled = (
                compiled[: int(item["start"])]
                + str(item["spoken"])
                + compiled[int(item["end"]) :]
            )
        compiled = " ".join(compiled.split())
        decisions = []
        discoveries = []
        prosody = []
        status = "safe_fallback"
        mode_used = "deterministic"
        validation = {
            "valid": False,
            "errors": [
                error
                for attempt in attempts
                for error in attempt.validation.get("errors", [])
            ],
            "warnings": [
                warning
                for attempt in attempts
                for warning in attempt.validation.get("warnings", [])
            ],
        }
    plan = {
        "version": 1,
        "case_id": case_id,
        "status": status,
        "mode_requested": requested_mode,
        "mode_used": mode_used,
        "model": model_name,
        "source_hash": hashlib.sha256(display_text.encode("utf-8")).hexdigest(),
        "language": language,
        "voice_language": voice_language,
        "deterministic_text": deterministic_text,
        "protected_template": protected_template,
        "compiled_text": compiled,
        "known_pronunciations": known,
        "candidates": candidates,
        "decisions": decisions,
        "discoveries": discoveries,
        "prosody": prosody,
        "validation": validation,
        "attempts": [
            {
                "mode": attempt.mode,
                "valid": bool(attempt.validation.get("valid")),
                "errors": list(attempt.validation.get("errors", [])),
                "warnings": list(attempt.validation.get("warnings", [])),
            }
            for attempt in attempts
        ],
    }
    return SpeechPlanResult(text=compiled, plan=plan, responses=responses)
