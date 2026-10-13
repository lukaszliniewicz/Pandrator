# Sound and music generation requests

The **Sounds & Music** playground and the MCP sound tools use the same model
catalogue and generation settings. A generated clip becomes a candidate. You
can audition it, save it to the library, and then place its saved variant in a
project. Changing the narration voice does not change saved sounds.

## Choosing inputs

Select Sound effect or Music, choose a model, and describe the clip. Models
that support lyrics offer a separate lyrics field. MiniMax Music 3 requires
lyrics; other song models may accept them optionally. The catalogue also lists
the duration range, compute backends, and advanced controls for each model.
See [choosing a model](sound-and-music-models.md) for practical differences.

MCP and HTTP requests identify the model with `model_id` and use these fields:

| Field | Purpose |
| --- | --- |
| `kind` | Select `sound` or `music`. |
| `prompt` | Describe the effect or musical style. |
| `lyrics` | Supply words to sing when the model supports them. |
| `duration_seconds` | Choose a duration within the model's request range. Some models treat this as a maximum budget. |
| `seed` | Use a recorded seed or leave it blank for a new alternative. |
| `count` | Request between one and eight candidates. |
| `parameters` | Set model-specific controls listed in the catalogue. |
| `backend` | Select an available CPU, Vulkan, or CUDA backend. |
| `threads` | Set the CPU thread count within the available hardware limit. |

Unsupported settings are rejected before generation starts. Optional settings
use the selected model's defaults. The preview and saved candidate details show
the resolved settings, so you can see which values were used.

### YuE2 instrumental mode

Enable **Instrumental** on a YuE2 model to use the managed Mothersuperior AR
adapter. The first generation downloads and verifies a 140 MB BF16 adapter;
the same cached adapter is reused across the BF16, Q8, and Q4 base models.
The adapter uses full score planning and the `[instrumental]` lyrics marker.
Turning the option off restores the ordinary lyrics field.

For MCP or HTTP generation, set `parameters.instrumental` to `true`, omit
`lyrics` (or supply `[instrumental]`), and leave `cot` at `full`. Other lyrics
and planning modes are rejected while instrumental mode is active. The saved
request and runtime identity retain the mode, adapter revision, and checksum.
Ordinary YuE2 requests retain their existing behavior.

The [adapter](https://huggingface.co/Mothersuperior/YuE2-instrumental-cot-full-loras)
is licensed CC BY-NC 4.0 for noncommercial use. BF16 is closest to the original
base weights; Q8/Q4 merges are requantized and can produce different results.
Adapter loading and musical quality still require a short trial on your backend.

### Downloading clips

Use **Download** beside a ready candidate to save its audio without adding it
to the library. Open a saved clip to download any of its variants. Downloads
preserve the stored file and its format: generated clips are WAV, while imported
clips retain their original audio format.

## Seeds and alternatives

Leaving the seed blank chooses a random seed when the request is accepted.
Retrying a request with the same idempotency key returns the same candidates,
seeds, and jobs. Use a new key to request new alternatives. **Regenerate** chooses
a new seed unless you entered one explicitly.

A batch starts from one base seed and increments it for each candidate. Every
candidate has its own status and audio. Generation jobs share the audio.cpp
service and run according to its resource limits; a batch does not imply that
all candidates will synthesize at once.

Saved settings describe the request, model package, and runtime that produced
the clip. Reusing a seed helps repeat an experiment with the same setup; it does
not guarantee identical audio across different model packages or runtimes.
Opening an existing candidate does not automatically set its seed for the next
request. Copy its settings explicitly when you want to reuse them.

## Packages and availability

Pandrator downloads the selected model package and verifies its required files
against the catalogue's recorded revisions and checksums. Verified files already
managed by Pandrator can be reused. Downloads can be cancelled. Opening the
catalogue or checking status does not download model weights.

A listed model may still need installation or a compatible runtime. The model
chooser explains missing files or unavailable backends. H3's INT8 variant
requires CUDA and cannot run on a CPU or Vulkan build.

The standard CPU sound-generation path keeps the project voice selection and
resident speech model intact. Other workloads share the configured service and
hardware limits.

See [advanced settings](audio-generation-settings.md) for tuning controls and
[sounds and mixing](sounds-and-mixing.md) for placement, playback, and export.
