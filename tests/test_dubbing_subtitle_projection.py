from pandrator.logic.dubbing.subtitle_projection import project_subtitle_display


def _overlapping_rows(*, turn_ids):
    rows = [
        {"start_ms": 0, "end_ms": 2000, "text": "First fragment", "speaker": "A"},
        {"start_ms": 1500, "end_ms": 3000, "text": "continues here", "speaker": "A"},
    ]
    if turn_ids is not None:
        for row, turn_id in zip(rows, turn_ids, strict=True):
            row["turn_id"] = turn_id
    return rows


def test_projection_keeps_different_turns_separate_and_retains_ids():
    projected = project_subtitle_display(
        _overlapping_rows(turn_ids=("turn-a", "turn-b")), {}
    )

    assert len(projected) == 2
    assert [item["turn_id"] for item in projected] == ["turn-a", "turn-b"]


def test_projection_can_merge_within_same_turn_and_keeps_legacy_when_ids_absent():
    same_turn = project_subtitle_display(
        _overlapping_rows(turn_ids=("turn-a", "turn-a")), {}
    )
    legacy = project_subtitle_display(_overlapping_rows(turn_ids=None), {})

    assert len(same_turn) == 1
    assert same_turn[0]["turn_id"] == "turn-a"
    assert len(legacy) == 1
