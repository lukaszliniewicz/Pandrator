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
