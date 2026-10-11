"""Focused descriptor checks for compiler-owned generation defaults."""

from __future__ import annotations

import pytest

from pandrator.logic import audio_cpp_generation_models as registry


@pytest.mark.parametrize(
    ("model_id", "steps", "guidance"),
    (
        ("ace_step_base_bf16", 32, 7.0),
        ("ace_step_base_q8_0", 32, 7.0),
        ("ace_step_xl_sft_bf16", 32, 7.0),
        ("ace_step_xl_sft_q8dit", 32, 7.0),
        ("ace_step_turbo_bf16", 8, None),
        ("ace_step_turbo_q8_0", 8, None),
        ("ace_step_xl_turbo_bf16", 8, None),
        ("ace_step_xl_turbo_q8dit", 8, None),
    ),
)
def test_ace_variant_descriptors_define_compiler_defaults(model_id, steps, guidance):
    parameters = registry.get_model(model_id).parameter_descriptors()
    assert parameters["num_inference_steps"]["default"] == steps
    if guidance is None:
        assert "guidance_scale" not in parameters
    else:
        assert parameters["guidance_scale"]["default"] == guidance


@pytest.mark.parametrize(
    "name",
    ("temperature", "guidance_scale", "codec_duration_sec", "codec_guidance_scale"),
)
def test_heartmula_positive_controls_are_descriptor_driven(name):
    descriptor = registry.get_model("heartmula_f16").parameter_descriptors()[name]
    assert descriptor["exclusive_minimum"] == 0
    assert "minimum" not in descriptor


def test_minimax_h3_uses_the_published_audio_shift_control_name():
    parameters = registry.get_model("minimax_h3_q4_k").parameter_descriptors()
    assert parameters["audio_flow_shift"]["default"] == 3.0
    assert "flow_shift_temp" not in parameters
