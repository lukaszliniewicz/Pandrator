"""Passage-first planning default and optional second-pass regroup selection.

Pure unit tests: no database, no TTS, no model calls. The DB-backed run path
(staging, generation, activation, fallback) is covered in
tests/test_web_voiceover_regroup.py.
"""

import pytest

from pandrator.logic.dubbing.passage_regroup import (
    RegroupPassage,
    mismatch_allowance_ms,
    normalize_generation_mode,
    normalize_regroup_settings,
    regenerated_group_fits,
    select_regroup_candidates,
    validate_regroup_settings,
)
from pandrator.logic.dubbing.speech_blocks import create_speech_blocks
from pandrator.web.workflow_handlers import _voiceover_second_pass


def passage(
    key,
    ordinal,
    start_ms,
    end_ms,
    duration_ms,
    *,
    speaker="SPEAKER_00",
    voice="",
    voice_id="",
    language="en",
    display_chars=20,
    speech_chars=20,
    risk_flags=(),
    user_locked=False,
    boundary_after_cut=False,
    source_refs=None,
):
    return RegroupPassage(
        key=key,
        ordinal=ordinal,
        speaker=speaker,
        voice=voice,
        voice_id=voice_id,
        language=language,
        start_ms=start_ms,
        end_ms=end_ms,
        take_duration_ms=duration_ms,
        display_chars=display_chars,
        speech_chars=speech_chars,
        risk_flags=tuple(risk_flags),
        user_locked=user_locked,
        boundary_after_cut=boundary_after_cut,
        source_refs=(tuple(source_refs) if source_refs is not None else (ordinal + 1,)),
    )


def trio(*, duration=3000, gap=200, span=3000, **kwargs):
    """Three back-to-back fitting passages starting at 0."""
    rows = []
    cursor = 0
    for index, name in enumerate("abc"):
        rows.append(
            passage(
                name,
                index,
                cursor,
                cursor + span,
                duration,
                **kwargs,
            )
        )
        cursor += span + gap
    return rows


# --- settings contract -----------------------------------------------------


def test_regroup_defaults_are_coordinated_and_off():
    assert normalize_regroup_settings({}) == {
        "speech_block_generation_mode": "passage",
        "speech_block_regroup_enabled": False,
        "speech_block_regroup_max_mismatch_ms": 500,
        "speech_block_regroup_max_mismatch_percent": 15,
        "speech_block_regroup_max_gap_ms": 300,
        "speech_block_regroup_max_passages": 3,
        "speech_block_regroup_max_boundary_shift_ms": 500,
    }


def test_generation_mode_defaults_to_passage_and_accepts_legacy():
    assert normalize_generation_mode(None) == "passage"
    assert normalize_generation_mode("") == "passage"
    assert normalize_generation_mode("legacy") == "legacy"
    assert normalize_generation_mode(" Passage ") == "passage"
    with pytest.raises(ValueError):
        normalize_generation_mode("bogus")


@pytest.mark.parametrize(
    "key, bad",
    [
        ("speech_block_regroup_enabled", "yes"),
        ("speech_block_regroup_enabled", 1),
        ("speech_block_regroup_max_mismatch_ms", -1),
        ("speech_block_regroup_max_mismatch_ms", 2001),
        ("speech_block_regroup_max_mismatch_ms", True),
        ("speech_block_regroup_max_mismatch_ms", 1.5),
        ("speech_block_regroup_max_mismatch_percent", -1),
        ("speech_block_regroup_max_mismatch_percent", 51),
        ("speech_block_regroup_max_gap_ms", -1),
        ("speech_block_regroup_max_gap_ms", 2001),
        ("speech_block_regroup_max_passages", 1),
        ("speech_block_regroup_max_passages", 9),
        ("speech_block_regroup_max_boundary_shift_ms", -1),
        ("speech_block_regroup_max_boundary_shift_ms", 2001),
    ],
)
def test_invalid_regroup_values_raise(key, bad):
    with pytest.raises(ValueError):
        validate_regroup_settings({key: bad})
    with pytest.raises(ValueError):
        normalize_regroup_settings({key: bad})


