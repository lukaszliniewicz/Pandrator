# Pandrator 0.14.4 changes

Changes since [0.14.3](release-notes-0.14.3.md). This maintenance release integrates and credits Patrick Nadeau's performance work in PRs [#126](https://github.com/lukaszliniewicz/Pandrator/pull/126), [#127](https://github.com/lukaszliniewicz/Pandrator/pull/127), [#128](https://github.com/lukaszliniewicz/Pandrator/pull/128), [#129](https://github.com/lukaszliniewicz/Pandrator/pull/129), [#132](https://github.com/lukaszliniewicz/Pandrator/pull/132), [#134](https://github.com/lukaszliniewicz/Pandrator/pull/134) and [#136](https://github.com/lukaszliniewicz/Pandrator/pull/136), measured on a 5,167-segment cast book. The original contribution commits retain their authorship.

## Long cast plans

- The audio identity pass reuses the request's or job's voice catalogue instead of rebuilding and deep-copying it for every cast span, and remembers proven cast pins and voice language support for the pass instead of working them out for every segment. Segment listings fell from 268-281 s to about 3 s, plan status and revision history from over 900 s to about 16 s, and generation admission from about 700 s to 60 s ([#126](https://github.com/lukaszliniewicz/Pandrator/pull/126)).
- A segment listing runs the identity pass only when its response includes reuse fields, and then freezes the cast for that page's segments only. Admission, the opening check, recast and plan history keep the whole-revision check ([#128](https://github.com/lukaszliniewicz/Pandrator/pull/128)).
- Starting generation on an edited copy of a long plan finds the takes to inherit with one SQL query instead of loading every segment and take of the revision and its ancestors, cutting about 1.7 s from admission's write transaction ([#136](https://github.com/lukaszliniewicz/Pandrator/pull/136)).
- Workflow generation start, regrouping and early-block repair freeze the speech snapshot and compute audio identities before taking the writer, then recheck under a short write transaction. A change in the meantime is reported as a conflict or as "Plan or audio selection changed" ([#131](https://github.com/lukaszliniewicz/Pandrator/pull/131)).

## Server responsiveness

- Requests whose client has disconnected are skipped while queued, and heavy reads stop at the next segment checkpoint of the identity pass and cast freeze. Queued changes still run to their commit ([#132](https://github.com/lukaszliniewicz/Pandrator/pull/132)).
- Bearer tokens are resolved in a read snapshot, as browser sessions already were. `last_used_at` is written at most once a minute per token, as a short conditional update that gives way when the writer is busy ([#129](https://github.com/lukaszliniewicz/Pandrator/pull/129)).
- With background maintenance running, the database checkpoints its WAL every second on its own connection; commits checkpoint inline only if the WAL reaches 10,000 pages. A request waiting on the audit group commit gets the same 250 ms budget as the leader and otherwise saves its event to the durable spool. This removes an intermittent server-wide stall of about 30 s ([#137](https://github.com/lukaszliniewicz/Pandrator/pull/137)).
- The WAL is truncated back to 8 MiB after a complete checkpoint, so it no longer keeps its high-water size after heavy reads ([#134](https://github.com/lukaszliniewicz/Pandrator/pull/134), landed as [#139](https://github.com/lukaszliniewicz/Pandrator/pull/139)).
- Successful status polls, event streams, static assets and the app shell are no longer written to the audit log, which they previously filled (84% of a local install's rows were Manager status polls). Failed requests of those kinds are still recorded ([#140](https://github.com/lukaszliniewicz/Pandrator/pull/140)).

## MCP

- `pandrator_get_speech_plan_status` and `pandrator_list_speech_plan_revisions` request summaries and fetch audio reuse for the active revision only, so they answer within the client's timeout on long plans. Other revisions report `audio_reuse_checked: false`; a slow or incomplete detail read becomes a warning ([#127](https://github.com/lukaszliniewicz/Pandrator/pull/127)). Restart or reconnect MCP clients after upgrading.

## Interface

- Quick Transcribe applies its saved defaults only to options you have not changed, and leaves options whose value is not yet known out of the upload so the server's saved defaults apply ([#138](https://github.com/lukaszliniewicz/Pandrator/pull/138)).
- Voice settings panels show "Loading settings…" while they load, and an error with Retry if loading fails ([#138](https://github.com/lukaszliniewicz/Pandrator/pull/138)).
- Reading-view segment actions stay open while the pointer crosses the gap between the text and the action bar ([#141](https://github.com/lukaszliniewicz/Pandrator/pull/141)).

## Build and tests

- The web API contract and static bundle were regenerated for 0.14.3 ([#130](https://github.com/lukaszliniewicz/Pandrator/pull/130), [#133](https://github.com/lukaszliniewicz/Pandrator/pull/133)), and several end-to-end tests now wait for late-loading panels before acting ([#133](https://github.com/lukaszliniewicz/Pandrator/pull/133), [#138](https://github.com/lukaszliniewicz/Pandrator/pull/138)).
- Three tests that failed by chance are fixed: the job queue test now expects the first waiter by the queue's own ordering, since waiters enqueued within one Windows clock tick share a timestamp; a leak check no longer reads the random request id; and the Output removal test holds back only the refresh that follows its own export request.
- The minimum supported waitress version is 3.0.1.

## Components

Pandrator **0.14.4** and MCP **0.7.2**; Manager **0.9.31** unchanged. Existing Manager binaries can be reused without altering their signed update metadata.
