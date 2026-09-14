# Editing and regenerating an active speech plan

Date: 14 September 2026
Base: 0e9732c9

## Reproduced problems

An editorial edit creates an immutable descendant of a plan already used by a generation run. Previously, only completed takes available at the instant of copying were retained. Stale takes were dropped by later copies, and subsequent worker results remained attached solely to the ancestor. A regeneration could therefore succeed while remaining invisible in the active mix. Regenerating a standalone targeted run again could fail because it had no original full-generation parent. The drawer also used the pre-edit ID in its save-and-regenerate path and could race a pending blur/save.

## Implementation

- Preserve both completed and stale artifact-backed takes through copies; never copy a source row's running state as a new live worker.
- Preserve immutable run inputs. Editing alone does not pause generation. Publish late results into the active editorial descendant, matching text, voice, language, source references and other input fields. Mismatched old wording remains stale rather than current. Actual split, merge, restore and unrelated branches stop propagation.
- Reconcile pre-fix missing links during explicit edit/generation mutations, not GET requests. Shared audio artifacts are linked rather than duplicated on disk.
- Keep scheduling ownership separate from output-plan ownership. Targeted regeneration on an edited descendant requests a checkpoint pause of its running ancestor, then resumes it without manual Pause/Resume. Explicit user pauses and cancellation remain authoritative. Canceling a queued replacement releases the temporary pause or transfers resume responsibility to another waiting replacement.
- Preserve the original requested segment IDs on both automatic and manual resume. Interrupting an independent targeted regeneration must not expand it into a whole-plan run.
- Guard take selection so an in-flight result does not replace a user's later explicit selection.
- Serialize drawer saves, resolve changed IDs after editorial copying, wait for pending saves before regeneration, and block generation after a failed save until it is corrected. Use the returned ID for save-and-regenerate.
- Add Regenerate all stale in the drawer header. Enumerate the whole pinned active plan, independent of viewport/search. Exclude removed and never-generated rows, reject revision changes, and never turn an empty result into a full run.
- Show a queued-replacement notice and an explicit error when a save or request fails.

## Verification

192 focused backend tests passed in two runs:

```
.pixi/envs/default/bin/python -m pytest -q tests/test_generation_edit_audio.py tests/test_web_generation_regeneration.py tests/test_web_generation_topology.py tests/test_session_source_plan_controls.py tests/test_generation_audio_identity.py tests/test_web_voiceover_repair.py tests/test_web_repair_history_deletion.py --tb=short
# 87 passed
.pixi/envs/default/bin/python -m pytest -q tests/test_web_job_concurrency.py tests/test_web_workflow_handlers.py tests/test_web_speech_planning.py tests/test_generation_run_history_projection.py tests/test_generation_plan_review.py --tb=short
# 105 passed
```

Svelte check: zero errors and zero warnings. Production frontend build succeeded. The seven focused browser/helper checks in `web/tests/generation-edit-regeneration.spec.ts` passed in Chromium and Firefox. Browser testing used the disposable test workspace, not the user's live session. Its unrelated mock Silero catalogue probe returned 404; tests passed. Backend runs emitted the existing Python audioop deprecation warning.

A final additional regression covers an interrupted two-block targeted output and verifies that its resume never synthesizes the unrequested third block. The 31-test edit/regeneration subset passed after this guard was added, bringing distinct backend coverage in these focused runs to 193 tests.

The tests simulate controlled synthesis to exercise in-flight edits, retained stale audio, compatible and incompatible late results, repeated standalone regeneration, temporary scheduling pauses, failure, cancellation, and explicit selections. They do not claim a new full-session TTS generation or listening assessment.
