"""Shared structural-annotation invariants for speech markup."""

from __future__ import annotations

from pandrator.logic.speech_markup import ParsedSpeechMarkup
from pandrator.logic.speech_performance import Delivery


def assert_structural_annotation(
    source: ParsedSpeechMarkup | None,
    result: ParsedSpeechMarkup,
    *,
    mode: str,
) -> None:
    """Reject controls a structural annotation pass is not allowed to add.

    The comparison is made at character coverage rather than XML node
    boundaries, so a result may subdivide an authored scope while retaining
    exactly the same authored controls. Structural annotation can add
    dialogue/category/narrator/boundary structure, but it cannot manufacture
    delivery directions, events, voice bindings, or (in dialogue mode) speaker
    identities.
    """

    source_spans = tuple(source.spans) if source is not None else ()
    # Parsed markup omits optional delivery controls that have no value.
    empty_delivery = Delivery().model_dump(mode="json", exclude_none=True)
    for result_span in result.spans:
        # Parsed source spans cover the identical transcript. Compare each
        # overlapping metadata scope once, rather than scanning per character.
        overlaps = [
            span
            for span in source_spans
            if span.end > result_span.start and span.start < result_span.end
        ]
        for source_span in overlaps or [None]:
            expected_delivery = (
                source_span.delivery if source_span is not None else empty_delivery
            )
            if result_span.delivery != expected_delivery:
                raise ValueError(
                    "Structural speech annotation introduced or changed delivery "
                    f"controls over {result_span.start}:{result_span.end}; "
                    "preserve authored delivery directions exactly."
                )

            if result_span.voice:
                expected_voice = source_span.voice if source_span is not None else None
                if result_span.voice != expected_voice:
                    raise ValueError(
                        "Structural speech annotation introduced or changed an "
                        f"explicit voice binding over {result_span.start}:{result_span.end}; "
                        "voice bindings must already be authored."
                    )

            if mode == "dialogue" and result_span.speaker_id:
                expected_speaker = (
                    source_span.speaker_id if source_span is not None else None
                )
                if result_span.speaker_id != expected_speaker:
                    raise ValueError(
                        "Dialogue annotation introduced a speaker identity over "
                        f"{result_span.start}:{result_span.end}; preserve authored "
                        "speaker assignments or use speakers mode."
                    )

    source_events = tuple(source.events) if source is not None else ()
    if tuple(result.events) != source_events:
        raise ValueError(
            "Structural speech annotation introduced or changed vocal events; "
            "preserve authored events exactly."
        )


__all__ = ["assert_structural_annotation"]
