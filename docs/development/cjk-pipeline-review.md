# CJK pipeline review

Reviewed on 18 September 2026 against Pandrator 0.9.4. The focus is Japanese batch translation, subtitles and voiceover, with Chinese and Korean regression coverage.

## Alignment follow-up

Qwen3 forced alignment was subsequently added for Japanese, Chinese, Korean and
Cantonese automatic routing, with an explicit choice for its other supported
languages. See [Qwen forced alignment](../guides/qwen-forced-alignment.md).
The original Canary-only limitations below describe the initial CJK review.

## Subtitle defaults

Automatic language limits use the actual subtitle track language, not the voiceover target for every track. New/default-valued configurations use these source-preserving caption presets:

| Language | Full-width units per line | Units per second | Lines |
| --- | ---: | ---: | ---: |
| Japanese | 16 | 7 | 2 |
| Chinese, either script | 16 | 9 | 2 |
| Korean | 16 | 12 | 2 |
| Other languages | 60 | 20 | 2 |

The Japanese automatic profile favors complete lecture/caption text. It is not a claim of Netflix delivery compliance. A stricter Japanese translation-subtitle profile of 13 units per line and 4 per second can be entered by disabling automatic limits. Existing non-default custom settings remain custom. Explicit per-run limits also override inherited automatic settings unless that request explicitly enables automatic limits.

For CJK display capacity and reading-speed calculations, a full-width grapheme counts as one unit and a half-width grapheme as half a unit. Combining marks, supplementary Han, variation selectors and Hangul Jamo are not counted as independent visible characters. This is a layout approximation, not pixel measurement. Python source and pronunciation offsets remain code-point offsets, and synthesis-engine character limits remain separate.

## Changes by pipeline stage

- **Language routing:** Normalize language names, ISO aliases and regional/script tags, including `ja-JP`, `jpn`, `ko-KR`, `zh-Hans`, `zh-Hant` and `cmn-Hans-CN`. Preserve the Simplified/Traditional distinction for translation and track labels. Normalize aliases for audio.cpp and Kokoro without overriding explicitly selected dialects.
- **Correction and translation:** Do not inject Western inter-word spaces when joining Japanese/Chinese cue fragments or exporting a plain transcript. Retain Korean word spacing. Apply the conservative CJK request-size adjustment to normalized source-language aliases, including Korean. Native and passive dispatch use the same language-aware display projection.
- **Subtitle composition:** Support unspaced text, weighted grapheme capacity and conservative opening/closing-punctuation and small-kana line-break guards. Preserve inline ideographic and non-breaking spaces. Long CJK text no longer takes the Western single-token overflow path. Candidate-range fitting uses numeric prefix widths to avoid repeatedly tokenizing every possible overlapping range.
- **Timing:** Retain coarse CJK ASR phrase envelopes rather than treating every phrase as an abnormally long Western word. Display-split oversized phrase tokens without inventing acoustic word anchors or duplicating canonical ownership. Source and translated exports use their own language settings.
- **Speech construction:** Preserve code-point provenance when joining cues. Subtitle character-wrap opportunities are never used as speech cuts. Unpunctuated over-limit CJK narration raises an actionable error instead of cutting arbitrarily through the text. Native punctuation fallback works independently of the selected speech provider.
- **Pronunciation and optimization:** Accept reviewed kana/Hangul readings; allow multi-character Japanese names to match before particles. Guarded and flexible compilation retain native-script spacing. Retention checks recognize grapheme-sized edits inside unspaced text. Replace exact shipped English-centric legacy prompt fingerprints at use time, without changing custom prompts. Pass each item's declared language to optimization. Latin accent removal no longer romanizes a CJK passage or destroys dakuten.
- **Alignment safety:** Known unsupported languages bypass the bundled Canary CTC aligner and retain caption/native-turn timing with diagnostics. Source/audio language is checked, not the translation target. An explicitly supplied custom aligner is not assumed to share Canary's coverage; unknown non-file model selections are rejected rather than silently falling back.

## Verification

After the final fixes:

- Fast regression lane: **796 passed** (`pytest -q -n 2`, file list from `scripts/test_lanes.py`).
- Focused CJK, exported-video timing, settings, speech planning, subtitle finalization and rebalancing: **150 passed**.
- Workflow plans and parity/workspace: **49 passed**.
- Workflow handlers: **71 passed**.
- `npm --prefix web run check`: **0 errors, 0 warnings**.
- `git diff --check`: clean.

These suite counts overlap and must not be added into a unique-test total. The earlier broad review also exercised Manager/installer, MCP and media lanes. A settings test asserting the old English-only prompt was updated; the media timing failure was fixed by preserving explicit per-run reading limits and re-run successfully, rather than weakening its expected timestamps. Browser assets were rebuilt from the changed Svelte source.

The dedicated CJK fixture file covers Japanese, both Chinese scripts, Korean spacing, mixed Latin/CJK text, combining kana, supplementary Han, Jamo, code-point span ownership, native-script pronunciation, language routing, alignment refusal, and source-text conservation before any paid model calls.

## Limits and batch-use guidance

This review does not certify translation quality, Japanese pronunciation, all speech models, or live end-to-end model output. The line-breaking guards are deliberately conservative, not a full morphological segmenter or complete implementation of Unicode line breaking. Uncertain kanji-name readings still need a reviewed glossary. Timing inherited from captions/native turns remains coarse when an appropriate acoustic aligner is unavailable.

Reading-speed targets cannot always be met inside fixed source timing without condensing text. The compositor preserves content rather than silently deleting it. Subtitle display splitting is not permission to use those same small pieces as voiceover speech blocks.

Before a large production batch, validate a representative Japanese sample containing names, quotations, numerals, mixed-script acronyms and a long sentence. Review subtitle readability and actual synthesized pronunciation separately.

## Primary references

- [Netflix Japanese timed-text guide](https://partnerhelp.netflixstudios.com/hc/en-us/articles/215767517-Japanese-Timed-Text-Style-Guide): distinct translated-subtitle and SDH limits; full-width/half-width counting.
- [Unicode line-breaking algorithm](https://www.unicode.org/reports/tr14/): line-break opportunities, combining sequences and language tailoring.
- [Canary CTC aligner model card](https://huggingface.co/cstr/canary-ctc-aligner-GGUF): the bundled aligner's 25-European-language coverage, which does not include Japanese, Chinese or Korean.
