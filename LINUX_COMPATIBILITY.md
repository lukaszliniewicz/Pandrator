# Linux Compatibility Review and Implementation Plan

Status: initial implementation pass plus Fedora SSH validation for installer import, Pixi bootstrap, and Pandrator core headless setup. Model server launch/install paths remain deferred for per-backend review.

Review date: 2026-07-05

## Scope

This document captures the first Linux compatibility pass for:

- the Pandrator installer and launcher (`pandrator_installer`, `pandrator_installer_launcher.py`)
- Pandrator itself (`main.py`, `pandrator/`)
- release and packaging scripts where they affect Linux support

The current README already presents the GUI installer as Windows-oriented. The code confirms that the installer and launcher are currently Windows-scoped, while the main Pandrator app is closer to portable when run from a writable source checkout with dependencies installed in a local environment.

## Summary

The main Linux blocker is the installer/launcher layer, not the core Qt app. The installer imports Windows-only modules at import time, downloads Windows executables, creates Windows-only Pixi manifests, discovers dependencies with Windows commands, and launches backend services through `.bat` or PowerShell scripts.

Pandrator itself should be smoke-testable on Linux after local environment setup, but it writes mutable state relative to the application/source directory. That is acceptable for a portable writable workspace, but not for a conventional read-only Linux install layout.

The Linux compatibility direction should be Pixi-first and portable-first: install runtime dependencies into Pandrator-managed Pixi environment(s) inside the workspace wherever possible. Host drivers, desktop/audio services, and device permissions are assumed prerequisites and are outside installer scope. Optional external tools such as Calibre can be detected and explained, but should not be installed by the Linux installer.

## Original Blocking Findings and Current Status

### 1. Installer import fails on Linux

Status: resolved in the initial implementation pass. `winreg` is now guarded, and Fedora self-check/import validation passes.

`pandrator_installer/operations.py` imports `winreg` unconditionally. `pandrator_installer/service.py` imports `OperationsMixin`, and `pandrator_installer/cli.py` imports `HeadlessInstaller`, so importing `pandrator_installer_launcher.py` on Linux will fail before any platform guard can run.

Affected paths:

- `pandrator_installer/operations.py`
- `pandrator_installer/service.py`
- `pandrator_installer/cli.py`
- `pandrator_installer_launcher.py`

Required direction:

- Move Windows-only imports behind platform checks.
- Keep the launcher importable on Linux even when Windows-only installer features are unavailable.
- Add import-only tests that simulate or run on non-Windows.

### 2. Pixi bootstrap is Windows-only

Status: resolved for x86_64 Fedora core bootstrap. Pixi binary name, download URL, temp suffix, executable permissions, and manifest platform are now platform-aware.

The installer pins:

- binary name: `pixi.exe`
- download URL: `pixi-x86_64-pc-windows-msvc.exe`
- temporary suffix: `.exe`
- manifest platform: `win-64`

Affected paths:

- `pandrator_installer/constants.py`
- `pandrator_installer/pixi.py`

Required direction:

- Add platform helpers for Pixi binary name, download URL, executable suffix, and manifest platform.
- Use `linux-64` for x86_64 Linux.
- Set executable permission after downloading Pixi on POSIX systems.
- Support an already-installed `pixi` from `PATH` as a fallback or override.

### 3. System dependency installation assumes Windows package managers

Status: partially resolved. Linux skips Windows-only installers and uses detection/guidance for optional Calibre. Windows package-manager behavior remains available on Windows.

The installer uses or references:

- `where`
- `winget`
- Chocolatey
- `msiexec`
- Windows Calibre MSI extraction
- eSpeak NG MSI and `libespeak-ng.dll`
- bundled Windows FFmpeg zip and `ffmpeg.exe`

Affected paths:

- `pandrator_installer/operations.py`
- `pandrator_installer/constants.py`

Required direction:

