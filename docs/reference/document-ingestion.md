# Document ingestion and narration pipeline

This reference explains how Pandrator turns a document into reviewable
narration text. It covers the shared workflow and the format-specific PDF and
EPUB paths, including OCR, deterministic cleanup, optional model-assisted
cleanup, artifacts, failure behavior, and the parameters that affect later
speech segmentation.

For a shorter task-oriented walkthrough, start with
[your first audiobook](../getting-started/first-audiobook.md). For supported
input and output containers, see
[supported formats and exports](formats-and-exports.md).

## The pipeline at a glance

```text
upload, paste, or reuse a source
  -> immutable source artifact and source-library record
  -> format-specific extraction
  -> deterministic cleanup and chapter detection
  -> extracted-text comparison artifact
  -> optional constrained model-assisted cleanup
  -> cleaned-text artifact
  -> narration preparation and editable generation-plan revision
  -> generated takes and take selection
  -> assembly
  -> audiobook export with chapters, metadata, and cover
```

The boundaries matter:

- The **source** is the file or pasted text supplied by the user. Pandrator
  does not overwrite it during cleaning.
- The **extraction baseline** is text produced by the format adapter before
  any optional model pass. PDF and EPUB adapters do different amounts of safe
  deterministic work at this boundary, as described below.
- **Cleaned text** is the reviewable input to narration preparation.
- A **generation segment** is an editable unit of narration with chapter,
  paragraph, pause, language, and voice-related state. It is not generated
  audio.
- A **take** is audio generated for one segment. Assembly and export consume
  selected takes, not merely the latest file in a directory.

Every stage creates a new managed result. Rerunning a stage can make an older
downstream result stale, but does not rewrite the original upload.

## Ways to add a document

| Entry path | What Pandrator stores |
| --- | --- |
| Upload | The original file becomes a managed upload artifact and a reusable source-library asset. |
| Paste | The browser creates a UTF-8 TXT upload from the pasted text; the remaining pipeline is the TXT path. |
| Reuse | An existing source-library asset is attached to the new session without duplicating its conceptual source record. |
| Add later | The session is created first; a source can be attached from its Sources view. |

The new-session **URL** option belongs to subtitle and voiceover workflows. It
downloads public audio/video from sites supported by `yt-dlp`; it is not a
general PDF or EPUB URL fetcher. Playlists are disabled.

Browser uploads up to 32 MiB use a normal multipart request. Larger uploads use
resumable chunks: 8 MiB by default, with a valid range of 1–16 MiB, per-chunk
hash validation, optional whole-file SHA-256 validation, and a 24-hour upload
session. The default resumable-upload ceiling is 100 GiB. The separate 10 GiB
Flask multipart ceiling protects direct requests; it is not the browser's
large-file path.

Upload accepts a source before deciding which workflow adapter will consume
it. An unsupported document extension can therefore upload successfully and
then fail clearly when **Clean source** starts.

## Format behavior

| Format | Extraction path | Important dependency or limitation |
| --- | --- | --- |
| TXT or pasted text | UTF-8 text, accepting an optional byte-order mark | Use UTF-8; review line and paragraph boundaries. |
| PDF | Geometry-aware PyMuPDF extraction, with optional page-local OCR | Scans, unusual layouts, and large pages cost more and need visual review. |
| EPUB | OPF/spine-aware archive parsing and deterministic book cleanup | Publisher markup varies widely; spine and navigation can disagree. |
| DOCX | Calibre `ebook-convert` to text | Calibre must be installed and discoverable. |
| MOBI | Calibre `ebook-convert` to text | Calibre must be installed and discoverable. |

Text-native sources are normally the most predictable. Conversion is not a
claim that visual layout, tables, sidebars, or image meaning can be recovered
perfectly.

## Shared source-cleaning controls

Defaults below are the current web-workspace defaults. The normal UI exposes
the common settings; advanced integrations can also provide the phase maps.

