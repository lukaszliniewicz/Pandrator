# Voice modes, speech preparation, and session Trash

Implemented the accepted proposal in this checkout. No installed managed slot
was activated, no production service was restarted, and no real session was
used as a deletion fixture.

## Behavior

- Audiobooks and voiceovers share an explicit single/multiple voice choice.
  Switching mode preserves cast and annotations and starts no LLM work.
  New sessions use strict single-voice semantics. Existing sessions and frozen
  historical runs keep legacy behavior until the mode is explicitly saved.
  Strict single mode ignores stored block and alternate-take voice assignments
  in snapshots, previews, rendering, and audio reuse checks.
- Speaker recognition, delivery direction, and explicitly combined analysis
  have separate saved model/guidance/context/batch preferences. Drafts retain
  input/configuration provenance and resolved native model identity. Automatic
  validation preserves words, explicit voice bindings, and protected source
  structure. Adoption rejects drafts based on superseded adopted annotations.
- Final-unit text optimization runs as a cancellable preparation job. It produces
  a new reviewable speech plan without starting synthesis. Failed, canceled, or
  stale work leaves the previous plan selected. Duplicate document optimization
  and rewriting beneath XML/adopted annotations are rejected, including legacy
  automatic/direct generation entry points. Analysis preference changes alone
  do not invalidate recorded audio.
- Trash now offers a preview and permanent deletion. Owned managed files are
  removed; shared library files and external originals are retained. A durable
  claim prevents restoration once cleanup starts and permits retries after
  partial failure. Active work, unsafe paths, and external dependencies block
  deletion before files are removed.
- Automatic Trash expiry is off by default. Enabling a day count schedules
  future moves to Trash; existing entries retain their deadlines. Disabling
  expiry pauses scheduled deletions. Already claimed cleanup still finishes.
  Startup and hourly sweeps catch up while the application is running.
- Matching API/OpenAPI and MCP tools expose voice setup, analysis purpose,
  deletion preview/confirmation, and retention policy. The old audiobook
  setup endpoint remains available for compatibility.

## Verification

Focused checks cover pass isolation and cumulative annotations, stale adoption,
preparation cancellation and input changes, legacy inline guards, strict and
legacy voice rendering, managed voice references, audio identity, shared-file
aliases, partial cleanup retries, stale restore/update races, stopped partial
runs, inclusive expiry deadlines, old Trash migration, and HTTP/MCP contracts.

The parent inspected the rendered workflow, independent pass settings, passive
speaker draft, audiobook/voiceover parity, voice-mode switching, Trash controls,
and the deletion preview on desktop and a narrow viewport. The actual deletion
operation was exercised through disposable backend/API fixtures, not through a
real user session. No real LLM or speech-provider inference was requested.

Checks used the repository Python environments, pinned Ruff 0.16.6, Basedpyright
with the existing debt baseline and import-cycle checking, Vulture, test-lane
ownership, Svelte/TypeScript checking, ESLint, Knip, and a production UI build.
Temporary-storage quota errors interrupted some later runs; affected checks
were rerun with fixtures outside `/tmp` and logs in disk-backed scratch storage.

Recorded pytest results (overlapping suites; not a unique-test total):

- Voice setup, audiobook compatibility, rendering, cast runtime, preview, and
  audio identity: **103 passed** in the final combined run.
- Initial preparation/analysis/purge/startup/fork/bundle integration: **87 passed**.
- Final purge/analysis/settings/foundation checks: **84 passed** initially;
  three fixture assumptions were corrected and **all 3 passed** on focused
  rerun. The separate HTTP deletion contract regression also passed.
- Preparation including the final legacy inline guards: **8 passed**.
- MCP voice setup, purge, performance, architecture, and server: **37 passed**.

Final commands included changed-file
`uvx --from ruff==0.16.6 ruff check`,
`python scripts/test_lanes.py check`, and `git diff --check`.
Basedpyright reported no new diagnostics against the existing baseline;
Svelte checking reported zero errors and warnings. ESLint, Knip, and
`npm run build` passed. Disposable browser server and tab were closed after
acceptance; the installed application was left running unchanged.

Specialists were configured as Luna/max for bounded research and voice/MCP
implementation, Sol/high for preparation/analysis/purge implementation, and
Astra/high for the deletion safety review. The parent owned architecture,
integration, UI, and acceptance. The review found and verified fixes for shared
path aliases and stale session revival after partial cleanup.
