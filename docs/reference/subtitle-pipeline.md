# Subtitle-to-speech pipeline and parameter reference

This is the durable map of Pandrator's subtitle, correction, translation, and
voiceover data flow. It describes both native UI/API execution and passive MCP
dispatch. Defaults below are the web-workspace defaults.

For task-oriented instructions, start with
[your first subtitles](../getting-started/first-subtitles.md),
[your first voiceover](../getting-started/first-voiceover.md), or
[passive dispatch](../guides/passive-dispatch.md). This page owns the technical
stage and parameter model; the workflow guides deliberately do not repeat it.

## The pipeline in one pass

```text
audio/video
  -> STT engine output
  -> engine-neutral timed segments and words
  -> deterministic display cues (transcription revision)
  -> optional correction revision
  -> optional translation revision
  -> optional reviewed display/speech variants
  -> speaker-safe speech blocks
  -> immutable generation plan, generated takes, and selections
  -> timing alignment and audio assembly
  -> subtitle/audio/video export
```

The important distinction is that display cues, LLM batches, and speech blocks
are different objects:

- A **timed word** is the finest STT-aligned token.
- A **display cue** is a readable, timed subtitle event. It is what SRT/VTT and
  the subtitle review UI show.
- An **LLM batch** is a temporary semantic work unit containing several display
  cues. Its boundary is not a subtitle edit.
- A **speech block** is a synthesis unit. It may cover several display cues, or
  one long cue may produce several speech blocks.
- An **alignment group** joins speech blocks that share a source timing window
  so their audio is fitted together rather than made to compete for that window.

Documents and revisions preserve stage history. Artifacts identify files;
document revisions and segment lineage identify editable subtitle state. A
downstream result is pinned to the exact upstream artifact, revision, content
hash, and relevant stage selections.

## 1. STT output and normalization

Pandrator first makes a processing copy of the source media, then runs the
selected STT engine. Engine-specific JSON is adapted to
`pandrator.transcript.v1`:

- `TimedSegment`: text, `start_ms`, `end_ms`, optional speaker, identifier,
  metadata, and zero or more words.
- `TimedWord`: text, `start_ms`, `end_ms`, optional speaker, confidence, and
  engine metadata.
- `NormalizedTranscript`: ordered segments, source format, language, and
  metadata. Downstream code consumes this rather than an engine's private wire
  format.

| Setting | Default | Effect |
| --- | --- | --- |
| `stt_engine` | `whisper` | Selects Whisper, Parakeet, or MOSS Transcribe-Diarize. |
| `stt_model_quantization` | `f16` | Model precision/quantization. Lower precision saves memory and may reduce accuracy. |
| `stt_compute_backend` | `auto` | Chooses CPU, CUDA, Vulkan, Metal, or automatic selection. |
| `stt_compute_device` | `0` | Device index when the backend exposes more than one device. |
| `stt_language` | `auto` | Fixed recognition language or automatic detection. |
| `stt_threads` | `0` | Worker-thread override; zero lets the runtime decide. |
| `stt_chunk_seconds` | `0` | General chunk duration; zero uses the engine policy. |
| `stt_chunk_overlap_seconds` | `3.0` | Re-decoded overlap for non-MOSS chunk stitching. More overlap protects seams but costs time and can duplicate text. |
| `stt_hotwords` | empty | Terms to bias when the engine supports them. |
| `stt_lid_backend` | `whisper` | Language-identification backend. |
| `stt_beam_size` | `1` | Search breadth. Larger beams cost more and are not automatically better for every engine. |
| `whisper_prompt` | empty | Whisper-specific vocabulary/context hint. |
| `parakeet_decoder` | `tdt` | Parakeet decoder selection. |
| `diarization_enabled` | `false` | Adds speaker labels for supported non-MOSS engines. MOSS supplies native turns. |
| `moss_max_chunk_seconds` | `120` | MOSS context window when the general chunk size is automatic. |
| `moss_chunk_overlap_seconds` | `0` | MOSS overlap is separate and off by default because native speaker turns make duplicated overlap especially harmful. |
| `moss_vad_enabled` | `false` | Enables VAD before MOSS decoding. |
| `moss_ctc_alignment_enabled` | `true` | Adds word-level CTC alignment to MOSS segment output. Without timed words, cue composition falls back to timed segments. |
| `moss_ctc_aligner_model` | `auto` | Selects the MOSS word aligner. |
| `moss_ctc_padding_seconds` | `0.5` | Audio padding around a segment sent to the word aligner. |
| `crispasr_vad_enabled` | `true` | Enables Silero VAD for engines using the shared CrispASR path. |
| `crispasr_vad_threshold` | `0.5` | Speech-probability cutoff. Higher is stricter. |
| `crispasr_vad_min_speech_ms` | `250` | Rejects shorter detected speech islands. |
| `crispasr_vad_min_silence_ms` | `800` | Silence needed to split a region. |
| `crispasr_vad_speech_pad_ms` | `30` | Context added around detected speech. |
| `crispasr_vad_max_speech_seconds` | `300` | Maximum VAD region before a forced split. |