| Setting | Default | Effect |
| --- | --- | --- |
| `agentic` | `false` | Runs only deterministic extraction when false. When true, a configured LLM performs the app-executed five-phase review described below. Passive MCP cleanup uses a separate run and leaves this false. |
| `max_iterations` | `53` | Total model-turn budget distributed across enabled cleanup phases. It is a ceiling, not a target. |
| `phase_max_iterations` | `{}` | Optional per-phase ceilings. Supplied values override the distributed total for those phases and are clamped to pipeline limits. |
| `phase_names` | all phases | Advanced ordered subset of `metadata`, `navigation`, `boilerplate`, `repeated_elements`, and `chapter_marking`. |
| `request_timeout_seconds` | `600` | Deadline for one provider request during model-assisted cleanup. |
| `remove_footnotes` | `false` | Permits deterministic and model-assisted removal of likely footnotes. Leave false when notes belong in the narration. |
| `filter_citations` | `true` | Filters citation-like note references where the format adapter can identify them safely. |
| `model_name` | configured default | Optional model selection for app-executed cleanup. This is not passive dispatch. |

Changing a setting and rerunning **Clean source** produces a new cleaned-text
artifact. Review the diff and downstream stale-state warnings before
regenerating audio.

## PDF ingestion

### Text, geometry, and reading order

Pandrator reads native PDF text with PyMuPDF and keeps page number, bounding
box, page dimensions, font evidence, source method, confidence where
available, and reading-order information for each block. It uses geometry to
distinguish likely two-column pages from ordinary visual rows, groups nearby
lines into paragraphs, and repairs conservative hyphenated line breaks.

Page-to-page continuation is deliberately cautious. It joins only compatible
adjacent prose and avoids headings, terminal punctuation, bilingual-looking
joins, and common abbreviations. This helps remove page-layout seams without
turning separate paragraphs into one sentence.

The cleaned-text writer also reflows clear lowercase continuations left between
physical PDF blocks after reviewed marginal deletions. Source blocks remain
separate in the inspection index for page-level provenance; only the final text
loses the artificial audible pause. Validation distinguishes expected
non-narrative page deletion and text relocated into reviewed repairs from
substantive text that genuinely disappeared.

### OCR controls

| Setting | Default | Effect |
| --- | --- | --- |
| `pdf_ocr_mode` | `auto` | `auto` OCRs only pages whose native layer looks sparse or corrupt; `force` OCRs every page; `off` never OCRs. Older saved values `always` and `never` remain accepted aliases. |
| `pdf_ocr_language` | `auto` | Selects the OCR language/model family. A concrete language can improve recognition when automatic selection is weak. |
| `pdf_ocr_dpi` | `200` | Raster resolution for OCR. The accepted range is 120–400 DPI. Higher values use more time and memory and do not repair a wrong reading order by themselves. |

Automatic OCR considers native character volume, suspicious one-token line
patterns, replacement/bad-character ratios, and related page diagnostics.
OCR runs page by page through the lazy CPU ONNX PaddleOCR integration. If OCR
fails for one page and usable native text exists, Pandrator records a warning
and retains the native extraction instead of discarding the page.

Use **Automatic** first. Use **Always OCR** for an image-only scan or a PDF with
a consistently useless hidden text layer. Use **Never OCR** when native text is
known to be reliable or the document is too expensive to rasterize.

### Deterministic PDF cleanup

| Setting | Default | Effect |
| --- | --- | --- |
| `pdf_remove_toc` | `true` | Removes high-confidence contents sections from narration. |
| `pdf_remove_repeated_marginals` | `true` | Removes high-confidence repeated headers, footers, and page numbers. |
| `remove_footnotes` | `false` | Removes likely PDF footnotes only when explicitly enabled. |

The PDF adapter also identifies likely structural headings and schedules
`[[Chapter]]` markers. Deterministic operations are applied before final text
is saved and are supplied to the optional model review, so the model evaluates
the actual baseline instead of proposing the same removals again.

The structured ingestion cache is keyed by the source fingerprint, adapter
version, and normalized OCR configuration. Session-local
`source_ingestion/source_document.json` and `ingestion_report.json` files hold
the structured cache and diagnostics. An adjacent `.pycroppdf.json` provenance
sidecar from a derived PDF is also reflected in the ingestion report.

These detailed diagnostic files are session data, not individually selectable
workflow artifacts. The source, extraction comparison, and cleaned text are
the durable artifact boundaries visible to the workflow.

