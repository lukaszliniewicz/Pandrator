# Voice modes, speech preparation, and session retention

Status: architecture proposal for discussion, not an implementation or release.
Source baseline: `a504c08e`, inspected 2026-09-27. No live sessions were changed.

## Product decisions

Keep audiobook and voiceover as the existing workflow kinds. Add a consistent,
prominent voice-mode choice to both: one voice or multiple voices. These are
presets within a workflow, not new workflow kinds.

Separate three independent concerns:

| Concern | Purpose | Can change spoken words? |
| --- | --- | --- |
| Speech-text optimization | Pronunciation and speech-specific wording | Yes, within the selected preservation policy |
| Speaker identification and casting | Identify who speaks and assign voices | No |
| Delivery directions | Specify how an utterance should be performed | No |

Multiple voices must not require rewriting, emotional directions, or an LLM
call when usable speaker labels or manual assignments already exist. Delivery
directions must work in single-voice mode too. Source speaker IDs and fictional
characters remain distinct identities; an LLM must not invent acoustic identity
from transcript wording alone.

## Current UI and configuration evidence

- `web/src/lib/AudiobookCastCard.svelte` exposes single/multiple voice modes
  through the audiobook setup API.
- `web/src/lib/SessionWorkspace.svelte` renders that card only for audiobooks.
  Voiceovers expose casting through `SpeechPlanCard` and `PerformancePanel`.
- `web/src/lib/PerformancePanel.svelte` already has separate `casting_enabled`
  and `performance_enabled` controls. Its delivery-analysis request has its own
  model and instructions and produces a draft for adoption.
- Dialogue recognition lives in speech optimization settings. Choosing
  dialogue/speakers forces document-time optimization in the UI.
- `pandrator/web/audiobook_setup.py` currently makes multiple voices a bundle
  of casting enabled, document optimization enabled, speaker annotation, and
  annotation-only mode. The new contract should stop equating multiple voices
  with this particular preparation mechanism.
- `docs/reference/speech-optimization.md` documents standalone, generation-time,
  and passive optimization paths. Native standalone and generation-time paths
  share `tts_optimization_model` but have separate batch-size settings.
- `pandrator/web/generation_rendering.py:440-461` applies explicit segment voice
  bindings before checking `casting_enabled`. The existing off switch therefore
  does not promise strictly single-voice output.
- `pandrator/web/performance_plans.py:192-214` preserves the existing speaker
  structure during XML delivery analysis. A combined recognition/delivery call
  would require a new orchestration contract, not simply relabeling this pass.

## Proposed preparation contract

Use the existing revisioned speech-plan/artifact architecture. Avoid adding a
second plan engine or a parallel set of booleans that can contradict existing
settings. Expose voice mode through a shared configuration service; maintain
one authoritative value and adapt the existing audiobook API for compatibility.

The normal dependency order is:

1. Select accepted source/correction/translation text.
2. Apply optional speech-text optimization.
3. Preserve, manually assign, or identify speaker spans; review the cast.
4. Build synthesis units respecting speaker boundaries and voiceover timing.
5. Optionally add delivery directions to those units; review/adopt the result.
6. Compile and preview provider requests, then synthesize.

Speaker labels supplied by transcription or imported markup should survive
text transformations through stable identities and explicit mappings. A changed
wording or segmentation invalidates dependent span annotations unless a mapping
is validated. A changed cast invalidates affected compiled requests, without
forcing speaker recognition to run again. A changed delivery setting does not
force text optimization to run again.

Keep document and final-unit speech optimization as explicit execution choices.
For final-unit optimization, finish it during preparation before adopting
word-anchored delivery directions. Do not silently rewrite text underneath an
already reviewed annotation. If legacy generation-time execution remains, it
must validate/rebase dependencies or require new preparation before proceeding.

Recognition and delivery analysis each get independent model, prompt/guidance,
context, batching, and execution-mode settings, inheriting visible defaults when
unset. Every run records the resolved configuration and input revision.

Offer a combined recognition-and-delivery action only when both use compatible
inputs and execution settings and spoken wording is already fixed. One LLM
response may carry both results, but they remain separately validated and
reviewable. Combining calls is optional; it must not silently replace one pass's
model/settings with the other's. Text rewriting stays a separate operation in
the initial design.

