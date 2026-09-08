# Quick transcription

Use `pandrator_transcribe` for transcription without a permanent session.
Inspect the live schema for exact arguments and the configured STT defaults
when provider, language, or model choice matters.

1. For a file, browse an operator-approved named source root and select the
   returned relative path. Do not pass an arbitrary absolute server path.
   Small supplied clips can instead use base64, up to 8 MiB decoded; do not
   fetch a large recording into model context to encode it.
2. Request `txt`, `srt`, or `json` and use a stable idempotency key. The
   configured local or cloud STT service performs recognition; the harness
   model does not transcribe the audio through passive dispatch.
3. The tool waits up to 30 seconds. If it returns pending work, keep the
   temporary transcription ID and call `pandrator_transcription_get`.
4. Read or page the result with `pandrator_transcription_result`. Switching
   output format uses the existing recognition result. Check pagination and
   return the complete requested transcript, not just its first page.
5. Results expire one hour after completion. Deliver or save them while
   available. Do not use session artifact-download tools for these temporary
   results; follow the transcription tool's result actions.

`pandrator_transcription_cancel` stops work;
`pandrator_transcription_delete` removes temporary data. This workflow leaves
no permanent session or source-library entry, but uses temporary disk storage
and retains job metadata. Do not describe it as having zero storage or logging.
