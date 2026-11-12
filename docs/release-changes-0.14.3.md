# Pandrator 0.14.3 changes

Changes since [0.14.2](release-notes-0.14.2.md). This maintenance release integrates and credits Patrick Nadeau's contributions in PRs [#121](https://github.com/lukaszliniewicz/Pandrator/pull/121), [#122](https://github.com/lukaszliniewicz/Pandrator/pull/122) and [#123](https://github.com/lukaszliniewicz/Pandrator/pull/123), followed by integration fixes and regression tests in [#125](https://github.com/lukaszliniewicz/Pandrator/pull/125). The original contribution commits retain their authorship.

## Generation state, heartbeats and cancellations

- Generation identity inspection avoids holding a SQLite writer lock during the opening check. Job heartbeats continue during inspection.
- Audio identity lookup avoids repeated plan/revision ORM queries, keeping query counts bounded for long generation jobs without relaxing per-segment authorisation.
- Cancellation reconciliation is atomic and prevents late terminal updates from overwriting a cancellation.
- ElevenLabs v4 WebSocket timeouts are clamped to prevent floating point overshoot.

## Cross-platform builds and testing

- Browser build inputs and generated text outputs use LF consistently. Checks detect CRLF and mixed line endings, including untracked generated assets, with fail-closed behaviour for Git errors within a checkout. Gitless source archives are still supported.
- Windows pytest temporary roots use short paths with ownership markers and restricted ACLs, cross-process root initialization protection, fail-safe cleanup, and carefully limited pruning. Local environments unable to establish a safe short root fall back to pytest's normal location.
- Language-scoped workflow readiness now waits for its initial asynchronous settings load, including protection against an older request marking a deactivated view ready.
- Regression coverage was expanded for generation ownership, cancellation races, build reproducibility, Windows temp-root safety and workflow readiness.

## Components

Pandrator **0.14.3**; Manager **0.9.31** and MCP **0.7.1** unchanged. Existing Manager binaries can be reused without altering their signed update metadata.
