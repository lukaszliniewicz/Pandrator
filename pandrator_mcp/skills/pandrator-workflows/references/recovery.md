# Admission, recovery and small receipts

## Follow one generation operation

Retain `generation_operation_id` from the generation or regeneration receipt.
When advertised, poll `pandrator_get_generation_operation(operation_id=...,
wait_seconds=60, response_mode="structured")`. Its stable ID follows preparation
and the committed synthesis job across reloads. Phase and state describe work;
`coverage.export_ready` describes whether every included block has available
selected audio. Coverage is computed at terminal synthesis, or independently
with `pandrator_get_generation_run_coverage`. It does not judge listening quality
or guarantee successful rendering. Event pages and full settings are for diagnosis.

`pandrator_cancel_work(work_type="job", work_id=<operation id>, ...)` also cancels
a validated child after preparation has completed. Keep the original root ID.
On older servers, poll `get_work` and follow `result.followup_work` to synthesis;
preparation success alone does not mean audio exists.

## Lost response or reload

An HTTP/MCP timeout does not prove rejection. Retain and reuse the same
idempotency key for the same request. Inspect its saved work handle, or list
recent work scoped to the session and `generation.admit`/`generation.run`, before
starting another attempt. Never generate a fresh key solely because a request
timed out. Browser admission keeps only the request digest, retry key and job ID
locally; it does not save source text or credential data there.

If preparation inputs changed, keep the returned conflict code/details, refresh
the preview, and use a new accepted selection/revision. A terminal, known failed
preparation may be retried after resolving its cause. An unknown result first
needs inspection. Cancellation of an admission also requests cancellation of
its committed child; inspect terminal states before dependent work.

## Durable permanent cleanup

After authorized trashing, call `pandrator_preview_session_deletion` with a stable
idempotency key. Its default `admission=true` returns inspection work promptly.
Poll it, then read `pandrator_get_session_purge_inspection(job_id=...)` for the
complete stored blockers, revision and impact token. Work status contains only a
small summary. If eligible and deletion is authorized, call
`pandrator_delete_session_permanently(confirm=true)` with those exact values and
a stable key, then poll the deletion work. Its handle and target ID remain
readable after the session is gone. Cancellation is unavailable after the journal
claims irreversible cleanup; journal recovery reuses the accepted identity.

Keys replay during the configured reservation retention window (24 hours by
default, at most 168 hours), including after target deletion. Beyond that window,
inspect the retained work/journal; do not infer indefinite key retention. The
legacy synchronous tools require explicit `admission=false` and may exceed a
host's deadline. Do not repeatedly run a full inspection while polling.

Pure compilation previews are read-only POST operations. A timeout reports
`safe_to_retry_read`; it is safe to repeat that preview. Writes with an unknown
outcome require inspection or exact replay with their existing key. A connection
failure does not authorize an unkeyed mutation retry.

## Provider failures

- **Quota / rate limit:** record the operation and returned retry hint. Native
  voice-creation quota is separate from stock synthesis. Do not loop on a quota
  failure or silently substitute a voice.
- **Credentials / permissions / scope:** inspect the resolved route and redacted
  origin; ask only for genuinely missing configuration. Browser authentication
  does not establish application API access.
- **Unsupported model / capability:** refresh the scoped catalogue and inspect
  the returned model contract. Preserve the user's requested model choice until
  a supported alternative is authorized.
- **Uncertain external creation:** keep its admission/attempt ID and inspect
  native or saved voice resources before retrying a write. Do not clear an
  uncertainty marker just to make the button work again.

Use bounded event pages and redacted diagnostics to explain the stage and
actionable cause. A provider failure should not hide a successfully admitted
job or imply that every provider operation is unavailable.

## Worker interruption and continuation

Inspect work state and its linked generation run. Use the existing resume or
continuation flow; preserve completed takes and accepted selections. A lost
worker lease cannot authorize a stale worker to commit new work. Do not delete
the session, voices, takes or exported files to recover a job. If existing work
is active, observe or cancel it within the user's scope before starting a
replacement.

Deliver files with a compact receipt: IDs/revisions, route/model/voice,
instructions/context summary, terminal state and output paths. Report remaining
provider/account blockers accurately. Listening quality belongs to the user;
ASR and acoustic inspection are optional diagnostics.
