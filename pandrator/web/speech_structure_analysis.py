"""Configured-model speech structure annotation.

This module keeps the model-facing annotation pass separate from TTS text
optimization.  The model may describe structure and delivery intent, but the
returned text is always checked against the host text before anything is
stored.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pandrator.logic.llm_handler import (
    ChatCompletionResult,
    chat_completion_with_metadata,
)
from pandrator.logic.speech_markup import (
    ParsedSpeechMarkup,
    assert_authored_markup_preserved,
    parse_speech_markup,
)
from pandrator.logic.speech_performance import Delivery

from .generation_controls import get_generation_controls, merge_character_proposals
from .speech_planning import _extract_json
from .tts_optimization import OptimizationUsage

ANNOTATION_MODES = frozenset({"off", "dialogue", "speakers"})
MAX_BATCH_UNITS = 8
MAX_BATCH_TEXT_CHARS = 24_000

SPEECH_STRUCTURE_SYSTEM_PROMPT = """You are a constrained speech-structure annotation component.
Annotate the supplied speech units without rewriting their text. Preserve every
character, whitespace character, and unit boundary exactly. Return JSON only:
{"items":[{"unit_id":1,"speech_xml":"..."}],"character_proposals":[]}

The compact XML dialect is: <segment id="UNIT_ID" boundary_after="...">; optional
<dialogue> containing <speaker ref="CHARACTER_ID">, <speaker n="KNOWN_NAME">, or
category/voice-only <speaker g="male|female|androgynous|unspecified"> /
<speaker voice="VOICE">; <narrator voice="VOICE"> and <span voice="VOICE">.
The ref and n speaker attributes are mutually exclusive; neither is needed when g
is non-unspecified or voice is specified. Never invent voice bindings. The only
boundary_after values are continuation, dialogue_turn, paragraph, scene, chapter.
The nonspoken whole-scope metadata tags are <ins>, <em>, <pace>, <cadence>, and
<emphasis>. Their delivery values are parser-defined: pace natural|slower|brisk;
cadence continuing|concluding|questioning|contrast; emphasis light|moderate|strong;
instruction and emotion are plain bounded directions. Events, when already authored,
use only pause, laugh, chuckle, sigh, inhale, exhale, cough, gasp, clear_throat.
Do not add new emotion/instruction/event controls in this structural pass.

