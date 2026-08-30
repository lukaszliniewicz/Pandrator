# Create your first audiobook

An audiobook session turns a document or pasted text into reviewable narration
segments, generated takes, an assembled timeline, and a final audio or M4B
export. Preparation, generation, assembly, and export are separate stages so a
new take does not silently replace a finished book.

For exact PDF/OCR and EPUB behavior, cleanup controls, artifacts, and
segmentation defaults, keep the
[document-ingestion reference](../reference/document-ingestion.md) nearby.

## Before you begin

Install or configure one TTS provider. **Kokoro** is a lightweight local first
choice; a cloning engine such as **Qwen3 TTS Base** or **XTTS v2** needs a
usable reference voice. Preview a built-in voice or prepare a reference sample
before generating a large book.

If cleanup or speech-text optimization will use an LLM, check whether it is
local or cloud-hosted and which document text it will receive.

## 1. Create the session and import text

Create an **Audiobook** session and provide TXT, PDF, EPUB, DOCX, MOBI, pasted
text, or a reusable document from the source library. PDF quality varies:
text-native PDFs are easier than scans, and OCR should be reviewed carefully.
The public-URL importer is for audio/video transcription workflows, not general
PDF or EPUB downloads.

Pandrator keeps the original source artifact. Cleanup produces a new,
reviewable result rather than overwriting it.

## 2. Clean and structure the document

Review the extraction baseline and diagnostics, headers, page numbers, broken
line wrapping, footnotes, OCR errors, and chapter boundaries. Prefer
deterministic cleanup for predictable patterns. Optional model-assisted cleanup
uses constrained structural edits and should be reviewed before its result
becomes the selected clean source.

PDF users should start with automatic OCR and change it only after inspecting
the native result. EPUB users should verify spine order and chapter markers,
especially in scholarly, illustrated, or unusually structured editions.

Check chapter titles and metadata now. They affect navigation and M4B chapter
markers later.

## 3. Segment narration

Segmentation creates editable speech units with pause and structure metadata.
Read several neighboring segments aloud before generating: a boundary that is
visually tidy can still sound unnatural. Fix the text or boundary at this
stage rather than trying to repair every generated take afterward.

Optional whole-document speech optimization creates a separate speech-text
revision. Generation-time optimization can perform similar work per batch;
avoid enabling both unless you deliberately want two transformations.

## 4. Choose the voice and generation settings

Select the provider, model, voice, language, and any service-supported
sampling or style controls. For a cloned voice, use a clean, single-speaker
sample with an accurate transcript. Short tests reveal pronunciation,
language, noise, and pacing problems much more cheaply than a full run.

Use the [pronunciation library](../guides/pronunciation-and-speech.md) for known
names and terms. It changes only the TTS request; your displayed source remains
readable.

## 5. Generate and review takes

Generation creates takes for individual narration segments. Listen at chapter
starts, dialogue, difficult names, language changes, and transitions between
separately generated passages. Edit speech text or pronunciation rules and
regenerate only the affected segments. Keep a previous take when it is better;
generation does not force selection of the newest result.

Closing the browser does not stop an active durable job. Reopen Pandrator from
Manager and inspect the job rather than starting a duplicate run.

## 6. Assemble and export

Assembly follows the selected take for every segment and constructs the final
audio sequence. Listen across joins and chapter boundaries before export.
Then choose WAV, MP3, Opus, FLAC, or M4B. M4B can include chapters, metadata,
and cover art.

Keep the source document, selected speech revision, chosen takes, and exported
file as distinct review points. A successful generation job does not imply
that assembly or export has run.

## Quality checklist

- The selected clean text has no extraction or OCR residue.
- Chapter and paragraph boundaries sound intentional.
- The chosen voice supports the text language.
- Reference voice material is clean and lawfully usable.
- Difficult names have reviewed pronunciations.
- Representative generated takes were heard before the full run.
- Every segment has the intended selected take.
- Chapter joins and final metadata were checked after assembly.

For supported file types, see [formats and exports](../reference/formats-and-exports.md).
For provider and voice choices, see [providers and voices](../guides/providers-and-voices.md).
