[![Download for Windows (.exe)](https://img.shields.io/badge/Download_for_Windows-.exe-2563eb?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.10.1/PandratorManager-0.9.26-windows-x86_64.exe) [![Download for Linux (.AppImage)](https://img.shields.io/badge/Download_for_Linux-.AppImage-168572?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.10.1/PandratorManager-0.9.26-x86_64.AppImage)

**Pandrator 0.10.1 improves local transcription and fixes export after generated audio.**

Download the Windows `.exe` or Linux `.AppImage` above to install or update through Pandrator Manager. GitHub's **Source code** archives are for developers.

Includes **Pandrator 0.10.1**, **Manager 0.9.26**, and **MCP 0.5.0**. The audio.cpp runtime remains **0.8.1**.

- **Qwen3 ASR now runs through CrispASR 0.8.36**, with VAD-aware bounded chunking, verified on-demand GGUF downloads, and Qwen/Canary word alignment where supported.
- **Export after generation is fixed.** The selected completed generation run is assembled automatically when needed, with stricter run, settings, boundary, and artifact validation.
- **Transcription cleanup:** source-language settings are preserved more consistently, vocal-isolation jobs receive realistic time budgets, and Qwen timing output is validated before use.

Existing projects and generation history are retained. Manager 0.9.26 updates the CrispASR catalogue to 0.8.36.
