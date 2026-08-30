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

## Audio

Use WAV or FLAC when preserving a lossless intermediate matters. MP3 and Opus
are compact delivery formats. M4B is the audiobook container when chapters,
metadata, and cover art should travel with one file.

Generation takes, assembled audio, and final exports are separate artifacts.
Select and assemble takes before expecting an export to reflect them.

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