Normalization also repairs implausible word spans and removes strong,
time-aligned MOSS seam duplicates before cue composition.

## 2. Initial display cues

When timed words exist, Pandrator builds one source string with word-to-character
spans, scores possible boundaries, and finds a low-cost global partition. It
will not cross a speaker change or a hard silence. Sentence punctuation,
clause punctuation, speaker changes, SaT boundary probability, gaps, weak cue
starts/ends, duration, and reading speed all influence the chosen boundaries.

| Setting | Default | Effect |
| --- | --- | --- |
| `max_chars_per_line` | `48` | Preferred visual line width. Final wrapping uses linguistic and balanced-line heuristics. |
| `max_lines` | `2` | Maximum display lines per cue. |
| `max_cps` | `20.0` | Reading-speed target in visible characters per second. It is a cost/extension target, not permission to shred a fast phrase into tiny cues. |
| `min_duration_ms` | `833` | Desired minimum display duration when the following cue leaves room. |
| `max_duration_ms` | `7000` | Maximum display duration and one limit on cue capacity. |
| `min_gap_ms` | `80` | Desired presentation gap between adjacent cues. Source word timing wins when there is no room. |
| `phrase_gap_ms` | `600` | A meaningful pause that rewards a boundary but does not force one. |
| `hard_gap_ms` | `1500` | Silence a display cue may never cross. This is deliberately lower than the LLM editorial pause default. |
| `sentence_boundary_threshold` | `0.25` | Minimum SaT boundary probability allowed to affect the optimizer. |
| `boundary_correction_enabled` | `false` | Reserved/compatibility setting; the current primary cue composer is already the deterministic optimizer above. |
| `merge_threshold_ms` | `250` | Legacy compatibility alias. Speech blocks have their own explicit merge threshold. |

If word timing is unavailable, Pandrator finalizes the engine's timed segments
instead. Display cue numbers are regenerated in timeline order; immutable
revision lineage, not an SRT number alone, is the durable identity.

## 3. Correction and translation

Native UI/API execution calls a configured LLM provider. Passive MCP dispatch
does not: Pandrator snapshots and queues work, while the model already running
in Codex/OpenCode/etc. claims and processes each batch itself. Both paths use
the same cue identity, batch construction, timing policy, and editorial
instructions.

Shared settings:

| Setting | Default | Effect |
| --- | --- | --- |
| `char_limit` | `6000` | Maximum source characters in a semantic batch. |
| `max_segments_per_batch` | `40` | Maximum actionable cues in a batch. The first reached limit wins; the splitter prefers a sentence or speaker boundary and will not cut an overlap when avoidable. |
| `llm_concurrent_calls` | `1` | Native execution concurrency. One is quality-first because previous accepted output and evolving glossary can flow forward. Passive dispatch is sequential for now. |
| `context` | `true` | Enables boundary continuity context for native LLM work. |
| `context_before` | `8` | Maximum previous corrected/translated output cues supplied as non-actionable context. |
| `context_after` | `2` | Maximum following source cues supplied as non-actionable context. |
| `timing_context_mode` | `full` | `full`, `overlap_only`, or `none`; see below. |
| `substantial_gap_ms` | `2000` | In `full` mode, tells the model which audible pause normally deserves a rhetorical boundary. It does not change cue timing. |
| `no_remove_subtitles` | `false` | When true, correction cannot delete and translation cannot emit `[REMOVE]`. |
| `instructions` | empty | User policy appended to the stage's built-in protocol. |
| `model_name` | default model | Native LLM only. Passive dispatch uses no Pandrator model. |
| `reasoning_effort` | model default | Native LLM only; stronger reasoning can help difficult passages but costs latency/tokens. |
| `request_timeout_seconds` | `600` | Native provider request deadline. It is separate from a passive batch lease. |

