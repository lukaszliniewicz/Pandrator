"""Zoom transcript parsing helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import llm_handler
from .llm_config import resolve_dubbing_llm_settings
from .settings import migrate_dubbing_payload


_SPEAKER_LINE_RE = re.compile(r"^\s*([^:]+):\s*(.*)")
_TIMESTAMP_RE = re.compile(r"^\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}")
_SEQUENCE_RE = re.compile(r"^\d+$")

ZOOM_CORRECTION_PROMPT_TEMPLATE = """Your task is to correct a machine-generated transcript of a meeting.

Instructions:
1. You will receive a chunk of the transcript. It contains speaker labels (e.g., "John Doe:").
2. Preserve the speaker labels and the overall format exactly as they are. Do not add, remove, or modify speaker labels.
3. Correct spelling, punctuation, capitalization, and obvious transcription errors to create fluent, grammatically correct sentences.
4. Remove filler words (e.g., "um", "uh") and unnecessary repetitions.
5. Ensure the text flows logically. You can merge or split paragraphs under the same speaker if it improves readability, but do not move text between different speakers.
6. Return ONLY the corrected transcript text. Do not add any comments, explanations, or introductory phrases. Your output should start directly with the first speaker's name.

Here is the transcript chunk to correct:
{transcript_chunk}
"""


@dataclass(frozen=True)
class ZoomCorrectionResult:
    transcript_text: str
    cost: float = 0.0
    response_count: int = 0
    output_path: str = ""


def parse_zoom_vtt(file_handle: Iterable[str]) -> list[dict[str, str]]:
    """Parse Zoom VTT lines into speaker/text utterances."""
    utterances: list[dict[str, str]] = []
    for raw_line in file_handle:
        line = str(raw_line or "").strip()
        if not line:
            continue
        if "WEBVTT" in line or _TIMESTAMP_RE.match(line) or _SEQUENCE_RE.match(line):
            continue

        match = _SPEAKER_LINE_RE.match(line)
        if not match:
            continue

        speaker, text = match.groups()
        utterances.append({"speaker": speaker.strip(), "text": text.strip()})

    return utterances


def group_zoom_utterances(utterances: list[dict[str, str]]) -> list[dict[str, str]]:
    """Group consecutive Zoom utterances by speaker."""
    if not utterances:
        return []

    grouped: list[dict[str, str]] = []
    current_speaker = utterances[0]["speaker"]
    current_text_parts = [utterances[0]["text"]]

    for utterance in utterances[1:]:
        speaker = utterance["speaker"]
        text = utterance["text"]
        if speaker == current_speaker:
            current_text_parts.append(text)
            continue

        grouped.append({"speaker": current_speaker, "text": " ".join(current_text_parts).strip()})
        current_speaker = speaker
        current_text_parts = [text]

    grouped.append({"speaker": current_speaker, "text": " ".join(current_text_parts).strip()})
    return grouped


def create_transcript_chunks_from_grouped(
    grouped_utterances: list[dict[str, str]],
    char_limit: int,
) -> list[str]:
    """Create LLM-ready transcript chunks from grouped utterances."""
    chunks: list[str] = []
    current_chunk_parts: list[str] = []
    current_char_count = 0
    limit = max(1, int(char_limit))

    for utterance in grouped_utterances:
        speaker_line = f"{utterance['speaker']}:"
        text_line = utterance["text"]
        block_len = len(speaker_line) + len(text_line) + 3

        if current_chunk_parts and current_char_count + block_len > limit:
            chunks.append("\n".join(current_chunk_parts).rstrip())
            current_chunk_parts = []
            current_char_count = 0

        current_chunk_parts.extend([speaker_line, text_line, ""])
        current_char_count += block_len

    if current_chunk_parts:
        chunks.append("\n".join(current_chunk_parts).rstrip())

    return chunks


def build_zoom_correction_prompt(transcript_chunk: str) -> str:
    return ZOOM_CORRECTION_PROMPT_TEMPLATE.format(transcript_chunk=str(transcript_chunk or "").strip())


def normalize_zoom_correction_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Migrate legacy Zoom settings to the shared correction-model contract."""
    return migrate_dubbing_payload(settings, settings.get("llm_provider_configs"))


def correct_transcript_chunks(
    chunks: list[str],
    settings: dict[str, Any],
    *,
    completion_func: Callable[..., llm_handler.ChatCompletionResult] = llm_handler.chat_completion_with_metadata,
) -> ZoomCorrectionResult:
    normalized_settings = normalize_zoom_correction_settings(settings)
    llm_settings = resolve_dubbing_llm_settings(normalized_settings, stage="zoom")
    corrected_chunks: list[str] = []
    total_cost = 0.0
    response_count = 0
    max_tokens = int(normalized_settings.get("max_tokens") or normalized_settings.get("llm_max_tokens") or 4000)

    for chunk in chunks:
        prompt = build_zoom_correction_prompt(chunk)
        messages = [{"role": "user", "content": prompt}]
        result = completion_func(
            messages=messages,
            model_name=llm_settings.model_name,
            llm_settings=llm_settings.llm_settings,
            max_tokens=max_tokens,
        )
        content = str(result.content or "").strip()
        if not content:
            raise ValueError("Zoom transcript correction returned an empty response.")
        corrected_chunks.append(content)
        total_cost += float(result.cost or 0.0)
        response_count += 1

    return ZoomCorrectionResult(
        transcript_text="\n".join(corrected_chunks).strip(),
        cost=total_cost,
        response_count=response_count,
    )


def correct_zoom_vtt_content(
    vtt_content: str,
    settings: dict[str, Any],
    *,
    completion_func: Callable[..., llm_handler.ChatCompletionResult] = llm_handler.chat_completion_with_metadata,
) -> ZoomCorrectionResult:
    utterances = parse_zoom_vtt(str(vtt_content or "").splitlines())
    grouped = group_zoom_utterances(utterances)
    char_limit = int(settings.get("llm_char") or settings.get("char_limit") or 6000)
    chunks = create_transcript_chunks_from_grouped(grouped, char_limit)
    if not chunks:
        raise ValueError("No speaker-labeled transcript lines were found in the Zoom VTT content.")
    return correct_transcript_chunks(chunks, settings, completion_func=completion_func)


def correct_zoom_vtt_file(
    vtt_file: str | Path,
    output_dir: str | Path,
    settings: dict[str, Any],
    *,
    completion_func: Callable[..., llm_handler.ChatCompletionResult] = llm_handler.chat_completion_with_metadata,
) -> ZoomCorrectionResult:
    input_path = Path(vtt_file)
    output_path = Path(output_dir) / f"{input_path.stem}_corrected_transcript.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = correct_zoom_vtt_content(
        input_path.read_text(encoding="utf-8-sig"),
        settings,
        completion_func=completion_func,
    )
    output_path.write_text(result.transcript_text, encoding="utf-8")
    return ZoomCorrectionResult(
        transcript_text=result.transcript_text,
        cost=result.cost,
        response_count=result.response_count,
        output_path=str(output_path),
    )
