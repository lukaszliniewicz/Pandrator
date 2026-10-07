# Supported formats and exports

Pandrator distinguishes what it can recognize or import from the normalized
working representation used by a workflow. A recognized file may therefore be
converted before editing or generation.

## Input and output matrix

| Category | Supported formats and behavior |
| --- | --- |
| Documents | TXT, PDF, EPUB, DOCX, MOBI, or pasted text |
| Subtitle sources | SRT working input; WebVTT, ASS, and SSA are also recognized as subtitle sources |
| Subtitle output | SRT or WebVTT; selected cues can also be concatenated as text |
| Audio input | AAC, AIFF, FLAC, M4A/MKA, MP3, OGG, Opus, WAV, WMA |
| Video input | MP4, MKV, WebM, AVI, MOV |
| Audiobook and audio output | M4B, MP3, Opus, FLAC, WAV |
| Video books | MP4 with narration and burned-in reading text; a separate SRT or WebVTT file is retained |
| Video output | MP4-oriented export with selectable audio and subtitle tracks |

Exact codec support also depends on the installed FFmpeg build and the
container selected for export.

## Documents

Text-native documents produce the most predictable extraction. PDFs can be
text-native, scanned, multi-column, or visually structured in a way that does
not map cleanly to reading order. OCR and AI-assisted cleanup are optional and
must be reviewed. MOBI conversion needs Calibre; other Manager operations do
not require it.

Keep the original artifact and treat cleaned text as a new reviewable stage.
For the format branches, PDF/OCR controls, EPUB spine and navigation behavior,
artifact boundaries, cleanup settings, and narration parameters, see
[document ingestion and narration](document-ingestion.md).

## Subtitles

The durable editing pipeline normalizes subtitle sources to timed display
cues. SRT is the primary working interchange. Export uses the selected
transcription, correction, or translation revision—not speech blocks generated
later for TTS.

SRT and WebVTT differ in syntax and player support. Open the final file in the
target player or editor and check cue order, overlaps, line wrapping, encoding,
and language metadata.

## Collecting multilingual exports

Open a project's **Languages** section, select languages, and preview translation,
generation or export before submitting it. Each language keeps its own inputs,
settings and completed outputs. Generation requires a reviewed current speech
plan; translated SRT exports do not require generated audio. Passive language
work still waits for its agent submission and review.

**Collect outputs** verifies selected completed exports and offers their manifest.
The manifest records stable project/language/version filenames and artifact
hashes. Prepare a ZIP only when every selected export is verified; an incomplete
collection stays incomplete. Changing inputs or output settings requires a fresh
preview and export. Durable operation progress survives an application restart;
eligible failed languages can be retried without rerunning completed children.

## Audio

Use WAV or FLAC when preserving a lossless intermediate matters. MP3 and Opus
are compact delivery formats. M4B is the audiobook container when chapters,
metadata, and cover art should travel with one file.

Generation takes, assembled audio, and final exports are separate artifacts.
Select and assemble takes before expecting an export to reflect them.

## Video books and timed audiobook text

In an audiobook project's **Output** section, select a completed **Audio version**
and choose **Video book** or **Timed subtitles** as the export target. These exports
reuse the saved narration and its pauses. Changing the presentation does not
generate new speech.

**Reading passages** displays fitted text on a solid background, centred by default.
The default aims for 12-second passages, with a 20-second maximum and up to four
lines. Sentence and paragraph boundaries, word timings, and available space determine
the actual cue length. Reading text stays visible through pauses. **Plain captions**
uses the project's language-aware subtitle limits and places text near the bottom.
Text size, colours, alignment, resolution and chapter headings are configurable.
Use **Preview 25 seconds** to inspect the current controls before saving or exporting;
the preview starts at the time you specify and stops at the end of the book if shorter.

Fitted passages use validated provider word timings when available, otherwise a
local forced aligner works from the exact text sent to speech synthesis. Timing is
cached by the saved audio and spoken text, so a change of font, colour or cue length
can reuse it. **Whole segments** uses existing segment boundaries and skips alignment.
This is the fastest subtitle-only mode; long segments can still be too large for a
video frame. Use fitted passages or a smaller text size in that case.

Displayed wording defaults to the original when it can be mapped reliably to speech.
Anchored phonetic name replacements can retain their original spelling and share the
timed interval of the spoken name. Inserted, removed or substantially rewritten text
can require spoken wording instead; the export reports any fallback. **Require original
wording** stops when mapping is uncertain. Whole segments can always display the complete
original wording without word-level mapping. Review a representative preview after
speech-text optimisation or pronunciation changes.

Video export requires FFmpeg with H.264/AAC and libass, plus an installed font that
covers the displayed language. A font file can be selected under **More controls**.
Timed subtitles need no video renderer or font. Exported files and timing records
remain separate artifacts tied to the selected audio version.

## Video and subtitle tracks

Voiceover export can preserve, mix/duck, or replace source audio. Subtitle
choices can include source, translation, or bilingual tracks and can be soft
or burned where supported. Soft subtitles remain selectable and editable in a
compatible player; burned subtitles become pixels in the rendered video.

Test a short representative render before a long final export, especially when
mixing audio, burning subtitles, or changing codecs.

## URL imports

URL import uses `yt-dlp` for supported public media sources. Service support can
change independently of Pandrator. You are responsible for the source
service's terms and applicable law, and for confirming that downloaded media
is the expected quality and language.

For the workflow around these formats, see
[your first audiobook](../getting-started/first-audiobook.md),
[your first subtitles](../getting-started/first-subtitles.md), and
[your first voiceover](../getting-started/first-voiceover.md).
