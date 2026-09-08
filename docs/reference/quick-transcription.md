# Quick transcription

Quick Transcribe accepts audio or video and returns TXT, SRT, or JSON without creating a session, source-library entry, or permanent artifact. The Home shortcut and **Quick Transcribe** navigation item open `/transcribe`. You can upload a file or record from your microphone, preview it locally, and then transcribe it.

Recognition uses the configured STT defaults, worker queue, and shared ASR resource lock. Selecting another output format retrieves the same recognition result. It does not rerun ASR. Microphone capture needs HTTPS or localhost; file uploads also work where microphone capture is unavailable.

## Lifetime and limits

- Sources: up to **256 MiB**, with at most **two hours** of audio. Browser recording stops at two hours.
- Uploads expire after one hour if not submitted. Submitted work has a 24-hour deadline; the normal queue and worker must be running.
- Completed results remain available for **one hour**. Failed/canceled work is also cleaned up. Working audio is removed after processing; original caller files are never deleted.
- Delete removes temporary files immediately when idle; running work is canceled before its files are removed. A cleanup loop runs every 30 seconds in the application process and on startup. Expired results are immediately inaccessible even before the next sweep.
- These operations use temporary disk storage. Compact job/idempotency metadata can remain, but transcript content is not written into generic job result history or captured job logs.
- Each result belongs to its authenticated principal. Another automation client's token cannot retrieve or cancel it. The owner browser sessions share the application's owner identity.
- Local versus cloud processing follows the configured/selected STT service. A quick operation does not change provider settings or silently select a different provider.

## One-request HTTP upload

The multipart endpoint accepts a `file` and an optional JSON-encoded `options` field. Supply a stable `Idempotency-Key` for the exact input/options; reuse it on connection failure.

```bash
curl -H "Authorization: Bearer $PANDRATOR_TOKEN" \
  -H 'Idempotency-Key: quick-note-20260908-001' \
  -F 'file=@note.m4a' \
  -F 'options={"format":"srt","language":"auto"}' \
  'http://localhost:8097/api/v1/transcriptions?wait_seconds=30&response=raw'
```

With `response=raw`, completed requests return the selected transcript with its content type. Otherwise the response is a JSON status envelope. If work is still pending, HTTP `202` returns an `id` and `job_id` to retrieve later. A terminal failure is reported in the envelope's `status` and `error`; it is not returned as transcript text. Waiting is bounded to 30 seconds and does not cancel unfinished work.

Options: `format` (`txt`, `srt`, `json`), `language`, `engine`, `model_quantization`, and `compute_backend` (`auto`, `cpu`, `cuda`, `vulkan`, `metal`). Omitted recognition options inherit the configured defaults. Arbitrary provider URLs, credentials, executable paths, and server filesystem paths are not request parameters.

## Resumable HTTP upload

1. `POST /api/v1/transcriptions` with JSON `{filename, size_bytes, sha256, ...options}` and `Idempotency-Key`. This allocates a temporary upload and returns `id`, `chunk_size`, and `next_chunk_index`.
2. `PUT /api/v1/transcriptions/{id}/chunks/{index}` with the binary chunk. Chunks are 8 MiB except the final remainder. Send them in order. An identical chunk replay is accepted; different bytes at an existing index return a conflict.
3. `POST /api/v1/transcriptions/{id}/start` with `{"wait_seconds":30}`. The server verifies the assembled SHA-256 and submits exactly one job. Repeated starts reuse it.
4. `GET /api/v1/transcriptions/{id}?wait_seconds=30&format=txt` retrieves status and, when small enough, an inline result.

Replaying the metadata request with the same idempotency key returns the current upload/job. Different input metadata with that key returns `409`. Reusing an expired/deleted operation before its idempotency retention ends returns `410`; use a new key for a new transcription.

`POST /api/v1/transcriptions/{id}/cancel` cancels processing. `DELETE /api/v1/transcriptions/{id}` cancels if necessary and deletes temporary data. Reads require `app.read`; the dedicated transcription mutations require `app.run`. Browser requests use the normal CSRF protection.

## Results

`GET /api/v1/transcriptions/{id}/result?format=srt` downloads the complete result. The download is authenticated and not publicly shareable.

The status envelope includes `result_available`, `inline_result`, `result_url`, `expires_at`, progress, and status. Inline content is limited to 32 KiB; a larger result is omitted in full, never silently truncated. Download it, or use:

```text
GET /api/v1/transcriptions/{id}/result?format=json&offset=0&limit=16000
```

This returns `{format, content, offset, total_chars, next_offset}`. Offsets count decoded Unicode characters. Follow `next_offset` until null and concatenate `content` to reconstruct the complete result. For JSON, the paged content is serialized JSON text, not a fragment of a parsed object.

TXT contains plain transcript text. SRT uses Pandrator's existing subtitle composer. JSON uses `pandrator.transcript.v1`, with normalized segments, available words/timestamps/speakers, language, actual engine/compute backend, and a plain `text` field. Missing metadata is not fabricated.

## MCP

`pandrator_transcribe` hides the upload/start sequence:

```json
{
  "source": {"kind":"local_file","root":"recordings","path":"meeting.m4a"},
  "format":"srt",
  "language":"auto",
  "wait_seconds":30,
  "idempotency_key":"meeting-transcription-001"
}
```

The path is relative to a configured named root **on the MCP host**. The MCP adapter opens and uploads that file; the Pandrator application may be on another machine. Existing named-root and symlink restrictions apply. Use `pandrator_browse_local_sources` to inspect configured roots.

For small clips, use `{"kind":"base64","filename":"note.webm","data":"..."}` as the source. Base64 input is limited to 8 MiB decoded. Prefer local files for larger sources so audio bytes need not pass through a language model's context.

Follow up with `pandrator_transcription_get`, `pandrator_transcription_result`, `pandrator_transcription_cancel`, or `pandrator_transcription_delete`. The result tool exposes bounded text pages. All calls use the normal MCP result envelope and the configured authenticated application target.

Live streaming captions, transcript editing, and promotion into a permanent session are outside this workflow's first version.