### Editing a PDF before extraction

The PDF editor is a separate, non-destructive preparation step. It can:

- inspect mixed page geometry and rotation;
- classify alternating page sides with `first_page_side=right` or `left`;
- address all, left, right, single, or explicit page ranges such as `1-3, 7`;
- crop, white out rectangles, or delete pages; and
- save a derived PDF with a provenance sidecar.

Coordinates use unrotated PDF points. The backend rejects unknown pages,
out-of-MediaBox rectangles, and overwriting the source. Run extraction on the
derived artifact and keep the original until the result has been reviewed.

## EPUB ingestion

### Book order and structure

Pandrator reads `META-INF/container.xml`, the package document, manifest,
spine, metadata, and EPUB navigation. Deterministic narration follows spine
order. The structured metadata pass can inspect remaining manifest documents
after the spine for evidence, but those extra documents are not silently added
to deterministic narration.

The structured index records, where available:

- Dublin Core title, creator, language, publisher, date, and identifier;
- navigation title, target path/fragment, order, and depth;
- element tags, classes, IDs, `epub:type`, ARIA/role evidence, and DOM paths;
- meaningful image alternative text and captions; and
- candidate structural roles for headings, notes, boilerplate, and navigation.

Metadata read from the book is stronger than a filename guess. Navigation is
evidence, not absolute truth: page lists, nested contents entries, and broken
targets must not become audiobook chapters by themselves.

### Deterministic EPUB cleanup

The EPUB adapter:

- skips dedicated contents, footnote, title/license/legal, obvious front/end
  boilerplate, and Project Gutenberg wrapper material when confidently found;
- removes visual-only blocks while preserving meaningful prose and useful
  alternative text where appropriate;
- recognizes direct EPUB semantics, markup, multilingual numbered headings,
  navigation evidence, and conservative fallbacks for chapter detection;
- emits chapter boundaries as `[[Chapter]]Title`;
- resolves EPUB noteref/footnote links and can reposition note text near its
  reference; and
- applies `remove_footnotes` and `filter_citations` without treating every
  superscript or number as disposable.

If structured EPUB indexing fails but deterministic text extraction succeeds,
Pandrator can continue with a cleaned-text fallback and records the loss of
raw-markup tools as a warning. A corrupt container, missing rootfile/package
document, or unreadable archive still fails the stage.

### Optional model cleanup and the EPUB baseline

Model-assisted EPUB cleanup works on the deterministic cleaned text, not on
the raw publisher markup. EPUB metadata and navigation hints are retained, but
raw-markup selector tools are intentionally unavailable in that pass. This
enforces a critical invariant: enabling a model may refine the baseline, but a
no-op or incomplete response cannot resurrect a removed contents page,
illustration caption, license block, citation, or footnote.

This is a quality/safety tradeoff. Deterministic structure remains the source
of truth; the model focuses on residual text-level problems. Review the clean
text whenever unusual typography or a complex scholarly edition matters.

## Constrained model-assisted cleanup

When `agentic=true`, Pandrator runs five focused phases in order:

1. metadata;
2. navigation and contents;
3. boilerplate;
4. repeated elements; and
5. chapter marking and completeness review.

The model does not receive permission for an unconstrained whole-book rewrite.
The normal phases permit only typed metadata updates, deletion by stable
blocks, line ranges or selectors, and chapter marking/unmarking. A guarded
compatibility operation can replace at most five matched blocks and 1,000
characters, but the normal five-phase pipeline does not grant that operation.
Phase-specific allowed-operation sets, inspection requirements, finish
reviews, iteration ceilings, validation, and an audit trail constrain the
result.

The final application is deterministic. Pandrator validates likely remaining
contents/boilerplate, missing or removed chapter boundaries, excessive
deletion, and related structure before registering the result. Warnings mean
“review this,” not “the text is definitely unusable.”

Agentic cleanup writes a session-local audit set containing the working index,
raw text, cleaned text, typed rules, a report, and a diff. Token/cost usage is
recorded when the provider reports it.

### Passive host-model cleanup