def test_ui_contract_bounds_are_accepted_verbatim():
    """The numeric UI contract: ms keys 0..2000, percent 0..50, passages 2..8."""

    settings = normalize_regroup_settings(
        {
            "speech_block_regroup_max_mismatch_ms": 2000,
            "speech_block_regroup_max_mismatch_percent": 50,
            "speech_block_regroup_max_gap_ms": 2000,
            "speech_block_regroup_max_passages": 8,
            "speech_block_regroup_max_boundary_shift_ms": 2000,
        }
    )
    assert settings["speech_block_regroup_max_mismatch_ms"] == 2000
    assert settings["speech_block_regroup_max_mismatch_percent"] == 50
    assert settings["speech_block_regroup_max_gap_ms"] == 2000
    assert settings["speech_block_regroup_max_passages"] == 8
    assert settings["speech_block_regroup_max_boundary_shift_ms"] == 2000
    validate_regroup_settings(
        {
            "speech_block_regroup_max_mismatch_ms": 0,
            "speech_block_regroup_max_mismatch_percent": 0,
            "speech_block_regroup_max_gap_ms": 0,
            "speech_block_regroup_max_passages": 2,
            "speech_block_regroup_max_boundary_shift_ms": 0,
        }
    )


def test_zero_percent_means_exact_fit_and_eight_passages_cap_at_three():
    settings = normalize_regroup_settings(
        {
            "speech_block_regroup_max_mismatch_percent": 0,
            "speech_block_regroup_max_passages": 8,
        }
    )
    assert settings["speech_block_regroup_max_mismatch_percent"] == 0
    assert settings["speech_block_regroup_max_passages"] == 8
    rows = trio()
    rows.append(passage("d", 3, 9600, 12600, 3000))
    selection = select_regroup_candidates(rows, max_chars=300, max_passages=8)
    assert selection.groups == [("a", "b", "c")]


def test_sparse_validation_ignores_unknown_keys():
    validate_regroup_settings(
        {"speech_block_regroup_max_gap_ms": 100, "some_future_key": "x"}
    )
    validate_regroup_settings({})


def test_migrate_dubbing_payload_carries_coordinated_defaults():
    from pandrator.logic.dubbing.settings import migrate_dubbing_payload

    migrated = migrate_dubbing_payload({})
    assert migrated["speech_block_generation_mode"] == "passage"
    assert migrated["speech_block_regroup_enabled"] is False
    assert migrated["speech_block_regroup_max_mismatch_ms"] == 500
    assert migrated["speech_block_regroup_max_mismatch_percent"] == 15
    assert migrated["speech_block_regroup_max_gap_ms"] == 300
    assert migrated["speech_block_regroup_max_passages"] == 3
    assert migrated["speech_block_regroup_max_boundary_shift_ms"] == 500


def test_migrate_dubbing_payload_repairs_invalid_stored_values():
    from pandrator.logic.dubbing.settings import migrate_dubbing_payload

    migrated = migrate_dubbing_payload(
        {
            "speech_block_generation_mode": "LEGACY",
            "speech_block_regroup_enabled": "yes",
            "speech_block_regroup_max_gap_ms": 999999,
        }
    )
    assert migrated["speech_block_generation_mode"] == "legacy"
    assert migrated["speech_block_regroup_enabled"] is False
    assert migrated["speech_block_regroup_max_gap_ms"] == 300


def test_normalize_dubbing_state_copies_new_fields():
    from types import SimpleNamespace

    from pandrator.logic.dubbing.settings import (
        migrate_dubbing_payload,
        normalize_dubbing_state,
    )

    state = SimpleNamespace(**migrate_dubbing_payload({}))
    normalize_dubbing_state(state)
    assert state.speech_block_generation_mode == "passage"
    assert state.speech_block_regroup_enabled is False


def test_workspace_validator_accepts_and_rejects_new_keys():
    from pandrator.web.workspace import validate_voiceover_repair_settings

    validate_voiceover_repair_settings(
        {
            "speech_block_generation_mode": "legacy",
            "speech_block_regroup_enabled": True,
            "speech_block_regroup_max_mismatch_ms": 0,
            "speech_block_regroup_max_mismatch_percent": 0,
            "speech_block_regroup_max_gap_ms": 0,
            "speech_block_regroup_max_passages": 8,
            "speech_block_regroup_max_boundary_shift_ms": 0,
        }
    )
    with pytest.raises(ValueError):
        validate_voiceover_repair_settings({"speech_block_generation_mode": "bogus"})
    with pytest.raises(ValueError):
        validate_voiceover_repair_settings({"speech_block_regroup_enabled": "yes"})
    with pytest.raises(ValueError):
        validate_voiceover_repair_settings({"speech_block_regroup_max_passages": 9})


