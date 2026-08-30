# Troubleshooting

Start with the smallest layer that can explain the failure. Reinstalling
everything first destroys useful evidence and is an impressively expensive way
to discover that a port was occupied.

## First checks

1. Confirm the intended Manager workspace and Pandrator data root.
2. Reopen Manager and inspect active or recently failed operations.
3. Check free disk space and whether another process owns the required port.
4. Run the relevant readiness or `doctor` check.
5. Download diagnostics before repair or removal.
6. Retry only after identifying whether the preceding action is still running.

## Manager does not open Pandrator

- Confirm that the launcher discovered the expected workspace.
- In Manager, inspect Pandrator's installed state and runtime status.
- Start or repair Pandrator through a reviewable action rather than launching a
  Python module from an arbitrary environment.
- On Linux, try `APPIMAGE_EXTRACT_AND_RUN=1` if AppImage mounting fails.
- On a headless host, use `--no-open` and the printed URL instead of expecting a
  desktop browser.

If the local application is unavailable, the Manager recovery interface still
runs independently. Exact commands are in the
[Manager guide](../../pandrator_manager/README.md).

## A local service is unavailable

- Confirm that the component is installed for the selected compute variant.
- Review service status and readiness, not merely whether a process exists.
- Check GPU/backend compatibility and memory pressure.
- Repair an incomplete environment through Manager.
- Test a smaller model or CPU path where supported.
- Include the component ID, compute backend, quantization, and diagnostics in a
  support request.

Do not hand-edit a managed virtual environment or active runtime slot unless
you are debugging source code and are prepared to discard it.

## A job appears stuck

Closing the browser does not stop a durable job. Reopen the session and inspect
work status, progress events, and last error. Check whether the worker is
running with the same data root as the web process. Cancel only when the job
reports cancellation is supported.

For cloud providers, distinguish a slow request from provider authentication,
quota, timeout, and rate-limit failures. For local models, inspect service
readiness and resource use.

## Transcription or subtitles look wrong

- Set a known language instead of automatic detection when possible.
- Add supported vocabulary hints for names and domain terms.
- Inspect word alignment, diarization, VAD, and chunk seams separately.
- Compare the normalized transcription with the initial display-cue revision.
- Review speaker changes, overlaps, hard silences, fast cues, and media ends.
- Correct source-language recognition errors before translation.

The [subtitle pipeline reference](../reference/subtitle-pipeline.md) explains
which settings affect STT, cue composition, LLM batches, and speech blocks.

## Passive dispatch does not advance

- Inspect run state and active batch metadata.
- A current lease may belong to another worker or idempotent replay.
- Renew a slow batch before expiry; release it when abandoning work.
- Use only the stable cue IDs and actionable cues returned by claim.
- Repair validation errors under the still-valid lease rather than creating a
  new run.
- Treat source/output conflicts as evidence that a selected revision changed.
- If finalization is transiently incomplete, retry the same final submission
  with the same idempotency key.

See [passive dispatch](../guides/passive-dispatch.md) for the state model.

## MCP cannot connect

Run, in order:

```text
pandrator-mcp target list
pandrator-mcp target test TARGET
pandrator-mcp doctor --target TARGET
pandrator-mcp print-config
```

Check that the host launches the exact stdio command generated for the pinned
target and that no token or origin was pasted into model-visible arguments.
For remote targets, verify DNS zone, TLS, configured origin, identity pin, and
scope. An intentional rebuild requires owner verification and deliberate
re-enrollment; do not bypass an identity mismatch.

## Export fails or looks different

- Confirm the selected subtitle revision and selected audio take for every
  segment.
- Confirm the requested container supports the chosen codecs and tracks.
- Check FFmpeg availability and diagnostics.
- Inspect a small output in an external player before a long final render.
- For M4B, verify chapters, metadata, cover, and assembled audio first.
- For video, distinguish soft from burned subtitles and original, mixed, or
  replacement audio.

## Prepare a useful issue

Open a [GitHub issue](https://github.com/lukaszliniewicz/Pandrator/issues) with:

- operating system and architecture;
- Pandrator, Manager, and affected component versions;
- workspace topology: local, headless, LAN/VPN, or HTTPS;
- session kind and selected provider/model/backend;
- exact action and smallest reproducible sequence;
- expected and actual result; and
- the reviewed **Download diagnostics** archive when relevant.

Diagnostics exclude known credential stores and redact known sensitive fields,
but third-party logs may still contain media names, paths, or provider detail.
Inspect every archive before sharing it publicly.
