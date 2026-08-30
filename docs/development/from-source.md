# Develop Pandrator from source

Packaged Manager releases are the easiest way to use Pandrator. A source
checkout is for development, debugging, and deliberately managed deployments.
The repository uses Pixi to lock Python and Node tooling consistently.

## Install environments

Install [Pixi](https://pixi.sh/), clone the repository, and use the committed
lockfile:

```bash
git clone https://github.com/lukaszliniewicz/Pandrator.git
cd Pandrator
pixi install --locked
pixi install --environment web-build --locked
```

The default `dev` environment contains Python 3.11, editable workspace
packages, test and analysis tools. `web-build` supplies the locked Node.js
toolchain and uses `npm ci`; do not substitute a globally installed package
tree when validating a change.

## Build and run

Build the web client and start the application:

```bash
pixi run --environment web-build web-build
pixi run serve-web
```

Run the durable worker in another terminal with the same checkout and data
configuration:

```bash
pixi run run-worker
```

Closing the browser does not stop either process or an active job.

## Command-line interface

Use `pandrator --help` for session, workflow, provider, voice, export,
authentication, migration, and diagnostics commands. Add `--json` for stable
machine-readable output when supported.

Manager and MCP are separate workspace packages with their own entry points
and READMEs:

- [Pandrator Manager](../../pandrator_manager/README.md)
- [Pandrator MCP](../../pandrator_mcp/README.md)

## Test lanes

Validate lane ownership before running tests:

```bash
pixi run check-test-lanes
```

`test-fast` runs measured fast tests with file-affinity workers. `test-full`
is the authoritative serial Python suite. `test-profile` runs the serial suite
and reports the slowest tests.

Use focused tests while developing, then the lane appropriate to the changed
surface. Web changes also need the locked formatter, linter, Svelte check,
dead-code check, and production build. MCP changes should include real-stdio
and compatibility tests when protocol framing or metadata is affected.

Run the documentation checker for Markdown changes:

```bash
pixi run check-docs
```

## Generated contracts

`openapi.json`, `web/src/lib/api.generated.ts`, and bundled web static assets
are generated outputs. Update their source definitions and use the repository
tasks to regenerate them; do not hand-edit generated clients or hashed static
files. Verify regeneration is stable before committing.

## Python distributions

Build distributions in a clean temporary directory, run Twine metadata checks,
and use `scripts/audit_python_distributions.py` before publishing. The root
application, Manager, and MCP have separate version and package metadata.
Package-specific READMEs must remain beside their projects because build
metadata uses them.

## Before opening a pull request

- Inspect existing working-tree changes and avoid unrelated cleanup.
- Update tests and public documentation with behavior.
- Run focused checks plus the relevant authoritative lane.
- Regenerate committed API or web artifacts when required.
- Run `git diff --check` and the documentation checker.
- Report commands run and known limits; do not claim repository-wide
  cleanliness from a focused check.

Continue with [contributing](contributing.md).
