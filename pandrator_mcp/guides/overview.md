# How Pandrator works

Pandrator is a browser-based production workspace for audiobooks, dubbing,
subtitles, and voiceover. A session is the durable unit of work. It records the
workflow kind, source and target languages, outcome choices, stage selections,
and revisioned settings.

Sources become artifacts. Each processing stage creates a new artifact rather
than silently replacing the old one. The selected artifact for a stage is the
input to downstream stages. This makes review, rollback, reruns, and provenance
possible.

Providers describe language-model and speech-service connections. Credentials
are saved through Pandrator's credential settings; they are not workflow
arguments and must never be pasted into an agent conversation.

Long-running operations are durable work items backed by Pandrator jobs. An
agent reads them through the payload-free `/api/v1/work` projection. The
projection contains state, progress, bounded result summaries, and redacted
events. Raw queue payloads are intentionally outside the MCP contract.

Pandrator Manager owns installation, updates, repair, component lifecycle, and
managed service processes. Normal remote agent access reaches Manager through
Pandrator's authenticated same-origin proxy. Direct Manager recovery is a
separate, optional HTTPS trust boundary with a different credential.

Consequential operations follow a review-first pattern:

1. inspect current state and revisions;
2. create an immutable plan;
3. show its exact effects, risks, and digest;
4. obtain explicit confirmation when required;
5. execute that exact plan with an idempotency key; and
6. return a durable work reference for monitoring or cancellation.