- Replace generic program detection with `shutil.which`.
- Prefer Pandrator-managed Pixi environments for CLI and Python dependencies such as `git`, `ffmpeg`, `pandoc`, Python packages, and eSpeak-related runtime packages where available.
- On Linux, detect optional external tools where useful, but do not manage host driver, desktop, audio, or permission setup.
- Treat Calibre as optional and MOBI-specific. The installer should not offer to install Calibre directly; it should only detect `ebook-convert`/Calibre and explain that MOBI import requires it.
- Do not try to install system packages automatically unless explicitly designed and approved.

### 4. Backend launch and readiness checks are Windows-script based

Status: intentionally deferred. Linux install/update now exits early for model-server components instead of entering unreviewed `.bat`/PowerShell bootstrap paths.

Most backend setup and launch paths expect `run.bat`, `run.ps1`, `cmd /c`, `powershell`, and `.pixi/envs/.../python.exe`.

Affected paths:

- `pandrator_installer/catalog.py`
- `pandrator_installer/components.py`
- `pandrator_installer/runtime.py`
- `pandrator_installer/storage.py`
- `scripts/benchmark_tts.py`

Examples:

- XTTS, VoxCPM, FishS2, and RVC launcher commands use `cmd /c run.bat`.
- Voxtral uses PowerShell.
- runtime readiness checks look for `python.exe`.
- component markers are `.bat`/`.ps1` files.

Required direction:

- Add platform-aware launcher script resolution:
  - Windows: `run.bat` or `run.ps1`
  - Linux/macOS: `run.sh` where upstream repos provide it
- Add platform-aware Pixi environment Python paths:
  - Windows: `.pixi/envs/<env>/python.exe`
  - POSIX: `.pixi/envs/<env>/bin/python`
- Update component markers to support alternative platform markers.
- Ensure scripts are executable on POSIX, or invoke them through `bash`.

### 5. Kokoro runtime environment uses Windows path separators

Status: resolved for the shared helper; `PYTHONPATH` now uses `os.pathsep`.

`get_kokoro_runtime_env` builds `PYTHONPATH` with `;`, which is wrong on Linux.

Affected path:

- `pandrator_installer/components.py`

Required direction:

- Use `os.pathsep` when joining `PYTHONPATH`.

### 6. Installer GUI has smaller Windows-only UI/actions

Status: partially resolved. Log opening now uses Qt's platform-neutral file opener; remaining copy/path examples should be handled during broader GUI Linux polish.

Examples:

- `os.startfile` is used directly for opening logs.
- path-space warning suggests `C:\Pandrator`.
- some messages refer to Windows certificates or Windows install paths.

Affected paths:

- `pandrator_installer/gui/actions.py`
- `pandrator_installer/gui/main_window.py`
- `pandrator_installer/components.py`

Required direction:

- Use `QDesktopServices.openUrl(QUrl.fromLocalFile(...))` or a small platform helper for opening files.
- Make path examples platform-specific.
- Keep Windows-specific troubleshooting text behind Windows branches.

## Pandrator App Findings

### Mostly portable areas

The main app already uses portable Python APIs for most local file work. It also has some platform-aware open-folder behavior:

- Windows: `os.startfile`
- macOS: `open`
- Linux: `xdg-open`

Core document/audio operations currently prefer system tools from `PATH`:

- `ebook-convert` for the legacy Calibre document conversion path
- `ffmpeg`

Required direction:

- Resolve tools from the active Pandrator Pixi environment first.
- Use explicit user overrides next.
- Use system `PATH` only as a fallback for optional external tools or developer/source-checkout use.

The Subdub and XTTS trainer helpers already check both `pixi.exe` and `pixi` in some places.

## Pixi-First Locality Principle

Linux support should keep the Pandrator install self-contained by default.

Dependency resolution order:

1. Executable or library inside the active Pandrator Pixi environment.
2. Explicit user override, for example `PANDRATOR_PIXI_EXE`, `PANDRATOR_HOME`, or a future tool-specific override.
3. Portable/bundled tool inside the Pandrator workspace.
4. System `PATH`, only for optional external tools or source-checkout fallback behavior.
5. Installer warning or documentation hint.

The Linux installer should not run `sudo`, `dnf`, `apt`, or similar system package managers. Windows support can keep its existing behavior until it is separately revised.

