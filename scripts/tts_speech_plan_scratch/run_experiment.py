#!/usr/bin/env python3
"""Exercise guarded and contextual TTS speech-plan prompts against a local LLM.

This is an isolated experiment. It does not import or modify Pandrator's web
workflow. The only optional runtime integrations are NeMo text normalization
and the host's Hunspell executable.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_CASES = SCRIPT_DIR / "cases.json"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "Outputs" / "tts_speech_plan_scratch"

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
        "zero",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
        "twenty",
        "thirty",
        "forty",
        "fifty",
        "sixty",
        "seventy",
        "eighty",
        "ninety",
        "hundred",
        "thousand",
    }
)
SIMPLE_RESPELLING_RE = re.compile(
    r"^[a-z]+(?:-[a-z]+)*(?: [a-z]+(?:-[a-z]+)*)*$"
)


GUARDED_SYSTEM_PROMPT = """You are a constrained speech-planning component.
You receive display text, a deterministic text-normalization result, stable
tokens, known pronunciations, and unresolved candidate spans.

Decide every unresolved candidate exactly once. A candidate whose
resolution_policy is required must not use keep: choose the appropriate
pronounce, verbalize, or spell_letters action unless the result is genuinely
uncertain. Review candidates may use keep. Allowed actions:
- pronounce: provide an English-oriented phonetic respelling in lowercase
  ASCII syllables separated by ASCII hyphens, for example ee-mah-oh-kah.
- verbalize: provide the complete words that should be spoken.
- spell_letters: provide letters separated by spaces.
- keep: the supplied text is already suitable.
- uncertain: changing it would be unsafe.

Do not rewrite the sentence. Do not return decisions for known pronunciations.
Independently audit the complete deterministic text for anything NeMo left
awkward or unspoken, including single-letter abbreviations, duplicated words,
broken ranges, symbols, and OCR-like debris. Report each additional problem in
discoveries; it must cite valid input token IDs and reproduce the exact source
text. Never repeat a supplied candidate or known pronunciation as a discovery.
Use prosody only for a pause that is not already conveyed by punctuation;
normally return an empty list and never return more than two entries. Return
JSON only, with this exact top-level shape:
{
  "case_id": "same ID as input",
  "decisions": [
    {
      "span_id": "P1 or N1",
      "action": "pronounce|verbalize|spell_letters|keep|uncertain",
      "spoken": "string or null",
      "confidence": "high|medium|low"
    }
  ],
  "discoveries": [
    {
      "start_token_id": "T1",
      "end_token_id": "T1",
      "source_text": "exact source text",
      "action": "pronounce|verbalize|spell_letters",
      "spoken": "string",
      "confidence": "high|medium|low"
    }
  ],
  "prosody": [
    {"after_token_id": "T1", "kind": "short_pause|sentence_break"}
  ]
}
Use empty arrays when there are no entries. Never add explanations."""


CONTEXTUAL_SYSTEM_PROMPT = """You are a contextual speech-text editor.
You receive display text, a deterministic text-normalization result, a protected
speech template, stable tokens, known pronunciations, and unresolved spans.

Return a complete speech_template for natural TTS delivery. You may fix missed
verbalization, speech-only punctuation, and phrasing, but you must not paraphrase,
add facts, remove facts, or correct the display text. Every placeholder in the
protected template must occur in your speech_template exactly as many times and
in the same spelling. Placeholders are literal protected text: copy strings such
as {{P1}} and {{N1}} into speech_template; never replace them with either their
source text or their spoken value. The host performs those substitutions later.
Do not invent placeholders.

Example:
- protected template: "Doctor {{P1}} read {{N1}}."
- valid speech_template: "Doctor {{P1}} read {{N1}}."
- invalid speech_template: "Doctor ee-mah-oh-kah read chapter four."
- decisions may separately map P1 to ee-mah-oh-kah and N1 to chapter four.

Decide every unresolved candidate exactly once. A candidate whose
resolution_policy is required must not use keep: choose the appropriate
pronounce, verbalize, or spell_letters action unless the result is genuinely
uncertain. Review candidates may use keep. Allowed actions:
- pronounce: lowercase ASCII syllables separated by ASCII hyphens, such as
  ee-mah-oh-kah.
- verbalize: the complete words to speak.
- spell_letters: letters separated by spaces.
- keep: retain the candidate's written form.
- uncertain: retain it because changing it would be unsafe.

