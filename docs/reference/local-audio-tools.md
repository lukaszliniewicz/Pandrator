# Local model navigation and audio preprocessing

Pandrator can use the installed audio.cpp runtime for model inference without installing a separate Python machine-learning environment. These integrations target audio.cpp 0.8.1 or later.

## Finding a speech model

For audio.cpp, model controls group the catalogue by family, then meaningful variant or language, then precision. Qwen Base, CustomVoice and VoiceDesign stay separate because their voice inputs differ. Pocket models are grouped by language. Exact model IDs remain visible and are saved unchanged.

The same grouping is used in session speech settings, the full TTS settings panel, provider defaults, and the local component package list. Search includes entries inside collapsed groups. Expanding a group or entering a search does not select, install or load a model. Selecting a TTS model is distinct from installing its package.

State labels use evidence: Loaded means the runtime reports it loaded; Installed requires an installed record; Installable means a package is available. Merely appearing in a catalogue is not proof of installation. Unknown/custom saved selections are retained instead of silently replaced.

## Qwen3 recognition and word alignment

Choose **Qwen3 ASR** in a transcription stage's Recognition model control. Select 0.6B for the smaller recognizer or 1.7B for the larger recognizer. Their pinned Q8 downloads are approximately 1.15 GB and 2.47 GB respectively.

Qwen recognition supports 30 languages, but Qwen Forced Aligner supports only 11. An aligner locates supplied words in a recording; it is not a recognizer.

For timestamped transcription, select the **source** language explicitly:

- Qwen alignment: Chinese, English, Cantonese, French, German, Italian, Japanese, Korean, Portuguese, Russian and Spanish.
- Canary CTC fallback after Qwen recognition: Czech, Danish, Dutch, Finnish, Greek, Hungarian, Polish, Romanian and Swedish. This path uses the Manager-installed CrispASR runtime; its alignment model downloads on demand.
- The remaining Qwen recognition languages do not currently have a validated timing path in this integration. Use another timed recognizer such as Whisper for subtitles in those languages. The recognizer's lower-level transcript-only API retains the broader recognition coverage.

The translation target is not used to choose a word aligner. Unsupported timing combinations fail before normalization, vocal isolation, model downloads or inference. The existing Qwen Forced Aligner is also selectable for aligning supplied captions. Low-level transcript-only requests do not load the forced aligner.

Long recordings are processed in bounded chunks with original offsets retained. The automatic Qwen chunk setting resolves to a non-VAD native mode for these bounded chunks, avoiding an implicit dependency on an uninstalled Silero model. Explicit VAD mode remains an advanced choice and requires its runtime assets. Token-budget, output, timestamp-bound and cancellation checks reject incomplete results rather than inventing word timing. Original transcript punctuation is restored onto safely matched word surfaces before subtitle composition.

## Vocal isolation before transcription

Open **Audio preprocessing** in the transcription settings. Vocal isolation defaults to **Off**. The choices are:

- **BS-RoFormer**, approximately 173 MB.
- **Mel-RoFormer**, approximately 252 MB.

Isolation reduces music around vocals. It does not reliably select one speaker from other voices, and it can damage useful speech details. Leave it off for clean recordings.

The selected model runs locally before recognition or caption alignment. Recognition and alignment use a derivative; original media remains available for playback and export. Output duration is checked against the input using a small absolute tolerance, not a percentage of a long recording. A requested preprocessing failure stops the task rather than silently reverting to untreated audio.

## Microphone voice-sample cleanup

In the voice library, open **Samples & setup**, record a sample, and enable **Clean background noise with DeepFilterNet2** before saving when needed. This checkbox defaults to off.

DeepFilterNet2 uses an approximately 9 MB model. It runs on the job worker after upload, not as a live browser filter. The worker processes at 48 kHz before the existing voice-sample normalization step. Raw uploads and provenance remain available; cleaning does not mark a transcript reviewed. A failed or cancelled save keeps the local recording available for retry.

## First-use downloads and cancellation

Only models actually requested for processing download. Model sources are allowlisted and pinned to immutable revisions, byte sizes and SHA-256 digests. Concurrent requests share a process-safe model lock; incomplete or invalid downloads are not activated. Verified cache entries are reused. Existing Manager assets may be reused read-only when they match the pinned file.

These auxiliary processing models use the workspace cache, separate from the Manager's TTS package inventory. Status inspection never downloads a model. Jobs report model preparation and processing progress and can be cancelled through the usual activity controls. Native processing has a finite timeout. Originals are not overwritten.

## Local execution and memory

Pandrator serializes native audio.cpp processing with local audio.cpp speech generation across processes. Waiting jobs remain cancellable. Automatic backend selection is treated as potentially using a GPU in the job queue, including vocal isolation and microphone cleanup.

Before GPU processing, Pandrator checks the managed audio.cpp server for resident speech models, releases only those models through the server's unload API, and verifies they are unloaded. The next speech request loads its model again. If release cannot be confirmed, processing stops with a choice to stop the speech service or select CPU. CPU processing skips GPU residency management. This coordinates Pandrator's callers; independently operated servers and clients remain outside its control.

Qwen recognition with word timing can itself require both recognition and alignment weights. Serializing jobs does not make their combined memory footprint disappear. CPU is available explicitly in Qwen's local processing settings; separation on CPU can be substantially slower than the recording's duration.

## Verification scope

Native CPU verification uses audio.cpp 0.8.1 with short synthetic recordings for Qwen 0.6B recognition/alignment, BS-RoFormer, Mel-RoFormer and DeepFilterNet2. English recognition matched the reference and produced word timings. Polish recognition followed by CrispASR Canary alignment completed, with a final word ending 34 ms beyond the recording (within the 50 ms validation tolerance), but the synthetic Polish sample had recognition errors. These checks establish functional integration and preservation of originals, not listening quality or language-by-language accuracy. Qwen 1.7B and GPU stability were not retested in this crash-recovery verification.

Primary references: [audio.cpp](https://github.com/0xShug0/audio.cpp), [Qwen3 model integration](https://github.com/0xShug0/audio.cpp/blob/main/docs/models/qwen3.md), [audio.cpp audio tools](https://github.com/0xShug0/audio.cpp/blob/main/docs/audio_tools.md), and [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR).