The same five structural concerns, followed by an explicit text-repair pass,
can instead be reviewed by the model already running in an MCP host. Create a
source-cleaning dispatch run for an attached PDF or EPUB. Pandrator prepares
the structured baseline as durable work, then leases `metadata`, `navigation`,
`boilerplate`, `repeated_elements`, and `chapter_marking` packets followed by
`text_repair`.

Each later packet is derived from the persisted index after accepted earlier
operations have been applied. A navigation deletion therefore cannot remain as
actionable boilerplate or chapter evidence merely because all packets were
prepared at run creation.

The host model must accept or reject every server proposal and may add only the
typed operations allowed by that phase. The initial claim is deliberately
bounded, but it is not the model's only view: while holding the lease the model
can browse ranges, search text or regular expressions, inspect blocks and
context, query navigation/heading/footnote structure, preview selectors, and
batch independent inspections over either the original baseline or the current
working document. Returned live block IDs become auditable legal targets for
that batch. The final phase can use `replace_block` for confirmed OCR, joining,
encoding, or parser defects rather than forcing deletion or accepting damaged
text.

For PDF chapter review, weak section, numbered-note, and decorative-title
matches stay visible as heading evidence but are not automatically proposed as
chapters. The host model may inspect and mark them when the book's actual
structure warrants it.
Pandrator never sends this run to its configured LLM provider. There is no
model-token or iteration budget in the run; `evidence_limit` only bounds the
number of evidence items disclosed per phase. It defaults to 500 and accepts
20–2,000. After all phases, Pandrator applies and validates the accepted
operations using the same deterministic artifact boundary.

See [passive processing through MCP](../guides/passive-dispatch.md) for the
lease, retry, privacy, and tool-level workflow.

## From clean text to narration segments

**Prepare text** converts the selected cleaned-text artifact into a JSON
artifact and an editable generation-plan revision. `[[Chapter]]` lines become
chapter segments; they are protected during normalization and later supply
audiobook chapter markers.

| Text setting | Default | Effect |
| --- | --- | --- |
| `enable_sentence_splitting` | `true` | Splits overlong narration at safe boundaries. |
| `max_sentence_length` | `200` | Preferred maximum characters for prepared sentences. Later TTS block limits can be smaller. |
| `enable_sentence_appending` | `true` | Appends short neighboring sentences when the combined segment remains within the length policy. |
| `enable_nemo_normalization` | `true` | Applies deterministic written-to-spoken normalization only when the selected source language is supported. `auto` no longer silently means English. |
| `normalize_all_caps` | `true` | Normalizes likely all-caps words while protecting common acronyms, Roman numerals, and chapter structure. |
| `remove_diacritics` | `false` | Transliterate/remove diacritics only when explicitly requested; this can damage names and meaning. |
| `remove_quotation_marks` | `false` | Strips quotation marks only when explicitly requested. |

Choose a concrete session source language when you know it. With `auto`,
language-independent sentence segmentation still runs, while language-specific
NeMo normalization is skipped rather than guessed as English.

Generation applies a second, provider-facing segmentation policy:

| TTS/audio setting | Default | Effect |
| --- | --- | --- |
| `speech_block_min_chars` | `10` | Preferred lower size for a synthesis block. |
| `speech_block_max_chars` | `220` | Maximum provider-facing block size. |
| `speech_block_merge_threshold` | `250` ms | Maximum short gap for merging complete neighboring utterances. |
| `speech_block_continuation_threshold_ms` | `3000` ms | Pause tolerance when an unfinished sentence continues. |
| `speech_block_max_internal_gap_ms` | `1800` ms | Maximum silence allowed inside one synthesis block. |
| `sentence_silence_ms` | `250` ms | Default assembly pause after an ordinary sentence. |
| `paragraph_silence_ms` | `700` ms | Default assembly pause after a paragraph. |

Narration segments and TTS blocks are related but not interchangeable. Editing
or resegmenting the generation plan can stale existing takes because their
text/boundaries no longer match.

## UI, API, and MCP behavior

The same stage model is used from all three surfaces:

- The UI saves session settings, creates an immutable workflow plan, and runs
  the selected stages as durable jobs.
- API clients attach/reuse source assets, save revisioned settings, create a
  workflow plan, execute that exact plan, and monitor durable work.