def test_regroup_settings_are_planning_only_for_audio_identity():
    from pandrator.web.generation_audio_identity import _material_settings

    base = {"tts": {"service": "XTTS"}, "audio": {}}
    assert _material_settings(base) == _material_settings(
        {
            "tts": {
                "service": "XTTS",
                "speech_block_generation_mode": "legacy",
                "speech_block_regroup_enabled": True,
                "speech_block_regroup_max_mismatch_ms": 100,
                "speech_block_regroup_max_mismatch_percent": 5,
                "speech_block_regroup_max_gap_ms": 50,
                "speech_block_regroup_max_passages": 2,
                "speech_block_regroup_max_boundary_shift_ms": 100,
            },
            "audio": {},
        }
    )


# --- second-pass gating ----------------------------------------------------


def test_second_pass_never_combines_repair_and_regroup():
    tts = lambda **over: {"tts": {"service": "XTTS", **over}}  # noqa: E731
    assert (
        _voiceover_second_pass(
            tts(speech_block_regroup_enabled=True),
            operation="generate",
            has_selected_ids=False,
            workflow_kind="voiceover",
        )
        == "regroup"
    )
    # Passage mode ignores the legacy early-repair flag.
    assert (
        _voiceover_second_pass(
            tts(speech_block_early_repair_enabled=True),
            operation="generate",
            has_selected_ids=False,
            workflow_kind="voiceover",
        )
        is None
    )
    assert (
        _voiceover_second_pass(
            tts(
                speech_block_generation_mode="legacy",
                speech_block_early_repair_enabled=True,
            ),
            operation="generate",
            has_selected_ids=False,
            workflow_kind="voiceover",
        )
        == "repair"
    )
    # Legacy mode ignores the regroup flag.
    assert (
        _voiceover_second_pass(
            tts(
                speech_block_generation_mode="legacy",
                speech_block_regroup_enabled=True,
            ),
            operation="generate",
            has_selected_ids=False,
            workflow_kind="voiceover",
        )
        is None
    )


@pytest.mark.parametrize(
    "snapshot, operation, selected, kind",
    [
        ({"tts": {"speech_block_regroup_enabled": True}}, "rvc", False, "voiceover"),
        (
            {"tts": {"speech_block_regroup_enabled": True}},
            "generate",
            True,
            "voiceover",
        ),
        (
            {"tts": {"speech_block_regroup_enabled": True}},
            "generate",
            False,
            "audiobook",
        ),
        (
            {
                "tts": {"speech_block_regroup_enabled": True},
                "regroup_parent_run_id": "parent",
            },
            "generate",
            False,
            "voiceover",
        ),
        (
            {
                "tts": {
                    "speech_block_generation_mode": "legacy",
                    "speech_block_early_repair_enabled": True,
                },
                "early_repair_parent_run_id": "parent",
            },
            "generate",
            False,
            "voiceover",
        ),
    ],
)
def test_second_pass_stays_idle_outside_first_pass_voiceover(
    snapshot, operation, selected, kind
):
    assert (
        _voiceover_second_pass(
            snapshot,
            operation=operation,
            has_selected_ids=selected,
            workflow_kind=kind,
        )
        is None
    )


# --- passage default vs legacy planning ------------------------------------


SRT_TWO_THOUGHTS = """1
00:00:00,000 --> 00:00:03,000
The first complete thought.

2
00:00:03,500 --> 00:00:06,500
The second complete thought.
"""


def test_passage_default_does_not_pack_adjacent_thoughts():
    blocks = create_speech_blocks(SRT_TWO_THOUGHTS, target_language="en")
    assert [block["text"] for block in blocks] == [
        "The first complete thought.",
        "The second complete thought.",
    ]
    for block in blocks:
        assert "nearby_complete_utterances_packed" not in [
            event["reason_code"] for event in block["provenance"]["formation_events"]
        ]


