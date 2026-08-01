# Pandrator Manager

`pandrator-manager` is Pandrator's Qt-free, per-user installation, update,
repair, removal, and runtime control plane. It runs independently of the
Pandrator application, browser, and optional tray client, and exposes an
authenticated loopback API plus a standalone setup/recovery WebUI. Explicit
private-network and HTTPS-ingress profiles support headless servers and GPU
pods without changing the loopback-only default.

The current release is 0.9.9. For most users, the easiest installation is the
Windows executable or Linux AppImage on the
[Pandrator 0.7.0 release page](https://github.com/lukaszliniewicz/Pandrator/releases/tag/v.0.7.0).
These packages include their own runtime and do not require a suitable system
Python.

## Safety and ownership

- One Manager instance controls one explicit workspace.
- Install, update, repair, removal, and runtime changes show an exact plan
  before they run.
- The Manager checks disk space, paths, ports, downloads, and other
  prerequisites before changing the installation.
- Components are staged and validated before becoming active. A failed
  activation returns to the previous working slot.
- The Manager stops only processes it can identify and removes only paths it
  owns.
- User data is separate from replaceable runtimes and is preserved by default.
- The optional tray is only a client. Closing it does not stop the Manager,
  Pandrator, or a running service.
- Product updates are checked against project-signed manifests and artifact
  hashes.

The Windows executable is not Authenticode-signed and may produce an
**Unknown publisher** or SmartScreen warning. Verify it against the release's
single `SHA256SUMS` file when you download it.

## Installation

The native packages are the recommended installation:

- Windows:
  [PandratorManager-0.9.9-windows-x86_64.exe](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.7.0/PandratorManager-0.9.9-windows-x86_64.exe)
- Linux:
  [PandratorManager-0.9.9-x86_64.AppImage](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.7.0/PandratorManager-0.9.9-x86_64.AppImage)

If you already have Python 3.11 or 3.12, install the Manager as an isolated
command-line tool:

```bash
pipx install pandrator-manager
# or
uv tool install pandrator-manager
```

The tray dependencies are included so native and Python installations can use
the desktop tray without a separate extra. For a downloaded wheel, replace the
package name with its local path.

For development from this checkout:

```bash
python -m pip install -e ./pandrator_manager
```

A normal Python package installation does not enable autostart, launch a
daemon, download a component, or request elevation.

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
PandratorManager-0.9.9-windows-x86_64.exe setup --workspace D:\path\to\parent
```

`--workspace` takes precedence over `PANDRATOR_WORKSPACE`, which takes
precedence over launcher discovery and the remembered preference. Headless
deployments should pass one of those explicit values; `--no-open` never opens
a native dialog. The launcher can also reuse the workspace preference written
by older Pandrator installers.

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
Manager's component definition has a supported recipe; see
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

### MCP recovery enrollment

Pandrator MCP normally reaches Manager through the authenticated application
proxy. This keeps the permanent Manager client bearer on the managed host.

An operator who needs app-down remote diagnostics or recovery may expose the
Manager's separately bounded HTTPS recovery origin and enroll a distinct MCP
credential:

```bash
pandrator-mcp target add production \
  --mode external \
  --origin https://pandrator.example \
  --recovery-origin https://recovery.pandrator.example \
  --scope app.read \
  --scope manager.read \
  --recovery-scope manager.read \
  --recovery-scope manager.runtime

pandrator-mcp target login production
pandrator-mcp target pin production
pandrator-mcp target login production --manager-recovery
pandrator-mcp doctor --target production
```

Open the one-use Manager recovery URL first so the enrollment approval opens
in an authorized browser. The approval page shows the client, both canonical
origins, requested scopes, and expiry. The issued recovery credential is
audience- and instance-bound, expires after at most 30 days, is rate-limited
per client, and is stored directly in the workstation's OS keyring.

Direct automation is disabled unless Manager is configured with an exact
HTTPS recovery origin. It cannot use the permanent local bearer, change
network settings, access arbitrary files, or bypass native plans and
confirmations. Application and recovery clients can be revoked independently
through their owner-authorized client-administration APIs.

The local workspace owner can inspect and revoke recovery clients without
copying the permanent Manager bearer out of the host:

```bash
pandrator-manager --workspace /srv/pandrator automation-client list
pandrator-manager --workspace /srv/pandrator automation-client revoke CLIENT_ID --yes
```

The recovery browser offers the same owner operation. After server-side
revocation, delete the controller's separate keyring value with
`pandrator-mcp target logout production --manager-recovery --yes`.

For a pod, persist both the Pandrator data root and the Manager workspace/state
and restrict a cross-namespace `--network-bind-host 0.0.0.0` listener with
platform network policy. Losing either state volume changes the identity that
the MCP pins and requires deliberate re-enrollment.

The complete workstation, LAN, external-server, pod, and agent-host setup is in
the
[Pandrator MCP guide](https://github.com/lukaszliniewicz/Pandrator/tree/main/pandrator_mcp).

## Legacy import, releases, and uninstall

Inspect legacy state without changing it:

```bash
pandrator-manager --workspace /path/to/workspace legacy
pandrator-manager --workspace /path/to/workspace legacy-import --yes
```

The first command only inspects the older installation. Import keeps unknown
files in place, can be repeated safely, and refuses to proceed if the inspected
source changes before confirmation. Use `--json` for automation.

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
and signing-key rotation. Callers cannot replace the project's trusted key
with an arbitrary one.

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
destructive confirmation. The launcher performs final cleanup after the
Manager exits and removes only files owned by the selected installation.

## Surfaces

- The Pandrator WebUI shows provider installation and runtime actions where
  they are relevant.
- The manager recovery WebUI works before Pandrator is installed and while it
  is stopped or unhealthy.
- The CLI provides recovery and automation parity.
- `pandrator-tray` provides convenience actions only and is never required.

The tray extra is never a daemon dependency. `pandrator-tray --check` reports
whether a usable desktop backend exists; on headless Linux it returns
`unavailable: No graphical desktop session is available.` without importing
an X11 backend or creating the requested workspace. Quitting or losing the
tray does not stop any managed process.

## Current component coverage

The Manager installs, updates, starts, stops, repairs, and removes the current
component catalogue:

- Kokoro supports CPU, CUDA, ROCm on supported modern AMD hardware, and Metal.
- VoxCPM currently requires CUDA.
- CrispASR offers Whisper, Parakeet, and MOSS choices; MOSS diarization with Q8
  precision is the default.
- Qwen3 TTS defaults to the 1.7B model. Automatic hardware selection sends
  unsupported or older AMD hardware to the CPU path.
- XTTS inference is available, but XTTS fine-tuning is not currently offered
  by the Manager.

Pandrator and source-backed engines are downloaded into staging, checked, and
activated side by side. Manager updates use the project-signed release
channel.

## Getting help

Use [GitHub Issues](https://github.com/lukaszliniewicz/Pandrator/issues) for
problems or suggestions. After a failure, use **Download diagnostics** in the
Manager WebUI and attach the ZIP with the action you attempted. The bundle
excludes Manager databases, sessions, credential files, and raw environment
variables; it also redacts known sensitive fields and local paths. Review it
before sharing.

## License

Pandrator Manager is released under the
[MIT License](https://github.com/lukaszliniewicz/Pandrator/blob/main/LICENSE).
Third-party runtimes, services, and models retain their own licences and usage
conditions.