Assumed host prerequisites outside Linux installer scope:

- GPU kernel drivers and host driver stacks. CUDA/PyTorch runtime packages can be env-local, but the NVIDIA/AMD driver itself is not managed by Pandrator.
- Display and desktop session services such as X11, Wayland, portals, and desktop open handlers.
- Audio servers and host device access such as PipeWire, PulseAudio, ALSA devices, and user permissions.
- Base Linux shared libraries required by Qt or audio/video wheels when they are not fully supplied by Pixi packages. The installer may report a clear diagnostic, but should not install host libraries.
- Optional Calibre/`ebook-convert` for MOBI import.
- An existing system `pixi` or `git` may be used as a bootstrap fallback, but the target installer flow should work with a workspace-local Pixi binary and Pixi-provided tools after bootstrap.

### App runtime risks on Linux

Pandrator writes mutable state relative to the source/application root or current working directory:

- `logs/`
- `Outputs/`
- `pandrator_settings.json`
- `pandrator_state.sqlite3`
- `tts_voices/library/`

Affected paths:

- `main.py`
- `pandrator/logic/settings_handler.py`
- `pandrator/logic/session_handler.py`
- `pandrator/logic/state_db_handler.py`
- `pandrator/logic/voice_library_handler.py`

This works for a writable checkout. It is not suitable for `/opt`, `/usr`, AppImage internals, RPM-managed files, or other read-only app locations.

Required direction:

- Introduce an app data directory resolver.
- Prefer portable workspace locations for installer-managed/source-checkout installs.
- Support XDG locations for future distro/AppImage-style packaging:
  - config: `$XDG_CONFIG_HOME/pandrator` or `~/.config/pandrator`
  - state/data: `$XDG_STATE_HOME/pandrator` or `~/.local/state/pandrator`
  - cache: `$XDG_CACHE_HOME/pandrator` or `~/.cache/pandrator`
- Keep a compatibility mode for existing portable/source installs.
- Allow environment overrides, for example `PANDRATOR_HOME`, `PANDRATOR_OUTPUTS_DIR`, or similar.

### API key persistence is shell-specific

`pandrator/logic/config_handler.py` appends exports to `~/.bashrc` on Linux/macOS. That misses zsh/fish/desktop launchers and can duplicate values.

Required direction:

- Prefer storing provider configuration inside Pandrator settings, with API keys redacted in logs and backups.
- If environment persistence remains, make it opt-in and shell-aware.

## Implementation Plan

### Phase 1: Make installer importable and self-checkable on Linux

Goal: `python3 pandrator_installer_launcher.py --self-check` succeeds on Fedora.

Tasks:

- Guard `winreg` imports and all registry access.
- Guard `ctypes.windll` usage.
- Keep `OperationsMixin` importable on non-Windows.
- Replace direct `os.startfile` usage with a platform-neutral helper.
- Add tests for launcher import and self-check on non-Windows.
- Make `run_self_check` report platform capability status rather than assuming Windows.

Acceptance checks:

```bash
python3 -c "import pandrator_installer_launcher"
python3 pandrator_installer_launcher.py --self-check
```

### Phase 2: Add platform abstraction helpers

Goal: all installer code asks one small platform layer for OS-specific facts.

Tasks:

- Add a platform module, for example `pandrator_installer/platforms.py`.
- Provide helpers for:
  - `is_windows`
  - `is_linux`
  - Pixi platform string
  - Pixi binary name
  - Pixi download URL
  - executable suffix
  - environment Python path
  - shell script candidate names
  - open-file/open-folder behavior
- Replace inline `os.name`, hardcoded `.exe`, and hardcoded Windows paths where practical.

Acceptance checks:

```bash
python3 -m unittest tests.test_installer_architecture
python3 pandrator_installer_launcher.py --self-check
```

### Phase 3: Make Pixi setup platform-aware

Goal: headless Pandrator-core installation can create a workspace-local Linux Pixi runtime.

Tasks:

