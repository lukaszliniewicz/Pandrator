# Qwen3 forced alignment

Pandrator supports Qwen3-ForcedAligner-0.6B Q8_0 through the local audio.cpp CLI. It aligns a supplied transcript to its matching audio; it does not translate, recognize new text, or synthesize speech. Qwen is a non-autoregressive aligner, not a CTC aligner. Historical `*_ctc_model` setting names remain supported for API compatibility.

## Choosing an aligner

In the caption-timing or MOSS transcription settings, choose **Forced aligner**:

- **Automatic** uses Qwen3 for Japanese, Chinese, Korean and Cantonese, and Canary CTC for other supported languages.
- **Qwen3** explicitly selects Qwen for Chinese, English, Cantonese, French, German, Italian, Japanese, Korean, Portuguese, Russian or Spanish.
- **Canary CTC** explicitly retains the existing European-language alignment path.

Set the **source/audio language**. The translation target must not determine alignment of the original recording. Unknown language is not silently treated as English for explicit Qwen requests. Script inference is only used when source language is unavailable.

The caption selector is `caption_alignment_ctc_model`; the MOSS selector is `moss_ctc_aligner_model`. Both accept `auto`, `canary-ctc-aligner` and `qwen3-forced-aligner`. The existing caption alignment method values (`ctc`, `ctc_asr_fallback`, `asr`) remain wire-compatible. Diagnostics distinguish `qwen3_forced_alignment` and `qwen3_alignment` from real CTC evidence.

## Installation and storage

As an Administrator, install audio.cpp through Pandrator Manager. Pandrator finds
its `audiocpp_cli` executable automatically. For an unmanaged installation, set
`AUDIO_CPP_CLI` to the executable's full path. Choose CPU, an installed GPU backend
or Automatic in the transcription compute settings.

If Linux reports **Permission denied** when starting the aligner, update Manager
and repair the audio.cpp component. Updating Pandrator alone does not update the
Manager executable. For a manually installed CLI, check its executable permission:

```bash
chmod u+x /path/to/audiocpp_cli
```

Replace the example path with your own executable; managed installations should
use Manager's repair action.

The Q8_0 model download is about **1.13 GB**. An Administrator must authorize
acquiring a missing model; other users can reuse a model that is already
installed. It is cached separately from voice-generation packages under
`Pandrator/cache/aligners/qwen3` in managed installations, or
`~/.cache/pandrator/aligners/qwen3` otherwise. Later runs reuse the cache, including
offline runs. Downloads are checked against a pinned SHA-256 before activation;
concurrent requests share the download lock, and cancellation removes incomplete
files.

Operator-only advanced runtime overrides are `qwen_aligner_model_path`, `qwen_aligner_executable`, `qwen_aligner_cache_dir` and `qwen_aligner_backend`. A custom model must be a compatible Qwen forced-aligner GGUF, not an ASR or TTS model. Custom local files are operator-supplied, not verified against the bundled model digest.

Only the model file is fetched from the internet. Audio and transcripts are not uploaded. The aligner is not listed as a voice or speech-generation model.

## Batch and text handling

Up to eight bounded caption/audio pairs share one native model load. Native MOSS turns use the same bounded-batch adapter. The audio.cpp request-sequence JSON interface keeps Unicode text out of shell arguments and avoids Windows command-line text limits. Temporary clips are retained only within bounded processing groups.

Each request must have its own exact matching mono 16-bit 16 kHz WAV and transcript. Standalone pairs exceeding 300 seconds are rejected: the application does not guess how to divide an untimed transcript across arbitrary audio chunks. Caption requests retain their existing smaller context windows.

Japanese/Chinese alignment uses source-preserving alignment-only units. This avoids audio.cpp 0.8.1 treating a long kana run as a single word. Native timestamp units can differ from space-delimited words. They are mapped back to exact original grapheme spans and punctuation, without changing display text or manufacturing interpolated acoustic timestamps. They must not be used directly as TTS speech-block boundaries.

The adapter validates finite, monotonic sample timestamps within the clip, full lexical text conservation, and caption-boundary ownership. Missing, extra, changed or cross-cue units are rejected. A model's placeholder confidence field is not presented as acoustic certainty: the existing VAD/temporal quality assessment remains the confidence basis. A failed cue or MOSS turn retains its original caption/native-turn timing and a diagnostic.

## Source-relative timing diagnostics

Caption-alignment diagnostics include `source_timing_offsets`: signed start/end
deltas from the original caption boundaries, their median and nearest-rank p95
absolute deltas, plus at most 20 largest outliers identified by cue ID. The
outlier threshold is a strict boundary shift greater than 2,000 ms. Rejected cues
are counted separately; the summary uses final accepted cues, including ASR
fallback fills. It contains no transcript text.

A boundary shift is distinct from the alignment acceptance window, which covers
the whole original cue interval plus configured padding. A large shift can be
accepted structurally and still deserve listening review. These diagnostics do
not change timestamps, reject another cue or represent acoustic confidence.

## Timing limitations

Successful alignment means the output passes the adapter's structural checks; it does not measure acoustic timestamp accuracy or pronunciation quality. Review timing against the original audio before a large batch, especially around names, numbers and mixed scripts.

Primary references:

- [Official Qwen forced-aligner model](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B): 11 supported languages and the five-minute per-pair bound.
- [audio.cpp Qwen integration](https://github.com/0xShug0/audio.cpp/blob/v0.8.1/docs/models/qwen3.md): native alignment and request options.
- [Pinned GGUF payload](https://huggingface.co/audio-cpp/audio.cpp-gguf/tree/dc6fecccc2b0c6bdda0a8b2f38fa61394fee0b9c/Qwen3-ForcedAligner-0.6B-GGUF): Q8_0 and F16 model artifacts.
