# Artifacts, selections, and revisions

Pandrator preserves intermediate and final products as artifacts. An artifact
has an ID, session, kind, role, state, size, and lineage. Model-facing list tools
return metadata only: they omit filesystem paths, arbitrary metadata, and
content.

Stages can produce multiple revisions. A session-stage selection identifies the
revision currently feeding downstream work. Selecting or restoring an older
upstream artifact can clear dependent selections so stale outputs are not
mistaken for current ones.

Use the workflow snapshot to understand current selections and stage status.
Use artifact metadata to identify candidates. Content, subtitle text, voice
samples, and source documents require purpose-specific bounded tools; do not
infer their content from filenames or paths.

Deletion, replacement, and selection changes are writes. Inspect revision and
impact first, then use an exact revision precondition so concurrent edits fail
with a conflict rather than overwriting newer work.

## Removing sessions and outputs

`pandrator_trash_session` requires the inspected session revision. This is
recoverable trash: files remain, active work blocks the change, and ordinary
session listings hide it. Use `pandrator_list_sessions(include_trashed=true)` and
`pandrator_restore_session` with the current revision to restore it.

For permanent cleanup, inspect a trashed session with
`pandrator_preview_session_deletion`. Proceed only when `can_purge` is true,
using the returned revision and impact token with
`pandrator_delete_session_permanently(confirm=true)`. The tool also requires
explicit confirmation; its `state` reports whether cleanup completed or failed.
`pandrator_get_session_trash_policy` and
`pandrator_update_session_trash_policy` manage automatic retention for future
trash events only. Setting `days=null` disables automatic cleanup.

`pandrator_delete_output` permanently removes one generated output file. Inspect
its session and artifact ID first. Active takes, reference samples, source uses,
active assemblies, and dependent work are protected by the native API. There is
no restore operation for the removed file. Repeating deletion returns not found;
inspect artifacts after an uncertain response. These deletion/restore tools do
not accept an idempotency key. They never imply deleting other sessions or files.