- Download the correct Pixi binary for Linux.
- Store the downloaded Pixi binary inside the Pandrator workspace, not in a system location.
- Use `pixi` instead of `pixi.exe` on POSIX.
- Write `platforms = ["linux-64"]` on x86_64 Linux.
- `chmod +x` downloaded POSIX binaries.
- Allow `PANDRATOR_PIXI_EXE` or a `pixi` executable from `PATH`.
- Create or reuse a Pandrator installer/runtime Pixi environment for common tools.
- Prefer Pixi-provided executables over system `PATH` for installer operations after bootstrap.
- Update tests that assert Windows-specific Pixi paths.

Acceptance checks:

```bash
python3 pandrator_installer_launcher.py --self-check
python3 pandrator_installer_launcher.py --headless-install --workspace /tmp/pandrator-linux --components ""
```

Expected:

- self-check reports the resolved Linux Pixi binary and manifest platform
- the headless install creates workspace-local `bin/pixi`
- generated manifests use `platforms = ["linux-64"]` on x86_64 Fedora
- no system package manager or backend model-server bootstrap is invoked

### Phase 4: Pixi-first dependency resolution and external guidance

Goal: Linux install flow installs or resolves dependencies inside Pandrator-managed Pixi environments first, and gives actionable messages only for optional external tools or out-of-scope host prerequisites.

Tasks:

- Replace `where` detection with a resolver that checks the active Pixi environment before falling back to `shutil.which`.
- Add helpers for resolving env-local executables, for example:
  - `git`
  - `ffmpeg`
  - `pandoc`
  - Python
  - eSpeak-related binaries/libraries where Pixi packages can provide them
- Install common CLI dependencies into Pixi where possible instead of requiring system packages.
- Detect `ebook-convert` or Calibre only as an optional MOBI-import capability.
- Skip Windows-only fallback installers on Linux.
- Do not run Linux system package managers automatically.
- Treat GPU driver, display server, audio service, and device permission setup as host prerequisites outside installer scope.
- Report missing host prerequisites only as diagnostics or troubleshooting notes after a failed runtime check.

If MOBI import is needed, add a separate hint rather than an automatic installer action:

```bash
sudo dnf install calibre
```

Acceptance checks:

```bash
python3 -m unittest tests.test_installer_architecture
```

### Phase 4A: Document ingestion workflow and MOBI boundary

Goal: replace Calibre as the general document conversion path while keeping MOBI available through explicit Calibre support.

Tasks:

- Add both `pandoc` and `pypandoc` to the Pandrator Pixi environment and use them for Pandoc-supported document formats such as DOCX and RTF.
- Resolve Pandoc from the Pixi environment first, for example by setting `PYPANDOC_PANDOC` or using a local executable resolver.
- Do not rely on `pypandoc` user-level downloads when Pandoc can be supplied by Pixi.
- Keep existing PDF and EPUB ingestion paths unchanged.
- Keep `.mobi` in the supported source file list, but make it depend on Calibre's `ebook-convert`.
- Do not build a bespoke MOBI parser in this phase.
- Search for `ebook-convert` in normal locations:
  - `PATH`
  - bundled/portable Calibre layouts used by Pandrator on Windows
  - Windows install paths such as `C:\Program Files\Calibre2`
  - Linux install paths such as `/usr/bin`, `/usr/local/bin`, and `/opt/calibre`
- Add an app dialog when a user selects a MOBI file explaining that MOBI import requires Calibre/`ebook-convert`.
- Update installer messaging to present Calibre as an optional MOBI requirement only. The installer should detect and hint, not install Calibre.
- Route text produced by Pandoc/Calibre into the existing marking and cleaning pipeline.

Acceptance checks:

```bash
python3 -m unittest tests.test_document_ingestion
```

Manual checks:

- DOCX and RTF import produce text and enter the marking/cleaning workflow.
- PDF and EPUB behavior is unchanged.
- MOBI import succeeds when `ebook-convert` is available.
- MOBI import shows a clear warning and fails cleanly when `ebook-convert` is unavailable.

### Phase 5: Backend script resolution

