"""Canonical request and native argv compilation tests for generation routes."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from pandrator.logic import audio_cpp_generation_models as registry
from pandrator.logic import audio_cpp_generation_payload as payload
from pandrator.logic import audio_cpp_sfx as sfx


def _request(model_id: str, **extra):
    model = registry.get_model(model_id)
    request = {"model_id": model_id, "prompt": " A bright piano melody ", "seed": 17}
    if model.requires_lyrics:
        request["lyrics"] = "Stay with me"
    request.update(extra)
    return request


def _argv_for(model_id: str, **extra) -> list[str]:
    model = registry.get_model(model_id)
    request = sfx.validate_request(_request(model_id, **extra))
    model_path = Path("/cache") / model.id
    if not model.is_directory_model:
        model_path = model_path / model.model_path_asset.path
    return payload.build_argv("audiocpp_cli", request, model_path, "/private/out.wav")


def _option_values(argv: list[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for index, argument in enumerate(argv[:-1]):
        if argument == "--request-option":
            name, value = argv[index + 1].split("=", 1)
            result.setdefault(name, []).append(value)
    return result


def test_legacy_default_sfx_canonical_request_and_argv_remain_compatible():
    canonical = sfx.validate_request({"prompt": " Wind ", "seed": 19})
    assert canonical == {
        "model_id": "stable_audio_3_small_sfx_q8_0",
        "prompt": "Wind",
        "duration_seconds": 4.0,
        "seed": 19,
        "threads": min(4, __import__("os").cpu_count() or 1),
        "backend": "cpu",
        "num_inference_steps": 8,
        "guidance_scale": 1.0,
    }
    argv = payload.build_argv("audiocpp_cli", canonical, "/cache/stable.gguf", "/private/out.wav")
    assert argv[argv.index("--num-inference-steps") + 1] == "8"
    assert argv[argv.index("--guidance-scale") + 1] == "1"
    assert "--request-option" not in argv
    assert argv[argv.index("--out-format") + 1] == "pcm16"


@pytest.mark.parametrize("model_id", registry.model_ids())
def test_every_pinned_model_compiles_to_family_route_and_manifest_layout(model_id):
    model = registry.get_model(model_id)
    argv = _argv_for(model_id)
    assert argv[0] == "audiocpp_cli"
    assert argv[argv.index("--task") + 1] == "gen"
    assert argv[argv.index("--family") + 1] == model.family
    assert argv[argv.index("--backend") + 1] == model.backends[0]
    assert argv[argv.index("--model") + 1].startswith("/cache/" + model.id)
    assert argv[argv.index("--out") + 1] == "/private/out.wav"
    assert argv[argv.index("--out-format") + 1] == "pcm16"
    if model.is_directory_model:
        assert argv[argv.index("--model") + 1] == "/cache/" + model.id
    else:
        assert argv[argv.index("--model") + 1].endswith(model.model_path_asset.path)


def test_backend_kind_and_model_specific_duration_defaults_and_ranges():
    assert (
        sfx.validate_request(_request("stable_audio_3_small_music_q8_0", backend="VULKAN"))[
            "backend"
        ]
        == "vulkan"
    )
    assert sfx.validate_request(_request("midashenglm_gen_q8_0", kind="music"))["kind"] == "music"
    assert sfx.validate_request(_request("controlfoley_large_44k_q8_0"))["duration_seconds"] == 8
    assert sfx.validate_request(_request("midashenglm_gen_f32"))["duration_seconds"] == 20
    assert sfx.validate_request(_request("minimax_music3_q8_0"))["duration_seconds"] == 20
    assert sfx.validate_request(_request("yue2_main_q8_0"))["duration_seconds"] == 30
    assert sfx.validate_request(_request("minimax_h3_q4_k"))["duration_seconds"] == 5
    with pytest.raises(ValueError, match="kind"):
        sfx.validate_request(_request("controlfoley_large_44k_q8_0", kind="music"))
    with pytest.raises(ValueError, match="between 1 and 60"):
        sfx.validate_request(_request("controlfoley_large_44k_q8_0", duration_seconds=61))
    assert sfx.validate_request(_request("heartmula_f16", backend="cuda"))["backend"] == "cuda"
    assert (
        sfx.validate_request(_request("stable_audio_3_small_music_q8_0", backend="cuda"))["backend"]
        == "cuda"
    )
    assert sfx.validate_request(_request("minimax_h3_q4_k_int8_dit"))["backend"] == "cuda"
    with pytest.raises(ValueError, match="backend for minimax_h3_q4_k_int8_dit"):
        sfx.validate_request(_request("minimax_h3_q4_k_int8_dit", backend="cpu"))


@pytest.mark.parametrize("model_id", ["stable_audio_3_medium_f16", "stable_audio_3_medium_q8_0"])
def test_stable_medium_accepts_effects_and_music_without_changing_omitted_kind(model_id):
    assert sfx.validate_request(_request(model_id))["kind"] == "music"
    for kind in ("sound", "music"):
        request = sfx.validate_request(_request(model_id, kind=kind))
        assert request["kind"] == kind
        argv = _argv_for(model_id, kind=kind, duration_seconds=4)
        assert argv[argv.index("--family") + 1] == "stable_audio"
        assert argv[argv.index("--duration-seconds") + 1] == "4"


@pytest.mark.parametrize(
    ("model_id", "minimum", "maximum"),
    (
        ("stable_audio_3_small_music_q8_0", 1, 120),
        ("stable_audio_3_medium_q8_0", 1, 380),
        ("minimax_music3_q8_0", 1, 300),
        ("heartmula_q8_0", 1, 240),
        ("ace_step_turbo_q8_0", 10, 600),
        ("minimax_h3_q4_k", 4, 10),
    ),
)
def test_documented_generation_windows_reject_out_of_range_requests(model_id, minimum, maximum):
    for duration in (minimum, maximum):
        assert (
            sfx.validate_request(_request(model_id, duration_seconds=duration))["duration_seconds"]
            == duration
        )
    for duration in (minimum - 0.1, maximum + 0.1):
        with pytest.raises(ValueError, match="duration_seconds must be between"):
            sfx.validate_request(_request(model_id, duration_seconds=duration))


def test_parameter_defaults_are_fingerprinted_and_ace_turbo_hides_ignored_cfg():
    request = sfx.validate_request(
        _request(
            "ace_step_turbo_q8_0",
            lyrics="Stay with me",
            parameters={"bpm": 96, "sampler_mode": "heun"},
        )
    )
    assert request["parameters"]["bpm"] == 96
    assert request["parameters"]["sampler_mode"] == "heun"
    assert request["parameters"]["negative_prompt"] == "NO USER INPUT"
    assert request["parameters"]["lm_temperature"] == 0.85
    assert "guidance_scale" not in request["parameters"]
    options = _option_values(payload.build_argv("audiocpp_cli", request, "/model", "/out.wav"))
    assert options["bpm"] == ["96"]
    assert options["sampler_mode"] == ["heun"]
    assert "guidance_scale" not in options

    with pytest.raises(ValueError, match="Unknown parameter.*guidance_scale"):
        sfx.validate_request(_request("ace_step_turbo_q8_0", parameters={"guidance_scale": 2}))


@pytest.mark.parametrize(
    "model_id",
    (
        "ace_step_base_bf16",
        "ace_step_base_q8_0",
        "ace_step_xl_sft_bf16",
        "ace_step_xl_sft_q8dit",
    ),
)
def test_ace_base_and_sft_defaults_compile_once_to_native_controls(model_id):
    request = sfx.validate_request(_request(model_id))
    assert request["parameters"]["num_inference_steps"] == 32
    assert request["parameters"]["guidance_scale"] == 7.0

    argv = payload.build_argv("audiocpp_cli", request, "/model.gguf", "/out.wav")
    assert argv.count("--num-inference-steps") == 1
    assert argv[argv.index("--num-inference-steps") + 1] == "32"
    assert argv.count("--guidance-scale") == 1
    assert argv[argv.index("--guidance-scale") + 1] == "7"
    options = _option_values(argv)
    assert "num_inference_steps" not in options
    assert "guidance_scale" not in options


@pytest.mark.parametrize(
    "model_id",
    (
        "ace_step_turbo_bf16",
        "ace_step_turbo_q8_0",
        "ace_step_xl_turbo_bf16",
        "ace_step_xl_turbo_q8dit",
    ),
)
def test_ace_turbo_defaults_compile_steps_without_ignored_guidance(model_id):
    request = sfx.validate_request(_request(model_id))
    assert request["parameters"]["num_inference_steps"] == 8
    assert "guidance_scale" not in request["parameters"]

    argv = payload.build_argv("audiocpp_cli", request, "/model.gguf", "/out.wav")
    assert argv.count("--num-inference-steps") == 1
    assert argv[argv.index("--num-inference-steps") + 1] == "8"
    assert "--guidance-scale" not in argv
    options = _option_values(argv)
    assert "num_inference_steps" not in options
    assert "steps" not in options
    assert "guidance_scale" not in options


def test_ace_explicit_steps_are_lowered_only_to_the_native_steps_flag():
    request = sfx.validate_request(
        _request("ace_step_base_q8_0", parameters={"num_inference_steps": 64})
    )
    assert request["parameters"]["num_inference_steps"] == 64
    argv = payload.build_argv("audiocpp_cli", request, "/model.gguf", "/out.wav")
    assert argv.count("--num-inference-steps") == 1
    assert argv[argv.index("--num-inference-steps") + 1] == "64"
    options = _option_values(argv)
    assert "num_inference_steps" not in options
    assert "steps" not in options


def test_variant_load_options_are_compiler_owned_and_match_pinned_files():
    ace_cases = {
        "ace_step_base_bf16": "acestep-v15-base",
        "ace_step_turbo_q8_0": "acestep-v15-turbo",
        "ace_step_xl_sft_q8dit": "acestep-v15-xl-sft",
        "ace_step_xl_turbo_bf16": "acestep-v15-xl-turbo",
    }
    for model_id, variant in ace_cases.items():
        argv = _argv_for(model_id)
        assert f"ace_step.dit_model_path={variant}" in argv
        assert argv[argv.index("--task-route") + 1] == "text2music"
        assert argv[argv.index("--language") + 1] == "en"

    q4 = registry.get_model("minimax_music3_q4_0")
    argv = _argv_for(q4.id)
    session_options = [
        argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--session-option"
    ]
    assert "--load-option" not in argv
    assert "minimax_music3.language_model_gguf=language_model_q4_0.gguf" in session_options
    assert "minimax_music3.rvq_depth_decoder_gguf=rvq_depth_decoder_q8_0.gguf" in session_options
    assert "minimax_music3.flow_transformer_gguf=transformer_q4_0.gguf" in session_options
    assert all(
        filename in {Path(item.path).name for item in q4.files}
        for filename in (
            "language_model_q4_0.gguf",
            "rvq_depth_decoder_q8_0.gguf",
            "transformer_q4_0.gguf",
        )
    )


def test_ace_vocal_language_uses_native_text_input_instead_of_ignored_request_option():
    request = sfx.validate_request(_request("ace_step_turbo_q8_0", parameters={"language": "es"}))
    argv = payload.build_argv("audiocpp_cli", request, "/model", "/out.wav")
    assert argv[argv.index("--language") + 1] == "es"
    assert "language" not in _option_values(argv)


@pytest.mark.parametrize(
    ("model_id", "parameters"),
    (
        ("stable_audio_3_small_music_q8_0", {"sampler": "dpm++"}),
        ("controlfoley_large_44k_q8_0", {"num_inference_steps": 201}),
        ("midashenglm_gen_q8_0", {"guidance_scale": math.inf}),
        ("ace_step_base_q8_0", {"sampler_mode": "dpm++"}),
        ("heartmula_f16", {"top_k": True}),
        ("minimax_music3_q8_0", {"top_k": 0}),
    ),
)
def test_unsupported_or_invalid_parameters_are_never_ignored(model_id, parameters):
    with pytest.raises(ValueError):
        sfx.validate_request(_request(model_id, parameters=parameters))


def test_top_level_aliases_must_match_parameter_values():
    with pytest.raises(ValueError, match="Conflicting guidance_scale"):
        sfx.validate_request(
            _request(
                "stable_audio_3_small_music_q8_0",
                guidance_scale=1.0,
                parameters={"guidance_scale": 2.0},
            )
        )
    request = sfx.validate_request(
        _request(
            "stable_audio_3_small_music_q8_0",
            num_inference_steps=12,
            parameters={"num_inference_steps": 12},
        )
    )
    assert request["parameters"]["num_inference_steps"] == 12


def test_public_parameter_bounds_and_primitive_types():
    with pytest.raises(ValueError, match="at most 64"):
        sfx.validate_request(_request("heartmula_f16", parameters={"x" * 65: "value"}))
    with pytest.raises(ValueError, match="at most 2000"):
        sfx.validate_request(_request("heartmula_f16", parameters={"tags": "x" * 2001}))
    with pytest.raises(ValueError, match="at most 32"):
        sfx.validate_request(
            _request("heartmula_f16", parameters={str(index): True for index in range(33)})
        )
    with pytest.raises(ValueError, match="finite number"):
        sfx.validate_request(_request("heartmula_f16", parameters={"temperature": True}))
    with pytest.raises(ValueError, match="finite number"):
        sfx.validate_request(_request("heartmula_f16", parameters={"temperature": math.inf}))


def test_prompt_lyrics_seed_bounds_and_lyrics_capabilities():
    with pytest.raises(ValueError, match="prompt must not be empty"):
        sfx.validate_request(_request("heartmula_f16", prompt=" "))
    with pytest.raises(ValueError, match="at most 8000"):
        sfx.validate_request(_request("heartmula_f16", prompt="p" * 8001))
    with pytest.raises(ValueError, match="requires lyrics"):
        sfx.validate_request({"model_id": "minimax_music3_q8_0", "prompt": "song", "seed": 1})
    with pytest.raises(ValueError, match="does not support a separate lyrics"):
        sfx.validate_request(
            _request("stable_audio_3_small_music_q8_0", lyrics="not a lyrics field")
        )
    with pytest.raises(ValueError, match="at most 16000"):
        sfx.validate_request(_request("ace_step_turbo_q8_0", lyrics="l" * 16001))

    assert sfx.seed_maximum("controlfoley_large_44k_q8_0") == 2**31 - 1
    assert sfx.seed_maximum("midashenglm_gen_q8_0") == 2**31 - 1
    assert sfx.seed_maximum("stable_audio_3_small_music_q8_0") == 2**32 - 1
    assert sfx.seed_maximum("yue2_main_bf16") == 2**32 - 1
    assert sfx.seed_maximum("minimax_h3_q4_k") == 2**32 - 1
    with pytest.raises(ValueError, match="seed must be between"):
        sfx.validate_request(_request("controlfoley_large_44k_q8_0", seed=2**31))


def test_control_and_mida_emit_seed_request_options_equal_to_generic_seed():
    for model_id in (
        "controlfoley_large_44k_q8_0",
        "midashenglm_gen_q8_0",
        "minimax_music3_q8_0",
    ):
        argv = _argv_for(model_id, seed=1234)
        assert argv[argv.index("--seed") + 1] == "1234"
        assert _option_values(argv)["seed"] == ["1234"]


def test_mida_tags_prompt_and_preserves_already_tagged_prompt():
    argv = _argv_for("midashenglm_gen_q8_0", kind="music")
    assert argv[argv.index("--text") + 1] == "<|caption|><|music|> A bright piano melody"
    request = sfx.validate_request(
        _request("midashenglm_gen_q8_0", prompt="<|caption|><|sfx|>Wind")
    )
    argv = payload.build_argv("audiocpp_cli", request, "/model", "/out.wav")
    assert argv[argv.index("--text") + 1] == "<|caption|><|sfx|>Wind"


def test_yue2_compiles_instrumental_and_lyric_routes_without_generic_text():
    instrumental = sfx.validate_request(
        _request("yue2_main_q8_0", lyrics="", duration_seconds=8.01)
    )
    assert instrumental["parameters"]["num_inference_steps"] == 8
    argv = payload.build_argv("audiocpp_cli", instrumental, "/cache/yue2", "/out.wav")
    assert "--text" not in argv
    assert "--lyrics" not in argv
    options = _option_values(argv)
    assert options["style"] == ["A bright piano melody"]
    assert options["steps"] == ["8"]
    assert options["semantic_max_tokens"] == ["201"]
    assert options["semantic_min_tokens"] == ["200"]
    assert "duration_seconds" not in options
    session_options = [
        argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--session-option"
    ]
    assert "yue2.model_gguf=yue2-3b-q8_0.gguf" in session_options
    assert "yue2.vae_gguf=yue2-vae-f16.gguf" in session_options

    vocal = sfx.validate_request(_request("yue2_main_bf16", lyrics="A separate vocal line"))
    vocal_argv = payload.build_argv("audiocpp_cli", vocal, "/cache/yue2", "/out.wav")
    assert vocal_argv[vocal_argv.index("--lyrics") + 1] == "A separate vocal line"
    assert "--text" not in vocal_argv
    assert _option_values(vocal_argv)["style"] == ["A bright piano melody"]


@pytest.mark.parametrize("backend", ("vulkan", "cuda"))
def test_default_sfx_modern_requests_keep_gpu_support_while_legacy_is_cpu_only(backend):
    request = _request(registry.DEFAULT_MODEL_ID, kind="sound", backend=backend)
    assert sfx.validate_request(request)["backend"] == backend
    for field in ("kind", "lyrics", "parameters"):
        request.pop(field, None)
    with pytest.raises(ValueError, match="legacy requests support only the cpu"):
        sfx.validate_request(request)


@pytest.mark.parametrize(
    ("duration", "frames"),
    ((4, 107), (5, 124), (10, 243)),
)
def test_minimax_h3_uses_exact_dit_rounds_frame_budget_and_never_requests_video(duration, frames):
    model_id = "minimax_h3_q4_k_int8_dit" if duration == 4 else "minimax_h3_q4_k"
    argv = _argv_for(model_id, duration_seconds=duration, kind="sound")
    model = registry.get_model(model_id)
    assert argv[argv.index("--model") + 1].endswith(model.model_path_asset.path)
    assert argv[argv.index("--backend") + 1] == model.backends[0]
    assert "--text" in argv
    assert "--duration-seconds" not in argv
    options = _option_values(argv)
    assert options["num_frames"] == [str(frames)]
    assert options["return_video"] == ["false"]
    assert options["steps"] == ["50"]
    assert options["flow_shift"] == ["12"]
    assert options["audio_flow_shift"] == ["3"]
    assert "video" not in options


def test_minimax_h3_audio_flow_shift_is_lowered_once_and_old_name_is_rejected():
    argv = _argv_for("minimax_h3_q4_k", parameters={"audio_flow_shift": 5})
    options = _option_values(argv)
    assert options["audio_flow_shift"] == ["5"]
    assert "flow_shift_temp" not in options

    with pytest.raises(ValueError, match="Unknown parameter.*flow_shift_temp"):
        sfx.validate_request(_request("minimax_h3_q4_k", parameters={"flow_shift_temp": 5}))


@pytest.mark.parametrize(
    "name",
    ("temperature", "guidance_scale", "codec_duration_sec", "codec_guidance_scale"),
)
def test_heartmula_positive_parameters_accept_tiny_positive_values_but_reject_zero(name):
    tiny_positive = 1e-300
    normalized = sfx.validate_request(
        _request("heartmula_f16", parameters={name: tiny_positive})
    )
    assert normalized["parameters"][name] == tiny_positive

    with pytest.raises(ValueError, match=rf"parameters\.{name} must be greater than zero"):
        sfx.validate_request(_request("heartmula_f16", parameters={name: 0}))


@pytest.mark.parametrize(
    ("model_id", "parameter"),
    (
        ("yue2_main_q8_0", {"model_gguf": "client.gguf"}),
        ("minimax_h3_q4_k", {"return_video": True}),
        ("minimax_h3_q4_k", {"flow_shift_temp": 5}),
    ),
)
def test_yue2_and_h3_do_not_accept_unmanaged_or_unsupported_controls(model_id, parameter):
    with pytest.raises(ValueError, match="Unknown parameter"):
        sfx.validate_request(_request(model_id, parameters=parameter))