Timing disclosure is exact:

- `full`: every actionable cue has one `timing` object with `start_ms` and
  `end_ms`, plus either its preceding gap or positive overlap when applicable.
- `overlap_only`: only a cue with positive overlap has a `timing` object, and
  that object contains only `overlap_with_previous_ms`.
- `none`: no timing key or timing value is model-visible. Legacy boolean false
  now maps to this mode; overlap no longer leaks through it.

Correction returns typed operations over stable source-revision `cue_id`
values:

- `edit`: one cue, one replacement; timing is retained.
- `delete`: one or more consecutive cues, no replacement; forbidden by
  `no_remove_subtitles`.
- `merge`: consecutive cues; the replacement spans their combined time.
- `split`: one cue and multiple replacements; its duration is divided in
  proportion to replacement text length.

Cross-speaker merges require an explicit valid speaker decision. Line wrapping
is never an LLM responsibility; deterministic subtitle finalization runs after
editing.

LLM translation returns exactly one translation for every actionable `cue_id`
and may return only new `glossary_updates`. Manual glossary entries remain
authoritative. DeepL translation starts from the same deterministic cue mapping,
then repacks semantic groups up to its safe request-size limit. LLM batch,
timing, context, glossary, removal, instruction, and model-reasoning controls do
not apply to DeepL itself and are hidden when that backend is selected.

Web research, when enabled, is a separate bounded grounding pass. Global mode
researches the stage once; per-chunk mode researches each processing chunk.
Its evidence is kept separate from editable subtitle text and glossary state.

### Passive dispatch contract

Creation pins `kind`, source artifact/revision/hash, languages, instructions,
batch/context/timing policy, deletion policy, and glossary. Claim returns:

- one-based `batch_ordinal`;
- a canonical `task` contract;
- `batch.id_namespace = source_revision_cue`;
- `batch.valid_cue_ids` and the only actionable `batch.cues` array;
- `batch.context.previous_output` and `following_source`, which are never
  actionable and contain no timing;
- one short-lived `lease_token` and expiry.

Correction submission is `{kind: "correction", operations: [...]}`. Translation
submission is `{kind: "translation", translations: [...], glossary_updates:
{...}}`. Raw `response_text` remains a compatibility adapter only. Invalid
content retains a still-valid lease for repair. Batches are accepted in order;
only the final accepted batch atomically materializes the new stage artifact.

Passive-only controls:

| Parameter | Default | Effect |
| --- | --- | --- |
| `kind` | required | Chooses correction operations or one-for-one translation items. Pandrator never chooses a model. |
| `source_artifact_id` | selected eligible stage | Pins an exact transcription/correction artifact instead of relying on a later “current” lookup. |
| `source_language` / `target_language` | session values | Describe the task; translation requires a target language. |
| `glossary` | empty | Initial manual translation mappings. These remain authoritative over model-proposed additions. |
| `idempotency_key` | required for MCP writes | Identifies one logical create/claim/renew/release/submit action. Retry the same payload with the same key; use a new key for the next action. |
| `lease_seconds` | `900` | Gives a worker 30–3,600 seconds. Renew with the matching `batch_id` and `lease_token` before it expires. |
| `lease_token` | returned by claim | Short-lived capability scoped to one batch. A stale or mismatched token cannot submit. |
| `result` | preferred | Typed correction or translation result validated against the run and current cue IDs. |
| `response_text` | compatibility only | Legacy raw JSON/model text; mutually exclusive with `result`. |

Claim reports `run_status` and `batch_status` separately. This prevents an
idempotent replay of an already accepted batch from being mistaken for a
finished run; the next action remains “claim the next batch” until final
materialization completes.

