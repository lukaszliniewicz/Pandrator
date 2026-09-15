# Automatic repair batches and Undo

Automatic voiceover timing repair is one user-level operation, even when it tries many splits. The normal **Speech plans** history and the speech-plan selector show one **Automatic timing repair** entry per originating generation run. Manual plan changes remain separate entries.

## Inspecting a batch

A batch shows its accepted and attempted counts and whether it is running, completed, partially repaired, stopped, failed, or unchanged. The displayed result is the last **accepted** repair, not a later rejected candidate. When no repair was accepted, the preview is the original plan. The selector does not offer an extra copy of that unchanged plan.

**View repair details** loads a bounded list of internal checkpoints. Their outcomes and reasons remain available, including rejected replacements and interrupted attempts. **Preview original** or a checkpoint preview does not change the active plan or audio. An explicitly selected intermediate checkpoint is retained in the grouped entry and clearly labelled.

The generation drawer's separate split/repair-history widget remains completely hidden by default. Display → Show split / repair history reveals it when needed.

## Undo automatic repairs

New repair attempts record a fingerprint of the original plan and selected audio. Each accepted result records its own fingerprint. When the final accepted result is still active and both snapshots still match, **Undo automatic repairs** restores the pre-repair plan and its selected audio as **one new revision**.

Undo does not delete the original or repaired plans, checkpoints, takes, or audio files. The restored plan preserves compatible original audio; assembled outputs become out of date until reassembled. This is a restoration of the batch's original state, not waveform slicing or automatic regeneration.

Undo is refused if session work is active, the active plan has changed, the repaired text/options or selected takes have changed, or the pre-repair snapshot no longer matches. In-place edits are detected even when the plan revision ID did not change. The check and restoration occur in one write transaction; replaying the same idempotency key does not create another restoration.

For older batches without verified snapshots, automatic Undo is unavailable. Their checkpoints and original plans remain inspectable, and the existing **Restore as a new active revision** action is available as an explicit alternative. No history is silently rewritten to invent missing snapshot evidence.

## Storage and compatibility

This release changes user-facing revision history and adds guarded batch restoration. It deliberately **retains the existing immutable internal candidate revisions**. It does not yet deduplicate full-plan database snapshots or delete historic candidate records. Audio files already reused between checkpoints remain reused.

The raw `/generation-plan/revisions` API stays compatible for diagnostics and existing clients. Grouped history is available separately; grouping happens before pagination, so a batch is not repeated across pages. Expensive block/audio-identity inspection is limited to the displayed representative revisions rather than all hidden checkpoints.

### API

- `GET /api/v1/sessions/{sessionId}/generation-plan/history`: grouped history, `limit` 1–100 and optional `before_revision_number`.
- `GET /api/v1/sessions/{sessionId}/generation-plan/repair-batches/{batchId}`: aggregate batch state and paginated checkpoint details.
- `POST /api/v1/sessions/{sessionId}/generation-plan/repair-batches/{batchId}/undo`: requires `expected_revision_id`, `expected_state_hash`, and an `Idempotency-Key` header. Authenticated write permission and browser CSRF protections apply. A stale or ineligible request returns 409 without restoring anything.

The response's `entry_id` is the stable user-level history key. `id` remains an actual plan revision suitable for preview. `history_revision_number` orders a batch using its latest attempt, while `revision_number` still identifies its actual representative revision. A batch whose explicitly selected checkpoint differs from its accepted result is marked `is_repair_checkpoint`.