def test_legacy_mode_keeps_packing_as_selectable_old_behavior():
    blocks = create_speech_blocks(
        SRT_TWO_THOUGHTS, target_language="en", generation_mode="legacy"
    )
    assert len(blocks) == 1
    assert blocks[0]["text"] == (
        "The first complete thought. The second complete thought."
    )
    assert "nearby_complete_utterances_packed" in [
        event["reason_code"] for event in blocks[0]["provenance"]["formation_events"]
    ]


def test_passage_mode_keeps_logical_passages_independent():
    from pandrator.web.logical_passages import passage_srt

    content = passage_srt(
        [
            {"text": "First thought.", "start_ms": 0, "end_ms": 3000},
            {"text": "Second thought.", "start_ms": 3500, "end_ms": 6500},
        ]
    )
    blocks = create_speech_blocks(
        content, target_language="en", max_chars=300, preserve_source_boundaries=True
    )
    assert [block["text"] for block in blocks] == [
        "First thought.",
        "Second thought.",
    ]


def test_unknown_generation_mode_raises():
    with pytest.raises(ValueError):
        create_speech_blocks(SRT_TWO_THOUGHTS, generation_mode="bogus")


def test_mode_does_not_change_segmentation_identity():
    """An approved plan is never rebuilt silently for a mode flip.

    The plan digest covers segmentation settings but not the planning mode:
    when both modes produce the same segments (here one independent
    passage), preparing under either mode resolves to the same identity, so
    generation keeps the existing approved plan. The passage default only
    changes newly prepared topologies.
    """

    from pandrator.web.workflow_handlers import _generation_segmentation_settings

    assert _generation_segmentation_settings(
        {"speech_block_generation_mode": "passage"}
    ) == _generation_segmentation_settings({"speech_block_generation_mode": "legacy"})


def test_independent_passage_is_identical_in_both_modes():
    content = """1
00:00:00,000 --> 00:00:03,000
One independent passage.
"""
    assert create_speech_blocks(
        content, target_language="en", generation_mode="passage"
    ) == create_speech_blocks(content, target_language="en", generation_mode="legacy")


def test_generate_speech_blocks_file_defaults_to_passage(tmp_path):
    import json

    from pandrator.logic.dubbing.speech_blocks import generate_speech_blocks_file

    srt_path = tmp_path / "two_thoughts.srt"
    srt_path.write_text(SRT_TWO_THOUGHTS, encoding="utf-8")
    output_path = tmp_path / "two_thoughts_speech_blocks.json"

    generate_speech_blocks_file(tmp_path, srt_path)
    default_blocks = json.loads(output_path.read_text(encoding="utf-8"))
    assert [block["text"] for block in default_blocks] == [
        "The first complete thought.",
        "The second complete thought.",
    ]

    generate_speech_blocks_file(tmp_path, srt_path, generation_mode="passage")
    explicit_blocks = json.loads(output_path.read_text(encoding="utf-8"))
    assert explicit_blocks == default_blocks


def test_generate_speech_blocks_file_legacy_packs(tmp_path):
    import json

    from pandrator.logic.dubbing.speech_blocks import generate_speech_blocks_file

    srt_path = tmp_path / "two_thoughts.srt"
    srt_path.write_text(SRT_TWO_THOUGHTS, encoding="utf-8")
    output_path = tmp_path / "two_thoughts_speech_blocks.json"

    generate_speech_blocks_file(tmp_path, srt_path, generation_mode="legacy")
    legacy_blocks = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(legacy_blocks) == 1
    assert legacy_blocks[0]["text"] == (
        "The first complete thought. The second complete thought."
    )
    assert "nearby_complete_utterances_packed" in [
        event["reason_code"]
        for event in legacy_blocks[0]["provenance"]["formation_events"]
    ]


# --- true edit-cut protection ----------------------------------------------


def test_output_cut_positions_from_keep_ranges():
    from pandrator.logic.dubbing.passage_regroup import output_cut_positions_ms

    # Source [0,5000) + [8000,13000): one rendered join at output 5000.
    assert output_cut_positions_ms([(0, 5000), (8000, 13000)]) == [5000]
    # Contiguous ranges are one kept span: no false cut.
    assert output_cut_positions_ms([(0, 5000), (5000, 10000)]) == []
    # Overlapping ranges coalesce: no false cut.
    assert output_cut_positions_ms([(0, 6000), (4000, 10000)]) == []
    # Two true cuts accumulate output offsets.
    assert output_cut_positions_ms([(0, 2000), (5000, 7000), (9000, 11000)]) == [
        2000,
        4000,
    ]