Do not return decisions for known pronunciations. Independently read the entire
deterministic sentence aloud and fix anything NeMo left awkward or unspoken,
including single-letter abbreviations, duplicated words, broken ranges, symbols,
and OCR-like debris. Every such fix must appear both in speech_template and as a
discovery against valid token IDs with exact source_text. Return JSON only, with
exact source_text. Never repeat a supplied candidate or known pronunciation as
a discovery. Use prosody only for a pause that is not already conveyed by
punctuation; normally return an empty list and never return more than two
entries. Return JSON only, with this exact shape:
{
  "case_id": "same ID as input",
  "speech_template": "the complete sentence containing all placeholders",
  "decisions": [
    {
      "span_id": "P1 or N1",
      "action": "pronounce|verbalize|spell_letters|keep|uncertain",
      "spoken": "string or null",
      "confidence": "high|medium|low"
    }
  ],
  "discoveries": [
    {
      "start_token_id": "T1",
      "end_token_id": "T1",
      "source_text": "exact source text",
      "action": "pronounce|verbalize|spell_letters",
      "spoken": "string",
      "confidence": "high|medium|low"
    }
  ],
  "prosody": [
    {"after_token_id": "T1", "kind": "short_pause|sentence_break"}
  ]
}
Use empty arrays when there are no entries. Never add explanations."""


_NEMO_NORMALIZERS: dict[str, Any] = {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:5012/v1",
        help="OpenAI-compatible API root.",
    )
    parser.add_argument("--model", default="local-model")
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=("guarded", "contextual"),
        default=("guarded", "contextual"),
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--case-id", action="append", dest="case_ids")
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--max-tokens", type=int, default=900)
    parser.add_argument("--seed", type=int, default=3407)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "low", "medium", "high"),
        help="Optional OpenAI-compatible reasoning_effort value.",
    )
    parser.add_argument(
        "--nemo",
        choices=("on", "off"),
        default="on",
        help="Run NeMo before candidate detection.",
    )
    parser.add_argument("--hunspell-dictionary", default="en_US")
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Prepare NeMo text and candidates without calling the model.",
    )
    return parser.parse_args()


def load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("The corpus must be an object containing a cases list.")
    cases = payload["cases"]
    seen: set[str] = set()
    for case in cases:
        case_id = str(case.get("id") or "")
        if not case_id or case_id in seen:
            raise ValueError(f"Missing or duplicate case ID: {case_id!r}")
        if not str(case.get("text") or "").strip():
            raise ValueError(f"Case {case_id} has no text.")
        seen.add(case_id)
    return cases


def normalize_with_nemo(text: str, language: str) -> tuple[str, str | None]:
    try:
        from nemo_text_processing.text_normalization.normalize import Normalizer
    except Exception as exc:  # pragma: no cover - depends on the experiment host
        return text, f"NeMo unavailable: {type(exc).__name__}: {exc}"

    normalized_language = str(language or "en").lower().split("-", 1)[0]
    try:
        normalizer = _NEMO_NORMALIZERS.get(normalized_language)
        if normalizer is None:
            cache_dir = Path(
                os.environ.get(
                    "PANDRATOR_NEMO_CACHE_DIR",
                    Path.home() / ".cache" / "nemo_text_processing",
                )
            )
            cache_dir.mkdir(parents=True, exist_ok=True)
            normalizer = Normalizer(
                input_case="cased",
                lang=normalized_language,
                deterministic=True,
                cache_dir=str(cache_dir),
                overwrite_cache=False,
                post_process=True,
            )
            _NEMO_NORMALIZERS[normalized_language] = normalizer
        result = normalizer.normalize(
            text,
            verbose=False,
            punct_pre_process=False,
            punct_post_process=False,
        )
    except Exception as exc:  # pragma: no cover - model grammar dependent
        return text, f"NeMo failed: {type(exc).__name__}: {exc}"
    result = str(result or "").strip()
    if not result:
        return text, "NeMo returned empty text."
    return result, None


def hunspell_unknown_words(text: str, dictionary: str) -> tuple[set[str], str | None]:
    executable = shutil.which("hunspell")
    if not executable:
        return set(), "Hunspell executable unavailable."
    try:
        completed = subprocess.run(
            [executable, "-d", dictionary, "-l"],
            input=text + "\n",
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
    except Exception as exc:
        return set(), f"Hunspell failed: {type(exc).__name__}: {exc}"
    if completed.returncode not in (0, 1):
        message = completed.stderr.strip() or f"exit code {completed.returncode}"
        return set(), f"Hunspell failed: {message}"
    unknown = {
        line.strip().casefold()
        for line in completed.stdout.splitlines()
        if line.strip() and not line.startswith("@(#)")
    }
    return unknown, None


def _candidate_key(text: str) -> str:
    return " ".join(text.casefold().split())


def _candidate_in_text(text: str, candidate: str) -> bool:
    escaped = re.escape(candidate)
    prefix = r"(?<!\w)" if candidate and candidate[0].isalnum() else ""
    suffix = r"(?!\w)" if candidate and candidate[-1].isalnum() else ""
    return re.search(prefix + escaped + suffix, text) is not None


def detect_candidates(
    text: str,
    *,
    language: str,
    dictionary: str,
    seed_candidates: list[dict[str, Any]],
    known_pronunciations: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    candidates: collections.OrderedDict[str, dict[str, Any]] = collections.OrderedDict()

    def add_candidate(
        candidate_text: str,
        task: str,
        signals: list[str],
        *,
        seeded: bool = False,
        resolution_policy: str | None = None,
    ) -> None:
        candidate_text = str(candidate_text or "").strip()
        if not candidate_text or not _candidate_in_text(text, candidate_text):
            return
        key = _candidate_key(candidate_text)
        known_key = next(
            (known for known in known_pronunciations if _candidate_key(known) == key),
            None,
        )
        if known_key is not None:
            return
        existing = candidates.get(key)
        if existing is None:
            policy = resolution_policy or ("required" if seeded else "review")
            candidates[key] = {
                "text": candidate_text,
                "task": task,
                "signals": list(dict.fromkeys(signals)),
                "seeded": seeded,
                "detected_automatically": not seeded,
                "resolution_policy": policy,
            }
            return
        existing["signals"] = list(dict.fromkeys(existing["signals"] + signals))
        existing["seeded"] = bool(existing["seeded"] or seeded)
        existing["detected_automatically"] = bool(
            existing["detected_automatically"] or not seeded
        )
        if seeded:
            existing["task"] = task
        if seeded or resolution_policy == "required":
            existing["resolution_policy"] = "required"

    for seed in seed_candidates:
        add_candidate(
            str(seed.get("text") or ""),
            str(seed.get("task") or "pronunciation"),
            ["manual_seed", *[str(value) for value in seed.get("signals", [])]],
            seeded=True,
        )

    unknown_words, hunspell_warning = hunspell_unknown_words(text, dictionary)
    if hunspell_warning:
        warnings.append(hunspell_warning)
    for match in WORD_RE.finditer(text):
        word = match.group(0)
        first_letter = next((char for char in word if char.isalpha()), "")
        if (
            first_letter
            and first_letter.isupper()
            and not word.isupper()
            and word.casefold() in unknown_words
        ):
            signals = ["hunspell_oov", "proper_name_like"]
            if any(ord(char) > 127 for char in word):
                signals.append("non_ascii")
            add_candidate(word, "pronunciation", signals)

    residual_matches: list[tuple[re.Pattern[str], str, list[str]]] = [
        (URL_OR_EMAIL_RE, "verbalize", ["url_or_email", "survived_nemo"]),
        (ABBREVIATION_RE, "verbalize", ["abbreviation", "survived_nemo"]),
        (ROMAN_RE, "verbalize", ["roman_numeral", "survived_nemo"]),
        (RESIDUAL_NUMBER_RE, "verbalize", ["number", "survived_nemo"]),
        (ALL_CAPS_RE, "review", ["all_caps", "acronym_or_word"]),
        (SYMBOL_RE, "verbalize", ["symbol", "survived_nemo"]),
    ]
    for pattern, task, signals in residual_matches:
        for match in pattern.finditer(text):
            add_candidate(match.group(0), task, signals)

    for match in REPEATED_WORD_RE.finditer(text):
        add_candidate(
            match.group(0),
            "verbalize",
            ["adjacent_repeated_word", "survived_nemo"],
            resolution_policy="required",
        )

    word_matches = list(WORD_RE.finditer(text))
    for dash in re.finditer(r"[–-]", text):
        previous = next(
            (match for match in reversed(word_matches) if match.end() <= dash.start()),
            None,
        )
        following = next(
            (match for match in word_matches if match.start() >= dash.end()),
            None,
        )
        if (
            previous
            and following
            and previous.group(0).casefold() in NUMBER_WORDS
            and following.group(0).casefold() in NUMBER_WORDS
        ):
            add_candidate(
                dash.group(0),
                "verbalize",
                ["numeric_range_separator", "survived_nemo"],
                resolution_policy="required",
            )

    prefixes = collections.defaultdict(int)
    finalized: list[dict[str, Any]] = []
    for candidate in candidates.values():
        prefix = "P" if candidate["task"] == "pronunciation" else "N"
        prefixes[prefix] += 1
        finalized.append({"id": f"{prefix}{prefixes[prefix]}", **candidate})

    known: list[dict[str, Any]] = []
    for index, (term, respelling) in enumerate(known_pronunciations.items(), start=1):
        if _candidate_in_text(text, term):
            known.append(
                {
                    "id": f"K{index}",
                    "text": term,
                    "spoken": respelling,
                    "task": "pronunciation",
                    "status": "known",
                }
            )
    return finalized, known, warnings


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


def compact_token_anchors(tokens: list[dict[str, Any]]) -> str:
    return " ".join(f"{token['id']}={token['text']}" for token in tokens)


def _replace_term_with_marker(text: str, term: str, marker: str) -> tuple[str, int]:
    escaped = re.escape(term)
    prefix = r"(?<!\w)" if term and term[0].isalnum() else ""
    suffix = r"(?!\w)" if term and term[-1].isalnum() else ""
    return re.subn(prefix + escaped + suffix, marker, text)


def build_template(
    text: str,
    candidates: list[dict[str, Any]],
    known: list[dict[str, Any]],
) -> tuple[str, dict[str, int], list[str]]:
    warnings: list[str] = []
    template = text
    placeholder_counts: dict[str, int] = {}
    entries = sorted(
        [*known, *candidates],
        key=lambda item: len(str(item["text"])),
        reverse=True,
    )
    marker_map: dict[str, str] = {}
    for entry in entries:
        placeholder = "{{" + str(entry["id"]) + "}}"
        marker = f"\ue000{entry['id']}\ue001"
        template, count = _replace_term_with_marker(template, str(entry["text"]), marker)
        if not count:
            warnings.append(
                f"Could not place {entry['id']} ({entry['text']!r}) in deterministic text."
            )
            continue
        placeholder_counts[placeholder] = count
        marker_map[marker] = placeholder
    for marker, placeholder in marker_map.items():
        template = template.replace(marker, placeholder)
    return template, placeholder_counts, warnings


def prepare_case(
    case: dict[str, Any],
    *,
    nemo_enabled: bool,
    dictionary: str,
) -> dict[str, Any]:
    language = str(case.get("language") or "en")
    display_text = str(case["text"]).strip()
    if nemo_enabled:
        deterministic_text, nemo_warning = normalize_with_nemo(display_text, language)
    else:
        deterministic_text, nemo_warning = display_text, None
    candidates, known, detector_warnings = detect_candidates(
        deterministic_text,
        language=language,
        dictionary=dictionary,
        seed_candidates=list(case.get("seed_candidates") or []),
        known_pronunciations=dict(case.get("known_pronunciations") or {}),
    )
    base_template, placeholder_counts, template_warnings = build_template(
        deterministic_text,
        candidates,
        known,
    )
    placed_ids = {
        placeholder.removeprefix("{{").removesuffix("}}")
        for placeholder in placeholder_counts
    }
    candidates = [
        candidate for candidate in candidates if candidate["id"] in placed_ids
    ]
    known = [entry for entry in known if entry["id"] in placed_ids]
    warnings = detector_warnings + template_warnings
    if nemo_warning:
        warnings.insert(0, nemo_warning)
    return {
        "case_id": case["id"],
        "kind": case.get("kind", "unspecified"),
        "source": case.get("source"),
        "language": language,
        "voice_language": str(case.get("voice_language") or language),
        "display_text": display_text,
        "deterministic_text": deterministic_text,
        "tokens": tokenize(deterministic_text),
        "candidates": candidates,
        "known_pronunciations": known,
        "base_template": base_template,
        "placeholder_counts": placeholder_counts,
        "expect": dict(case.get("expect") or {}),
        "warnings": warnings,
    }


def prompt_payload(prepared: dict[str, Any], mode: str) -> dict[str, Any]:
    payload = {
        "case_id": prepared["case_id"],
        "language": prepared["language"],
        "voice_language": prepared["voice_language"],
        "display_text": prepared["display_text"],
        "deterministic_text": prepared["deterministic_text"],
        "token_anchors": compact_token_anchors(prepared["tokens"]),
        "known_pronunciations": prepared["known_pronunciations"],
        "unresolved_candidates": [
            {
                "id": candidate["id"],
                "text": candidate["text"],
                "suggested_task": candidate["task"],
                "signals": candidate["signals"],
                "resolution_policy": candidate["resolution_policy"],
            }
            for candidate in prepared["candidates"]
        ],
    }
    if mode == "contextual":
        payload["protected_speech_template"] = prepared["base_template"]
        payload["required_placeholder_counts"] = prepared["placeholder_counts"]
    return payload


def build_messages(prepared: dict[str, Any], mode: str) -> list[dict[str, str]]:
    system_prompt = (
        GUARDED_SYSTEM_PROMPT if mode == "guarded" else CONTEXTUAL_SYSTEM_PROMPT
    )
    payload = prompt_payload(prepared, mode)
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": "Plan this single sentence:\n"
            + json.dumps(payload, ensure_ascii=False, indent=2),
        },
    ]


def request_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float,
) -> tuple[dict[str, Any], float]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from model endpoint: {body}") from exc
    elapsed = time.perf_counter() - started
    return json.loads(body), elapsed


def call_model(
    prepared: dict[str, Any],
    mode: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    messages = build_messages(prepared, mode)
    payload: dict[str, Any] = {
        "model": args.model,
        "messages": messages,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "seed": args.seed,
        "stream": False,
    }
    if args.reasoning_effort:
        payload["reasoning_effort"] = args.reasoning_effort
    response, elapsed = request_json(
        args.endpoint.rstrip("/") + "/chat/completions",
        payload,
        timeout=args.timeout,
    )
    choices = response.get("choices") or []
    message = choices[0].get("message") if choices else {}
    content = message.get("content") if isinstance(message, dict) else ""
    return {
        "messages": messages,
        "request": {key: value for key, value in payload.items() if key != "messages"},
        "response": response,
        "raw_content": str(content or ""),
        "reasoning_content": (
            str(message.get("reasoning_content") or "")
            if isinstance(message, dict)
            else ""
        ),
        "latency_seconds": elapsed,
        "finish_reason": choices[0].get("finish_reason") if choices else None,
        "usage": response.get("usage") or {},
    }


def extract_json_object(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed, None
        return None, "Top-level JSON value is not an object."
    except json.JSONDecodeError:
        pass
    for offset, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _end = json.JSONDecoder().raw_decode(text[offset:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed, "Recovered JSON object from surrounding text."
    return None, "No valid JSON object found."


def _placeholder_counter(text: str) -> collections.Counter[str]:
    return collections.Counter(PLACEHOLDER_RE.findall(str(text or "")))


def _lexical_tokens(text: str) -> list[str]:
    return [
        token.casefold()
        for token in TOKEN_RE.findall(str(text or ""))
        if token.strip() and (PLACEHOLDER_RE.fullmatch(token) or any(ch.isalnum() for ch in token))
    ]


def _comparison_key(value: Any) -> str:
    return "".join(char for char in str(value or "").casefold() if char.isalnum())


def _surface_key(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def validate_plan(
    parsed: dict[str, Any] | None,
    *,
    mode: str,
    prepared: dict[str, Any],
    parse_note: str | None,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if parse_note:
        warnings.append(parse_note)
    if parsed is None:
        return {
            "parse_ok": False,
            "schema_ok": False,
            "errors": ["Response was not parseable JSON."],
            "warnings": warnings,
            "candidate_coverage": 0.0,
            "pronunciation_format_rate": None,
            "placeholder_integrity": None,
            "retention_ratio": None,
        }

    if parsed.get("case_id") != prepared["case_id"]:
        errors.append("case_id does not match the input.")
    decisions = parsed.get("decisions")
    discoveries = parsed.get("discoveries")
    prosody = parsed.get("prosody")
    if not isinstance(decisions, list):
        errors.append("decisions must be a list.")
        decisions = []
    if not isinstance(discoveries, list):
        errors.append("discoveries must be a list.")
        discoveries = []
    if not isinstance(prosody, list):
        errors.append("prosody must be a list.")
        prosody = []

    candidate_by_id = {
        candidate["id"]: candidate for candidate in prepared["candidates"]
    }
    expected_ids = set(candidate_by_id)
    returned_ids: list[str] = []
    pronunciation_checks: list[bool] = []
    for index, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            errors.append(f"Decision {index} is not an object.")
            continue
        span_id = str(decision.get("span_id") or "")
        returned_ids.append(span_id)
        if span_id not in expected_ids:
            errors.append(f"Decision references unknown span {span_id!r}.")
        action = str(decision.get("action") or "")
        if action not in ALLOWED_ACTIONS:
            errors.append(f"Decision {span_id!r} has invalid action {action!r}.")
        candidate = candidate_by_id.get(span_id)
        if (
            candidate
            and (
                candidate.get("resolution_policy")
                or ("required" if candidate.get("seeded") else "review")
            )
            == "required"
            and action == "keep"
        ):
            errors.append(
                f"Required candidate {span_id!r} cannot use keep."
            )
        confidence = str(decision.get("confidence") or "")
        if confidence not in ALLOWED_CONFIDENCE:
            errors.append(f"Decision {span_id!r} has invalid confidence.")
        spoken = decision.get("spoken")
        if action in {"pronounce", "verbalize", "spell_letters"}:
            if not isinstance(spoken, str) or not spoken.strip():
                errors.append(f"Decision {span_id!r} requires spoken text.")
            elif action == "pronounce":
                pronunciation_ok = bool(SIMPLE_RESPELLING_RE.fullmatch(spoken))
                pronunciation_checks.append(pronunciation_ok)
                if not pronunciation_ok:
                    errors.append(
                        f"Pronunciation {span_id!r} is not lowercase ASCII "
                        f"hyphenated respelling: {spoken!r}."
                    )
                if candidate and _comparison_key(spoken) == _comparison_key(
                    candidate["text"]
                ):
                    errors.append(
                        f"Pronunciation {span_id!r} repeats the written form unchanged."
                    )
            elif candidate and _surface_key(spoken) == _surface_key(
                candidate["text"]
            ):
                warnings.append(
                    f"Decision {span_id!r} uses {action!r} but leaves the text unchanged."
                )
        elif spoken not in (None, ""):
            warnings.append(
                f"Decision {span_id!r} supplied spoken text for {action!r}."
            )

    duplicate_ids = sorted(
        span_id
        for span_id, count in collections.Counter(returned_ids).items()
        if count > 1
    )
    if duplicate_ids:
        errors.append(f"Duplicate decisions: {', '.join(duplicate_ids)}.")
    missing_ids = sorted(expected_ids - set(returned_ids))
    if missing_ids:
        errors.append(f"Missing decisions: {', '.join(missing_ids)}.")

    validation_tokens = prepared["tokens"]
    if any("start" not in token or "end" not in token for token in validation_tokens):
        validation_tokens = tokenize(prepared["deterministic_text"])
    token_lookup = {
        token["id"]: (index, token)
        for index, token in enumerate(validation_tokens)
    }
    valid_token_ids = set(token_lookup)
    candidate_source_keys = {
        _comparison_key(candidate["text"]) for candidate in prepared["candidates"]
    }
    for index, discovery in enumerate(discoveries):
        if not isinstance(discovery, dict):
            errors.append(f"Discovery {index} is not an object.")
            continue
        start_id = str(discovery.get("start_token_id") or "")
        end_id = str(discovery.get("end_token_id") or "")
        if start_id not in valid_token_ids or end_id not in valid_token_ids:
            errors.append(f"Discovery {index} references invalid token IDs.")
        elif token_lookup[start_id][0] > token_lookup[end_id][0]:
            errors.append(f"Discovery {index} has a reversed token range.")
        else:
            start_token = token_lookup[start_id][1]
            end_token = token_lookup[end_id][1]
            expected_source = prepared["deterministic_text"][
                int(start_token["start"]) : int(end_token["end"])
            ]
            if str(discovery.get("source_text") or "") != expected_source:
                errors.append(
                    f"Discovery {index} source_text does not match its token range."
                )
        action = str(discovery.get("action") or "")
        if action not in {"pronounce", "verbalize", "spell_letters"}:
            errors.append(f"Discovery {index} has invalid action {action!r}.")
        if not str(discovery.get("source_text") or ""):
            errors.append(f"Discovery {index} has no source_text.")
        if not str(discovery.get("spoken") or ""):
            errors.append(f"Discovery {index} has no spoken text.")
        elif action == "pronounce" and not SIMPLE_RESPELLING_RE.fullmatch(
            str(discovery["spoken"])
        ):
            errors.append(
                f"Discovery {index} pronunciation is not a lowercase ASCII "
                "hyphenated respelling."
            )
        confidence = str(discovery.get("confidence") or "")
        if confidence not in ALLOWED_CONFIDENCE:
            errors.append(f"Discovery {index} has invalid confidence.")
        if _comparison_key(discovery.get("source_text")) in candidate_source_keys:
            warnings.append(
                f"Discovery {index} duplicates an existing candidate source."
            )

    for index, instruction in enumerate(prosody):
        if not isinstance(instruction, dict):
            errors.append(f"Prosody instruction {index} is not an object.")
            continue
        if str(instruction.get("after_token_id") or "") not in valid_token_ids:
            errors.append(f"Prosody instruction {index} references an invalid token.")
        if instruction.get("kind") not in {"short_pause", "sentence_break"}:
            errors.append(f"Prosody instruction {index} has an invalid kind.")

    placeholder_integrity: bool | None = None
    retention_ratio: float | None = None
    if mode == "contextual":
        speech_template = parsed.get("speech_template")
        if not isinstance(speech_template, str) or not speech_template.strip():
            errors.append("Contextual response has no speech_template.")
        else:
            actual_counts = _placeholder_counter(speech_template)
            expected_counts = collections.Counter(prepared["placeholder_counts"])
            placeholder_integrity = actual_counts == expected_counts
            if not placeholder_integrity:
                errors.append(
                    "speech_template changed, removed, duplicated, or invented placeholders."
                )
            base_tokens = _lexical_tokens(prepared["base_template"])
            output_tokens = _lexical_tokens(speech_template)
            retention_ratio = difflib.SequenceMatcher(
                a=base_tokens,
                b=output_tokens,
                autojunk=False,
            ).ratio()
            if retention_ratio < 0.85:
                warnings.append(
                    f"Low non-placeholder lexical retention ({retention_ratio:.3f})."
                )
    elif "speech_template" in parsed:
        warnings.append("Guarded response unexpectedly returned speech_template.")

    coverage = (
        len(expected_ids & set(returned_ids)) / len(expected_ids)
        if expected_ids
        else 1.0
    )
    pronunciation_rate = (
        sum(pronunciation_checks) / len(pronunciation_checks)
        if pronunciation_checks
        else None
    )
    return {
        "parse_ok": True,
        "schema_ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "candidate_coverage": coverage,
        "pronunciation_format_rate": pronunciation_rate,
        "placeholder_integrity": placeholder_integrity,
        "retention_ratio": retention_ratio,
    }


def _decision_map(parsed: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(parsed, dict) or not isinstance(parsed.get("decisions"), list):
        return {}
    return {
        str(decision.get("span_id")): decision
        for decision in parsed["decisions"]
        if isinstance(decision, dict) and decision.get("span_id")
    }


def _replace_spoken_segment(
    text: str,
    target: str,
    replacement: str,
    *,
    bounded: bool = False,
    occurrence: int | None = None,
) -> str:
    """Replace an opaque segment while inserting word boundaries when needed."""

    escaped = re.escape(target)
    if bounded:
        prefix = r"(?<!\w)" if target and target[0].isalnum() else ""
        suffix = r"(?!\w)" if target and target[-1].isalnum() else ""
        escaped = prefix + escaped + suffix
    pattern = re.compile(escaped)

    def rendered(match: re.Match[str]) -> str:
        spoken = replacement
        left = text[match.start() - 1] if match.start() else ""
        right = text[match.end()] if match.end() < len(text) else ""
        if left.isalnum() and spoken and spoken[0].isalnum():
            spoken = " " + spoken
        if right.isalnum() and spoken and spoken[-1].isalnum():
            spoken += " "
        return spoken

    if occurrence is None:
        return pattern.sub(rendered, text)
    matches = list(pattern.finditer(text))
    if occurrence < 0 or occurrence >= len(matches):
        return text
    match = matches[occurrence]
    return text[: match.start()] + rendered(match) + text[match.end() :]


def _source_occurrence(
    text: str,
    source: str,
    source_start: int,
) -> int:
    escaped = re.escape(source)
    prefix = r"(?<!\w)" if source and source[0].isalnum() else ""
    suffix = r"(?!\w)" if source and source[-1].isalnum() else ""
    matches = list(re.finditer(prefix + escaped + suffix, text))
    return sum(match.start() < source_start for match in matches)


def compile_preview(
    parsed: dict[str, Any] | None,
    *,
    mode: str,
    prepared: dict[str, Any],
) -> str:
    parsed = parsed or {}
    if mode == "contextual" and isinstance(parsed.get("speech_template"), str):
        speech = parsed["speech_template"]
    else:
        speech = prepared["base_template"]

    decisions = _decision_map(parsed)
    replacements: dict[str, str] = {}
    for known in prepared["known_pronunciations"]:
        replacements["{{" + known["id"] + "}}"] = str(known["spoken"]).replace("-", "")
    for candidate in prepared["candidates"]:
        placeholder = "{{" + candidate["id"] + "}}"
        decision = decisions.get(candidate["id"], {})
        action = decision.get("action")
        spoken = decision.get("spoken")
        if action in {"pronounce", "verbalize", "spell_letters"} and isinstance(
            spoken, str
        ):
            replacement = spoken.replace("-", "") if action == "pronounce" else spoken
        else:
            replacement = str(candidate["text"])
        replacements[placeholder] = replacement

    for placeholder, replacement in replacements.items():
        speech = _replace_spoken_segment(speech, placeholder, replacement)

    discoveries = parsed.get("discoveries")
    if isinstance(discoveries, list):
        compilation_tokens = prepared["tokens"]
        if any(
            "start" not in token or "end" not in token
            for token in compilation_tokens
        ):
            compilation_tokens = tokenize(prepared["deterministic_text"])
        token_lookup = {
            token["id"]: (index, token)
            for index, token in enumerate(compilation_tokens)
        }
        protected_source_keys = {
            _comparison_key(item["text"])
            for item in [
                *prepared["candidates"],
                *prepared["known_pronunciations"],
            ]
        }
        for discovery in discoveries:
            if not isinstance(discovery, dict):
                continue
            source = str(discovery.get("source_text") or "")
            spoken = str(discovery.get("spoken") or "")
            action = str(discovery.get("action") or "")
            if (
                not source
                or not spoken
                or source not in speech
                or _comparison_key(source) in protected_source_keys
            ):
                continue
            start_entry = token_lookup.get(
                str(discovery.get("start_token_id") or "")
            )
            end_entry = token_lookup.get(str(discovery.get("end_token_id") or ""))
            if (
                not start_entry
                or not end_entry
                or start_entry[0] > end_entry[0]
            ):
                continue
            start_token = start_entry[1]
            end_token = end_entry[1]
            expected_source = prepared["deterministic_text"][
                int(start_token["start"]) : int(end_token["end"])
            ]
            if source != expected_source:
                continue
            if action == "pronounce":
                spoken = spoken.replace("-", "")
            occurrence = _source_occurrence(
                prepared["deterministic_text"],
                source,
                int(start_token["start"]),
            )
            speech = _replace_spoken_segment(
                speech,
                source,
                spoken,
                bounded=True,
                occurrence=occurrence,
            )
    return " ".join(speech.split())


def expectation_metrics(
    prepared: dict[str, Any],
    parsed: dict[str, Any] | None,
    compiled: str,
) -> dict[str, Any]:
    expected_terms = [
        str(term) for term in prepared.get("expect", {}).get("pronunciation_terms", [])
    ]
    candidate_by_key = {
        _candidate_key(candidate["text"]): candidate
        for candidate in prepared["candidates"]
    }
    decisions = _decision_map(parsed)
    automatic_hits = 0
    merged_hits = 0
    pronunciation_decisions = 0
    details: list[dict[str, Any]] = []
    for term in expected_terms:
        candidate = candidate_by_key.get(_candidate_key(term))
        merged_hit = candidate is not None
        automatic_hit = bool(
            candidate and candidate.get("detected_automatically")
        )
        decision = decisions.get(candidate["id"], {}) if candidate else {}
        pronounced = decision.get("action") == "pronounce"
        merged_hits += int(merged_hit)
        automatic_hits += int(automatic_hit)
        pronunciation_decisions += int(pronounced)
        details.append(
            {
                "term": term,
                "merged_candidate": merged_hit,
                "automatic_candidate": automatic_hit,
                "model_action": decision.get("action"),
                "spoken": decision.get("spoken"),
            }
        )
    known_reuse = []
    for known in prepared["known_pronunciations"]:
        rendered = str(known["spoken"]).replace("-", "")
        known_reuse.append(
            {
                "term": known["text"],
                "rendered": rendered,
                "present_in_preview": rendered.casefold() in compiled.casefold(),
            }
        )
    total = len(expected_terms)
    return {
        "expected_pronunciation_terms": details,
        "automatic_detection_rate": automatic_hits / total if total else None,
        "merged_detection_rate": merged_hits / total if total else None,
        "model_pronunciation_rate": pronunciation_decisions / total if total else None,
        "known_reuse": known_reuse,
    }


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def append_jsonl(path: Path, payload: Any) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def format_candidate(candidate: dict[str, Any]) -> str:
    signals = ", ".join(candidate["signals"])
    seed = "; seeded" if candidate.get("seeded") else ""
    automatic = "; auto" if candidate.get("detected_automatically") else ""
    policy = candidate.get("resolution_policy") or (
        "required" if candidate.get("seeded") else "review"
    )
    return (
        f"`{candidate['id']}` {candidate['text']} → {candidate['task']}/{policy} "
        f"({signals}{seed}{automatic})"
    )


def build_report(
    prepared_cases: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    args: argparse.Namespace,
) -> str:
    result_lookup = {
        (result["case_id"], result["mode"]): result for result in results
    }
    lines = [
        "# TTS speech-plan scratch report",
        "",
        f"- Generated: {dt.datetime.now(dt.timezone.utc).isoformat()}",
        f"- Model: `{args.model}`",
        f"- Endpoint: `{args.endpoint}`",
        f"- Modes: {', '.join(args.modes)}",
        f"- NeMo: {args.nemo}",
        f"- Hunspell dictionary: `{args.hunspell_dictionary}`",
        "",
    ]
    if results:
        parse_ok = sum(result["validation"]["parse_ok"] for result in results)
        schema_ok = sum(result["validation"]["schema_ok"] for result in results)
        contextual = [
            result for result in results if result["mode"] == "contextual"
        ]
        placeholder_ok = sum(
            result["validation"]["placeholder_integrity"] is True
            for result in contextual
        )
        latencies = [float(result["latency_seconds"]) for result in results]
        lines.extend(
            [
                "## Summary",
                "",
                f"- Parseable JSON: {parse_ok}/{len(results)}",
                f"- Fully plan-valid: {schema_ok}/{len(results)}",
                (
                    f"- Contextual placeholder integrity: "
                    f"{placeholder_ok}/{len(contextual)}"
                    if contextual
                    else "- Contextual placeholder integrity: not tested"
                ),
                f"- Mean request latency: {sum(latencies) / len(latencies):.2f} s",
                "",
            ]
        )

    lines.extend(["## Cases", ""])
    for prepared in prepared_cases:
        lines.extend(
            [
                f"### {prepared['case_id']} — {prepared['kind']}",
                "",
                f"**Display:** {prepared['display_text']}",
                "",
                f"**NeMo/deterministic:** {prepared['deterministic_text']}",
                "",
                f"**Protected template:** `{prepared['base_template']}`",
                "",
            ]
        )
        if prepared["candidates"]:
            lines.append("Candidates:")
            lines.append("")
            lines.extend(
                f"- {format_candidate(candidate)}"
                for candidate in prepared["candidates"]
            )
            lines.append("")
        else:
            lines.extend(["Candidates: none.", ""])
        if prepared["known_pronunciations"]:
            lines.append(
                "Known: "
                + ", ".join(
                    f"{entry['text']} → `{entry['spoken']}`"
                    for entry in prepared["known_pronunciations"]
                )
            )
            lines.append("")
        if prepared["warnings"]:
            lines.append(
                "Preparation warnings: " + "; ".join(prepared["warnings"])
            )
            lines.append("")

        for mode in args.modes:
            result = result_lookup.get((prepared["case_id"], mode))
            if result is None:
                continue
            validation = result["validation"]
            status = "valid" if validation["schema_ok"] else "invalid"
            lines.extend(
                [
                    f"#### {mode} — {status}, {result['latency_seconds']:.2f} s",
                    "",
                    f"**Compiled speech preview:** {result['compiled_preview']}",
                    "",
                ]
            )
            parsed = result.get("parsed")
            if isinstance(parsed, dict):
                decisions = parsed.get("decisions") or []
                if decisions:
                    lines.append("Decisions:")
                    lines.append("")
                    for decision in decisions:
                        if isinstance(decision, dict):
                            lines.append(
                                f"- `{decision.get('span_id')}` "
                                f"{decision.get('action')}: "
                                f"`{decision.get('spoken')}` "
                                f"({decision.get('confidence')})"
                            )
                    lines.append("")
            if validation["errors"]:
                lines.append("Errors: " + "; ".join(validation["errors"]))
                lines.append("")
            if validation["warnings"]:
                lines.append("Warnings: " + "; ".join(validation["warnings"]))
                lines.append("")
            metrics = result["expectation_metrics"]
            for detail in metrics["expected_pronunciation_terms"]:
                lines.append(
                    f"Expected `{detail['term']}`: automatic="
                    f"{detail['automatic_candidate']}, action={detail['model_action']}, "
                    f"spoken=`{detail['spoken']}`"
                )
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()
    cases = load_cases(args.cases)
    if args.case_ids:
        selected = set(args.case_ids)
        cases = [case for case in cases if case["id"] in selected]
    if args.limit is not None:
        cases = cases[: max(0, args.limit)]
    if not cases:
        raise SystemExit("No cases selected.")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = args.output_dir or (DEFAULT_OUTPUT_ROOT / stamp)
    output_dir.mkdir(parents=True, exist_ok=True)
    prepared_cases = [
        prepare_case(
            case,
            nemo_enabled=args.nemo == "on",
            dictionary=args.hunspell_dictionary,
        )
        for case in cases
    ]
    write_json(output_dir / "prepared_cases.json", prepared_cases)

    results: list[dict[str, Any]] = []
    results_path = output_dir / "results.jsonl"
    if results_path.exists():
        results_path.unlink()
    if not args.prepare_only:
        total = len(prepared_cases) * len(args.modes)
        completed = 0
        for prepared in prepared_cases:
            for mode in args.modes:
                completed += 1
                print(
                    f"[{completed}/{total}] {prepared['case_id']} {mode}",
                    flush=True,
                )
                model_result = call_model(prepared, mode, args)
                parsed, parse_note = extract_json_object(model_result["raw_content"])
                validation = validate_plan(
                    parsed,
                    mode=mode,
                    prepared=prepared,
                    parse_note=parse_note,
                )
                compiled = compile_preview(
                    parsed,
                    mode=mode,
                    prepared=prepared,
                )
                result = {
                    "case_id": prepared["case_id"],
                    "mode": mode,
                    **model_result,
                    "parsed": parsed,
                    "validation": validation,
                    "compiled_preview": compiled,
                    "expectation_metrics": expectation_metrics(
                        prepared,
                        parsed,
                        compiled,
                    ),
                }
                results.append(result)
                append_jsonl(results_path, result)
                print(
                    f"  schema_ok={validation['schema_ok']} "
                    f"latency={model_result['latency_seconds']:.2f}s",
                    flush=True,
                )

    report = build_report(prepared_cases, results, args=args)
    (output_dir / "report.md").write_text(report, encoding="utf-8")
    write_json(
        output_dir / "run_config.json",
        {
            "cases": str(args.cases),
            "endpoint": args.endpoint,
            "model": args.model,
            "modes": args.modes,
            "temperature": args.temperature,
            "max_tokens": args.max_tokens,
            "seed": args.seed,
            "reasoning_effort": args.reasoning_effort,
            "nemo": args.nemo,
            "hunspell_dictionary": args.hunspell_dictionary,
            "prepare_only": args.prepare_only,
        },
    )
    print(f"Report: {output_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