Single-voice mode must have an explicit rendering contract: every utterance
uses the selected session voice. Retain existing cast assignments and speaker
annotations as inactive data. Any explicit segment/span voice overrides must
be reported and deactivated for this mode, rather than silently making it
multi-voice. Switching modes changes future plans, not historical takes or
running/resumable job snapshots.

For voiceovers, speaker turns remain inside their owning timing windows. Do
not introduce audiobook-style pauses, move subtitle cues, or merge across
different speakers merely to satisfy a preferred request size.

## Proposed UI

Show a shared Voices card near the start of voice setup. Audiobooks use the
labels “One narrator” / “Multiple voices”; voiceovers use “One voice” /
“Multiple voices.” Keep speech optimization and delivery directions as separate
optional steps. In multi-voice mode, expose the path “Speakers → Cast → Review.”

Show whether speakers came from the source, manual edits, or LLM analysis. Show
the effective model for each enabled LLM step and whether calls are combined.
Switching voice mode must preserve drafts and require saving/discarding pending
edits before applying the change.

## Session deletion and retention requirements

Currently, `pandrator/web/sessions.py:194-225` implements recoverable trash and
restore. `pandrator/web/maintenance.py:15-35` removes old setting histories,
job events, and temporary/log files; it does not purge trashed sessions.
`SourceAsset` and `SessionSource` in `pandrator/web/models.py:267-322` distinguish
reusable library sources from session attachments. Source/fork operations can
reuse a source asset, so cascading database deletion and recursive session-folder
deletion alone are insufficient. The legacy state database has separate trash
expiry metadata; this proposal targets the current web session API.

Provide both manual permanent deletion from Trash and optional automatic
retention. Use one purge service for UI, HTTP, MCP, and background cleanup.

- Default: keep sessions in Trash indefinitely. Offer “Delete after X days”
  with 30 days as a convenient suggested value, not an enabled default.
- Measure age from `trashed_at`, never last edit time. Show a concrete deletion
  date, or a reason automatic deletion is blocked.
- Enabling a policy applies to future trash actions by default. Applying it to
  existing Trash requires a preview of affected sessions; changing a setting
  must not unexpectedly wipe an existing backlog.
- Manual deletion offers a concrete preview of session-owned material, shared
  data retained, and blocking dependencies. Confirm the irreversible action in
  the product. The preview is advisory; execution rechecks current state.
- Purge only trashed sessions. Active/queued/paused/resumable operations and
  foreign references must be resolved explicitly or block deletion.
- Preserve shared source assets, voice-library assets, other sessions' files,
  externally supplied originals, and exports outside managed storage. Deleting
  a session removes its attachments, not the shared source library itself.
- Serialize restore versus purge and fence new job submissions once purge
  starts. A session cannot be restored after irreversible cleanup begins.
- Use a durable, retryable cleanup record for the database/filesystem boundary.
  Keep the deletion manifest until cleanup succeeds; do not report completion
  while owned files remain because of an error.
- Run automatic cleanup through the existing application maintenance lifecycle
  at startup and periodically while running. An offline app performs overdue
  cleanup after startup; it cannot promise deletion at an exact wall-clock time.
- Permanent deletion covers application-managed data, not independently retained
  backups. Keep any minimal deletion audit free of document/transcript content.

## Implementation boundaries and acceptance

Deliver shared voice-mode setup first, then separate preparation orchestration.
Keep deletion/retention as an independent change, with manual purge verified
before enabling the scheduled caller. Do not change default retention or erase
existing annotations during migration.

Voice acceptance must cover single/multiple voices crossed with directions
off/on, imported versus LLM/manual speakers, cast changes after preparation,
text edits invalidating annotations, distinct pass models, preserved voiceover
timing, and frozen resume behavior. Verify actual compiled provider requests,
not only UI controls. Parent verifies the rendered UI.

Purge acceptance must cover shared sources and fork references, active jobs,
restore races, cleanup retries after failure, managed-path boundaries, exact
retention cutoff, and upgrading with existing Trash. Use disposable sessions
and storage only; no real user session is an acceptance fixture.
