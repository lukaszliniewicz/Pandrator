# Install Pandrator

The native Pandrator Manager is the recommended installation path. It includes
its own runtime, creates one explicit workspace, and lets you add, update,
repair, or remove local speech and transcription components without requiring
Docker, WSL, or a suitable system Python.

## Choose the download

Open the [latest release](https://github.com/lukaszliniewicz/Pandrator/releases/latest).
Its **Which file should I download?** section identifies the current files.

| System | Asset pattern |
| --- | --- |
| Windows 10 or 11, 64-bit | `PandratorManager-…-windows-x86_64.exe` |
| Linux desktop, 64-bit | `PandratorManager-…-x86_64.AppImage` |
| Python 3.11 or 3.12, advanced use | `pandrator_manager-…-py3-none-any.whl` or `pipx install pandrator-manager` |
| MCP sidecar only | `pandrator_mcp-…-py3-none-any.whl` or `pipx install "pandrator-mcp[credential-stores,manager]"` |

Every release provides one `SHA256SUMS` file. On Linux, download it beside the
assets and verify a file with:

```bash
sha256sum --ignore-missing -c SHA256SUMS
```

On Windows, compare the release value with:

```powershell
Get-FileHash .\PandratorManager-*-windows-x86_64.exe -Algorithm SHA256
```

The Windows executable is not Authenticode-signed, so Windows may show
**Unknown publisher** or a SmartScreen warning. A matching release checksum
verifies the file you downloaded; it does not suppress that warning.

## Choose the installation location

The first interactive launch opens the operating system's folder chooser.
Select the **parent directory**. The Manager creates the managed installation
as:

```text
<selected parent>/Pandrator
```

For example, selecting `D:\Applications` creates
`D:\Applications\Pandrator`. Selecting `/srv` creates `/srv/Pandrator`.
Cancelling the chooser makes no installation changes.

The launcher remembers the canonical location. Selection precedence is:

1. the `--workspace` command-line option;
2. `PANDRATOR_WORKSPACE`;
3. the workspace identified by an installed launcher; and
4. the remembered preference.

Use `--choose-workspace` to reopen the chooser. Do not select the inner
`Pandrator` directory when the launcher asks for its parent again.

## Windows

Run the downloaded executable, choose the parent directory, and allow the
Manager to install Pandrator. The Manager opens its setup interface and then
the Pandrator browser interface. Under **Providers & services**, install only
the local engines you need.

The application, services, caches, and user data are placed inside the chosen
workspace, but user data is kept separate from replaceable runtime slots.
The Manager does not install operating-system packages. Calibre is optional
and needed only for MOBI conversion.

## Linux desktop

Make the AppImage executable and run it:

```bash
chmod +x PandratorManager-*-x86_64.AppImage
./PandratorManager-*-x86_64.AppImage
```

If AppImage mounting is unavailable:

```bash
APPIMAGE_EXTRACT_AND_RUN=1 ./PandratorManager-*-x86_64.AppImage
```

The same folder chooser and workspace rules apply as on Windows.

## Headless Linux

Pass the workspace explicitly and prevent browser launch:

```bash
./PandratorManager-*-x86_64.AppImage \
  setup --workspace /srv --no-open
```

The example creates `/srv/Pandrator`. A headless installation still needs
persistent Manager state and Pandrator data. Do not expose its loopback
listeners directly to the Internet. Follow the
[remote and headless guide](../operations/remote-and-headless.md) before
changing bind addresses or public origins.

## A useful first component set

You do not need every engine:

- **Kokoro** is a lightweight first choice for ready-made voices.
- **Qwen3 TTS Base** or **XTTS v2** is a practical starting point for voice
  cloning.
- **CrispASR** supplies local transcription engines, timestamps, and optional
  diarization.
- Add an LLM or translation provider only when you want its language work.
- Add **RVC** only when you want speech-to-speech voice conversion.

See [providers and voices](../guides/providers-and-voices.md) before choosing a
larger local model or compute backend.

## Update an existing installation

Download and run the newer Manager for your operating system. It reuses the
remembered workspace when possible; if asked, choose the same parent directory
as before. In Manager, use **Review updates** to prepare one reviewable plan for
available components, or review an individual component.

Projects and generated media are not runtime slots and are preserved through
ordinary updates. Keep an independent backup of important projects before a
major update. More detail is in
[updates, data, repair, and removal](../operations/updates-data-and-repair.md).

## Python and source installations

Python packages are intended for advanced installations and automation:

```bash
python -m pip install "pandrator[automation]"
pipx install pandrator-manager
pipx install "pandrator-mcp[credential-stores,manager]"
```

A Python package installation does not enable autostart, launch a daemon,
install a component, or request elevation. The native Manager remains the
simplest complete installation. Contributors should use the
[source-development guide](../development/from-source.md).

The `automation` extra places the MCP runtime in the Pandrator environment so
a current Manager can supervise the local HTTP service. It does not expose a
remote listener; remote agent workstations still install the standalone MCP
command shown above.
