# Code quality policy

Pandrator uses Ruff for Python linting, basedpyright for types and import cycles,
and Vulture for high-confidence dead code. Tool versions are pinned in the
development dependencies and Pixi lockfile. Run the same checks as CI:

```bash
pixi install --locked
pixi run quality
```

The checks also have individual tasks: `lint`, `typecheck`, `dead-code`, and
`check-test-lanes`. They complement the test lanes; they do not replace tests.
The Python quality workflow and the existing cross-platform test workflow run
on changes to `main` and pull requests targeting it.

Python tests use explicit lanes in `scripts/test_lanes.py`; its manifest check
requires every test file exactly once. Keep Windows web lanes in separate CI
jobs: measured SQLite/filesystem test times are substantially higher there.
Use JUnit timings when rebalancing lanes, and preserve full coverage on each OS.
Windows browser projects run in two Playwright shards for the same reason.
Browser assertions allow 20 seconds on Windows (8 seconds elsewhere), because
native CI traces repeatedly show cold-page hydration exceeding the shorter
budget. The total test timeout remains 45 seconds and automatic retries are off.
Fixtures must dispose their database before deleting temporary workspaces;
Linux's ability to unlink open database files can conceal missing cleanup.

## Lint and formatting

Ruff checks production code, scripts, and tests with `E4`, `E7`, `E9`, `F`, `I`,
and `B`: syntax/name errors, imports, and likely bugs. The repository baseline
for these rules is clean. Manager and MCP inherit the root configuration.
Add rules deliberately after examining their findings; do not enable `ALL` or
apply unsafe fixes indiscriminately.

Keep formatting consistent with nearby Python code. Avoid whole-file formatting
churn during a behavioral fix. Python formatting is not yet a repository-wide
CI gate. Import bootstraps and other intentional exceptions require a narrow
rule-specific suppression with a reason.

## Types and existing debt

Basedpyright replaces the former ad hoc mypy tooling. The root configuration uses
`standard`, Python 3.11, and all platforms, matching the oldest supported Python
version while checking platform branches. It checks all four production Python
packages. Tests and maintenance scripts are linted and executed but are outside
the initial type-check scope. Import-cycle diagnostics and unnecessary-ignore
diagnostics are enabled explicitly. A static import cycle is a review signal;
it does not by itself prove a runtime import failure.

The committed `.basedpyright/baseline.json` records remaining legacy diagnostics.
A passing check means **no unrecorded diagnostics**, not that all legacy code is
type-correct. New code must not add debt. Prefer precise boundary types, local
type narrowing, and validated input over `Any`, unchecked casts, or ignores.
Keep a suppression only for a demonstrated checker or third-party typing issue,
with the diagnostic name and rationale.

Running `pixi run typecheck` locally removes resolved entries automatically.
Commit those reductions. In CI, basedpyright locks the baseline and fails for
new diagnostics or a stale baseline. Do not use `--writebaseline` to make a
feature pass. Expanding it requires a separately explained tooling migration or
verified relocation of an existing diagnostic, with the raw findings reviewed.
The matching mechanism uses diagnostic locations and is not a semantic proof;
review remains necessary.

Use the basedpyright editor extension with the project's selected Pixi Python
interpreter. Keep its server version aligned with the pinned CLI. Do not run
competing Python diagnostic engines with different rule sets and assume their
results are interchangeable.
The CLI task explicitly selects its running Python interpreter, so an unrelated
`.venv` in the checkout cannot silently supply a different set of dependencies.

## Dead code and frontend checks

Vulture fails on findings with 100% confidence across the production packages
and scripts. Lower-confidence findings need manual review because plugins,
callbacks, dynamic exports, and framework entry points may be used indirectly.
Never delete a public API solely because static analysis cannot find a caller.

The web client uses its locked Prettier, ESLint, Svelte/TypeScript, and Knip checks:

```bash
pixi run --environment web-build web-install
pixi run --environment web-build npm --prefix web run quality
pixi run --environment web-build web-build
```

Run the Svelte check and build sequentially: both write `.svelte-kit` metadata.
Then run relevant browser tests against the resulting production build.

For supporting tool behavior, see the [Ruff linter documentation](https://docs.astral.sh/ruff/linter/),
[basedpyright configuration](https://docs.basedpyright.com/latest/configuration/config-files/),
and [baseline behavior](https://docs.basedpyright.com/latest/benefits-over-pyright/baseline/).
