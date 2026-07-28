# Pandrator Manager

`pandrator-manager` is Pandrator's Qt-free, per-user installation, update,
repair, removal, and runtime control plane. It runs independently of the
Pandrator application, browser, and optional tray client, and exposes an
authenticated loopback API plus a standalone setup/recovery WebUI. Explicit
private-network and HTTPS-ingress profiles support headless servers and GPU
pods without changing the loopback-only default.

Version `0.9.0` is the first production Manager cutover. Its wheel, source
distribution, exact-wheel native bootstrap, project-signed handoff, and
preserve-data uninstall are qualified on Windows 11 and Fedora. The native
artifacts are the recommended consumer installation path; a PyPI upload is a
separate distribution decision.

The Linux build also produces Qt-free canonical and versioned AppImages,
`PandratorManager-x86_64.AppImage` and
`PandratorManager-0.9.0-x86_64.AppImage`, plus SHA-256 sidecars. The Manager
remembers the selected installation folder, supports loopback, private-LAN,
and explicit HTTPS-ingress recovery profiles, and can update itself without a
second manual executable download.

The Windows build produces
`PandratorManager-0.9.0-windows-x86_64.exe` and the deterministic
`pandrator-manager-0.9.0-windows-x86_64.zip` runtime bundle. The executable is
deliberately unsigned at the Windows publisher/Authenticode layer; project
release manifests use a separate Ed25519 signature.

## Safety and ownership

- Installing the wheel runs no build-time or install-time host mutations.
- One manager instance owns one explicit workspace.
- API discovery validates the full manager process identity and a protected
  per-workspace credential; browser code never receives that credential.
- Mutations require an immutable plan, explicit confirmations, idempotency,
  and a durable operation journal.
- Typed host preflight runs at planning and again immediately before mutation;
  it fails before changing the host when a required prerequisite is invalid.
- Shared Pixi bootstrap artifacts are platform-qualified, pinned by version
  and SHA-256, cached once, promoted atomically, and rollback-journalled.
- Component activation uses side-by-side slots. Ownership acquisition,
  ownership release, component state, configuration revision, and terminal
  success are committed atomically.
- Stops require a recorded process identity; removals require positive path
  ownership.
- User data is outside manager-owned runtime roots and is preserved by
  default.
- The tray is an optional client. Exiting it has no effect on the manager or
  child services.

Product release activation verifies threshold Ed25519 signatures, hashes,
target selectors, version/sequence monotonicity, and key-rotation boundaries.
This is project release-manifest signing, not Windows Authenticode, and it
does not require an EV certificate. Windows bootstrap executables remain
explicitly Authenticode-unsigned and can produce Unknown-publisher or
SmartScreen warnings.
The retained signing key's public half is embedded in the Manager. The private
half remains outside the repository. Automatically discovered and manually
selected manifests use the same fail-closed verification and are reverified
immediately before activation.

## Installation

For development from this checkout:

```bash
python -m pip install ./pandrator_manager
```

Optional tray support is separate:

```bash
python -m pip install "./pandrator_manager[tray]"
```

After a public release, the intended advanced-user forms are:

```bash
pipx install pandrator-manager
# or
uv tool install pandrator-manager
```

For a locally built wheel, replace `pandrator-manager` with the wheel path.
The native bootstrap remains the recommended consumer path because it does
not assume a suitable system Python and can install the stable external
launcher.

The supported Python range is 3.11–3.12. A normal package installation does
not enable autostart, launch a daemon, download a component, or request
elevation.

### Installation location

On its first interactive desktop launch, the native Windows or Linux launcher
opens the operating system's folder chooser. Select the **parent directory**;
the managed installation is created as `<selected directory>/Pandrator`.
Cancelling the chooser makes no installation changes.

The launcher stores the canonical parent directory in the user's platform
configuration directory and reuses it on later launcher, CLI, and optional
tray runs. The installed stable launcher also identifies its own workspace,
while autostart, manager handoff, and every component operation carry that
workspace explicitly. This prevents a later component installation from
silently returning to the home directory.

Use `--choose-workspace` to reopen the chooser, or select a location
non-interactively:

```bash
PandratorManager-x86_64.AppImage setup --workspace /path/to/parent
PandratorManager-0.9.0-windows-x86_64.exe setup --workspace D:\path\to\parent
```

`--workspace` takes precedence over `PANDRATOR_WORKSPACE`, which takes
precedence over launcher discovery and the remembered preference. Headless
deployments should pass one of those explicit values; `--no-open` never opens
a native dialog. The new launcher can also read the workspace preference
written by the feature-frozen Qt AppImage during migration.

## Basic use

```bash
pandrator-manager --workspace /path/to/workspace start-manager
pandrator-manager --workspace /path/to/workspace status
pandrator-manager --workspace /path/to/workspace list
pandrator-manager --workspace /path/to/workspace open --recovery
```

The mutation grammar is plan first, then execute the same action only when the
component reports it as supported:

