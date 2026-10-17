# Providers, local models, and voices

The **Audio models** catalogue offers language-name and code autocomplete from
its supported-language inventory. Speech model details show the default audiobook
segment target separately from provider input/output limits and configurable
runtime text-splitting defaults. Planning targets leave headroom for output
budgets; they are starting points for representative listening tests.

Fish's application chunk default is 300 characters (native audio.cpp: 200),
with a 270-character audiobook target. Whole-segment directions are recommended;
phrase cues are best effort. Qwen's output budget comes from the checkpoint,
with a 2,048-frame fallback.

In **Voices**, filter by origin to find designed voices, reference recordings or
built-in speakers. **Redesign** opens an existing designed voice with its current
brief and save target, so you can change the prompt and audition new candidates
before saving a sample.

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

Installation, service advertisement, health, and a prepared voice reference are
separate checks. A Manager-installed package may not be in a service's current
model roster. Unknown health is reported as unknown, not as a stopped service.
For audio.cpp, a required reference belongs to the selected model's request;
Pocket's optional reference and hybrid voice mode do not mean cloning is always
required. Keep standalone `kokoro` separate from audio.cpp `kokoro_82m_*` package
IDs when comparing auditions and project settings.

The model chooser shows dated provider deprecation information without changing
saved settings. See [provider model lifecycle](../reference/provider-lifecycle.md)
before moving a project to another API model.

## Local component chooser

The Manager shows available compute variants, model licences, and downloads
before changing the installation.

**audio.cpp** is the main local TTS provider and the default in a fresh
workspace. Its model packages cover Qwen3 TTS, Fish Audio S2 Pro, VoxCPM2,
Chatterbox, MagpieTTS, OmniVoice, PocketTTS, Voxtral TTS, FireRedTTS3, and BreezeTTS 2.
Select packages in the Manager and select an installed model in generation
settings. XTTS, Silero, Kokoro, and Voxtral retain their dedicated providers.

| Component | Best suited to | Typical compute |
| --- | --- | --- |
| audio.cpp | Multiple speech model families in one runtime | CPU, Vulkan, or CUDA; model requirements vary |
| Kokoro 82M | Lightweight built-in voices | CPU, CUDA, and supported modern AMD GPUs |
| XTTS v2 | Multilingual voice cloning and fine-tuned models | CPU or CUDA |
| Voxtral 4B | Preset voices | audio.cpp, or the dedicated WGPU backend |
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
projects keep their provider, model, voice, and takes. An upgrade also preserves
an existing workspace's inherited TTS default. Uninstalled compatibility
components are grouped under **Compatibility backends**; ordinary speech
pickers show a compatibility provider when it is the saved selection.

To move a project, start audio.cpp with the desired model package installed,
open **Generate audio** settings, and choose **Switch to audio.cpp**. Pandrator
proposes an installed model from the matching family. Review its model and
voice, preview a representative sample, then save. If the family is not
installed, choose and install the package first.

A voice is reused only if audio.cpp already advertises that native voice or
the managed voice has a ready audio.cpp reference link. Otherwise, choose or
link the reference in the Voice Library. Old provider options and reference
settings are cleared on the switch; model IDs, quantizations, and voice uploads
are not treated as equivalent. The original component and generated takes
remain available. Changing a global or generic project service setting also
clears stale selections, so choose a valid target model and voice before
starting generation.

## audio.cpp runtime versions

The Manager pins audio.cpp **0.9.2** for Linux CPU/Vulkan/CUDA and Windows
CPU/Vulkan/CUDA, with SHA-256-verified upstream archives. Linux CPU and Vulkan
use the portable builds. Windows CUDA uses the 12.4 binary plus its bundled
cudart runtime archive. Linux CUDA uses the upstream
`audio-v0.9.2-bin-ubuntu-x64-cuda12.8-colab.tar.gz` binary best-effort: the
colab tag means it was built for Colab (CUDA 12.8), it ships without a bundled
runtime. It needs a compatible NVIDIA driver and system CUDA 12 libraries
(including cuBLAS and cuFFT), NCCL 2, and OpenSSL 3. It has not been tested on
local NVIDIA hardware. Use Vulkan when those dependencies are unavailable. Staging refuses
an unexpected archive layout instead of installing a broken slot.

Updating the runtime does not replace selected models, voices, or generation
settings. Model package revisions and digests remain independently pinned.

