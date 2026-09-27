"""Pass-specific limits for automatic XML speech analysis."""

from __future__ import annotations

from pandrator.logic.speech_markup import ParsedSpeechMarkup


def assert_pass_result(
    source: ParsedSpeechMarkup,
    result: ParsedSpeechMarkup,
    *,
    purpose: str,
    allow_vocalizations: bool,
) -> None:
    if source.boundary_after != result.boundary_after:
        raise ValueError("Speech analysis must preserve the source boundary.")
    if purpose == "speakers" and source.events != result.events:
        raise ValueError("Speaker analysis must preserve all delivery events.")
    if purpose in {"delivery", "combined"}:
        remaining = iter(result.events)
        for event in source.events:
            if not any(candidate == event for candidate in remaining):
                raise ValueError("Combined analysis changed a source event.")
        if not allow_vocalizations:
            source_counts: dict[tuple[int, str, int | None], int] = {}
            for event in source.events:
                key = (event.offset, event.kind, event.duration_ms)
                source_counts[key] = source_counts.get(key, 0) + 1
            for event in result.events:
                key = (event.offset, event.kind, event.duration_ms)
                count = source_counts.get(key, 0)
                if count:
                    source_counts[key] = count - 1
                elif event.kind != "pause":
                    raise ValueError("This analysis did not authorize added vocalizations.")

    if purpose == "delivery":
        return

    for span in result.spans:
        overlaps = (
            original
            for original in source.spans
            if original.start < span.end and original.end > span.start
        )
        for original in overlaps:
            if span.voice != original.voice:
                raise ValueError("Speech analysis changed an explicit voice binding.")
            if original.speaker_id and span.speaker_id != original.speaker_id:
                raise ValueError("Speech analysis changed a source speaker identity.")
            if (
                original.voice_category != "unspecified"
                and span.voice_category != original.voice_category
            ):
                raise ValueError("Speech analysis changed a source voice category.")
            if original.dialogue and not span.dialogue:
                raise ValueError("Speech analysis removed source dialogue.")
            if original.narrator and not span.narrator:
                raise ValueError("Speech analysis removed source narration.")
            if purpose == "speakers" and span.delivery != original.delivery:
                raise ValueError("Speaker analysis changed delivery controls.")
