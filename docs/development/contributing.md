# Contributing

Focused bug fixes, features, tests, and documentation corrections are welcome.
The most useful contribution has a clear user outcome, a scoped diff, evidence
that it works, and an honest description of what was not tested.

## Before changing code

Search existing issues and inspect nearby source, tests, API contracts, and
documentation. Preserve unrelated working-tree changes and untracked files.
For a larger feature, describe the behavior, non-goals, data migration,
security boundary, and compatibility expectations before implementing it.

Do not change generated files without changing their source. Do not treat a
local model/provider assumption as universal; Pandrator supports several
compute and deployment paths.

## Code quality

- Support the Python versions declared by the affected package.
- Use Ruff for Python linting and formatting conventions already present in
  the component.
- Keep public interfaces typed and validate untrusted input at the boundary.
- Add migrations for durable schema changes; do not mutate an existing
  released migration.
- Preserve artifact lineage, revision selection, idempotency, and review-first
  plans when changing workflow or Manager behavior.
- Keep stdio stdout reserved for MCP protocol frames.
- Avoid broad refactors in the same change as a behavioral fix.

## Tests

Add focused regression or acceptance tests for changed behavior. Validate test
lane ownership with `pixi run check-test-lanes`; use `pixi run test-full` as
the authoritative serial Python suite before a broad release change. Run the
web checks and build for UI or generated-client work.

Tests should cover rejected input and failure state as well as the happy path,
especially for filesystem ownership, network policy, credentials, leases,
idempotency, migrations, and finalization.

## Documentation style

Public product docs belong under `docs/`; package-specific exact commands stay
in the relevant component README. Write for a user trying to complete a task:

- lead with the decision or outcome;
- explain data loss, external transfer, cost, and security implications;
- prefer stable names and the latest-release page to version-pinned filenames;
- link to one canonical detail instead of copying it;
- distinguish shipped behavior from proposals; and
- keep field evidence, qualification notes, and release notes out of the
  public documentation tree.

Run `pixi run check-docs` after changing Markdown. The checker validates local
paths and headings without making network requests.

## Pull requests

Include:

- the problem and user-visible outcome;
- important design or compatibility decisions;
- migration, security, or deployment implications;
- exact checks run and their results; and
- known limitations or untested platforms.

Keep commits reviewable and do not mix generated caches, local environments,
distribution output, model data, credentials, or personal field notes into the
patch.

## Reporting security problems

Do not place credentials, private media, voice samples, exploitable deployment
details, or unredacted diagnostics in a public issue. Use a private maintainer
contact or GitHub's private vulnerability-reporting mechanism when available.
For an ordinary bug, use [GitHub Issues](https://github.com/lukaszliniewicz/Pandrator/issues).

Development setup and checks are documented in
[development from source](from-source.md).