The 0.9.2 catalogue contains 122 families and 380 packages. New speech packages
include Voxtral 4B TTS and French PocketTTS, alongside KittenTTS2, KugelAudio and
English/Japanese LFM2.5-Audio checkpoints. Kitten TTS 2 offers native
presets and optional cloning with a matching transcript and a 1–30 second
reference. Kugel offers preset voices. LFM's language belongs to its checkpoint:
English offers four voices; Japanese uses one built-in voice with no voice ID.
Manager installs LFM's matching backbone, encoder, vocoder and tokenizer together.
The VoiceTut fine-tune uses Egyptian Arabic (`arz`), including Arabic/English
code-switching; it does not inherit OmniVoice's general language list.

Model tuning follows each family's native scalar request parameters, including
new enums, text controls and defaults. Load/session controls remain part of the
managed package configuration. The catalogue also lists new recognition,
restoration and other audio models; catalogue membership does not imply a
corresponding Pandrator workflow or acoustic verification. See the
[0.9.2 release](https://github.com/0xShug0/audio.cpp/releases/tag/v0.9.2).

## Preset voice names and languages

Preset choices show readable names while keeping the provider's voice identifier
for synthesis. For example, Voxtral's `fr_female` appears as **French · Female**,
Kokoro's `af_heart` as **Heart**, and Supertonic's `F1` as **Female 1**. The voice
library and project settings use the same metadata.

Voxtral's fifteen locale-labelled presets cover Arabic, Dutch, French, German,
Hindi, Italian, Portuguese and Spanish. Its five general presets have no
documented native language. Fresh defaults prefer a matching locale preset;
English uses the upstream general default, **Casual · Male**. Voxtral and Qwen
voices remain selectable across their model's supported languages: a preset's
language describes its voice context, not a hard synthesis restriction.
[Mistral](https://docs.mistral.ai/studio/audio/text_to_speech/speech) and
[Qwen](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice) recommend
matching the voice language to the text for best quality.

PocketTTS's English GGUF packages bundle **Alba**, and its English safetensors
package bundles 26 voices. French GGUF packages bundle **Cosette**. Cosette also
exists in the English safetensors package; the package determines its language.
Classic Kitten Mini 0.8 exposes its eight voices with names such as **Bella**
and **Leo**, retaining their native expression IDs. NeuTTS voices get readable
names, with undocumented per-voice language and gender left unspecified.

Fresh audio.cpp defaults follow the native declared defaults, including
Supertonic **M1**, Kitten TTS 2 **Bruno**, and NeuTTS **emily**. Explicit saved
model defaults, language defaults, voice names and project selections remain
authoritative.

## Language and capability checks

Language support differs by model and sometimes by voice. Pandrator filters
choices using capabilities reported by an installed service, but that cannot
guarantee pronunciation quality for every language pair or cloned voice.

Pandrator translates the project language into the model's
request format:

| Model family | Language sent to audio.cpp |
| --- | --- |
| Qwen3 TTS | Native names such as `German`; covers Base, CustomVoice, and VoiceDesign. |
| FireRedTTS3 | Native names such as `German`, with native `ZH_*` dialect tags preserved. |
| MagpieTTS | Language codes, preserving `ar-AE`, `ar-SA`, `ar-MSA`, and `pt-BR`. |
| Chatterbox, OmniVoice | Base language codes such as `de`. |
| Kokoro | The selected voice's phonemizer language, preserving British `en-gb` and American `en-us`. Generic English accepts either voice accent. |
| Voxtral | No request hint: pronunciation follows the text and selected preset. |
| Fish Audio S2, VoxCPM2, BreezeTTS | No language hint: these projects infer language from the input. |
| PocketTTS | No request hint: language belongs to the loaded package. The English package rejects a request to switch languages. |

An omitted hint does not add language support to a model. These contracts were
checked against the pinned audio.cpp runtime and its
[model specifications](https://github.com/0xShug0/audio.cpp/tree/v0.9.2/model_specs).

Generation failures retain the provider's explanation, model, and HTTP status.
Temporary failures still retry; known audio.cpp language, cached-voice, and
unknown-option rejections stop immediately even when the server labels them
HTTP 500.

Before a large run:

1. confirm the service is ready;
2. confirm model and compute compatibility;
3. confirm the requested language is reported;
4. preview the voice or generate a short representative sample; and
5. test difficult names, numbers, abbreviations, and language changes.

CrispASR offers Whisper for broad transcription coverage and other engines for
different speed, timestamp, and diarization trade-offs. A known language is
usually safer than automatic detection for long, consistent media.

### Japanese with Kokoro

audio.cpp supports Japanese Kokoro with language `ja` and voices **Alpha**
(`jf_alpha`), **Gongitsune** (`jf_gongitsune`), **Nezumi** (`jf_nezumi`),
**Tebukuro** (`jf_tebukuro`), and **Kumo** (`jm_kumo`). Choose a Japanese voice;
an English voice cannot be paired with the Japanese phonemizer.

In the audio.cpp installation settings, change **Kokoro Japanese support** from
**Not installed (smaller download)** to **Add Japanese support**, select a Kokoro
package, and apply the update. Existing installations can add it the same way;
Japanese support is optional and off by default.

Japanese needs native **MeCab** to analyse the text and the **UniDic**
pronunciation dictionary. The add-on downloads about **502 MiB** and reserves
about **3 GiB** of extra disk space for one Kokoro precision, including the
download cache, prepared model and temporary dictionary extraction during
generation. Selecting both precisions requires more space. Cached downloads
are reused. These are private managed dependencies; no Python binding,
administrator access, or system MeCab configuration is needed.
The first generation after loading the prepared model can take longer while
audio.cpp extracts its resources; later requests reuse them until the model unloads.

The small upstream GGUF includes the Japanese G2P table but omits UniDic.
Pandrator verifies and retains that original model, makes a separate GGUF with
UniDic embedded, and selects it when the add-on is enabled. Its voices, tensor
bytes and precision stay intact. Disabling the add-on and applying another
update returns to the original model; previous slots remain available for rollback.
MeCab and dictionary redistribution notices are retained with the installation.
The dictionary is the pinned UniDic 3.1.0 package used by the upstream converter,
not an automatically selected newer dictionary.

For a manually managed audio.cpp server, re-exporting with
`--embed-multilingual-resources` embeds the dictionary but still requires native
MeCab. Installing Python dictionaries alone does not add them to an existing
GGUF. Linux uses `libmecab.so.2` by default, with `AUDIOCPP_MECAB_LIBRARY` as an
override. See the pinned
[Kokoro multilingual packaging guide](https://github.com/0xShug0/audio.cpp/blob/0097d365a00b52a6991ad7618f0a04e8d2935e47/tests/kokoro_tts/MULTILINGUAL_GGUF.md).

## Voice references

A voice reference should contain one speaker, little background noise or room
echo, and an accurate transcript. Keep the source and rights information with
the voice. A technically good sample is not automatically lawful or ethical to
use; obtain consent and respect model/provider voice policies.

Pandrator keeps voice samples and transcripts as their own artifacts. The
voice catalogue exposes reusable identity and provider bindings without
returning raw samples to routine list operations.

## Fine-tuned XTTS bundles

Canceling queued training stops it immediately; running training stays in
`cancel_requested` until the background job acknowledges cancellation.
Canceling a finished run preserves its outcome. Retrying failed, canceled, or
interrupted training creates a new run with the original audio, transcript, and
settings; the source artifacts must still be available.

An uploadable XTTS bundle is one flat directory containing exactly:

- `config.json`
- `model.pth`
- `speakers_xtts.pth`
- `vocab.json`

Update Pandrator and repair or update XTTS before importing if an older service
does not advertise model upload. In a project's **Generate audio** settings,
select XTTS, choose a new model ID, and upload all four files together. Nested
training directories, incomplete exports, and an existing model ID are
rejected. User models belong in managed user data, not a versioned service
source directory.

## Native and compatible cloud services

OpenAI, Google Gemini, native ElevenLabs, and compatible custom speech
endpoints can be configured where supported. Native ElevenLabs uses its own API
contract, not an OpenAI-compatible one. A third-party intermediary that exposes
an OpenAI-compatible speech API should be added as a custom provider instead.

ElevenLabs offers `eleven_v4` for content generation and `eleven_v4_turbo` for
realtime speech. Pandrator uses native Text-to-Dialogue HTTP for v4 and collects
a complete Text-to-Dialogue WebSocket turn for Turbo, saving it as a normal take.
Both support inline delivery directions. v4 accepts stability and similarity;
the current Turbo WebSocket contract exposes stability. Style, speed and speaker
boost are rejected for these routes. v4 receives up to 100 characters of
unspoken context on either side; Turbo has no documented hidden context fields.
Requests are limited to 2,000 characters including inline tags to follow the
dialogue API's reliability recommendation. The v4 model itself advertises a
10,000-character limit. Existing older models keep their native REST route.

ElevenLabs' API list prices are $80 per million characters for v4, v3 and
Multilingual v2, and $40 for v4 Turbo and Flash/Turbo v2.5. Through 12 October
2026, the advertised v4/v4 Turbo launch rates are $22/$11 per million characters.
Pandrator's default cost estimates switch to regular prices after that date;
explicit account pricing overrides still apply. Subscription allowances, taxes
and Voice Library multipliers can change the actual bill. See
[API pricing](https://elevenlabs.io/pricing/api).

Use your ElevenLabs API key and a voice ID available to that account. Existing
instant/professional clones may need retraining for v4; do not assume a voice is
ready merely because its ID is saved. Voice Design's creation models remain
separate from the speech-generation model. Higher MP3 bitrates and some PCM/WAV
rates require a higher subscription tier. Native v4 alignment is not yet used;
Pandrator's existing alignment workflow remains available. The newer
[WebSocket guide](https://elevenlabs.io/docs/eleven-api/guides/how-to/websockets/realtime-tdd)
documents v4 Turbo while portions of the API reference still describe v3.

Google Gemini and Vertex AI both offer `gemini-3.8-flash-tts` and
`gemini-3.8-flash-lite-tts`. Flash is the fresh-install default for expressive
audiobook narration; Flash Lite trades some acting nuance and dialect coverage
for lower cost and higher throughput. Explicit saved model selections remain
selected when upgrading.

| Gemini API model ID | Vertex model ID | Role |
| --- | --- | --- |
| `gemini-3.8-flash-tts` | `gemini-3.8-flash-tts` | Expressive narration and acting |
| `gemini-3.8-flash-lite-tts` | `gemini-3.8-flash-lite-tts` | Lower-cost, high-volume narration |
| `gemini-3.1-flash-tts-preview` | `gemini-3.1-flash-tts-preview` | Existing projects |
| `gemini-2.5-flash-preview-tts` | `gemini-2.5-flash-tts` | Existing projects |
| `gemini-2.5-pro-preview-tts` | `gemini-2.5-pro-tts` | Existing projects |

Google lists **17 November 2026 as the earliest possible shutdown date** for the
older Gemini API TTS models, rather than a guaranteed retirement date. The Vertex
3.8 models are currently in Preview. Language evidence is model- and route-specific:
the current Gemini API cards list Turkish and Vietnamese, while the Vertex 3.8
table omits them. Pandrator preserves the published script variants and Flash
Lite's narrower language list. See [model availability](https://ai.google.dev/gemini-api/docs/deprecations)
and [Vertex language support](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/text-to-speech/overview#languages).

For the Gemini API, configure a Gemini API key in the credential store. Pandrator
sends 3.8 requests to `POST /v1beta/interactions` with request storage disabled.
Vertex uses `aiplatform.googleapis.com`, your Google Cloud project, and either
service-account credentials or Application Default Credentials. Enable the Vertex
API and use the **`global` location** for both 3.8 models. Their speech endpoint is
`POST /v1/projects/{project}/locations/global/publishers/google/models/{model}:generateContent`.
Gemini 3.8 is available through this Vertex API, separately from the Cloud
Text-to-Speech API. See [Google's TTS guide](https://ai.google.dev/gemini-api/docs/speech-generation)
and [the Vertex setup guide](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/text-to-speech/overview).

In **Speech services → Test & refresh**, Pandrator discovers the extended voice
library and stored custom voices for the configured account/project. Existing
prebuilt names such as Kore remain available if discovery fails. Library and
stored `voice_...` IDs are available only with the 3.8 models. A voice's language
and accent metadata describes its native delivery; model language support governs
which text languages can be synthesized.

In **Voice Library → Design a reusable voice**, select Gemini or Vertex as the
voice-design provider. Enter a name, language and description, then choose
**Create & save voice**. Each creation saves a new library voice immediately,
including its description, provider ID, credential/project association and
Google-generated audition. The audition has Google's own text; it is not a
reviewed recording reference. A saved voice works with both 3.8 models and remains
selectable after a restart, even when optional voice discovery is unavailable.
Switching the Google project or Gemini API key does not move the voice into the
new scope. Restore the original credentials to use it there.

Google creates one stored voice per request; Pandrator does not blindly retry an
uncertain creation. If a request is interrupted, inspect **Activity & logs** and
refresh the Google voice catalogue before creating another voice. The saved local
request retains its description and provider identity whenever Google returned it.
Google permits 200 stored designed/replicated voices per project. Stored voices
expire one year after their last use; using them renews that period. Removing a
local library entry does not delete the remote Google voice; manage remote voices
through [Google's Voices API](https://ai.google.dev/api/voices).

For Gemini, creation uses `POST /v1beta/voices` with `store=true` and the chosen
design model. Vertex creation uses `POST /v1beta1/projects/{project}/locations/global/voices`;
the creation request omits the model as required by Vertex, and is billed at Flash
TTS rates. Google supplies the audition rather than accepting custom preview text.
See [Gemini voice design](https://ai.google.dev/gemini-api/docs/voice-design)
and [Vertex voice design and billing](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/text-to-speech/voice-design).

[Voice replication](https://ai.google.dev/gemini-api/docs/voice-replication)
requires reference speech and a separate consent recording from the same adult
speaker. Creation from recordings is not integrated into Pandrator's designer;
existing replicated IDs remain usable. Stateless replication keys expire after
seven days and are not listed by voice discovery.

Both 3.8 models send speech directions in unspoken structured metadata, separate
from the transcript. Use concise delivery directions. Enable `tts_context_mode`
as `before` or `both` (Generation settings → Text context) to include accepted neighbouring text as unspoken delivery
context; this is text guidance, without audio continuity or dedicated previous/next
API fields. Momentary vocal events use tags such as `<laugh>` and `<short pause>`.
Unary output is a complete 24 kHz WAV file. Both models have an 8,192-input-token
and 16,384-output-token limit; these are not transcript character limits.

Standard unary prices in USD per million tokens are the same on the two routes:

| Model | Input through 31 Dec 2026 | Audio output through 31 Dec 2026 | Input / output from 1 Jan 2027 |
| --- | --- | --- | --- |
| Flash 3.8 | $0.50 | $9 | $1 / $18 |
| Flash Lite 3.8 | $0.50 | $6 | $1 / $12 |
| Flash 2.5 | $0.50 | $10 | — |
| Flash 3.1 Preview | $1 | $20 | — |
| Pro 2.5 | $1 | $20 | — |

At 25 audio tokens per second, output costs approximately **$0.81 per hour for
Flash** or **$0.54 for Flash Lite** during the launch period, plus text input.
Older Flash 2.5 output is about $0.90 per hour; Flash 3.1 and Pro 2.5 are about
$1.80 per hour. Older-model rows show current prices; their future availability
is subject to Google's retirement schedule. Vertex voice creation uses the Flash
TTS input/output rates for the generated audition; Google's example (220 input
tokens, 1,100 audio tokens) is about one cent at the promotional rate. No separate
voice-storage fee is documented. Gemini Developer API documentation does not
explicitly establish voice-design billing parity with Vertex.
Pandrator estimates text tokens and output duration; this is not a billing receipt.
Configured pricing overrides remain authoritative. Batch, Flex, Priority tiers,
and account credits are outside this estimate. See [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)
and [Google Cloud TTS pricing](https://cloud.google.com/text-to-speech/pricing).

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

## MAI Voice 2.1 delivery

MAI Voice 2.1 and 2.1 Flash use the published 97-voice matrix, with a separate
model suffix for each voice ID. Requests validate styles against the selected
voice's exact list. Older `MAI-Voice-2` and `MAI-Voice-2-Flash` selections resolve
to 2.1 equivalents; saved built-in profiles refresh their old catalogue without
changing credentials or the selected voice stem. The picker uses Microsoft's locale metadata.
Leaving Azure Speech style empty omits the SSML emotion/style tag; style degree
has no effect without a style. This uses the voice's natural default delivery,
which can remain expressive or conversational. It does not request a flat
neutral mode. See [Microsoft's MAI documentation](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/mai-voices).