## 4. From display cues to speech blocks

Speech blocks optimize synthesis, not subtitle display. Pandrator reconstructs
unfinished same-speaker utterances, splits them at balanced linguistic
boundaries under the hard engine size limit, then packs nearby complete
utterances when safe.

| Setting | Default | Effect |
| --- | --- | --- |
| `speech_block_min_chars` | `10` | Soft quality target used to avoid tiny fragments where possible. It is not a hard rejection rule. |
| `speech_block_max_chars` | `220` | Hard maximum text sent to TTS. Long utterances are split using punctuation, conjunctions, whitespace, then a hard fallback. |
| `speech_block_merge_threshold` | `250` | Maximum ordinary gap for packing nearby complete utterances, subject to same-speaker and size checks. |
| `speech_block_continuation_threshold_ms` | `3000` | Maximum gap for reconstructing an unfinished same-speaker sentence before size splitting. |
| `speech_block_max_internal_gap_ms` | `1800` | Independent hard guard: no speech block may span a larger internal silence, even if the sentence is unfinished. |
| target language | session target | Selects language-aware sentence/conjunction splitting. |

Reviewed display text and reviewed speech text are partitioned as a pair. The
spoken variant stays under the TTS limit; the display variant is not duplicated
merely to satisfy that limit. Each block keeps exact contributing subtitle
references, speaker, and `alignment_group`.

These three gaps answer different questions and should not be conflated:

- subtitle `hard_gap_ms` (1500): may a display cue cross this silence? No.
- LLM `substantial_gap_ms` (2000): should prose normally preserve a rhetorical
  boundary here? Yes, as editorial evidence only.
- speech-block continuation/internal-gap settings (3000/1800): may unfinished
  speech be reconstructed, and what silence may synthesized audio span?

## 5. Generation, alignment, and output

The speech-block list becomes an immutable generation plan. Each segment has a
revision, voice/service/model settings, generated takes, and a selected take.
Changing a take does not rewrite the subtitle revision. `tts_batch_size` (10)
is a request/streaming throughput preference negotiated with the service;
`max_attempts` (5) bounds generation retries. Service-specific sampling and
voice parameters affect sound, not subtitle or speech-block boundaries.

For timed voiceover, selected audio is mapped back through each block's source
subtitle references. Blocks sharing an `alignment_group` are concatenated
before being fitted to the shared timing window. Relevant assembly defaults:

| Setting | Default | Effect |
| --- | --- | --- |
| `synchronization_delay_ms` | `800` | Maximum allowed start delay when fitting generated speech. |
| `synchronization_speed` | `1.2` | Maximum speed-up factor used to fit a block. |
| `synchronization_sentence_gap_ms` | `100` | Minimum generated gap retained between aligned sentence blocks. |
| `audio_verification_mode` | `off` | Optional signal-level checks for suspicious generated audio. |
| `sentence_silence_ms` | `250` | Narration pause after a sentence; timed subtitle blocks normally derive timing from the source instead. |
| `paragraph_silence_ms` | `700` | Narration pause after a paragraph. |

Assembly can preserve source audio, mix and duck it under generated speech, or
produce dubbing only. Export then chooses subtitle-only/text/media output,
source/translation/dual subtitle selection, SRT/VTT, soft or burned subtitles,
and audio/video codec settings. Subtitle export uses the finalized selected
revision; speech blocks never replace display cues as the subtitle source.

## Quality-first operating defaults

For correction or LLM translation, keep 6,000 characters, 40 cues, sequential
execution, eight previous/two following context cues, and full timing unless a
measured token constraint requires otherwise. Reduce batch size before removing
context or timing. `overlap_only` is the sensible first economy step; `none` is
appropriate only when timing cannot help the task or disclosure must be
minimized. Increase concurrency only when the latency saving matters more than
cross-batch continuity and evolving glossary consistency.

Related public guidance:

- [Correction and translation](../guides/correction-and-translation.md)
- [Pronunciation and speech text](../guides/pronunciation-and-speech.md)
- [Supported formats and exports](formats-and-exports.md)
