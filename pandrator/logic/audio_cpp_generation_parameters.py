"""Model-specific audio generation controls and researched setting help.

Reviewed against audio.cpp v0.9.1 and upstream owner guidance on 11 October 2026.
See docs/reference/2026-10-11-audio-generation-settings.md for evidence,
recommendations and differences between native defaults and app defaults.
"""

from __future__ import annotations

from typing import Any


def _parameter(
    type_name: str,
    label: str,
    *,
    description: str | None = None,
    default: Any = ...,
    minimum: int | float | None = None,
    maximum: int | float | None = None,
    exclusive_minimum: int | float | None = None,
    options: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    descriptor: dict[str, Any] = {"type": type_name, "label": label}
    if description is not None:
        descriptor["description"] = description
    if default is not ...:
        descriptor["default"] = default
    if minimum is not None:
        descriptor["minimum"] = minimum
    if maximum is not None:
        descriptor["maximum"] = maximum
    if exclusive_minimum is not None:
        descriptor["exclusive_minimum"] = exclusive_minimum
    if options is not None:
        descriptor["options"] = list(options)
    return descriptor


_PARAMETER_HELP: dict[str, dict[str, str]] = {
    "stable_audio": {
        "num_inference_steps": (
            "Sampling steps trade computation for refinement. Start at 8 for these "
            "post-trained checkpoints; fewer steps can reduce quality, and more than 8 "
            "does not necessarily improve it."
        ),
        "guidance_scale": (
            "Controls how strongly sampling follows the description. It is documented "
            "for Stable Audio 3 Base checkpoints. "
            "The Small and Medium checkpoints here are post-trained; keep 1.0. "
            "audio.cpp accepts this setting, but supported benefit here is not established."
        ),
        "negative_prompt": (
            "Describes qualities to avoid through guidance. Stability documents this "
            "for Base checkpoints; the post-trained Small and Medium models here "
            "have no established useful effect. Leave it blank; audio.cpp still "
            "accepts and forwards this setting."
        ),
        "sampler": (
            "Selects the Stable Audio 3 sampling algorithm: pingpong or Euler. "
            "Leave unset for the runtime default; no universal quality winner is established."
        ),
    },
    "controlfoley": {
        "num_inference_steps": (
            "Euler flow denoising steps. Start at the documented 25; lowering it "
            "trades refinement for speed."
        ),
        "guidance_scale": (
            "Strength of classifier-free prompt guidance. Start at the documented "
            "4.5; increasing it is an experiment, not a guaranteed quality improvement."
        ),
        "negative_prompt": (
            "Optional qualities or sounds to discourage through the unconditional "
            "guidance branch. Start blank unless there is something specific to reduce."
        ),
    },
    "midashenglm_gen": {
        "guidance_scale": (
            "Classifier-free guidance strength; 1 disables guidance. Start at the documented 2.0."
        ),
        "stop_threshold": (
            "Stop-head probability threshold for ending variable-length generation. "
            "Start at 0.5; change it only when investigating clips that stop too early or late."
        ),
        "min_stop_step": (
            "Minimum generated steps before a predicted stop is accepted. "
            "Start at the documented 5; this is not a duration in seconds."
        ),
    },
    "ace_step": {
        "num_inference_steps": (
            "DiT denoising steps. Turbo uses 8; upstream recommends 32–64 for "
            "Base/SFT, where Pandrator starts at 32."
        ),
        "guidance_scale": (
            "DiT prompt guidance for Base/SFT. Upstream recommends 5–9 and defaults "
            "to 7, which Pandrator uses; the generic audio.cpp CLI defaults to 1. "
            "Turbo ignores this control and does not expose it here."
        ),
        "bpm": (
            "Tempo in beats per minute. Leave blank for automatic planning; "
            "the upstream documented range is 30–300."
        ),
        "keyscale": (
            "Optional musical key and scale, such as C Major, Am or F# minor. "
            "Leave blank for automatic selection."
        ),
        "timesignature": (
            "Optional meter value: 2, 3, 4 or 6 represents 2/4, 3/4, 4/4 or 6/8. "
            "Leave blank for automatic selection."
        ),
        "language": (
            "Vocal language hint, sent to the native language field. audio.cpp "
            "defaults to English; set the language used by your lyrics."
        ),
        "negative_prompt": (
            "Optional text to exclude from the music. Keep the native NO USER INPUT "
            "baseline unless there is something specific to discourage."
        ),
        "lm_temperature": (
            "Sampling temperature for the music planner language model. "
            "Start at the native 0.85 baseline."
        ),
        "lm_cfg_scale": (
            "Guidance for the planner language model, separate from DiT audio "
            "guidance. Start at the native 2.0 baseline."
        ),
        "lm_top_k": (
            "Planner sampling keeps this many likely next tokens. "
            "The native baseline 0 disables top-k filtering."
        ),
        "lm_top_p": (
            "Planner nucleus sampling retains a probability mass of likely "
            "next tokens. Start at the native 0.9 baseline."
        ),
        "lm_repetition_penalty": (
            "Planner token repetition penalty. The native baseline 1.0 applies "
            "no additional repetition penalty."
        ),
        "sampler_mode": (
            "DiT sampling algorithm, Euler or Heun. Leave unset for native Euler; "
            "no universal quality winner is established."
        ),
    },
    "heartmula": {
        "tags": (
            "Comma-separated genre, mood, instrument and vocal-style tags, such as "
            "piano, happy, acoustic. Native documentation asks for tags; add a short "
            "set rather than relying on an empty value."
        ),
        "temperature": (
            "Music-token sampling temperature. Start at 1.0; it must be greater than zero."
        ),
        "top_k": "Music-token top-k sampling limit. Start at the native 50 baseline.",
        "guidance_scale": (
            "MuLa music-token guidance. Start at 1.5; it must be greater than zero."
        ),
        "codec_duration_sec": (
            "Codec decode chunk length, separate from the requested song duration. "
            "Keep 29.76 seconds unless investigating chunking; it must be positive."
        ),
        "num_inference_steps": "Codec flow-solver steps. Start at the native 10 baseline.",
        "codec_guidance_scale": (
            "Codec-stage guidance, separate from MuLa token guidance. "
            "Start at 1.25; it must be greater than zero."
        ),
    },
    "minimax_music3": {
        "num_inference_steps": (
            "Euler flow denoising steps per chunk. Start at the native 30; "
            "fewer steps reduce computation and can reduce refinement."
        ),
        "guidance_scale": ("Flow-transformer guidance. Start at the native 1.7; 0 disables CFG."),
        "ar_guidance_scale": (
            "Autoregressive semantic/depth guidance, separate from flow guidance. "
            "Start at the native 1.5; 0 disables CFG."
        ),
        "top_k": (
            "Top-k limit for semantic and residual-code sampling. Start at the native 50 baseline."
        ),
    },
    "yue2": {
        "num_inference_steps": (
            "Acoustic-flow ODE steps in the non-autoregressive stage. "
            "Start at the native 8; more steps increase computation."
        ),
        "guidance_scale": (
            "Semantic classifier-free guidance. Pandrator starts at 1.0 for full "
            "planning; the native off route uses 1.01 as its reference default."
        ),
        "cot": (
            "Symbolic score planning: off skips ABC planning; melody or full uses "
            "the score-planning routes. Pandrator defaults to full, while the "
            "native quickstart demonstrates off."
        ),
        "semantic_temperature": (
            "Semantic-code sampling temperature. Start at the native 1.0 baseline."
        ),
        "semantic_top_p": (
            "Semantic-code nucleus-sampling probability mass. Start at the native 0.95 baseline."
        ),
        "semantic_top_k": ("Semantic-code top-k sampling limit. Start at the native 100 baseline."),
        "semantic_repetition_penalty": (
            "Penalty on repeated semantic codec tokens. Start at the native 1.2 baseline."
        ),
    },
    "minimax_h3": {
        "num_inference_steps": (
            "Joint DiT denoising steps. Start at the documented 50; fewer steps "
            "are faster, and no universal quality optimum has been established."
        ),
        "guidance_scale": (
            "Joint DiT classifier-free guidance. Start at the documented 1.0 baseline."
        ),
        "flow_shift": (
            "Video FlowMatch noise-schedule shift in H3’s joint model. "
            "Keep the documented 12; audio-only output still computes joint latents."
        ),
        "audio_flow_shift": (
            "Audio FlowMatch noise-schedule shift. Start at the documented 3; "
            "treat changes as experimental tuning rather than a quality guarantee."
        ),
    },
}


def parameters_for_model(package_id: str, family: str) -> tuple[tuple[str, dict[str, Any]], ...]:
    common: dict[str, dict[str, Any]] = {}
    if family == "stable_audio":
        common = {
            "num_inference_steps": _parameter("integer", "Inference steps", default=8, minimum=1),
            "guidance_scale": _parameter("number", "Guidance scale", default=1.0),
            "negative_prompt": _parameter("string", "Negative prompt"),
            "sampler": _parameter("string", "Sampler", options=("pingpong", "euler")),
        }
    elif family == "controlfoley":
        common = {
            "num_inference_steps": _parameter(
                "integer", "Inference steps", default=25, minimum=1, maximum=200
            ),
            "guidance_scale": _parameter(
                "number", "Guidance scale", default=4.5, minimum=0, maximum=20
            ),
            "negative_prompt": _parameter("string", "Negative prompt", default=""),
        }
    elif family == "midashenglm_gen":
        common = {
            "guidance_scale": _parameter(
                "number", "Guidance scale", default=2.0, minimum=0, maximum=20
            ),
            "stop_threshold": _parameter(
                "number", "Stop threshold", default=0.5, minimum=0, maximum=1
            ),
            "min_stop_step": _parameter(
                "integer", "Minimum stop step", default=5, minimum=0, maximum=4096
            ),
        }
    elif family == "ace_step":
        common = {
            "num_inference_steps": _parameter(
                "integer", "Inference steps", default=8 if "turbo" in package_id else 32, minimum=1
            ),
            "bpm": _parameter("integer", "Tempo"),
            "keyscale": _parameter("string", "Key and scale"),
            "timesignature": _parameter("string", "Time signature"),
            "language": _parameter("string", "Vocal language", default="en"),
            "negative_prompt": _parameter("string", "Negative prompt", default="NO USER INPUT"),
            "lm_temperature": _parameter("number", "Language model temperature", default=0.85),
            "lm_cfg_scale": _parameter("number", "Language model guidance scale", default=2.0),
            "lm_top_k": _parameter("integer", "Language model top K", default=0, minimum=0),
            "lm_top_p": _parameter("number", "Language model top P", default=0.9),
            "lm_repetition_penalty": _parameter(
                "number", "Language model repetition penalty", default=1.0
            ),
            "sampler_mode": _parameter("string", "Sampler mode", options=("euler", "heun")),
        }
        # Turbo DiT routes ignore CFG. Do not advertise a control the runtime
        # will silently disregard for these package variants.
        if "turbo" not in package_id:
            common["guidance_scale"] = _parameter("number", "Guidance scale", default=7.0)
    elif family == "heartmula":
        common = {
            "tags": _parameter("string", "Style tags"),
            "temperature": _parameter("number", "Temperature", default=1.0, exclusive_minimum=0),
            "top_k": _parameter("integer", "Top K", default=50, minimum=1),
            "guidance_scale": _parameter(
                "number", "Guidance scale", default=1.5, exclusive_minimum=0
            ),
            "codec_duration_sec": _parameter(
                "number", "Codec duration", default=29.76, exclusive_minimum=0
            ),
            "num_inference_steps": _parameter("integer", "Inference steps", default=10, minimum=1),
            "codec_guidance_scale": _parameter(
                "number", "Codec guidance scale", default=1.25, exclusive_minimum=0
            ),
        }
    elif family == "minimax_music3":
        common = {
            "num_inference_steps": _parameter("integer", "Inference steps", default=30, minimum=1),
            "guidance_scale": _parameter("number", "Guidance scale", default=1.7, minimum=0),
            "ar_guidance_scale": _parameter(
                "number", "Autoregressive guidance scale", default=1.5, minimum=0
            ),
            "top_k": _parameter("integer", "Top K", default=50, minimum=1),
        }
    elif family == "yue2":
        common = {
            "num_inference_steps": _parameter(
                "integer", "Inference steps", default=8, minimum=1, maximum=100
            ),
            "guidance_scale": _parameter(
                "number", "Guidance scale", default=1.0, minimum=0, maximum=20
            ),
            "cot": _parameter(
                "string",
                "Chain-of-thought mode",
                default="full",
                options=("off", "melody", "full"),
            ),
            "semantic_temperature": _parameter(
                "number", "Semantic temperature", default=1.0, minimum=0, maximum=5
            ),
            "semantic_top_p": _parameter(
                "number", "Semantic top P", default=0.95, minimum=0, maximum=1
            ),
            "semantic_top_k": _parameter(
                "integer", "Semantic top K", default=100, minimum=1, maximum=1000
            ),
            "semantic_repetition_penalty": _parameter(
                "number",
                "Semantic repetition penalty",
                default=1.2,
                minimum=0.001,
                maximum=10,
            ),
        }
    elif family == "minimax_h3":
        common = {
            "num_inference_steps": _parameter(
                "integer", "Inference steps", default=50, minimum=1, maximum=200
            ),
            "guidance_scale": _parameter(
                "number", "Guidance scale", default=1.0, minimum=0, maximum=20
            ),
            "flow_shift": _parameter(
                "number", "Flow shift", default=12.0, minimum=0.001, maximum=100
            ),
            "audio_flow_shift": _parameter(
                "number",
                "Audio scheduler shift",
                default=3.0,
                minimum=0.001,
                maximum=100,
            ),
        }
    for name, descriptor in common.items():
        descriptor["description"] = _PARAMETER_HELP[family][name]
    return tuple((name, value) for name, value in common.items())
