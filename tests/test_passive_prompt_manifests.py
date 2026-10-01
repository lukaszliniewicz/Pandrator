"""Shared manifest reuse must not depend on individual batch size."""

import pytest

from pandrator.logic.dubbing.llm_correction import build_correction_task_instructions
from pandrator.logic.dubbing.llm_translation import build_translation_task_instructions


@pytest.mark.parametrize("builder,extra", [
    (build_correction_task_instructions, {}),
    (build_translation_task_instructions, {"source_language": "en", "target_language": "pl"}),
])
@pytest.mark.parametrize("logical_passages", [False, True])
def test_passive_structured_instructions_are_stable_across_batch_sizes(builder, extra, logical_passages):
    kwargs = {**extra, "dispatch_result": True, "structured_context": True, "logical_passages": logical_passages}
    first = builder(subtitle_count=3, **kwargs)
    assert first == builder(subtitle_count=9, **kwargs)
    assert "batch.cue_count" in first
    assert "cue_id" in first
    assert builder(subtitle_count=3, **extra) != builder(subtitle_count=9, **extra)
