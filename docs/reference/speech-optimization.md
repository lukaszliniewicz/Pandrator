# Speech-text optimization and dispatch reference

Pandrator keeps display text separate from the text sent to a speech engine.
Speech optimization may expand abbreviations and numerals, repair
speech-affecting punctuation or OCR defects, and prepare difficult written
forms for a selected voice or TTS service. It must preserve meaning and does
not replace source correction or translation.

For task-oriented pronunciation guidance, see
[pronunciation and speech text](../guides/pronunciation-and-speech.md). For a
model already running in an MCP host, see
[passive dispatch](../guides/passive-dispatch.md).

## Three execution paths

| Path | Unit and timing | Result | Model caller |
| --- | --- | --- | --- |
| Standalone, before generation | SRT cue, prepared narration row, or TXT document | Separate, reviewable `tts_optimized` artifact | Pandrator's configured LLM provider |
| During generation | Final generation segment or speech unit | Speech text stored with the generation plan/take | Pandrator's configured LLM provider |
| Passive MCP dispatch | One or more pinned SRT cues, JSON narration rows, or a TXT unit | Normal `tts_optimized` artifact after all batches pass validation | The model already running in the MCP host |

Standalone optimization is the quality-first choice when you want to compare
the complete source and optimized text before spending speech-generation time.
Generation-time optimization is useful when the final synthesis segmentation
or per-segment voice context matters. Enabling both normally repeats the same
kind of transformation, so do so only deliberately.

Passive dispatch is a standalone whole-document stage. It does not pause an
active audio-generation job and ask an external worker to service its internal
segments. Finish the passive run, review the materialized artifact, and then
start generation from that artifact.

## Guarded and flexible modes

The user-facing modes are:

- **Guarded**: the model decides constrained operations over stable tokens and
  protected pronunciation candidates. Pandrator compiles and validates the
  result.
- **Flexible**: the model may improve speech-only punctuation and phrasing in a
  protected template, but placeholders, facts, and meaning remain invariant.
  Invalid output falls back through the guarded protocol.

The older prompt-rewrite mode is retired from the UI and normal settings. Old
saved sessions remain readable and migrate to Guarded when their text settings
are saved; this is compatibility, not a third mode users need to choose.

Both current modes validate each unit independently. A failed unit becomes an
individual retry or a deterministic safe fallback; valid siblings do not have
to be discarded.

## Model-request batching

Batch size controls transport, not editorial identity. With a size of `1`, one
speech unit is sent per provider request. This is often the safest setting for
small local models. A larger size sends several independently protected cases
in one request, reducing request overhead and letting a capable model see
nearby units while it disambiguates names, numbers, or phrasing.

The outer batch contract requires every case identity exactly once. Pandrator
then runs its normal token, placeholder, meaning-retention, pronunciation, and
plan validation for each returned case. Missing, duplicate, or invalid cases
are retried individually. Text, decisions, tokens, and placeholders cannot
move between cases.

`llm_concurrent_calls` controls how many model-request batches may run at once;
it does not change the number of units inside a request. Higher concurrency is
useful for throughput but prevents accepted output from becoming sequential
context for a later concurrent batch. Prefer one concurrent request for
context-sensitive prose, then raise units per request before concurrency.

## Native settings

| Setting | Default | Effect |
| --- | --- | --- |
| `llm_tts_document_optimization` | `false` | Enables the separate, reviewable stage before generation. |
| `llm_tts_optimization` | `false` | Enables optimization of final speech units during generation. |
| `speech_optimization_mode` | `guarded` | Selects `guarded` or `flexible`. |
| `llm_tts_document_batch_size` | `8` | Units per standalone provider request. Use `1` for a model that handles one case more reliably. |
| `llm_tts_batch_size` | `3` | Units per generation-time provider request. Use `1` for strict single-unit calls. |
| `llm_concurrent_calls` | `1` | Provider requests allowed concurrently. Applies to batches, not individual units. |
| `tts_optimization_model` | configured default | Model used by native standalone and generation-time optimization. Passive dispatch ignores it. |
| `reasoning_effort` | model default | Optional native-provider reasoning level. |
| `speech_plan_min_retention` | `0.9` | Minimum protected text-retention ratio for Flexible mode. |
| `speech_plan_save_proposals` | `true` | Saves proposed pronunciation entries for review; proposals never become active automatically. |
| `request_timeout_seconds` | `600` | Native provider deadline. It is unrelated to a passive lease. |

Historical divided-prompt and free-form speech prompts remain readable for old
session data but are not shown in the normal workflow. They apply only to the
retired compatibility executor, not Guarded or Flexible planning.

## Standalone source and output shapes

- **SRT**: every non-empty source cue is one stable unit. Optimized text is
  written into a new SRT with original timing. Speakers are retained in the
  editable revision metadata.
- **JSON**: every non-empty prepared narration row is one unit. Pandrator keeps
  the original row, records `source_text`, and writes
  `tts_optimized_sentence`.
- **TXT**: the complete text is one unit. Batch character targets never split
  an individual unit.

Standalone native execution creates a reviewable revision and records provider
usage. Passive execution records `provider: null`, `model: null`, its run ID,
the source artifact, batch count, and the same `tts_optimized` output role.

## Passive speech-optimization contract

Creation accepts an eligible audiobook or voiceover session and optionally an
exact source artifact. Automatic selection prefers:

- audiobook: `prepared_text`, then `clean_text`, then a supported TXT upload;
- voiceover: `translation`, then `correction`, then `transcription`, then a
  supported SRT, JSON, or TXT upload.

Only SRT, JSON, and TXT are speech-text sources. An EPUB or PDF must first pass
through document extraction and narration preparation; media must first be
transcribed.

| Parameter | Default | Effect |
| --- | --- | --- |
| `char_limit` | `20000` | Target source characters per transport batch, up to 1,000,000. A single unit is never split and may exceed it. |
| `max_units_per_batch` | `100` | Maximum actionable units in one claim, up to 500. |
| `context_before` | `4` | Accepted output units from the preceding batch supplied as read-only context. |
| `context_after` | `2` | Following source units supplied as read-only context. |
| `include_timing` | `true` | Adds one timing object to each actionable SRT unit. Context never repeats timing. |
| `language` / `voice_language` | source/session values | Describes written and target voice language without selecting a model. |
| `tts_service` | empty | Optional service hint for spoken forms. It does not call that service. |
| `instructions` | empty | Additional speech policy appended to the preservation contract. |
| `lease_seconds` | `900` | Claim lifetime, renewable from 30 to 3,600 seconds. |

The claim contains the only actionable `batch.units`, their stable `unit_id`
values, a required identity/order contract, and read-only boundary context.
Submit `kind: speech_optimization` and exactly one non-empty `text` result for
every `unit_id` in the supplied order. A validation failure keeps an unexpired
lease usable for repair. Accepted batches are sequential; the last one
materializes the artifact only if the pinned source, relevant stage selections,
and previous optimization output head are unchanged.

There is no Pandrator provider, model-token budget, iteration budget, or
reasoning setting in this path. Character and unit limits bound transport; the
MCP host chooses the model and how much reasoning it uses.

## Quality review

Text validation protects identity and meaning, but only listening establishes
whether a transformation helps the selected voice. Review short representative
takes for names, numbers, acronyms, language changes, rhythm, pauses, and
provider-specific regressions. Keep the display/source revision unchanged
unless the underlying text itself is wrong.