Goal: backend install/readiness/launch code can use Linux-compatible upstream scripts.

Tasks:

- Keep model server launch fixes deferred until each backend is reviewed individually.
- Do not mass-enable Linux model server launching just because a `run.sh` exists.
- Replace fixed markers like `repo/run.bat` with platform-aware marker lists.
- Add helper to resolve backend launch scripts.
- Use `bash run.sh` on Linux where upstream repos provide it.
- Replace `.pixi/envs/default/python.exe` checks with platform-aware env Python paths.
- Update:
  - XTTS
  - VoxCPM
  - FishS2
  - Voxtral
  - Chatterbox
  - Magpie
  - RVC
- Keep Kokoro and Silero direct Pixi/Python launch paths, but fix environment path separators.

Acceptance checks:

```bash
python3 pandrator_installer_launcher.py --headless-install --workspace /tmp/pandrator-linux --components kokoro_cpu
python3 pandrator_installer_launcher.py --headless-install --workspace /tmp/pandrator-linux --components silero
```

Then test one upstream `run.sh` backend at a time.

### Phase 6: Portable app data directory cleanup

Goal: Pandrator can run from a portable writable workspace by default, and from a read-only app directory with mutable data elsewhere when packaged that way.

Tasks:

- Add an app path resolver with explicit modes:
  - portable workspace mode for installer-managed/source-checkout installs
  - XDG mode for future distro/AppImage-style packaging
  - explicit override mode through `PANDRATOR_HOME` and related variables
- Move or optionally resolve:
  - settings
  - state DB
  - logs
  - sessions/output
  - voice library index/storage
- Preserve portable behavior for existing source-checkout users and installer-managed installs.
- Add migration or first-run fallback behavior for existing files in app root.

Acceptance checks:

```bash
PANDRATOR_HOME=/tmp/pandrator-home python3 main.py
```

Expected:

- app starts
- settings/state/logs are created under the configured Linux-compatible location
- no writes are required in the source tree except intentional portable workspace mode

### Phase 7: Packaging and release flow

Goal: Linux release artifacts are clearly separate from Windows `.exe` artifacts.

Tasks:

- Keep Windows PyInstaller `.exe` build flow intact.
- Add Linux packaging targets separately:
  - source tar/zip
  - optional AppImage or PyInstaller Linux binary
  - optional RPM later
- Update release scripts that assume `PandratorInstaller.exe`.
- Do not mix Windows and Linux backend bundles unless backend runtime layout is platform-specific.

Acceptance checks:

```bash
python3 scripts/build_release_packages.py --help
```

Linux-specific package commands should not require a Windows installer executable.

## Fedora SSH Validation

Validated on Fedora x86_64 on 2026-07-05:

- `pandrator_installer_launcher.py --self-check` passes and reports:
  - `platform=linux-x86_64`
  - `pixi=pixi`
  - `manifest=linux-64`
- focused installer architecture tests pass on Fedora.
- workspace-local Pixi download works and runs as `pixi 0.72.0`.
- generated Pixi manifests use `platforms = ["linux-64"]`.
- a core headless install with no backend components completed successfully in `/tmp/pandrator-core-probe`.
- the generated `pandrator_installer` Pixi environment imports Pandrator core through `QT_QPA_PLATFORM=offscreen`.
- selecting a deferred backend component, for example `kokoro_cpu`, exits early with a clear Linux deferral message instead of entering backend bootstrap code.

Model server installation and launch paths were not validated in this pass by design.

## Fedora SSH Validation Plan

Run these in increasing cost order.

### 1. Baseline platform info

```bash
ssh fedora 'uname -a; python3 --version; command -v pixi || true; command -v git || true; command -v ebook-convert || true'
```

### 2. Import checks

```bash
ssh fedora 'cd /path/to/Pandrator && python3 -c "import pandrator_installer_launcher"'
ssh fedora 'cd /path/to/Pandrator && python3 pandrator_installer_launcher.py --self-check'
```

Expected result after the first implementation pass: both commands pass.

### 3. Pixi bootstrap and installer environment

