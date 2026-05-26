<p align="left">
  <img src="pandrator.png" alt="Icon" width="200" height="200"/>
</p>

# Pandrator: a multilingual GUI audiobook, subtitle and dubbing generator with voice cloning and translation
>[!TIP]
>**TL;DR:**
> - Pandrator is not an AI model itself, but a GUI framework for Text-to-Speech, subtitle and translation projects. It can generate audiobooks and subtitles/dubbing by leveraging several AI tools, custom workflows and algorithms. It has an installer and works on Windows out of the box. It is not necessary to set up WSL or Docker containers.
> - It supports a wide range of TTS models: Fish S2 Pro, Voxtral, XTTSv2, Kokoro, voxcpm2, Silero, OpenAI and Gemini, as well as custom openai compatible implementations.
> - When installing: if you don't have a GPU, choose Kokoro or Silero. If you do have one with at least 8GB of VRAM, and it supports your language, use Voxtral. For voice cloning and a wide range of languages, use XTTS v2 (works even with 4GB GPUs and on CPU).
> - The easiest way to use it is to download one of the precompiled **[archives](https://1drv.ms/f/s!AgSiDu9lV3iMnPFKPO5BB_c72OLjtQ?e=3fRZMG)** - simply unpack them and use the included launcher. See **[this table](#self-contained-packages)** for their contents and sizes.
> - You can talk to me or share tips/workflows/ideas on the Discord server.
>
> [![](https://dcbadge.limes.pink/api/server/JZzHv3MnaV)](https://discord.gg/https://discord.gg/JZzHv3MnaV)



## Quick Demonstration
This video shows the process of launching Pandrator, selecting a source file, starting generation, stopping it and previewing the saved file. It has not been sped up as it's intended to illustrate the real performance (you may skip the first 35s when the XTTS server is launching, and please remember to turn on the sound). 

https://github.com/user-attachments/assets/7cab141a-e043-4057-8166-72cb29281c50

And here you can see the dubbing workflow - from a YT video, through transcription, translation, speech generation to synchronisation. 

https://github.com/user-attachments/assets/dfd4b6e8-3eda-49e4-bff4-f1683ec4cf21

## About Pandrator

Pandrator aspires to be easy to use and install - it has a one-click installer and a graphical user interface. It is a tool designed to perform two tasks: 
- transform text, PDF (including see-through cropping), EPUB and SRT files into spoken audio in multiple languages based chiefly on open source software run locally, including preprocessing to make the generated speech sound as natural as possible by, among other things, splitting the text into paragraphs, sentences and smaller logical text blocks (clauses), which the TTS models can process with minimal artifacts. Each sentence can be regenerated if the first attempt is not satisfactory, including marking for regeneration using mouse or keyboard actions when listening back to the generation. Voice cloning is possible for models that support it, and text can be additionally preprocessed using LLMs (to remove OCR artifacts or spell out things that the TTS models struggle with, like Roman numerals and abbreviations, for example),
- generate dubbing either directly from a video file, including transcription (using [WhisperX](https://github.com/m-bain/whisperX)), or from an .srt file. It includes a complete workflow from a video file to a dubbed video file with subtitles - including translation using a variety of APIs and techniques to improve the quality of translation. [Subdub](https://github.com/lukaszliniewicz/Subdub), a companion app developed for this purpose, can also be used on its own. You can also correct or translate subtitles without generating audio. 

At the moment, Pandrator supports multiple TTS backends: [XTTS v2](https://huggingface.co/coqui/XTTS-v2) via the OpenAI-compatible [XTTS2 API server](https://github.com/lukaszliniewicz/xtts2_api), [Fish Audio S2 Pro GGUF](https://huggingface.co/rodrigomt/s2-pro-gguf) via [fishs2-cpp-fastapi](https://github.com/lukaszliniewicz/fishs2-cpp-fastapi), [Voxtral](https://huggingface.co/mistralai/Voxtral-4B-TTS-2603) via [voxtral-fastapi](https://github.com/lukaszliniewicz/voxtral-fastapi), [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) via [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI), and [Silero](https://github.com/snakers4/silero-models) via `silero-api-server`. It also supports commercial and custom OpenAI-compatible audio endpoints and optional [RVC Python (JarodMica fork)](https://github.com/JarodMica/rvc-python) post-processing. For local LLM text preprocessing, Pandrator works well with OpenAI-compatible local servers such as LM Studio and Ollama-compatible endpoints.

## Supported Languages
- XTTS supports English (en), Spanish (es), French (fr), German (de), Italian (it), Portuguese (pt), Polish (pl), Turkish (tr), Russian (ru), Dutch (nl), Czech (cs), Arabic (ar), Chinese (zh-cn), Japanese (ja), Hungarian (hu) and Korean (ko). 

- FishS2 currently uses multilingual Fish S2 GGUF models and OpenAI-compatible voice upload endpoints via `fishs2-cpp-fastapi`.

- Voxtral currently supports Arabic (ar), English (en), German (de), Spanish (es), French (fr), Hindi (hi), Italian (it), Dutch (nl), and Portuguese (pt) via preset voices exposed by `voxtral-fastapi`.

- Kokoro currently supports English (en), Spanish (es), French (fr), Hindi (hi), Italian (it), Japanese (ja), Portuguese (pt), and Chinese (zh-cn).

- Silero supports English, German, Russian, Spanish, French, Hindi, Russian, Tatar, Ukrainian, Uzbek and Kalmyk. 

## Samples
The samples were generated using the minimal settings - no LLM text processing, RVC or TTS evaluation, and no sentences were regenerated. Both XTTS and Silero generations were faster than playback speed, and Silero used only one CPU core. 

https://github.com/user-attachments/assets/1c763c94-c66b-4c22-a698-6c4bcf3e875d

https://github.com/lukaszliniewicz/Pandrator/assets/75737665/118f5b9c-641b-4edd-8ef6-178dd924a883

Dubbing sample, including translation ([video source](https://www.youtube.com/watch?v=_SwUpU0E2Eg&t=61s&pp=ygUn0LLRi9GB0YLRg9C_0LvQtdC90LjQtSDQu9C10LPQsNGB0L7QstCw)):

https://github.com/user-attachments/assets/1ba8068d-986e-4dec-a162-3b7cc49052f4

## Requirements

### Hardware Requirements

| TTS Model       | CPU Requirements                                              | GPU Requirements                                                       |
|------------|---------------------------------------------------------------|-------------------------------------------------------------------------|
| XTTS       | A reasonably modern CPU with 4+ cores (for CPU-only generation)              | NVIDIA GPU with 4GB+ of VRAM for good performance                        |
| FishS2     | CPU mode exists but is generally too slow for practical long-form usage       | NVIDIA GPU strongly recommended (8GB+ VRAM practical target)             |
| Voxtral    | N/A (GPU-only backend in current wrapper)                    | Dedicated GPU strongly recommended (4GB+ practical minimum)             |
| Kokoro     | Works well on modern CPUs; install includes direct eSpeak setup on Windows    | Optional (CPU path is supported)                                         |
| Silero     | Performs well on most CPUs regardless of core count                   | N/A                                                                     |

### Dependencies
This project relies on several APIs and services (running locally) and libraries, notably:

#### Required
- One or more local/remote TTS endpoints:
  - [XTTS2 API](https://github.com/lukaszliniewicz/xtts2_api) (OpenAI-compatible XTTS v2 server)
  - [fishs2-cpp-fastapi](https://github.com/lukaszliniewicz/fishs2-cpp-fastapi) (OpenAI-compatible Fish S2 server)
  - [voxtral-fastapi](https://github.com/lukaszliniewicz/voxtral-fastapi) (OpenAI-compatible Voxtral server)
  - [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) (OpenAI-compatible Kokoro server)
  - [silero-api-server](https://pypi.org/project/silero-api-server/) (Silero backend)
  - Commercial/custom OpenAI-compatible audio endpoints
- [FFmpeg](https://github.com/FFmpeg/FFmpeg) for audio encoding.
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
- [RVC Python (JarodMica fork)](https://github.com/JarodMica/rvc-python) for enhancing voice quality and cloning results with [Retrieval Based Voice Conversion](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI).
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
- `--only` to build only selected packages (for example `--only kokoro`),
- `--skip-voxtral-with-rest` to skip the combined Voxtral + XTTS/WhisperX/RVC package,
- `--no-hardlinks` to force plain copies,
- `--prepare-force` to reinstall auto-prepared source installs,
- `--installer-script` and `--python-exe` to control how headless source preparation is executed.


### GUI Installer and Launcher (Windows)

![pandrator_installer_launcher_KLoHrNDIps](https://github.com/user-attachments/assets/2be46b49-9e79-4281-89ed-5797bdfbe28b)

Run `pandrator_installer_launcher.exe` from [Releases](https://github.com/lukaszliniewicz/Pandrator/releases). The executable is built from `pandrator_installer_launcher.py`.

For automation, the launcher also supports headless installation:

```powershell
python pandrator_installer_launcher.py --headless-install --workspace "D:/pandrator-builds/core" --components "kokoro"
```

> [!NOTE]
> Some antivirus tools may flag standalone executables. If needed, add an exception or run from source.

You can install components incrementally (during first setup or later):

- Pandrator core app
- XTTS2 API (`XTTS` GPU or `XTTS CPU only`)
- FishS2 API (`FishS2`)
- Voxtral API (`Voxtral`, GPU only)
- Kokoro API (`Kokoro`)
- Silero API
- Optional tools: RVC Python, WhisperX, Easy XTTS Trainer

Current installer flow:

1. Creates `Pandrator/` in the selected location.
2. Installs/checks Calibre.
3. Downloads shared Pixi runtime to `Pandrator/bin/pixi.exe`.
4. Clones required repositories (`Pandrator`, `Subdub`) and selected server repos (`xtts2_api`, `fishs2-cpp-fastapi`, `voxtral-fastapi`, `Kokoro-FastAPI`).
5. Sets up Pandrator dependencies and selected optional environments/tools.
6. Bootstraps XTTS2, FishS2, Voxtral, and Kokoro via their own launcher scripts.

Launch tab options:

- `Pandrator`
- `XTTS` (+ `Use CPU`, `DeepSpeed`)
- `FishS2`
- `Voxtral`
- `Kokoro`
- `Silero`

If a local TTS server is launched from the launcher, Pandrator is auto-started with the matching connect flag (`-connect -xtts`, `-connect -fishs2`, `-connect -voxtral`, `-connect -kokoro`, `-connect -silero`).

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

   ```
   cd Pandrator
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
   python -m uvicorn api.src.main:app --host 127.0.0.1 --port 8880
   ```

7. Run Silero API (if installed):

   ```
   python -m silero_api_server
   ```

#### Folder Structure

After installation, your folder structure should look like this:

```
Pandrator/
├── Pandrator/
├── Subdub/
├── xtts2_api/ (optional)
├── fishs2-cpp-fastapi/ (optional)
├── voxtral-fastapi/ (optional)
├── Kokoro-FastAPI/ (optional)
├── easy_xtts_trainer/ (optional)
```

For more detailed information on using specific components or troubleshooting, please refer to the documentation of each individual repository.

## Quick Start Guide

### Basic Usage: Audiobooks
If you don't want to use the additional features like RVC, you have everything you need in the **Session tab**. 

#### Session
Either create a new session or load an existing one (select a folder in `Outputs` to do that).

#### File selection and preprocessing
Choose a `.txt`, `.srt`, `.pdf`, `.epub`, `.mobi` or `.docx` file. If you choose a PDF or EPUB file, a preview window will open with the extracted text. For PDFs, you will be able to crop the document (with translucent pages) to remove headers and footers or selected pages. You may edit the extracted text (OCRed books often have poorly recognized text from the title page, for example) and check/add paragraphs and Chapter markers (they will be created automatically for EPUB files). Files that contain a lot of text, regardless of format, can take a moment to finish preprocessing before generation begins. The GUI will freeze, but as long as there is processor activity, it's simply working.

#### Selecting the TTS Engine and the voice
1. Select the TTS service you want to use (XTTS, FishS2, Voxtral, Kokoro, Silero, or OpenAI-Compatible cloud TTS) and then choose the language.
   - Cloud providers (OpenAI, Gemini, and custom OpenAI-compatible endpoints) are managed in the **Providers** tab.
   - In the Session tab, pick the cloud provider and then choose its model/voice.
2. Choose the voice you want to use.
   1. **XTTS**, voices are short, 6-12s `.wav` files (22050hz sample rate, mono) stored in the `tts_voices` directory (`Pandrator/Pandrator/tts_voices`). You can upload and select them via the GUI. The XTTS model uses the audio to clone the voice. It doesn't matter what language the sample is in, you will be able to generate speech in all supported languages, but the quality will be best if you provide a sample in your target language. You may use the sample one in the repository or upload your own. Please make sure that the audio is between 6 and 12s, mono, and the sample rate is 22050hz. You may use a tool like Audacity to prepare the files. The less noise, the better. You may use a tool like [Resemble AI](https://github.com/resemble-ai/resemble-enhance) for denoising and/or enhancement of your samples on [Hugging Face](https://huggingface.co/spaces/ResembleAI/resemble-enhance). You may put several samples in a folder inside `tts_voices` and the model will use all of them at once (generally up to 4). It can improve the quality. 
   2. **FishS2** supports reference-based voice cloning through uploaded local voice profiles (`/v1/audio/voices`) and uses OpenAI-compatible model/voice fields.
   3. **Voxtral** exposes preset voices and model choices from the local API server. Current voice presets cover Arabic, English, German, Spanish, French, Hindi, Italian, Dutch and Portuguese. It is GPU-only in the current local wrapper.
   4. **Kokoro** exposes local preset voices through an OpenAI-compatible API and supports language-conditioned synthesis (`lang_code`) for supported languages.
   5. **Silero** offers a number of voices for each language it supports. It doesn't support voice cloning. Simply select a voice from the dropdown after choosing the language.

#### Output options
The default output format is .m4b. You can also select opus, mp3 or wav, choose a cover image and provide metadata.

#### Generation 
Click on "Start Generation" to begin. You may stop and resume it later, or close the programme and load the session later.

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

### General Audio Settings
1. You can change the length of silence appended to the end of sentences and paragraphs.
2. You can enable a fade-in and -out effect and set the duration.
3. You can enable RVC. For this to work, you have to install RVC_Python. You can do this in the Installer/Launcher at any time. You need to select a model - an RVC model consists of two files. A `.pth ` and an `.index ` file. They need to have the same name (e.g. voicex.pth and voicex.index). For best results, use the same voice for XTTS. You can also fine-tune the RVC options such as pitch.

### General Text Pre-Processing Settings
1. You can disable/enable splitting long sentences and set the max length a text fragment sent for TTS generation may have (enabled by default; it tries to split sentences whose length exceeds the max length value; it looks for punctuation marks (, ; : -) and chooses the one closest to the midpoint of the sentence; if there are no punctuation marks, it looks for conjunctions like "and"; it performs this operation twice as some sentence fragments may still be too long after just one split).
2. You can disable/enable appending short sentences (to preceding or following sentences; disabled by default, which may improve flow because the length of text fragments sent to the model is more uniform).
3. Remove diacritics (useful when generating text that contains many foreign words or transliterations from foreign alphabets, e.g. Japanese). Do not enable this if you generate in a language that needs diacritics, like German or Polish. The pronunciation will be wrong then.
4. Remove quotation marks (useful for models that sometimes read quotation marks aloud).

### LLM Pre-processing
- Enable LLM processing to use language models for preprocessing text before sending it to the TTS API. For example, you may ask the LLM to remove OCR artifacts, spell out abbreviations, and correct punctuation.
- You can define up to three prompts for text optimization. Each prompt is sent to the LLM API separately, and the output of the last prompt is used for TTS generation.
- For each prompt, you can enable/disable it, set the prompt text, choose the LLM model to use, and enable/disable evaluation (if enabled, the LLM API will be called twice for each prompt, and then again for the model to choose the better result).
- Manage providers/models in the **Providers** tab, then refresh built-in catalogs from the **Text Processing** tab if needed.

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


