# Providers, local models, and voices

Pandrator separates providers, models, voices, and generated takes. A provider
is a configured service; a model is one of its processing choices; a voice is a
reusable identity or reference; and a take is generated audio for one segment.
Keeping those concepts separate makes it possible to change a provider without
rewriting source text or silently replacing selected audio.

## Local or cloud

| Provider type | Typical data it receives | Main trade-off |
| --- | --- | --- |
| Local transcription | Source audio or a processing copy | Uses local compute and storage; media stays on the host. |
| Local TTS | Speech text and local voice references | Uses local compute; larger models may need substantial RAM or GPU memory. |
| Local LLM server | Text selected for cleanup, correction, translation, or optimization | Privacy depends on the server actually being local and its own configuration. |
| Cloud LLM or translation | Submitted text, instructions, glossary, and request metadata | Convenient and often capable, but text leaves the Pandrator host and may incur cost. |
| Cloud TTS | Speech text, voice identifier, settings, and sometimes reference audio | Fast access to hosted voices; content and voice material leave the host. |

Provider readiness shows whether a service is reachable and whether a
credential is configured. Routine status and agent tools do not return the
credential value.

## Local component chooser

The Manager shows available compute variants, model licences, and downloads
before changing the installation.

**audio.cpp** is the main local TTS provider and the default in a fresh
workspace. Its model packages cover Qwen3 TTS, Fish Audio S2 Pro, VoxCPM2,
Chatterbox, MagpieTTS, OmniVoice, PocketTTS, FireRedTTS3, and BreezeTTS 2.
Select packages in the Manager and select an installed model in generation
settings. XTTS, Silero, Kokoro, and Voxtral retain their dedicated providers.

| Component | Best suited to | Typical compute |
| --- | --- | --- |
| audio.cpp | Multiple speech model families in one runtime | CPU, Vulkan, or CUDA; model requirements vary |
| Kokoro 82M | Lightweight built-in voices | CPU, CUDA, and supported modern AMD GPUs |
| XTTS v2 | Multilingual voice cloning and fine-tuned models | CPU or CUDA |
| Voxtral 4B | Preset voices | WGPU-compatible accelerator |
| Silero | Efficient regional language packs | CPU |
| CrispASR | Transcription, timestamps, and diarization | CPU, CUDA, Vulkan, or Apple Silicon |
| RVC | Speech-to-speech voice conversion | CPU or CUDA |

Hardware needs vary with model size, quantization, input length, and compute
backend. Start with the smallest model that meets the task instead of
installing the entire catalogue and hoping your SSD develops a sense of purpose.
The Linux audio.cpp CUDA build remains best-effort without NVIDIA hardware
verification.

## Switching from a compatibility provider

Standalone Qwen3 TTS, Fish S2 Pro, VoxCPM2, Chatterbox, and Magpie are
compatibility providers. Existing installations remain manageable and saved
sessions keep their provider, model, voice, and takes. An upgrade also preserves
an existing workspace's inherited TTS default. Uninstalled compatibility
components are grouped under **Compatibility backends**; ordinary speech
pickers show a compatibility provider when it is the saved selection.

To move a session, start audio.cpp with the desired model package installed,
open **Generate audio** settings, and choose **Switch to audio.cpp**. Pandrator
proposes an installed model from the matching family. Review its model and
voice, preview a representative sample, then save. If the family is not
installed, choose and install the package first.

A voice is reused only if audio.cpp already advertises that native voice or
the managed voice has a ready audio.cpp reference link. Otherwise, choose or
link the reference in the Voice Library. Old provider options and reference
settings are cleared on the switch; model IDs, quantizations, and voice uploads
are not treated as equivalent. The original component and generated takes
remain available. Changing a global or generic session service setting also
clears stale selections, so choose a valid target model and voice before
starting generation.

## Language and capability checks

Language support differs by model and sometimes by voice. Pandrator filters
choices using capabilities reported by an installed service, but that cannot
guarantee pronunciation quality for every language pair or cloned voice.

Before a large run:

1. confirm the service is ready;
2. confirm model and compute compatibility;
3. confirm the requested language is reported;
4. preview the voice or generate a short representative sample; and
5. test difficult names, numbers, abbreviations, and language changes.

CrispASR offers Whisper for broad transcription coverage and other engines for
different speed, timestamp, and diarization trade-offs. A known language is
usually safer than automatic detection for long, consistent media.

## Voice references

A voice reference should contain one speaker, little background noise or room
echo, and an accurate transcript. Keep the source and rights information with
the voice. A technically good sample is not automatically lawful or ethical to
use; obtain consent and respect model/provider voice policies.

Pandrator keeps voice samples and transcripts as their own artifacts. The
voice catalogue exposes reusable identity and provider bindings without
returning raw samples to routine list operations.

## Fine-tuned XTTS bundles

An uploadable XTTS bundle is one flat directory containing exactly:

- `config.json`
- `model.pth`
- `speakers_xtts.pth`
- `vocab.json`

Update Pandrator and repair or update XTTS before importing if an older service
does not advertise model upload. In a session's **Generate audio** settings,
select XTTS, choose a new model ID, and upload all four files together. Nested
training directories, incomplete exports, and an existing model ID are
rejected. User models belong in managed user data, not a versioned service
source directory.

## Native and compatible cloud services

OpenAI, Google Gemini, native ElevenLabs, and compatible custom speech
endpoints can be configured where supported. Native ElevenLabs uses its own API
contract, not an OpenAI-compatible one. A third-party intermediary that exposes
an OpenAI-compatible speech API should be added as a custom provider instead.

Keep provider secrets in Pandrator's configured credential backend, an
owner-restricted secret file, or the deployment secret store. Never paste a
key into a prompt, MCP tool argument, target profile, log, or source document.

## RVC is a separate transformation

RVC converts generated speech into a trained target voice. It does not replace
the TTS stage. Pandrator retains the original generated take and the converted
take so you can compare them. Listen for consonant loss, pitch artifacts,
noise, and identity drift before selecting the converted result.

For how voices enter an audiobook or timed voiceover, continue with
[your first audiobook](../getting-started/first-audiobook.md) or
[your first voiceover](../getting-started/first-voiceover.md).