```bash
pandrator-manager --workspace /path/to/workspace plan \
  --kind ACTION --component ID[:COMPUTE[:QUANTIZATION]]
pandrator-manager --workspace /path/to/workspace ACTION \
  --component ID[:COMPUTE[:QUANTIZATION]] --yes --wait
```

The first command is read-only. The action is available only when the
Manager's canonical component definition has a qualified recipe; see
[Current component coverage](#current-component-coverage).

Runtime and recovery examples:

```bash
pandrator-manager --workspace /path/to/workspace runtime-start tts.xtts
pandrator-manager --workspace /path/to/workspace operations --active
pandrator-manager --workspace /path/to/workspace cancel OPERATION_ID
pandrator-manager --workspace /path/to/workspace doctor
pandrator-manager --workspace /path/to/workspace autostart enable
```

Use `--json` for automation. The manager API and operation records are
versioned; clients should use stable component and service IDs rather than
persisting transient loopback ports.

## Headless and remote deployment

The native launcher can prepare exact workstation-facing URLs for both
setup/recovery and Pandrator:

```bash
pandrator-manager-launcher setup \
  --workspace /srv/pandrator \
  --remote-setup-url https://setup.example.test \
  --remote-pandrator-url https://pandrator.example.test \
  --trusted-proxy-hops 1 \
  --no-open
```

The command still prints a short-lived, one-use recovery URL when
`--no-open` is used. HTTPS mode expects an operated reverse proxy or ingress
and binds to `127.0.0.1` by default. For a pod ingress in another network
namespace, add `--network-bind-host 0.0.0.0` and restrict access with the
platform's network policy.

The URL token is only a bootstrap credential. After exchange, the browser
receives a revocable, HTTP-only authorization that survives manager restarts
and ordinary updates; only its SHA-256 digest is stored in manager state.
Loopback and HTTPS authorizations use a 30-day inactivity window and a 90-day
absolute limit. Trusted private HTTP offers a remembered-browser choice with a
7-day inactivity window and 30-day absolute limit, or a browser-session-only
authorization. Reloading the page renews CSRF state without requiring another
launch URL. The recovery header can sign out the current browser or forget all
authorized browsers.

For a trusted LAN or VPN without TLS, use exact
`http://server:port` URLs and `--allow-insecure-private-network`. The public
and listening ports must match, forwarded headers are not trusted, and an
owner password must be initialized locally because the manager will not
accept password submission over remote plain HTTP. `PANDRATOR_OWNER_PASSWORD`
is supported as a one-time deployment secret for first startup; it is
consumed from the environment and is not persisted or forwarded to backend
workers.

The remote setup manager controls only its own host and workspace; it is not a
multi-host controller. The normal Pandrator WebUI keeps host mutations local
by default. A deliberately administered single-owner deployment may set
`PANDRATOR_ALLOW_REMOTE_MANAGER_MUTATIONS=1`, while the recovery UI remains
the recommended remote installation and repair surface.

## Legacy import, releases, and uninstall

Inspect legacy state without changing it:

```bash
pandrator-manager --workspace /path/to/workspace legacy
pandrator-manager --workspace /path/to/workspace legacy-import --yes
```

The importer snapshots the positively identified legacy configuration,
reconciles known embedded data, preserves an existing web-migration marker and
session-signing secret, preserves unknown paths, and is idempotent.
The execute request is bound to the exact inspection digest and current state
revision; use `--json` to retain those details for automation.

Inspect accepted release slots or review a signed release:

```bash
pandrator-manager --workspace /path/to/workspace releases
pandrator-manager --workspace /path/to/workspace release-plan \
  --manifest /path/to/release-manifest.json
pandrator-manager --workspace /path/to/workspace release-update \
  --manifest /path/to/release-manifest.json --yes --wait
```

These commands verify the embedded trust policy, platform/architecture,
artifact length and SHA-256, release sequence, compatible manager version,
and key-rotation boundary. The production public trust root is compiled in;
tests and the lifecycle qualifier can inject an isolated ephemeral root only
at the application composition boundary. Callers cannot supply an ad-hoc
public key as authority.

Uninstall first emits a reviewable plan:

```bash
pandrator-manager --workspace /path/to/workspace uninstall
pandrator-manager --workspace /path/to/workspace uninstall \
  --preserve-data --yes --wait
pandrator-manager --workspace /path/to/workspace uninstall \
  --export-data /new/path/pandrator-data.zip --preserve-data --yes --wait
pandrator-manager --workspace /path/to/workspace uninstall \
  --purge-data --yes --wait
```

Preservation is the default. Export refuses to overwrite an existing
destination and verifies the ZIP before removal. Purge has a distinct
destructive confirmation. The external launcher performs final manager
cleanup after the daemon exits, including Windows paths beyond legacy
`MAX_PATH`; it deletes only operation-derived, positively validated residue.

## Surfaces

- The Pandrator WebUI exposes contextual provider installation and lifecycle
  actions while the manager remains the canonical state owner.
- The manager recovery WebUI works before Pandrator is installed and while it
  is stopped or unhealthy.
- The CLI provides recovery and automation parity.
- `pandrator-tray` provides convenience actions only and is never required.
- `pandrator-installer` is a temporary compatibility alias during migration.

The tray extra is never a daemon dependency. `pandrator-tray --check` reports
whether a usable desktop backend exists; on headless Linux it returns
`unavailable: No graphical desktop session is available.` without importing
an X11 backend or creating the requested workspace. Quitting or losing the
tray does not stop any managed process.

## Current component coverage

The Manager owns planning, source acquisition, immutable activation slots,
runtime tools, service validation, and removal for the released component
catalogue:

- Kokoro has a Manager-owned Pixi/bootstrap adapter, persistent model root,
  CPU/CUDA/ROCm/Metal selection, and an exact health contract.
- VoxCPM has a Manager-owned adapter that preserves the upstream dependency
  bootstrap while assigning its non-conflicting port and persistent
  model/voice/log roots. Its released recipe requires CUDA.
- CrispASR uses a pinned native asset and typed Whisper, Parakeet, and MOSS
  choices. MOSS diarization with Q8 precision is the default.
- Qwen3 TTS defaults to the 1.7B model. Automatic hardware choice is
  conservative: a ROCm utility alone is not evidence of GPU support, and
  legacy Polaris/Vega-class AMD adapters fall back to CPU.
- XTTS fine-tuning remains explicitly unavailable. Its training environment
  is not inferred from the separately qualified XTTS inference service.

Pandrator and source-backed engines update by cloning the configured HTTPS
repository, recording the exact retrieved commit, validating it in staging,
and activating it side by side. Manager binary updates use the separate
project-signed artifact channel.

## Build and qualification

Install build/test dependencies in an isolated Python 3.11 or 3.12
environment, then run:

```bash
python -m pytest -q tests/test_manager_*.py
ruff check --config pandrator_manager/pyproject.toml \
  pandrator_manager \
  scripts/build_manager_appimage.py \
  scripts/build_manager_bootstrap.py \
  scripts/build_manager_release_bundle.py \
  scripts/generate_manager_release_key.py \
  scripts/qualify_manager_lifecycle.py
python -m build pandrator_manager --outdir manager-dist
python scripts/build_manager_bootstrap.py --wheel-dir manager-dist
python scripts/build_manager_release_bundle.py \
  --output dist/pandrator-manager-0.9.0-windows-x86_64.zip  # Windows
python scripts/build_manager_appimage.py --wheel-dir manager-dist  # Linux
python scripts/qualify_manager_lifecycle.py
```

`build_manager_bootstrap.py` requires exactly one manager wheel in the
supplied directory, extracts it with traversal/link checks, directs
PyInstaller to that extracted package, verifies no manager module came from
the checkout, records both artifact and wheel SHA-256 values, and runs the
frozen self-check. PyInstaller caches are disposable. Windows output remains
explicitly Authenticode-unsigned.

On Linux, `build_manager_appimage.py` wraps that same wheel-fed native
bootstrap in desktop-friendly, Qt-free versioned and canonical AppImages and
writes adjacent SHA-256 checksums:

```bash
(cd dist && sha256sum --check PandratorManager-0.9.0-x86_64.AppImage.sha256)
APPIMAGE_EXTRACT_AND_RUN=1 \
  dist/PandratorManager-0.9.0-x86_64.AppImage self-check
```

The AppImage is only the distribution wrapper. First setup copies the native
bootstrap into the selected workspace, so manager startup and recovery do not
depend on the AppImage remaining mounted or on a graphical desktop session.
Interactive first setup uses `zenity`, `kdialog`, or `yad`, whichever the
desktop provides; explicit `--workspace` remains the portable server path.

`qualify_manager_lifecycle.py` creates a disposable workspace by default,
installs the frozen bootstrap, prepares a release with an ephemeral Ed25519
test key, performs the external manager handoff, verifies healthy takeover,
and executes preserve-data uninstall. It requires exact success, no cleanup
residue, no external control root, and only the data directory remaining.
Never pass a real workspace to this qualification command.

The release qualification record is maintained in
`INSTALLER_MANAGER_ARCHITECTURE.md`. The repository workflow
`.github/workflows/manager-ci.yml` repeats Manager tests on Python 3.11/3.12
and native package/lifecycle qualification on Windows and Linux. A release is
not published until the exact wheel, frozen bootstrap, Windows runtime ZIP,
Linux runtime ZIP, AppImage, signed manifest, checksums, browser UI, and
disposable lifecycle pass are recorded.

The source repository's
[`INSTALLER_MANAGER_ARCHITECTURE.md`](https://github.com/lukaszliniewicz/Pandrator/blob/main/INSTALLER_MANAGER_ARCHITECTURE.md)
defines the complete architecture, implementation phases, qualification
matrix, and Qt cutover gates.
