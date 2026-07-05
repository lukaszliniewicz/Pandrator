<p align="left">
  <img src="pandrator.png" alt="Icon" width="200" height="200"/>
</p>

# Pandrator: a multilingual GUI audiobook, subtitle and dubbing generator with voice cloning and translation
>[!TIP]
>**TL;DR:**
> - Pandrator is not an AI model itself, but a GUI framework for Text-to-Speech, subtitle and translation projects. It can generate audiobooks and subtitles/dubbing by leveraging several AI tools, custom workflows and algorithms. It has an installer and works on Windows out of the box. It is not necessary to set up WSL or Docker containers, though you may use it with any TTS API backend.
> - It supports a wide range of TTS models: Kokoro, Fish S2 Pro, Chatterbox, VoxCPM2, Voxtral, XTTSv2, Silero, OpenAI and Gemini, as well as custom TTS API servers.
> - When installing: if you don't have a GPU, choose Kokoro. If you want voice cloning, which Kokoro doesn't support by default, install RVC.
> - You can talk to me or share tips/workflows/ideas on the Discord server.
>
> [![](https://dcbadge.limes.pink/api/server/JZzHv3MnaV)](https://discord.gg/https://discord.gg/JZzHv3MnaV)



## About Pandrator

Pandrator aspires to be easy to use and install - it has a one-click installer and a graphical user interface. It is a tool designed to perform two tasks: 
- transform text, PDF (including see-through cropping), EPUB and SRT files into spoken audio in multiple languages based chiefly on open source software run locally, including preprocessing to make the generated speech sound as natural as possible by, among other things, splitting the text into paragraphs, sentences and smaller logical text blocks (clauses), which the TTS models can process with minimal artifacts. Each sentence can be regenerated if the first attempt is not satisfactory, including marking for regeneration using mouse or keyboard actions when listening back to the generation. Voice cloning is possible for models that support it, and text can be additionally preprocessed using LLMs (to remove OCR artifacts or spell out things that the TTS models struggle with, like Roman numerals and abbreviations, for example),
- generate dubbing either directly from a video file, including transcription (using [WhisperX](https://github.com/m-bain/whisperX)), or from an .srt file. It includes a complete workflow from a video file to a dubbed video file with subtitles - including translation using a variety of APIs and techniques to improve the quality of translation. [Subdub](https://github.com/lukaszliniewicz/Subdub), a companion app developed for this purpose, can also be used on its own. You can also correct or translate subtitles without generating audio. 

At the moment, Pandrator supports multiple TTS backends: [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) via [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI), [Fish Audio S2 Pro GGUF](https://huggingface.co/rodrigomt/s2-pro-gguf) via [fishs2-cpp-fastapi](https://github.com/lukaszliniewicz/fishs2-cpp-fastapi), [Chatterbox](https://huggingface.co/ResembleAI/chatterbox) via [chatterbox-fastapi](https://github.com/lukaszliniewicz/chatterbox-fastapi), [VoxCPM2](https://huggingface.co/openbmb/VoxCPM2) via [voxcpm_fastapi](https://github.com/lukaszliniewicz/voxcpm_fastapi), [Voxtral](https://huggingface.co/mistralai/Voxtral-4B-TTS-2603) via [voxtral-fastapi](https://github.com/lukaszliniewicz/voxtral-fastapi), [XTTS v2](https://huggingface.co/coqui/XTTS-v2) via the OpenAI-compatible [XTTS2 API server](https://github.com/lukaszliniewicz/xtts2_api), [Silero](https://github.com/snakers4/silero-models) via `silero-api-server`, and [Magpie](https://huggingface.co/nvidia/magpie_tts_multilingual_357m) via [magpie-fastapi](https://github.com/lukaszliniewicz/magpie-fastapi). It also supports commercial speech APIs and custom TTS endpoints, including OpenAI-compatible and common JSON APIs, plus optional post-processing via a dedicated [RVC Python API Service](https://github.com/lukaszliniewicz/rvc-python). For local LLM text preprocessing, Pandrator works well with OpenAI-compatible local servers such as LM Studio and Ollama-compatible endpoints.

## Supported Languages & Quality Characteristics

Speech quality, emotional expression, and voice variety differ significantly between languages for each backend depending on parameter size, training datasets, and model architecture.

### 1. Kokoro
* **Supported Languages:** English (en/en-gb), German (de), Spanish (es), French (fr), Hindi (hi), Italian (it), Japanese (ja), Portuguese (pt), and Chinese Simplified (zh-cn).
* **Quality & Performance:**
  * **English (US/UK) & Japanese:** *Excellent / High Quality.* They feature dedicated phoneme/prosody handling, resulting in exceptionally natural rhythm and expression, with a large catalog of voices.
  * **Other Languages (Spanish, French, German, Italian, Portuguese, Hindi, Chinese):** *Stable / Moderate Quality.* The voice catalog is significantly more limited (often only a few voices), and intonation may occasionally sound less natural due to smaller training datasets.
  * **Efficiency:** Natively extremely lightweight (82M parameters), making it the best option for running fast local TTS on CPU.

### 2. FishS2
* **Supported Languages:** Broad multilingual coverage including English (en), Spanish (es), French (fr), German (de), Italian (it), Portuguese (pt), Polish (pl), Turkish (tr), Russian (ru), Dutch (nl), Czech (cs), Arabic (ar), Chinese (zh-cn), Japanese (ja), Hungarian (hu), Korean (ko), and Hindi (hi).
* **Quality & Performance:**
  * **All Languages:** *High Quality.* Zero-shot voice cloning performs extremely naturally. Timber and prosody are well-preserved.
  * **Requirements:** CUDA GPU strongly recommended; CPU mode is generally too slow.

### 3. Chatterbox
* **Supported Languages:** English (en) natively, and 23 languages via the multilingual model: Arabic (ar), Chinese (zh), Danish (da), Dutch (nl), English (en), Finnish (fi), French (fr), German (de), Greek (el), Hebrew (he), Hindi (hi), Italian (it), Japanese (ja), Korean (ko), Malay (ms), Norwegian (no), Polish (pl), Portuguese (pt), Russian (ru), Spanish (es), Swahili (sw), Swedish (sv), and Turkish (tr).
* **Quality & Performance:**
  * **English:** *Excellent.* Best performance and lowest latency are achieved using `chatterbox-en` / `chatterbox-turbo` models.
  * **Other Languages:** *Good.* The multilingual model is versatile and supports zero-shot voice cloning, though minor "accent bleed" or phonetic errors may occur. Single Language Packs (Brazilian Portuguese, Latam/Spain Spanish, Chinese, Hindi) provide higher dialect stability.

### 4. VoxCPM2
* **Supported Languages:** Supports 30 languages without language tag input: Arabic, Burmese, Chinese, Danish, Dutch, English, Finnish, French, German, Greek, Hebrew, Hindi, Indonesian, Italian, Japanese, Khmer, Korean, Lao, Malay, Norwegian, Polish, Portuguese, Russian, Spanish, Swahili, Swedish, Tagalog, Thai, Turkish, and Vietnamese. Also natively supports 9 Chinese dialects (Cantonese, Wu, Sichuanese, Wu, Northeast Mandarin, Henan, Shaanxi, Shandong, Tianjin, and Minnan).
* **Quality & Performance:**
  * **All Languages:** *Studio Quality.* Outputs 48kHz high-fidelity audio (upscaled from 16kHz references using asymmetric AudioVAE). Captures natural speech rhythms, breathing, and micro-pauses without metallic artifacts.
  * **Speaker Similarity:** Exceptionally high zero-shot cloning similarity, though intonation can sometimes feel slightly flat/repetitive in cloning modes.

### 5. Voxtral
* **Supported Languages:** English, French, German, Spanish, Dutch, Portuguese, Italian, Hindi, and Arabic.
* **Quality & Performance:**
  * **All Languages:** *Frontier Quality.* Based on a 4B parameter model, providing extremely expressive, high-naturalness speech that rivals proprietary services. Zero-shot cloning is outstanding on the cloud API, but the open-weights release lacks the encoder weights. As a result, the local open-source server is limited to using the provided preset voices.
  * **Requirements:** GPU only (4GB+ VRAM minimum, 8GB+ recommended).

### 6. XTTSv2
* **Supported Languages:** English, Spanish, French, German, Italian, Portuguese, Polish, Turkish, Russian, Dutch, Czech, Arabic, Chinese, Japanese, Hungarian, Korean, and Hindi.
* **Quality & Performance:**
  * **All Languages:** *Good / High Quality Cloning.* Zero-shot voice cloning is highly flexible from short clips.
  * **Limitations:** Prone to speed drift, mumbling, and phonetic artifacts if the reference audio contains background noise or is too short.

### 7. Silero
* **Supported Languages:** English, German, Russian, Spanish, French, Hindi, Tatar, Ukrainian, Uzbek, and Kalmyk.
* **Quality & Performance:**
  * **All Languages:** *Legible / Robotic.* Highly optimized for CPU and low-resource devices, but the voice sounds dated, flat, and robotic compared to modern diffusion/neural architectures.

### 8. Magpie
* **Supported Languages:** English (en-US), Spanish (es-US), German (de-DE), French (fr-FR), Italian (it-IT), Chinese (zh-CN), Vietnamese (vi-VN), Hindi (hi-IN), and Japanese (ja-JP).
* **Quality & Performance:**
  * **All Languages:** *High Quality.* A compact 357M parameter model developed by NVIDIA that maintains a consistent voice identity (timbre) across different languages. High-fidelity output via NanoCodec with robust alignment.
  * **English:** Features 5 built-in voices (Aria, Sofia, Jason, Leo, John Van Stan) with emotional styling (Angry, Calm, Happy, Neutral, Sad, Fearful).
  * **Limitations:** Requires ~1.4 GB VRAM for GPU (also has CPU fallback). Occasionally experiences minor audio duplications or glitches at the end of sentences.

## Requirements

### Hardware Requirements

| TTS Model       | CPU Requirements                                              | GPU Requirements                                                       |
|------------|---------------------------------------------------------------|-------------------------------------------------------------------------|
| Kokoro     | Works well on modern CPUs; install includes direct eSpeak setup on Windows    | Optional (CPU path is supported)                                         |
| FishS2     | CPU mode exists but is generally too slow for practical long-form usage       | NVIDIA GPU strongly recommended (8GB+ VRAM practical target)             |
| Chatterbox | Supported via CPU mode, but notably slower than GPU           | NVIDIA GPU recommended (4GB+ VRAM); GPU-only for the multilingual model  |
| VoxCPM2    | N/A (GPU-only in current wrapper)                            | NVIDIA GPU required (8GB+ VRAM recommended)                             |
| Voxtral    | N/A (GPU-only backend in current wrapper)                    | NVIDIA GPU required (4GB+ VRAM practical minimum)                       |
| XTTSv2     | A reasonably modern CPU with 4+ cores (for CPU-only generation)              | NVIDIA GPU with 4GB+ of VRAM for good performance                        |
| Silero     | Performs well on most CPUs regardless of core count                   | N/A                                                                     |
| Magpie     | Supported via CPU mode, but notably slower                    | NVIDIA GPU recommended (~1.4GB VRAM required)                           |
| RVC        | Supported via CPU mode (slower)                               | NVIDIA GPU recommended for fast audio post-processing                    |

### Dependencies
This project relies on several APIs and services (running locally) and libraries, notably:

#### Required
- One or more local/remote TTS endpoints:
  - [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) (OpenAI-compatible Kokoro server)
  - [fishs2-cpp-fastapi](https://github.com/lukaszliniewicz/fishs2-cpp-fastapi) (OpenAI-compatible Fish S2 server)
  - [chatterbox-fastapi](https://github.com/lukaszliniewicz/chatterbox-fastapi) (OpenAI-compatible Chatterbox server)
  - [voxcpm_fastapi](https://github.com/lukaszliniewicz/voxcpm_fastapi) (OpenAI-compatible VoxCPM2 server)
  - [voxtral-fastapi](https://github.com/lukaszliniewicz/voxtral-fastapi) (OpenAI-compatible Voxtral server)
  - [XTTS2 API](https://github.com/lukaszliniewicz/xtts2_api) (OpenAI-compatible XTTS v2 server)
  - [silero-api-server](https://pypi.org/project/silero-api-server/) (Silero backend)
  - [magpie-fastapi](https://github.com/lukaszliniewicz/magpie-fastapi) (OpenAI-compatible Magpie server)
  - Commercial speech APIs and custom TTS endpoints
- [FFmpeg](https://github.com/FFmpeg/FFmpeg) for audio encoding.
- [NeMo Text Processing](https://github.com/NVIDIA/NeMo-text-processing) for deterministic written-to-spoken text normalization.
- [wtpsplit-lite](https://github.com/superlinear-ai/wtpsplit-lite) for punctuation-agnostic sentence boundary detection using SaT models.
- [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) for the PDF OCR pipeline (using ONNX PP-OCRv6/v5 models).
- [Sentence Splitter by mediacloud](https://github.com/mediacloud/sentence-splitter), [PyQt6](https://pypi.org/project/PyQt6/), [num2words by savoirfairelinux](https://github.com/savoirfairelinux/num2words), and others listed in `requirements.txt`.

For local OpenAI-compatible TTS wrappers used by Pandrator, the preferred ecosystem schema is:
- `POST /v1/audio/speech`
- `GET /v1/models`
- `GET /v1/audio/voices` (preferred voice catalog) with legacy `GET /v1/voices` support during migration
- `POST /v1/audio/voices` for cloning-capable backends (XTTS, FishS2), with legacy `/v1/files` fallback

#### Optional
- [Subdub](https://github.com/lukaszliniewicz/Subdub), a command line app that transcribes video files, translates subtitles and synchronises the generated speech with the video, made specially for Pandrator.
- [WhisperX by m-bain](https://github.com/m-bain/whisperX), an enhanced implementation of OpenAI's Whisper model with improved alignment, used for dubbing and XTTS training. 
- [Easy XTTS Trainer](https://github.com/lukaszliniewicz/easy_xtts_trainer), a command line app that enables XTTS fine-tuning using one or more audio files, made specially for Pandrator.
- [RVC Python API Service](https://github.com/lukaszliniewicz/rvc-python) for enhancing voice quality and cloning results via a dedicated local [Retrieval Based Voice Conversion](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI) service.
- [PyCropPDF](https://github.com/lukaszliniewicz/PyCropPDF) for manual PDF cropping/cleanup before ingestion.
- A local OpenAI-compatible LLM endpoint (for example LM Studio, Ollama-compatible endpoints, or other compatible providers) for LLM-based text pre-processing.

## Installation

### Self-contained packages
I've prepared packages (archives) that you can simply unpack - everything is preconfigured locally so you can launch quickly. You can download them from **[here](https://1drv.ms/f/s!AgSiDu9lV3iMnPFKPO5BB_c72OLjtQ?e=sLidui)**.

You can use the launcher to start Pandrator, update it and install new features. 

| Package | Contents | Unpacked Size | 
|---------|----------|---------------|
| 1       | Pandrator + Kokoro | Varies |
| 2       | Pandrator + XTTS + WhisperX + XTTS fine-tuning + RVC | Varies |
| 3       | Pandrator + Voxtral | Varies |
| 4       | Pandrator + Voxtral + XTTS + WhisperX + XTTS fine-tuning + RVC | Varies |

### Maintainer workflow: building the package zips

`scripts/build_release_packages.py` automates archive generation and keeps a reusable local block cache so you do not need to re-download/re-bootstrap every stack for each zip.

By default it creates/uses `package_release/` and runs all cache/staging/output work from that directory.

The script now supports two workflows.

1) Fully automated source preparation (recommended):

```powershell
python scripts/build_release_packages.py --prepare-sources --sources-root "D:/pandrator-builds/sources" --installer-exe "dist/PandratorInstaller.exe"
```

Kokoro-only build (prepare + package):

```powershell
python scripts/build_release_packages.py --prepare-sources --only kokoro --installer-exe "dist/PandratorInstaller.exe"
```

This runs `pandrator_installer_launcher.py` in headless mode to prepare/reuse 4 source installs under `--sources-root`:

- `core` (base runtime),
- `stack` (XTTS + WhisperX + XTTS fine-tuning + RVC),
- `kokoro`,
- `voxtral`.

2) Manual source paths (if you already manage source installs yourself):

```powershell
python scripts/build_release_packages.py --core-source "D:/pandrator-builds/core/Pandrator" --stack-source "D:/pandrator-builds/xtts-rvc/Pandrator" --kokoro-source "D:/pandrator-builds/kokoro/Pandrator" --voxtral-source "D:/pandrator-builds/voxtral/Pandrator" --installer-exe "dist/PandratorInstaller.exe"
```

What it does:

- reuses cached blocks in `.release_blocks/` and only refreshes changed inputs,
- assembles each package in `.release_staging/`,
- writes final archives to `release_packages/`,
- includes both `PandratorInstaller.exe` (or the path passed with `--installer-exe`) and the `Pandrator/` folder in every zip.

Those paths are inside `package_release/` unless `--release-root` is changed.

Useful flags:

- `--force-refresh` to rebuild all cached blocks,
- `--release-root` to change the working root directory,
- `--output-dir` (or `-o`) to choose where zip archives are written,
- `--only` to build only selected packages (for example `--only kokoro`),
- `--skip-voxtral-with-rest` to skip the combined Voxtral + XTTS/WhisperX/RVC package,
- `--no-hardlinks` to force plain copies,
- `--prepare-force` to reinstall auto-prepared source installs,
- `--installer-script` and `--python-exe` to control how headless source preparation is executed.


### GUI Installer and Launcher

Windows uses the single-file `PandratorInstaller.exe` from [Releases](https://github.com/lukaszliniewicz/Pandrator/releases). Linux uses the matching single-file `PandratorInstaller-x86_64.AppImage` artifact. Both are built from the stable `pandrator_installer_launcher.py` entry point and open the graphical installer. Release assets use those filenames directly so users can download the right installer for their platform without unpacking a source bundle.

On Linux:

```bash
chmod +x PandratorInstaller-x86_64.AppImage
./PandratorInstaller-x86_64.AppImage
```

The Linux AppImage defaults to installing under `~/Pandrator`. Use the install-location selector in the GUI, or pass `--workspace /path/to/parent`, to choose another parent directory. The installer keeps Pixi, environments, model caches, and downloaded repos under the selected workspace as much as possible. It does not install system packages on Linux.

To build and smoke-test the standalone installer locally:

```powershell
python -m pip install -r requirements-installer.txt
python scripts/build_installer.py
```

Run the same command on Linux from an activated Python environment to build the AppImage:

```bash
python3 -m venv .venv-installer
. .venv-installer/bin/activate
python -m pip install -r requirements-installer.txt
python scripts/build_installer.py
```

`scripts/build_installer.py` builds the Windows `.exe` on Windows and the Linux AppImage on Linux. The AppImage build must run on Linux.

For broad Linux compatibility, build release AppImages on the oldest glibc baseline you intend to support. The Fedora build path is useful for validation, but a newer Fedora-built AppImage may not run on older distributions.

For automation, the launcher also supports headless installation:

```powershell
python pandrator_installer_launcher.py --headless-install --workspace "D:/pandrator-builds/core" --components "kokoro"
# or CPU-only Kokoro:
python pandrator_installer_launcher.py --headless-install --workspace "D:/pandrator-builds/core" --components "kokoro_cpu"
python pandrator_installer_launcher.py --headless-install --workspace "D:/pandrator-builds/core" --components "rvc_cpu"
```

> [!NOTE]
> Some antivirus tools may flag standalone executables. If needed, add an exception or run from source.

Linux headless/source mode is still available from the repository root:

```bash
python3 -m venv .venv-installer
. .venv-installer/bin/activate
python -m pip install -r requirements-installer.txt
python pandrator_installer_launcher.py --self-check
python pandrator_installer_launcher.py --headless-install --workspace "$HOME/pandrator-workspace" --components "kokoro_cpu"
```

Fedora x86_64 validation currently covers the core Pixi install and Kokoro CPU through `Kokoro-FastAPI`: install, model download, `/health`, voice listing, and a short WAV generation request. Kokoro GPU uses the same installer path but still needs validation on a host with compatible NVIDIA drivers. Other local model-server components remain deferred on Linux until they are reviewed one by one.

Linux Calibre is optional and only needed for MOBI import. The installer detects `ebook-convert`/Calibre and reports the requirement, but does not install it. Kokoro uses dependencies inside its Pixi env and detects host eSpeak NG when available.

You can install components incrementally (during first setup or later):

- Pandrator core app
- XTTS2 API (`XTTS` GPU or `XTTS CPU only`)
- FishS2 API (`FishS2`)
- Chatterbox API (`Chatterbox` GPU or `Chatterbox CPU only`)
- VoxCPM2 API (`VoxCPM`)
- Voxtral API (`Voxtral`, GPU only)
- Kokoro API (`Kokoro` GPU or `Kokoro CPU only`)
- Silero API
- Magpie API (`Magpie` GPU or `Magpie CPU only`)
- RVC Service (`RVC` GPU or `RVC CPU only`)
- Optional tools: WhisperX, Easy XTTS Trainer

Current installer flow:

1. Creates `Pandrator/` in the selected location.
2. Installs/checks Calibre.
3. Downloads shared Pixi runtime to `Pandrator/bin/pixi.exe`.
4. Clones required repositories (`Pandrator`, `Subdub`) and selected server repos (`xtts2_api`, `fishs2-cpp-fastapi`, `chatterbox-fastapi`, `voxcpm_fastapi`, `voxtral-fastapi`, `Kokoro-FastAPI`, `magpie-fastapi`, `rvc-python`).
5. Sets up Pandrator dependencies and selected optional environments/tools.
6. Bootstraps XTTS2, FishS2, Chatterbox, VoxCPM2, Voxtral, Kokoro, Magpie, and RVC via their own launcher scripts.

Before using **Update**, close Pandrator and all services launched from the installation. The updater refuses to modify a running installation because Windows locks loaded environment files.

Updates automatically migrate older installations:

- Legacy in-process RVC is detected even when the new RVC service repository is absent. The updater prepares the dedicated RVC service first, defaults unknown legacy GPU capability to CPU, preserves `Pandrator/rvc_models`, and then removes the legacy RVC packages from Pandrator's main environment.
- Pynini and NeMo Text Processing are installed and import-verified on every update. This repair check is independent of the saved requirements hash.

Launch tab options:

- `Pandrator`
- `XTTS` (+ `Use CPU`, `DeepSpeed`)
- `FishS2`
- `Chatterbox` (+ `Use CPU`)
- `VoxCPM`
- `Voxtral`
- `Kokoro` (+ `Use CPU` when GPU support is installed)
- `Silero`
- `Magpie` (+ `Use CPU`)
- `RVC` (+ `Use CPU`; forced on for CPU-only installs)

If a local TTS server is launched from the launcher, Pandrator is auto-started with the matching connect flag (`-connect -xtts`, `-connect -fishs2`, `-connect -chatterbox`, `-connect -voxcpm`, `-connect -voxtral`, `-connect -kokoro`, `-connect -silero`, `-connect -magpie`).

To re-run setup from scratch, remove the generated `Pandrator/` folder and start again.

For additional functionality not yet included in the installer:
- Configure a local OpenAI-compatible LLM endpoint (for example LM Studio or an Ollama-compatible endpoint) if you want LLM text preprocessing and local translation.

Please refer to the repositories linked under [Dependencies](#dependencies) for detailed API-server options. The selected API server must be running for local TTS generation.

### Manual Installation

#### Prerequisites

- Git
- Python 3.11+
- Calibre
- FFmpeg on PATH (recommended)

#### Installation Steps

1. Install Calibre:

   - [https://calibre-ebook.com/download_windows](https://calibre-ebook.com/download_windows)

2. Clone the repositories:

   ```
   mkdir Pandrator
   cd Pandrator
   git clone https://github.com/lukaszliniewicz/Pandrator.git
   git clone https://github.com/lukaszliniewicz/Subdub.git
   ```

3. Install Pandrator dependencies:

   On Windows, use a Conda or Pixi environment and install Pynini from conda-forge first. NVIDIA does not support pip-only Pynini installation on Windows.

   ```
   cd Pandrator
   set PYTHONUTF8=1
   conda install -c conda-forge pynini=2.1.6.post1
   python -m pip install -r requirements.txt
   cd ..
   ```

4. Install Subdub dependencies:

   ```
   cd Subdub
   python -m pip install -e .
   cd ..
   ```

5. (Optional) Install XTTS2 API:

   ```
   git clone https://github.com/lukaszliniewicz/xtts2_api.git
   cd xtts2_api
   run.bat --cpu
   # or
   run.bat --backend cuda
   # Linux/macOS:
   # bash run.sh --cpu
   # bash run.sh --backend cuda
   cd ..
   ```

6. (Optional) Install FishS2 API:

   ```
   git clone https://github.com/lukaszliniewicz/fishs2-cpp-fastapi.git
   cd fishs2-cpp-fastapi
   run.bat
   # Linux/macOS:
   # bash run.sh
   cd ..
   ```

7. (Optional) Install Voxtral API:

   ```
   git clone https://github.com/lukaszliniewicz/voxtral-fastapi.git
   cd voxtral-fastapi
   run.bat
   # Linux:
   # bash run.sh
   cd ..
   ```

8. (Optional) Install Kokoro API:

    ```
    git clone https://github.com/remsky/Kokoro-FastAPI.git
    cd Kokoro-FastAPI
    python -m pip install -e .[cpu]
    # or for NVIDIA GPU support, use the upstream GPU extra and CUDA wheel index:
    # python -m pip install -e .[gpu] --extra-index-url https://download.pytorch.org/whl/cu126
    python docker/scripts/download_model.py --output api/src/models/v1_0
    cd ..
    ```

9. (Optional) Install Silero API:

   ```
   python -m pip install silero-api-server
   ```

10. (Optional) Install Easy XTTS Trainer:

   ```
   git clone https://github.com/lukaszliniewicz/easy_xtts_trainer.git
   cd easy_xtts_trainer
   pip install -r requirements.txt
   cd ..
   ```

11. (Optional) Install Magpie API:

    ```
    git clone https://github.com/lukaszliniewicz/magpie-fastapi.git
    cd magpie-fastapi
    run.bat
    # Linux:
    # bash run.sh
    cd ..
    ```

12. (Optional) Install RVC API:

    ```
    git clone https://github.com/lukaszliniewicz/rvc-python.git
    cd rvc-python
    run.bat --backend cuda
    # or
    run.bat --backend cpu
    cd ..
    ```

#### Running the Components

1. Run Pandrator:

   ```
   cd Pandrator
   python main.py
   ```

2. Run Pandrator with auto-connect to a local TTS backend:

   ```
   cd Pandrator
   python main.py -connect -xtts
   # or
   python main.py -connect -fishs2
   # or
   python main.py -connect -voxtral
   # or
   python main.py -connect -kokoro
   # or
   python main.py -connect -silero
   # or
   python main.py -connect -magpie
   ```

3. Run XTTS2 API (if installed):

   ```
   cd xtts2_api
   run.bat --cpu
   # or run.bat --backend cuda
   ```

4. Run FishS2 API (if installed):

   ```
   cd fishs2-cpp-fastapi
   run.bat
   ```

5. Run Voxtral API (if installed):

   ```
   cd voxtral-fastapi
   run.bat
   ```

6. Run Kokoro API (if installed):

    ```
    cd Kokoro-FastAPI
    set USE_GPU=false
    # or set USE_GPU=true if installed with GPU support
    python -m uvicorn api.src.main:app --host 127.0.0.1 --port 8880
    ```

7. Run Magpie API (if installed):

   ```
   cd magpie-fastapi
   run.bat
   ```

8. Run RVC API (if installed):

   ```
   cd rvc-python
   run.bat --backend cuda
   # or run.bat --backend cpu
   ```

#### Generated sentences
You can play back the generated sentences, also as a playlist, edit them (the text that will be used for regeneration), regenerate or remove individual ones. You can also mark them for regeneration. This is useful when you don't want to stop listening but work on all problematic sentences later. You can use the "m" key to mark the sentence that is currently playing or the right mouse button to mark both the current and the previous sentence (this can be useful if you're listening to the output and not looking at the screen).
"Save Output" concatenates the sentences generated so far and encodes them as one file.

### Dubbing

Pandrator offers a comprehensive workflow for generating dubbed videos from video files or existing subtitles. This includes transcription, translation, speech generation, and synchronization:

1. **Select a Video or SRT File:** 
    - **Video File:** Choose a video file. The audio will be extracted automatically, and transcription will be performed using WhisperX. 
    - **SRT File:** Select an existing SRT subtitle file. In this case, you also need to specify the corresponding video file (unless you only want to translate the subtitles).
2. **Transcription (if using a video file):**
    - **Language:** Select the language spoken in the original video.
    - **Model:** Choose a WhisperX model for transcription. Smaller models are faster, while larger ones provide higher accuracy. The `large-v3` model provides the best results. 
    - Pandrator will automatically run WhisperX to generate an SRT file containing the transcription.
3. **Translation (optional):**
    - **Enable Translation:** Toggle this option to translate the subtitles.
    - **Original and Target Languages:** Select the original language of the subtitles and the language you want to translate them into.
    - **Translation Provider:** Choose an LLM provider from your configured Providers catalog, or choose `DeepL`.
    - **Translation Model:** Choose a model from that provider's catalog (or type one manually if needed).
    - Manage provider API base URLs, keys and model catalogs in the **Providers** tab.
    - **Chain-of-thought (optional):** Enables additional reasoning effort for LLM-based translation/correction (not used with DeepL).
4. In order to generate speech, click on __Generate Dubbing Audio__. You will be able to edit/regenerate the sentences as in the Audiobook workflow. You can also choose to only transcribe the chosen video file or only translate a subtitle file.
5. **Synchronization:** When you're happy with the generated audio, click on __Add Dubbing to Video__. The dubbing will be synchronised with the video, producing a dubbed video file with embedded subtitles.

### TTS Provider Configuration
- OpenAI and Google Gemini are first-class TTS services, alongside local integrations such as Kokoro, Voxtral, and Magpie.
- `Custom` is reserved for user-created endpoints. Add and manage those endpoints in **Providers > TTS**.
- The **Wrapper Profile** selector contains curated recipes for popular third-party servers. Applying a profile fills its suggested local URL, route, request mapping, models, voices, and known defaults; all values remain editable before saving.
- For a new custom endpoint, enter its base URL and click **Auto-configure**. Pandrator safely inspects OpenAPI metadata and likely routes without generating audio, then presents the detected request mapping and confidence evidence for review before saving.
- Auto-configure supports OpenAI-compatible speech APIs and common JSON speech routes such as `POST /generate` with a text field. Models and voices are populated when the server documents or exposes catalogs.
- Multipart/form-data, Gradio, gRPC/WebSocket, and query-only wrappers are not offered as one-click profiles yet because they require additional request transports.
- First-class service base URLs are editable in **Providers > TTS**, including local service ports. These settings are stored in the app settings database, with the JSON settings file retained as a compatibility backup.

### General Audio Settings
1. You can change the length of silence appended to the end of sentences and paragraphs.
2. You can enable a fade-in and -out effect and set the duration.
3. You can enable RVC. For this to work, you have to install RVC_Python. You can do this in the Installer/Launcher at any time. You need to select a model - an RVC model consists of two files. A `.pth ` and an `.index ` file. They need to have the same name (e.g. voicex.pth and voicex.index). For best results, use the same voice for XTTS. You can also fine-tune the RVC options such as pitch.

### General Text Pre-Processing Settings
1. You can disable/enable splitting long sentences and set the max length a text fragment sent for TTS generation may have (enabled by default). The application dynamically adjusts the target maximum sentence length settings depending on the selected TTS service to match its recommended optimal block sizes (e.g. 350 for Kokoro/FishS2/Chatterbox, 300 for VoxCPM/Voxtral, 200 for XTTS/OpenAI). When splitting, it looks for punctuation marks (, ; : -) and chooses the one closest to the midpoint of the sentence; if there are no punctuation marks, it looks for conjunctions like "and".
2. You can disable/enable appending short sentences (to preceding or following sentences; disabled by default, which may improve flow because the length of text fragments sent to the model is more uniform).
3. Remove diacritics (useful when generating text that contains many foreign words or transliterations from foreign alphabets, e.g. Japanese). Do not enable this if you generate in a language that needs diacritics, like German or Polish. The pronunciation will be wrong then.
4. Remove quotation marks (useful for models that sometimes read quotation marks aloud).
5. NeMo Text Normalization is enabled by default for a conservative set of supported languages. It converts written forms such as dates, numbers, measurements, and abbreviations into spoken text before sentence splitting. Deterministic normalization is currently enabled for Arabic, German, English, Spanish, French, Hindi, Hungarian, Armenian, Italian, Japanese, Korean, and Portuguese. Other NeMo grammars remain disabled until their output is reliable in Pandrator's Windows runtime.
6. Sentence boundaries are detected with the multilingual `sat-3l-sm` model through `wtpsplit-lite`. The installer downloads the roughly 410 MiB ONNX model into Pandrator's portable cache. Existing rule-based segmenters remain available as automatic fallbacks if the model cannot load.
7. Punctuation and Case Normalization: The preprocessor automatically normalizes general punctuation marks and normalizes all-caps titles or chapters to standard title case to ensure smooth pronunciation (avoiding character-by-character spelling or unnatural pitch changes).
8. Source Previews & Plain-Text Paste: The source file selection dialog offers plain-text paste and a preview pane, allowing you to review and inspect source contents prior to import.

### LLM Pre-processing
- Enable LLM processing to use language models for preprocessing text before sending it to the TTS API. For example, you may ask the LLM to remove OCR artifacts, spell out abbreviations, and correct punctuation. When NeMo normalization is active, the LLM receives the already-normalized sentence.
- The processing order is NeMo text normalization, sentence splitting, optional LLM processing, then TTS generation.
- You can define up to three prompts for text optimization. Each prompt is sent to the LLM API separately, and the output of the last prompt is used for TTS generation.
- For each prompt, you can enable/disable it, set the prompt text, choose the LLM model to use, and enable/disable evaluation (if enabled, the LLM API will be called twice for each prompt, and then again for the model to choose the better result).
- Manage providers/models in the **Providers** tab, then refresh built-in catalogs from the **Text Processing** tab if needed.

### PDF ingestion and OCR

PDF imports preserve page geometry, font evidence, reading order, and extraction provenance until source cleaning is accepted. Pages with a plausible native text layer use PyMuPDF extraction; missing or poor text layers are OCRed automatically with the CPU ONNX versions of PP-OCRv6 medium detection and recognition. Unsupported scripts can be routed to a PP-OCRv5 language-specific model from the source-cleaning dialog.

The deterministic PDF pass conservatively reconstructs paragraphs and columns, removes high-confidence repeated headers, footers, page numbers, and table-of-contents entries, detects footnotes, and marks high-confidence chapters or sections. Its diff and diagnostics are available before the optional LLM source-cleaning agent runs.

PyCropPDF remains available before PDF import and writes a provenance sidecar containing source/output hashes, crop rectangles, whiteouts, deleted pages, and original-to-derived page mapping. Cropping is especially useful for scans with large borders, gutters, or persistent marginal content.

### RVC Quality Enhancement and Voice Cloning
- Enable RVC to enhance the generated audio quality and apply voice cloning.
- Select the RVC model file (.pth) and the corresponding index file using the "Select RVC Model" and "Select RVC Index" buttons in the Audio Processing tab.
- When RVC is enabled, the generated audio will be processed using the selected RVC model and index before being saved.

## Contributing
Contributions, suggestions for improvements, and bug reports are most welcome!

## Tips
- You can find a collection of voice samples, for example [here](https://aiartes.com/voiceai). They are intended for use with ElevenLabs, so you will need to pick an 8-12s fragment and save it as a 22050 Hz mono `.wav` using Audacity, for instance.
- You can find a collection of RVC models, for example [here](https://voice-models.com/).

## To-do
- [ ] Add importing/exporting settings.
- [X] Add support for proprietary APIs for text pre-processing and TTS generation.
- [ ] Add option to record a voice sample and use it for TTS to the GUI.
- [x] Add support for chapter segmentation
- [x] Add all API servers to the setup script.
- [x] Add support for custom XTTS models 
- [x] Add workflow to create dubbing from `.srt` subtitle files.
- [x] Include support for PDF files.
- [x] Integrate editing capabilities for processed sentences within the UI.
- [x] Add support for a lower quality but faster local TTS model that can easily run on CPU, e.g. Silero or Piper.
- [x] Add support for EPUB.


