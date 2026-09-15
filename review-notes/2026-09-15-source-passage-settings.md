# Configurable source logical passages

## Scope and defaults

Adds source-only passage assessment, global/session passage settings, read-only previews and explicit new-branch rebuilds. The numerical source defaults remain 60 minimum / 160 preferred / 20 sentence lookahead, with 650 ms ordinary cue joining and an 8,000 ms diagnostic span. These are natural-boundary preferences, not arbitrary forced cuts. Subtitle display defaults are 60 characters per line; stored overrides are preserved.

Ellipsis hesitations no longer count as complete source sentences. Bounded same-speaker repetitions and filler fragments can stay together. Legitimate short sentences, rhetorical repetition, speaker changes, long interruptions and uncertain timing remain protected. The shared target-speech classifier is unchanged. No claim of acoustic improvement is made.

## Preservation

Raw construction is pinned to its source revision/hash with settings and policy provenance. Saving settings does not rebuild accepted ledgers, correction/translation, speech plans or takes. Explicit rebuilds create separate, non-current, selectable source branches and remap copied cue/word identities. Source and settings guards are checked before construction. Idempotency uses the existing HTTP mechanism.

Legacy file-only subtitles without a materialized revision retain file-based translation instead of trying to attach an empty passage packet. Browser requests declare their JSON content type; settings revisions use If-Match. Rebuild guards are taken from the preview that was actually shown.

## Verification

- 180 backend/source-policy/dispatch/selection/passage-marker tests passed.
- 136 integration/settings/parameter/workflow/frontend-architecture tests passed, with the pre-existing help-popover architecture failure explicitly deselected. HEAD already contains window scroll/resize listeners in web/src/lib/help-popover.ts; it was left unchanged.
- 22 feature browser tests passed across Chromium and Firefox, including unmocked preview/rebuild and sparse override/reset API paths. Browser verification is repeated after final frontend bundling.
- Svelte/TypeScript check: zero errors and zero warnings; static build succeeded.
- No correction, translation, alignment or synthesis was rerun on user data. The Pascal baseline session and selected plan are preserved.

Test logs and local screenshots are in tmp/source-passage-release-20260915/ and tmp/source-passage-ui-20260915/. They are intentionally excluded from the commit. Runtime deployment uses a new local development slot with the old slot and pointer/database backups retained; this is not a signed product release.
