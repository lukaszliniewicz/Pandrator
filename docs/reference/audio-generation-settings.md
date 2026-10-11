# Advanced sound and music settings

Advanced settings let you adjust how the selected model generates a clip. Start
with its defaults, change one setting at a time, and compare short examples.
More inference steps or stronger guidance do not necessarily improve the result.

The values below describe Pandrator's audio.cpp v0.9.1 integration. The model
catalogue and the controls shown in the playground provide the current defaults,
choices, and allowed ranges. Unsupported parameters are rejected before a job
starts. Quantizations of the same checkpoint expose the same controls.

See [choosing a model](sound-and-music-models.md) for durations and required inputs.

## Stable Audio 3 Small and Medium

All six integrated variants are **post-trained checkpoints**. Stability documents
guidance and negative prompting for Base checkpoints only. audio.cpp still
accepts these fields; this does not establish useful effects on the integrated
checkpoints. Keep guidance at 1.0 and negative prompt blank. The UI leaves them
editable and explains the caveat. [Vendor inference guidance](https://github.com/Stability-AI/stable-audio-3/blob/main/docs/workflows/inference.md),
[pinned native guide](https://github.com/0xShug0/audio.cpp/blob/cf124a67cc55d8f65a9a15eec69edbff0fb212c8/docs/models/stable_audio.md),
[request parser](https://github.com/0xShug0/audio.cpp/blob/cf124a67cc55d8f65a9a15eec69edbff0fb212c8/src/models/stable_audio/request.cpp).

| Control | Meaning and starting point |
| --- | --- |
| `num_inference_steps` | Sampling refinement/computation tradeoff. **8**; fewer steps can degrade output, while more need not improve a post-trained checkpoint. Positive integer. |
| `guidance_scale` | Strength of description conditioning. **1.0**, with the Base-only caveat above. |
| `negative_prompt` | Qualities to discourage through guidance. **Blank**, with the same caveat. |
| `sampler` | `pingpong` or `euler`. Unset uses the runtime default; no universal winner established. |

Older Small SFX requests that omit kind, lyrics, and parameters use CPU with
eight inference steps and guidance 1.0. Playground requests use the settings
for the selected model.

## ControlFoley Large

These are documented native T2A starting points, not a text-only quality
conclusion drawn from video-conditioned evaluations.
[Pinned native guide](https://github.com/0xShug0/audio.cpp/blob/cf124a67cc55d8f65a9a15eec69edbff0fb212c8/docs/models/controlfoley.md).

| Control | Meaning and starting point |
| --- | --- |
| `num_inference_steps` | Euler flow denoising iterations. **25**, app **1–200**; fewer steps reduce computation and potentially refinement. |
| `guidance_scale` | Classifier-free prompt guidance. **4.5**, app **0–20**. Higher is not guaranteed to be better. |
| `negative_prompt` | Qualities/sounds to discourage through the unconditional branch. Start **blank**. |

## MiDashengLM Gen

Stop controls count steps, not seconds. Keep the baseline unless investigating
premature or late stopping.
[Pinned native guide](https://github.com/0xShug0/audio.cpp/blob/cf124a67cc55d8f65a9a15eec69edbff0fb212c8/docs/models/midashenglm_gen.md).

| Control | Meaning and starting point |
| --- | --- |
| `guidance_scale` | Classifier-free guidance, **2.0**, app **0–20**; **1** disables it. |
| `stop_threshold` | Stop-head probability threshold. **0.5**, **0–1**. |
| `min_stop_step` | Earliest step that permits a predicted stop. **5**, app **0–4096**. |

## ACE-Step 1.5 and XL

Base/SFT and Turbo need different DiT defaults. You can adjust the inference steps for the selected checkpoint. Base and XL SFT start at **32 steps/guidance 7.0**, matching
upstream starting guidance; Turbo/XL Turbo keep **8 steps** and omit ignored
DiT guidance. The generic native CLI defaults guidance to 1.0, distinct from
the app's Base/SFT choice. [Upstream inference guide](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/INFERENCE.md),
[pinned native guide](https://github.com/0xShug0/audio.cpp/blob/cf124a67cc55d8f65a9a15eec69edbff0fb212c8/docs/models/ace_step.md).

| Control | Meaning and starting point |
| --- | --- |
| `num_inference_steps` | DiT denoising. **8 Turbo; 32 Base/SFT**; upstream suggests **32–64** for Base/SFT. Positive integer. |
| `guidance_scale` | Base/SFT conditioning. **7.0**, upstream suggests **5–9**. Absent for Turbo. |
| `bpm` | Tempo; unset for automatic planning. Upstream documents **30–300**, without a new app bound. |
| `keyscale` | Optional key/scale, e.g. `C Major`, `Am`, `F# minor`. Unset for automatic choice. |
| `timesignature` | Optional `2`, `3`, `4`, `6` for 2/4, 3/4, 4/4, 6/8. Unset for automatic choice. |
| `language` | Native vocal language field; default **`en`**. Match the lyrics. |
| `negative_prompt` | Content to discourage; native baseline **`NO USER INPUT`**. |
| `lm_temperature` | Planner language-model sampling temperature. **0.85**. |
| `lm_cfg_scale` | Planner conditioning, separate from DiT guidance. **2.0**. |
| `lm_top_k` | Planner token filtering. **0 disables** top-k; non-negative integer. |
| `lm_top_p` | Planner nucleus-sampling probability mass. **0.9**. |
| `lm_repetition_penalty` | Planner token repetition penalty. **1.0**, no added penalty. |
| `sampler_mode` | DiT `euler` or `heun`; unset uses native **Euler**. |

## HeartMuLa

Token and codec stages have separate guidance. Codec duration is an **internal
decode chunk**, not requested song length. Controls that require a positive value reject zero. [Pinned native guide](https://github.com/0xShug0/audio.cpp/blob/cf124a67cc55d8f65a9a15eec69edbff0fb212c8/docs/music_generation.md),
[native specification](https://github.com/0xShug0/audio.cpp/blob/cf124a67cc55d8f65a9a15eec69edbff0fb212c8/model_specs/heartmula.json),
[owner repository](https://github.com/dapi4/heartmula).

| Control | Meaning and starting point |
| --- | --- |
| `tags` | Comma-separated genre, mood, instrument and vocal-style hints. Prose asks for tags while schema permits omission; supply a short relevant set. |
| `temperature` | Music-token sampling temperature. **1.0**, strictly positive. |
| `top_k` | Music-token top-k filter. **50**, positive integer. |
| `guidance_scale` | MuLa token conditioning. **1.5**, strictly positive. |
| `codec_duration_sec` | Decode chunk length. **29.76 s**, strictly positive; keep baseline unless investigating chunking. |
| `num_inference_steps` | Codec flow-solver iterations. **10**, positive integer. |
| `codec_guidance_scale` | Codec-stage conditioning. **1.25**, strictly positive. |

## MiniMax Music 3

Flow and autoregressive guidance are separate. Supply description and lyrics
separately; the existing lyrics requirement remains.
[Pinned native guide](https://github.com/0xShug0/audio.cpp/blob/cf124a67cc55d8f65a9a15eec69edbff0fb212c8/docs/community_models/minimax_music3.md),
[owner repository](https://github.com/MiniMax-AI/MiniMax-Music3).

| Control | Meaning and starting point |
| --- | --- |
| `num_inference_steps` | Euler flow denoising per chunk. **30**, positive integer. |
| `guidance_scale` | Flow-transformer conditioning. **1.7**; **0 disables** CFG. Non-negative. |
| `ar_guidance_scale` | Semantic/depth conditioning. **1.5**; **0 disables** CFG. Non-negative. |
| `top_k` | Semantic/residual-code filtering. **50**, positive integer. |

## YuE2

Semantic sampling and acoustic-flow sampling are separate. The app retains
explicit full planning; the native quickstart's off route uses another reference
guidance value. Neither route is established as a universal quality winner here.
[Pinned native guide](https://github.com/0xShug0/audio.cpp/blob/cf124a67cc55d8f65a9a15eec69edbff0fb212c8/docs/models/yue2.md),
[native specification](https://github.com/0xShug0/audio.cpp/blob/cf124a67cc55d8f65a9a15eec69edbff0fb212c8/model_specs/yue2.json).

| Control | Meaning and starting point |
| --- | --- |
| `num_inference_steps` | Acoustic-flow ODE iterations. **8**, app **1–100**. |
| `guidance_scale` | Semantic conditioning. App **1.0** for full planning; off reference **1.01**. App **0–20**. |
| `cot` | ABC score planning. **`full`** app default; `off` skips it; `melody` uses melody planning. Quickstart uses `off`. |
| `semantic_temperature` | Semantic-code temperature. **1.0**, app **0–5**. |
| `semantic_top_p` | Semantic nucleus-sampling probability mass. **0.95**, app **0–1**. |
| `semantic_top_k` | Semantic-code top-k filter. **100**, app **1–1000**. |
| `semantic_repetition_penalty` | Penalty on repeated semantic codec tokens. **1.2**, app **0.001–10**. |

## MiniMax-H3

H3 has separate video/audio FlowMatch scheduler shifts; audio-only export still
computes joint latents. Use `audio_flow_shift` to adjust the audio schedule. H3 remains experimental;
start with the documented defaults and review the model licence before use.
[Request parser](https://github.com/0xShug0/audio.cpp/blob/cf124a67cc55d8f65a9a15eec69edbff0fb212c8/src/community_models/minimax_h3/session.cpp),
[type defaults](https://github.com/0xShug0/audio.cpp/blob/cf124a67cc55d8f65a9a15eec69edbff0fb212c8/include/engine/community_models/minimax_h3/types.h),
[scheduler](https://github.com/0xShug0/audio.cpp/blob/cf124a67cc55d8f65a9a15eec69edbff0fb212c8/src/community_models/minimax_h3/pipeline.cpp).

| Control | Meaning and starting point |
| --- | --- |
| `num_inference_steps` | Joint DiT denoising. **50**, app **1–200**. |
| `guidance_scale` | Joint conditioning. **1.0**, app **0–20**. |
| `flow_shift` | Video noise-schedule shift. **12.0**, app **0.001–100**. |
| `audio_flow_shift` | Audio noise-schedule shift. **3.0**, app **0.001–100**. |

## Shared controls and batches

A blank seed requests a new alternative. Pandrator records the chosen seed;
retrying the same request with its idempotency key returns that candidate. A batch
of **1–8** candidates increments that base seed, wrapping at the model maximum.
Copying recorded settings/seed is explicit; opening a candidate does not pin
future generation. CPU threads default to the smaller of four and the logical
CPU count; the native validator rejects greater-than-available counts. More
threads do not guarantee faster generation.
[Native CLI controls](https://github.com/0xShug0/audio.cpp/blob/cf124a67cc55d8f65a9a15eec69edbff0fb212c8/docs/usage.md).

Candidate count is a Pandrator setting. Each candidate runs as a separate
generation job on the shared audio.cpp service. See
[generation requests](audio-generation-models.md) for seeds and batches.