```bash
ssh fedora 'cd /path/to/Pandrator && python3 pandrator_installer_launcher.py --headless-install --workspace /tmp/pandrator-linux --components ""'
```

After Pixi bootstrap is platform-aware, validate the workspace-local Pixi binary and environment rather than a system venv:

```bash
ssh fedora '/tmp/pandrator-linux/Pandrator/bin/pixi --version'
ssh fedora '/tmp/pandrator-linux/Pandrator/bin/pixi run -e pandrator_installer python -c "import sys; print(sys.executable)"'
```

Temporary venv smoke tests are acceptable before Pixi bootstrap works, but they should not be treated as the target Linux install path.

### 4. Core app dependency environment

```bash
ssh fedora 'cd /tmp/pandrator-linux/Pandrator/Pandrator && ../bin/pixi run -e pandrator_installer python -c "import main; print(\"ok\")"'
```

Potential issue: `nemo_text_processing` may require `pynini`. The installer should continue handling this through Pixi/conda-forge rather than plain pip.

### 5. Headless app import smoke

```bash
ssh fedora 'cd /tmp/pandrator-linux/Pandrator/Pandrator && QT_QPA_PLATFORM=offscreen ../bin/pixi run -e pandrator_installer python -c "import main; from pandrator.app_logic import AppLogic; logic=AppLogic(); logic.shutdown(); print(\"ok\")"'
```

### 6. GUI smoke, if display forwarding is available

```bash
ssh -Y fedora 'cd /tmp/pandrator-linux/Pandrator/Pandrator && ../bin/pixi run -e pandrator_installer python main.py'
```

If no display is available, keep GUI testing to `QT_QPA_PLATFORM=offscreen` import/startup tests.

### 7. Installer headless flow after fixes

```bash
ssh fedora 'cd /path/to/Pandrator && python3 pandrator_installer_launcher.py --headless-install --workspace /tmp/pandrator-linux --components ""'
```

### 8. Backend-by-backend validation after platform-aware launchers

Start with lower-risk services:

```bash
ssh fedora 'cd /path/to/Pandrator && python3 pandrator_installer_launcher.py --headless-install --workspace /tmp/pandrator-linux --components silero'
ssh fedora 'cd /path/to/Pandrator && python3 pandrator_installer_launcher.py --headless-install --workspace /tmp/pandrator-linux --components kokoro_cpu'
```

Then test upstream script-based services one at a time after `run.sh` support lands.

## Suggested Test Additions

- Import `pandrator_installer_launcher` on non-Windows without PyQt.
- Assert platform helper returns expected Pixi names/platforms for Windows and Linux.
- Assert generated Pixi manifest uses `linux-64` on Linux.
- Assert executable resolution prefers Pixi env paths before system `PATH`.
- Assert Linux installer flow does not invoke `sudo`, `dnf`, `apt`, `winget`, Chocolatey, or MSI installers.
- Assert `check_program_installed` or its replacement uses `shutil.which` only as fallback behavior.
- Assert Pandoc document conversion uses the Pixi-provided `pandoc` executable.
- Assert MOBI import checks `ebook-convert` availability and reports the Calibre requirement without marking MOBI as unsupported.
- Assert installer copy treats Calibre as optional and MOBI-specific.
- Assert backend launch commands use `run.sh`/`bash` on Linux and `run.bat`/PowerShell on Windows.
- Assert Kokoro `PYTHONPATH` uses `os.pathsep`.
- Add app path resolver tests for portable workspace, explicit override, and XDG packaging modes.

## Open Questions

- Should Linux support target only source/headless installation first, or should the GUI installer also be supported on Linux?
- Which remaining dependencies cannot practically live in Pixi and must be documented as host prerequisites?
- Which Linux package format is desired first: source bundle, AppImage, PyInstaller binary, RPM, or no packaged binary?
- Should portable workspace mode remain the default for all installer-managed installs, with XDG reserved for distro/AppImage packaging?
- Which backend services are first-class Linux targets? The README mentions Linux scripts for several, but RVC and some launch sections still document Windows commands only.
