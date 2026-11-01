"""Rendered mixes copy as finite audio files without source request history."""

from copy import deepcopy

import pytest

from pandrator.web.projects.copy_outputs import (
    COPIED_OUTPUT_KEY,
    SAVED_SPEECH_ROLE,
    SOUND_MIX_ROLE,
    freeze_saved_speech_output,
    validate_output_copy_graph,
)
from tests.test_project_output_copy import DiscardedManifest, artifact


def mixed_output():
    source = artifact(role=SOUND_MIX_ROLE)
    source["metadata_json"].update(
        sound_mix=DiscardedManifest(),
        audio_timeline={"version": 1, "sample_rate_hz": 24000, "total_frames": 48000},
        duration_ms=111,
        format="mp3",
    )
    return source


def test_mix_projection_discards_private_history_and_uses_actual_frame_duration():
    source = mixed_output()
    poison = source["metadata_json"]["sound_mix"]
    source["metadata_json"]["request"] = poison
    source["metadata_json"]["metadata"]["private_provider"] = poison
    copied = freeze_saved_speech_output(source, allow_sound_mix=True)
    assert copied["role"] == SAVED_SPEECH_ROLE
    assert copied["metadata_json"] == {
        COPIED_OUTPUT_KEY: {"version": 1, "kind": "sound_mix"},
        "duration_ms": 2000,
        "format": "wav",
        "metadata": {
            key: value for key, value in source["metadata_json"]["metadata"].items()
            if key != "private_provider"
        },
    }
    assert source["metadata_json"]["duration_ms"] == 111
    assert source["metadata_json"]["sound_mix"] is poison
    copied["metadata_json"]["metadata"]["title"] = "Destination title"
    assert source["metadata_json"]["metadata"]["title"] != "Destination title"


def test_mix_recopy_and_graph_validation_require_explicit_new_format_support():
    source = mixed_output()
    with pytest.raises(ValueError):
        freeze_saved_speech_output(source)
    copied = freeze_saved_speech_output(source, allow_sound_mix=True)
    before = deepcopy(copied)
    with pytest.raises(ValueError):
        freeze_saved_speech_output(copied)
    with pytest.raises(ValueError):
        validate_output_copy_graph({"artifacts": [copied], "artifact_edges": []})
    validate_output_copy_graph(
        {"artifacts": [copied], "artifact_edges": []}, allow_sound_mix=True)
    recopied = freeze_saved_speech_output(copied, allow_sound_mix=True)
    assert recopied == before and recopied is not copied
    assert recopied["metadata_json"] is not copied["metadata_json"]
    with pytest.raises(ValueError):
        validate_output_copy_graph({"artifacts": [copied], "artifact_edges": [
            {"parent_artifact_id": "old private input", "child_artifact_id": copied["id"]}
        ]}, allow_sound_mix=True)
    with pytest.raises(ValueError):
        validate_output_copy_graph(
            {"artifacts": [source], "artifact_edges": []}, allow_sound_mix=True)


@pytest.mark.parametrize("timeline", [
    {"sample_rate_hz": True, "total_frames": 48000},
    {"sample_rate_hz": 24000, "total_frames": 0},
    {"sample_rate_hz": 0, "total_frames": 1},
    {"sample_rate_hz": 1, "total_frames": 2**53},
])
def test_invalid_selected_mix_timing_is_rejected(timeline):
    source = mixed_output()
    source["metadata_json"]["audio_timeline"] = timeline
    with pytest.raises(ValueError):
        freeze_saved_speech_output(source, allow_sound_mix=True)


def test_missing_mix_timing_does_not_reuse_old_speech_duration_or_chapters():
    source = mixed_output()
    del source["metadata_json"]["audio_timeline"]
    copied = freeze_saved_speech_output(source, allow_sound_mix=True)
    assert "duration_ms" not in copied["metadata_json"]
    assert "chapters" not in copied["metadata_json"]
    assert "segment_count" not in copied["metadata_json"]