Keep existing authored speaker assignments, explicit voices, delivery controls,
events, and boundaries unchanged. Never infer identity solely from sex or voice
category, and never merge namesakes. Character proposals are objects such as
{"id":"c-alice","display_name":"Alice","aliases":["A."],"voice_category":"female","notes":"..."}
and are only suggestions for the host to validate. Return each supplied unit_id exactly
once and in the supplied order. The speech_xml transcript must equal the supplied
text; result items may omit a repeated text field. Do not put XML or markup in text.
"""


def _cancelled(cancel_event: Any) -> bool:
    return bool(cancel_event is not None and cancel_event.is_set())


def _source_for(source_markup: Mapping[str, str] | None, unit_id: int) -> str | None:
    if not source_markup:
        return None
    value = source_markup.get(str(unit_id))
    if value is None:
        value = source_markup.get(unit_id)  # type: ignore[arg-type]
    return value if isinstance(value, str) else None


def _batch_units(texts: Sequence[str]) -> list[list[tuple[int, str]]]:
    batches: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    current_chars = 0
    for index, text in enumerate(texts, start=1):
        text_value = str(text)
        # A single oversized unit must still be sent once; the bound applies to
        # normal request batches and splitting its text would change a unit.
        if current and (
            len(current) >= MAX_BATCH_UNITS
            or current_chars + len(text_value) > MAX_BATCH_TEXT_CHARS
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append((index, text_value))
        current_chars += len(text_value)
    if current:
        batches.append(current)
    return batches


def _assert_structural_only(
    source: ParsedSpeechMarkup | None,
    result: ParsedSpeechMarkup,
    *,
    mode: str,
) -> None:
    """Reject controls a structural annotation pass is not allowed to add.

    The comparison is made at character coverage rather than XML node
    boundaries, so a result may subdivide an authored scope while retaining
    exactly the same authored controls.  Structural annotation can add
    dialogue/category/narrator/boundary structure, but it cannot manufacture
    delivery directions, events, voice bindings, or (in dialogue mode) speaker
    identities.
    """

    source_spans = tuple(source.spans) if source is not None else ()
    empty_delivery = Delivery().model_dump(mode="json")
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


def _parse_markup(
    xml: str,
    *,
    unit_id: int,
    text: str,
    characters: list[dict[str, Any]],
    source_xml: str | None,
    mode: str,
) -> str:
    parsed = parse_speech_markup(
        xml,
        expected_segment_id=str(unit_id),
        expected_text=text,
        characters=characters,
    )
    if source_xml is not None:
        source = parse_speech_markup(
            source_xml,
            expected_segment_id=str(unit_id),
            expected_text=text,
            characters=characters,
        )
        assert_authored_markup_preserved(source, parsed)
    _assert_structural_only(
        source if source_xml is not None else None,
        parsed,
        mode=mode,
    )
    return parsed.xml


def _parse_response(
    content: str,
    expected: list[tuple[int, str]],
    *,
    mode: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    payload, note = _extract_json(content)
    if payload is None:
        raise ValueError(note or "model did not return a JSON object")
    rows = payload.get("items")
    if not isinstance(rows, list):
        raise TypeError("model response must contain an items list")
    expected_ids = [unit_id for unit_id, _text in expected]
    returned_ids: list[int] = []
    by_id: dict[int, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise TypeError("every speech annotation item must be an object")
        try:
            unit_id = int(str(row.get("unit_id")))
        except (TypeError, ValueError) as error:
            raise ValueError(
                "every speech annotation item needs a numeric unit_id"
            ) from error
        if unit_id in by_id:
            raise ValueError(f"duplicate speech annotation unit_id {unit_id}")
        by_id[unit_id] = row
        returned_ids.append(unit_id)
    if returned_ids != expected_ids:
        raise ValueError("return every unit_id exactly once and in supplied order")
    proposals = payload.get("character_proposals", [])
    if not isinstance(proposals, list):
        raise TypeError("character_proposals must be a list")
    if len(proposals) > 100:
        raise ValueError("character_proposals may contain at most 100 entries")
    if mode == "dialogue" and proposals:
        raise ValueError("dialogue annotation mode cannot propose speaker identities")
    for proposal in proposals:
        if not isinstance(proposal, dict):
            raise TypeError("each character proposal must be an object")

    xmls: list[str] = []
    for unit_id, text in expected:
        row = by_id[unit_id]
        supplied_text = row.get("text")
        if supplied_text is not None and supplied_text != text:
            raise ValueError(
                f"unit {unit_id} changed text; speech annotation cannot rewrite text"
            )
        xml = row.get("speech_xml")
        if not isinstance(xml, str) or not xml.strip():
            if mode != "off":
                raise ValueError(
                    f"annotation mode {mode!r} requires speech_xml for unit {unit_id}"
                )
            xmls.append("")
            continue
        # XML is deliberately parsed only after proposals are merged in the
        # submission transaction.  A new stable speaker ID must be usable by
        # the same response that proposed it, while malformed XML still rolls
        # the transaction back atomically.
        xmls.append(xml)
    return xmls, [dict(proposal) for proposal in proposals]


def _controls_snapshot(database: Any, session_id: str) -> dict[str, Any]:
    with database.immediate_session() as session:
        return get_generation_controls(session, session_id)


def _normalize_source_markup(
    database: Any,
    session_id: str,
    texts: Sequence[str],
    source_markup: Mapping[str, str],
) -> dict[str, str]:
    """Canonicalize source markup once against current controls and clean text."""

    if not source_markup:
        return {}
    from .generation_cast_runtime import remap_markup

    controls = _controls_snapshot(database, session_id)
    characters = list(controls.get("characters") or [])
    normalized: dict[str, str] = {}
    for unit_id, text in enumerate(texts, start=1):
        source_xml = _source_for(source_markup, unit_id)
        if source_xml is None:
            continue
        try:
            normalized[str(unit_id)] = remap_markup(
                source_xml,
                str(unit_id),
                text,
                characters,
            )
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"supplied speech_xml for unit {unit_id} no longer matches the clean text "
                f"or current character dictionary; revise the clean text before annotation "
                f"or use annotation_only: {error}"
            ) from error
    return normalized


def _commit_batch(
    database: Any,
    session_id: str,
    expected: list[tuple[int, str]],
    xmls: list[str],
    proposals: list[dict[str, Any]],
    *,
    source_markup: Mapping[str, str] | None,
    mode: str,
    origin: str,
) -> list[str]:
    with database.immediate_session() as session:
        controls = merge_character_proposals(
            session,
            session_id,
            proposals,
            origin=origin,
        )
        characters = list(controls.get("characters") or [])
        result: list[str] = []
        for (unit_id, text), xml in zip(expected, xmls, strict=True):
            if not xml:
                result.append("")
                continue
            result.append(
                _parse_markup(
                    xml,
                    unit_id=unit_id,
                    text=text,
                    characters=characters,
                    source_xml=_source_for(source_markup, unit_id),
                    mode=mode,
                )
            )
        return result


def annotate_speech_units(
    database: Any,
    session_id: str,
    texts: Sequence[str],
    *,
    mode: str,
    llm_settings: Any,
    model_name: str,
    cancel_event: Any,
    source_markup: dict[str, str] | None = None,
    on_usage: Callable[[ChatCompletionResult], None] | None = None,
) -> list[str]:
    """Annotate speech units and return canonical XML in input order.

    Character proposals and markup validation are committed together per batch;
    a malformed response therefore cannot leave an identity proposal behind.
    """

    requested_mode = str(mode or "off").strip().lower()
    if requested_mode not in ANNOTATION_MODES:
        raise ValueError("annotation mode must be off, dialogue, or speakers")
    normalized_texts = [str(text) for text in texts]
    if not normalized_texts:
        return []
    source_markup = {
        str(key): value
        for key, value in (source_markup or {}).items()
        if isinstance(value, str)
    }
    source_markup = _normalize_source_markup(
        database,
        session_id,
        normalized_texts,
        source_markup,
    )
    usage = OptimizationUsage()

    # Off mode never starts a model request, but still validates and preserves
    # authored markup so a later artifact cannot silently discard it.
    if requested_mode == "off":
        preserved: list[str] = []
        for unit_id, text in enumerate(normalized_texts, start=1):
            xml = _source_for(source_markup, unit_id)
            if xml is None:
                preserved.append("")
                continue
            preserved.append(xml)
        return preserved

    results = [""] * len(normalized_texts)
    for batch in _batch_units(normalized_texts):
        if _cancelled(cancel_event):
            return []
        first_id = batch[0][0]
        last_id = batch[-1][0]
        context = {
            "previous": [
                {"unit_id": index, "text": normalized_texts[index - 1]}
                for index in range(max(1, first_id - 2), first_id)
            ],
            "following": (
                [{"unit_id": last_id + 1, "text": normalized_texts[last_id]}]
                if last_id < len(normalized_texts)
                else []
            ),
        }
        last_error = ""
        for attempt in range(2):
            if _cancelled(cancel_event):
                return []
            controls = _controls_snapshot(database, session_id)
            request = {
                "mode": requested_mode,
                "character_revision": int(controls.get("revision") or 0),
                "characters": controls.get("characters") or [],
                "context": context,
                "items": [
                    {
                        "unit_id": unit_id,
                        "text": text,
                        **(
                            {"source_speech_xml": source_xml}
                            if (source_xml := _source_for(source_markup, unit_id))
                            is not None
                            else {}
                        ),
                    }
                    for unit_id, text in batch
                ],
            }
            system_prompt = SPEECH_STRUCTURE_SYSTEM_PROMPT
            if requested_mode == "dialogue":
                system_prompt += (
                    "\nIn dialogue mode, detect dialogue boundaries and optional g voice "
                    "categories only; do not identify speakers."
                )
            else:
                system_prompt += (
                    "\nIn speakers mode, identify speakers only with evidence from the "
                    "text, source markup, or known characters; propose a stable new ID "
                    "with aliases when a new identity is supported."
                )
            if last_error:
                system_prompt += (
                    f"\nCorrect the previous validation error: {last_error}"
                )
            response = chat_completion_with_metadata(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": "Annotate these speech units:\n"
                        + json.dumps(request, ensure_ascii=False),
                    },
                ],
                model_name=model_name,
                llm_settings=llm_settings,
                cancel_event=cancel_event,
            )
            if isinstance(response, str):
                response = ChatCompletionResult(content=response)
            usage.add(response)
            if on_usage is not None:
                on_usage(response)
            if _cancelled(cancel_event):
                return []
            try:
                xmls, proposals = _parse_response(
                    response.content,
                    batch,
                    mode=requested_mode,
                )
                committed = _commit_batch(
                    database,
                    session_id,
                    batch,
                    xmls,
                    proposals,
                    source_markup=source_markup,
                    mode=requested_mode,
                    origin="speech-structure-analysis",
                )
            except (TypeError, ValueError) as error:
                last_error = str(error)
                if attempt == 1:
                    raise ValueError(
                        f"speech structure annotation failed after two attempts: {error}"
                    ) from error
                continue
            for (unit_id, _text), xml in zip(batch, committed, strict=True):
                results[unit_id - 1] = xml
            break
    return results


__all__ = ["ANNOTATION_MODES", "annotate_speech_units"]