def test_candidate_crossing_rendered_cut_rejected_despite_contiguous_refs():
    from pandrator.logic.dubbing.passage_regroup import output_cut_positions_ms

    # Rendered edit joins source [0,6000) to [9000,15000): the retimed cues
    # are renumbered contiguously with no paragraph break, so the old
    # boundary_after_cut / integer-gap guards see nothing.
    cuts = output_cut_positions_ms([(0, 6000), (9000, 15000)])
    assert cuts == [6000]
    left = passage("a", 0, 3000, 6000, 3000, source_refs=(1,))
    right = passage("b", 1, 6000, 9000, 3000, source_refs=(2,))
    assert left.boundary_after_cut is False
    selection = select_regroup_candidates(
        [left, right], max_chars=300, source_cut_positions=cuts
    )
    assert selection.groups == []
    assert {"key": "b", "reason": "source_cut_inside_group"} in selection.rejected


def test_candidates_elsewhere_on_edited_recording_stay_eligible():
    rows = [
        passage("a", 0, 0, 3000, 3000, source_refs=(1,)),
        passage("b", 1, 3000, 6000, 3000, source_refs=(2,)),
        passage("c", 2, 6000, 9000, 3000, source_refs=(3,)),
        passage("d", 3, 9000, 12000, 3000, source_refs=(4,)),
    ]
    selection = select_regroup_candidates(
        rows, max_chars=300, max_passages=2, source_cut_positions=[6000]
    )
    assert selection.groups == [("a", "b"), ("c", "d")]


def test_passage_spanning_cut_excluded():
    spanning = passage("s", 0, 4000, 8000, 4000, source_refs=(1, 2))
    other = passage("o", 1, 8000, 11000, 3000, source_refs=(3,))
    selection = select_regroup_candidates(
        [spanning, other], max_chars=300, source_cut_positions=[6000]
    )
    assert selection.groups == []
    assert {"key": "s", "reason": "source_cut_inside_passage"} in selection.rejected


def test_adjacent_contiguous_ranges_produce_no_false_cut():
    from pandrator.logic.dubbing.passage_regroup import output_cut_positions_ms

    cuts = output_cut_positions_ms([(0, 5000), (5000, 10000)])
    assert cuts == []
    selection = select_regroup_candidates(
        trio(), max_chars=300, source_cut_positions=cuts
    )
    assert selection.groups == [("a", "b", "c")]


def test_known_edited_missing_mapping_skips_conservatively():
    selection = select_regroup_candidates(
        trio(), max_chars=300, edited_cuts_unknown=True
    )
    assert selection.groups == []
    assert len(selection.rejected) == 3
    assert {
        entry["reason"] for entry in selection.rejected
    } == {"edited_source_cuts_unknown"}


# --- candidate selection ---------------------------------------------------


def test_eligible_pair_and_trio():
    selection = select_regroup_candidates(trio(), max_chars=300)
    assert selection.groups == [("a", "b", "c")]
    assert selection.rejected == []


def test_four_eligible_passages_form_disjoint_groups():
    rows = trio()
    rows.append(passage("d", 3, 9600, 12600, 3000))
    selection = select_regroup_candidates(rows, max_chars=300)
    assert selection.groups == [("a", "b", "c")]


def test_max_passages_two_selects_pairs_only():
    selection = select_regroup_candidates(trio(), max_chars=300, max_passages=2)
    assert selection.groups == [("a", "b")]


def test_max_passages_is_hard_capped_at_three():
    selection = select_regroup_candidates(trio(), max_chars=300, max_passages=10)
    assert selection.groups == [("a", "b", "c")]


def test_combined_display_and_speech_chars_share_the_cap():
    rows = trio(display_chars=150, speech_chars=10)
    assert select_regroup_candidates(rows, max_chars=300).groups == []
    rows = trio(display_chars=10, speech_chars=150)
    assert select_regroup_candidates(rows, max_chars=300).groups == []
    rows = trio(display_chars=100, speech_chars=10)
    assert select_regroup_candidates(rows, max_chars=300).groups == [("a", "b")]
    rows = trio(display_chars=90, speech_chars=90)
    selection = select_regroup_candidates(rows, max_chars=300)
    assert selection.groups == [("a", "b", "c")]


