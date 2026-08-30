# Create your first subtitles

A subtitle session turns media or an existing subtitle file into timed,
reviewable cues. Transcription, correction, translation, and manual review are
separate revisions, so you can compare stages and choose the exact result to
export.

## 1. Choose the source

Create a **Subtitles** session. You can upload an SRT file or common audio and
video formats. WebVTT, ASS, and SSA are recognized as subtitle sources, while
the current editing pipeline uses SRT as its working subtitle representation.

If you already have reliable source-language subtitles, start from them. For
media, install CrispASR and choose a transcription engine appropriate for the
language and hardware.

## 2. Transcribe media

Choose the recognition language or automatic detection, compute backend,
model, and—when supported—word alignment and diarization. A fixed language is
usually safer when it is known. Vocabulary hints or a Whisper prompt can help
with names and domain terms.

Pandrator normalizes engine-specific output into timed segments and words,
then deterministically composes readable display cues. Review speaker changes,
long silences, overlaps, very fast cues, names, and the beginning and end of
every processing chunk.

## 3. Correct the source-language track

Manual review is the highest-control option. A configured LLM can propose
edits, deletes, merges, and splits in semantic batches. Passive dispatch can
instead let the model already running in an MCP host process the same kind of
queued work without a separate model API inside Pandrator.

Correction creates a distinct source-language revision. It retains or derives
timing according to the operation and never asks the model to perform visual
line wrapping. Review deletions, cross-speaker merges, and split timing.

See [correction and translation](../guides/correction-and-translation.md) and
[passive dispatch](../guides/passive-dispatch.md) for choosing a method.

## 4. Translate when needed

Choose the selected transcription or correction revision as the source.
DeepL provides a dedicated translation path; a configured LLM supports the
shared glossary, context, and reasoning controls; passive dispatch uses the
MCP-host model. Translation produces a target-language revision linked to the
exact source cues.

Review names, terminology, speaker labels, omissions, and how much text must
fit into each original timing window. A translated track can itself be the
source of a correction run; the result remains a new translation revision in
the same target language.

## 5. Review and select

Compare the source, correction, and translation tracks side by side. Selecting
an older upstream revision may invalidate downstream selections because those
results were derived from another source. Treat the reported impact as a real
lineage change rather than assuming a later translation is still current.

## 6. Export

Export the selected track as SRT or WebVTT, or concatenate it as a transcript.
For a dubbed result, continue with the
[voiceover workflow](first-voiceover.md) instead of treating subtitle export as
speech generation.

## Quality checklist

- The selected language and speaker labels are correct.
- Chunk seams contain no duplicated or missing words.
- Cues are readable without excessive speed or awkward line breaks.
- Correction did not remove meaning or merge different speakers.
- Translation preserves names, glossary decisions, and cue identity.
- The exact intended revision is selected before export.
- The exported file was opened in a player or subtitle editor for a final
  timing check.

The [subtitle pipeline reference](../reference/subtitle-pipeline.md) explains
every stage and its quality-oriented defaults.