- The MCP exposes both the application planning/execution surface and the
  passive source-cleaning dispatcher. PDF/EPUB parsing remains in Pandrator;
  the sidecar never reimplements it.

Setting `agentic=true` selects **app-executed** model cleanup through
Pandrator's configured provider. It does not create passive work. Passive
cleanup starts through the dedicated source-cleaning dispatch tools and uses
the MCP host model instead. Both paths converge on a validated `clean_text`
artifact, but their execution and credential boundaries remain explicit.

For exact MCP installation, scopes, planning, and monitoring contracts, see the
[Pandrator MCP guide](../../pandrator_mcp/README.md). The packaged
[audiobook agent guide](../../pandrator_mcp/guides/audiobooks.md) gives agents a
short workflow summary and points back to this reference.

## What to review

Before preparing narration:

1. Compare the source/extraction baseline with cleaned text at the beginning,
   middle, and end.
2. Check every chapter boundary and make sure `[[Chapter]]` is absent from
   ordinary prose.
3. Search for contents entries, repeated headers/footers, page numbers,
   publisher/license text, illustration captions, citations, and notes.
4. Inspect paragraph joins near PDF page boundaries and column changes.
5. Check names, abbreviations, dates, numbers, quotations, and languages that
   normalization may affect.
6. For EPUB, confirm that spine order matches the intended reading order.
7. Prepare a short representative section and listen before generating a full
   book.

After preparing narration, review segment boundaries, chapter/paragraph flags,
silence choices, voice/language selection, and any speech-optimized revision.
Generate a small sample before committing substantial provider time or cost.

## Failures and recovery

| Symptom | Likely cause and next step |
| --- | --- |
| Upload succeeds but Clean source fails with unsupported type | The source library can store more formats than the audiobook adapter consumes. Convert to TXT, PDF, EPUB, DOCX, or MOBI. |
| `invalid_pdf` or unreadable PDF | Open the source in a PDF viewer, save/repair a derived copy, then attach that copy. |
| OCR warning with native fallback | Inspect the named page. Try a concrete language, different DPI, `force`, or a repaired/cropped derived PDF. |
| PDF order is wrong | Use the PDF editor where useful, or convert a representative section to cleaner text. OCR cannot infer every layout. |
| EPUB reports missing `container.xml`, rootfile, or package document | The archive is corrupt or not a conforming EPUB. Repair or reconvert it before retrying. |
| EPUB structured-index fallback warning | Cleaned text remains available, but markup/selector evidence was unavailable. Review navigation and chapters carefully. |
| DOCX/MOBI conversion fails | Install or repair Calibre and verify that `ebook-convert` is discoverable. |
| Prepare text says a cleaned artifact is required | Finish/select **Clean source** first; preparation accepts cleaned TXT/Markdown artifacts, not the original binary document. |
| Agentic cleanup reaches a limit or emits validation warnings | Review its diff and report. Increase a phase budget only after confirming that more model turns address the actual problem. |
| Passive cleanup claim says `run_preparing` | Poll the run or its preparation job. PDF OCR may still be running locally. |
| Passive cleanup submission is rejected | Keep the current lease, repair the typed proposal decisions or operations using only exposed IDs, and resubmit with the same logical idempotency key. |

Check the job detail and bounded event log before rerunning. For wider service,
storage, or recovery problems, see
[troubleshooting](../operations/troubleshooting.md).

## Trust and resource boundaries

Document parsing is local, but files can still be hostile or pathologically
large. Managed paths are containment-checked and EPUB members are read without
extracting publisher paths to the filesystem. PDF OCR rasterization and EPUB
archive parsing can nevertheless consume substantial CPU, memory, and disk.
Pandrator does not currently promise one universal page-count or archive-
inflation budget for every document shape.

Use normal OS/storage limits for untrusted sources, keep the application and
parsers updated, and test a representative subset before enabling forced OCR
or model-assisted cleanup on a very large book. Uploading a file does not send
its text to a model; enabling app-executed cleanup, claiming a passive cleanup
packet in an MCP host, or later speech optimization can.
Review the provider boundary in
[privacy and security](../security/privacy-and-security.md) before execution.