def test_gap_boundary_is_gap_inclusive():
    # Perfect takes still drift early by the absorbed gaps: boundary one sits
    # 300ms early (accepted) but boundary two sits 600ms early (rejected).
    selection = select_regroup_candidates(trio(gap=300), max_chars=300)
    assert selection.groups == [("a", "b")]
    assert any(
        note["key"] == "c" and note["reason"] == "boundary_shift_too_large"
        for note in selection.rejected
    )
    assert select_regroup_candidates(trio(gap=200), max_chars=300).groups == [
        ("a", "b", "c")
    ]


def test_overlapping_windows_are_never_eligible():
    rows = trio()
    rows[1] = passage("b", 1, 2900, 5900, 3000)
    selection = select_regroup_candidates(rows, max_chars=300)
    assert selection.groups == []
    assert any(
        note["reason"] == "source_windows_overlap" for note in selection.rejected
    )


def test_duration_tolerance_is_symmetric_and_capped():
    # span 2000 -> allowance min(500, 300) = 300.
    assert select_regroup_candidates(
        [
            passage("a", 0, 0, 2000, 2300),
            passage("b", 1, 2100, 4100, 1700),
        ],
        max_chars=300,
    ).groups == [("a", "b")]
    assert (
        select_regroup_candidates(
            [
                passage("a", 0, 0, 2000, 2301),
                passage("b", 1, 2100, 4100, 2000),
            ],
            max_chars=300,
        ).groups
        == []
    )
    # span 10000 -> allowance min(500, 1500) = 500.
    assert select_regroup_candidates(
        [
            passage("a", 0, 0, 10000, 10500),
            passage("b", 1, 10100, 20100, 10000),
        ],
        max_chars=300,
    ).groups == [("a", "b")]
    assert (
        select_regroup_candidates(
            [
                passage("a", 0, 0, 10000, 10501),
                passage("b", 1, 10100, 20100, 10000),
            ],
            max_chars=300,
        ).groups
        == []
    )


@pytest.mark.parametrize("field", ["speaker", "voice", "voice_id", "language"])
def test_delivery_identity_mismatch_blocks_candidates(field):
    rows = trio()
    rows[1] = passage(
        "b",
        1,
        3200,
        6200,
        3000,
        **{
            "speaker": "SPEAKER_00",
            "voice": "",
            "voice_id": "",
            "language": "en",
            field: "other",
        },
    )
    selection = select_regroup_candidates(rows, max_chars=300)
    assert selection.groups == []
    assert any(
        note["reason"] == "speaker_voice_language_mismatch"
        for note in selection.rejected
    )


@pytest.mark.parametrize(
    "flag",
    ["shared_passage_timing", "estimated_internal_timing", "timing_overlap"],
)
def test_uncertain_shared_timing_is_not_independently_timed(flag):
    rows = trio(risk_flags=[flag])
    selection = select_regroup_candidates(rows, max_chars=300)
    assert selection.groups == []
    assert any(
        note["reason"] == "uncertain_shared_timing" for note in selection.rejected
    )


def test_user_locks_and_cut_boundaries_are_preserved():
    rows = trio()
    rows[1] = passage("b", 1, 3200, 6200, 3000, user_locked=True)
    assert select_regroup_candidates(rows, max_chars=300).groups == []

    rows = trio()
    rows[0] = passage("a", 0, 0, 3000, 3000, boundary_after_cut=True)
    selection = select_regroup_candidates(rows, max_chars=300)
    assert selection.groups == [("b", "c")]


def test_non_adjacent_ordinals_do_not_group():
    rows = trio()
    rows[1] = passage("b", 5, 3200, 6200, 3000)
    assert select_regroup_candidates(rows, max_chars=300).groups == []


def test_skipped_source_reference_means_an_actual_cut():
    rows = trio()
    rows[1] = passage("b", 1, 3200, 6200, 3000, source_refs=[3])
    rows[2] = passage("c", 2, 6400, 9400, 3000, source_refs=[4])
    # The cut between a (ref 1) and b (ref 3) is refused, while the truly
    # consecutive b/c pair still groups.
    selection = select_regroup_candidates(rows, max_chars=300)
    assert selection.groups == [("b", "c")]
    assert any(
        note["key"] == "b" and note["reason"] == "source_reference_cut"
        for note in selection.rejected
    )


def test_duplicate_source_reference_means_overlapping_windows():
    rows = trio()
    rows[1] = passage("b", 1, 3200, 6200, 3000, source_refs=[1])
    selection = select_regroup_candidates(rows, max_chars=300)
    assert selection.groups == []
    assert any(note["reason"] == "source_reference_cut" for note in selection.rejected)


def test_internally_gapped_member_is_rejected():
    rows = trio()
    rows[0] = passage("a", 0, 0, 3000, 3000, source_refs=[1, 3])
    selection = select_regroup_candidates(rows, max_chars=300)
    assert selection.groups == [("b", "c")]
    assert any(
        note["key"] == "a" and note["reason"] == "source_reference_cut"
        for note in selection.rejected
    )


def test_multi_reference_members_must_themselves_be_consecutive():
    rows = [
        passage("a", 0, 0, 3000, 3000, source_refs=[1, 2]),
        passage("b", 1, 3200, 6200, 3000, source_refs=[3]),
    ]
    assert select_regroup_candidates(rows, max_chars=300).groups == [("a", "b")]


def test_non_integer_references_fall_back_to_timestamps():
    rows = [
        passage("a", 0, 0, 3000, 3000, source_refs=["cue-a"]),
        passage("b", 1, 3200, 6200, 3000, source_refs=["cue-b"]),
        passage("c", 2, 6400, 9400, 3000, source_refs=["cue-c"]),
    ]
    assert select_regroup_candidates(rows, max_chars=300).groups == [("a", "b", "c")]


def test_cumulative_drift_rejects_triple_but_keeps_fitting_pair():
    rows = [
        passage("a", 0, 0, 4000, 4500),
        passage("b", 1, 4200, 8200, 4500),
        passage("c", 2, 8400, 12400, 4000),
    ]
    # Boundary one: 4500 - 4200 = +300 (accepted). Boundary two:
    # 9000 - 8400 = +600 (rejected).
    selection = select_regroup_candidates(rows, max_chars=300)
    assert selection.groups == [("a", "b")]
    assert any(
        note["key"] == "c" and note["reason"] == "boundary_shift_too_large"
        for note in selection.rejected
    )


def test_early_violation_is_not_hidden_by_later_cancellation():
    rows = [
        passage("a", 0, 0, 4000, 4500),
        passage("b", 1, 4200, 8200, 4500),
        passage("c", 2, 8400, 12400, 4000),
        # A later short take would pull the total back under the limit, but
        # the violated second boundary is checked before extending.
        passage("d", 3, 12600, 16600, 3500),
    ]
    selection = select_regroup_candidates(rows, max_chars=300)
    # c is refused from the (a, b) group, then pairs with d on its own
    # merits: the violated boundary is never bridged.
    assert selection.groups == [("a", "b"), ("c", "d")]
    assert any(
        note["key"] == "c" and note["reason"] == "boundary_shift_too_large"
        for note in selection.rejected
    )


def test_tight_boundary_shift_has_no_cancellation_hiding():
    rows = trio(duration=3300)
    # Boundary one: 3300 - 3200 = +100 (accepted at the limit).
    # Boundary two: 6600 - 6400 = +200 (rejected).
    selection = select_regroup_candidates(
        rows, max_chars=300, max_boundary_shift_ms=100
    )
    assert selection.groups == [("a", "b")]
    assert any(
        note["reason"] == "boundary_shift_too_large" for note in selection.rejected
    )


def test_mismatch_allowance_edges():
    assert mismatch_allowance_ms(0, max_mismatch_ms=500, max_mismatch_percent=15) == 0
    assert mismatch_allowance_ms(-5, max_mismatch_ms=500, max_mismatch_percent=15) == 0
    assert (
        mismatch_allowance_ms(2000, max_mismatch_ms=500, max_mismatch_percent=15) == 300
    )


def test_regenerated_group_fit_is_symmetric():
    assert regenerated_group_fits(9200, 9400) is True
    assert regenerated_group_fits(9600, 9400) is True
    assert regenerated_group_fits(8900, 9400) is True
    assert regenerated_group_fits(8899, 9400) is False
    assert regenerated_group_fits(9900, 9400) is True
    assert regenerated_group_fits(9901, 9400) is False
    assert regenerated_group_fits(100, 0) is False
    assert regenerated_group_fits(-1, 9400) is False
