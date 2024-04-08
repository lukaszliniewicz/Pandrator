<p align="left">
  <img src="pandrator.png" alt="Icon" width="200" height="200"/>
</p>

# Pandrator, a GUI audiobook and dubbing generator with voice cloning and AI text optimisation

Pandrator is a tool designed to transform text, PDF and SRT files into spoken audio in multiple languages based on open source software, including voice cloning, LLM-based text preprocessing and the ability to directly save generated subtitle audio to a video file by mixing the synchronized output with the original audio track of the video.

It leverages the [XTTS](https://huggingface.co/coqui/XTTS-v2), [Silero](https://github.com/snakers4/silero-models) and [VoiceCraft](https://github.com/jasonppy/VoiceCraft) model(s) for text-to-speech conversion and voice cloning, enhanced by RVC_CLI for quality improvement and better voice cloning results, and NISQA for audio quality evaluation. Additionally, it incorporates Text Generation Webui's API for local LLM-based text pre-processing, enabling a wide range of text manipulations before audio generation.

> It is still in alpha stage and I'm not an experienced developer (I'm a noob, in fact), so the code is far from perfect in terms of optimisation, features and reliability. Please keep this in mind and contribute, if you want to help me make it better.

- [Pandrator, an audiobook generator](#pandrator-an-audiobook-generator)
  - [Samples](#samples)
  - [Requirments](#requirments)
    - [Hardware](#hardware)
    - [Dependencies](#dependencies)
      - [Required](#required)
      - [Optional](#optional)
  - [Installation](#installation)
    - [Minimal One-Click Installation Executable (Windows with an Nvidia GPU only)](#minimal-one-click-installation-executable-windows-with-an-nvidia-gpu-only)
    - [Manual Installation](#manual-installation)
  - [Features](#features)
  - [Quick Start Guide](#quick-start-guide)
    - [Basic Usage](#basic-usage)
    - [General Audio Settings](#general-audio-settings)
    - [General Text Pre-Processing Settings](#general-text-pre-processing-settings)
    - [LLM Pre-processing](#llm-preprocessing)
    - [RVC Quality Enhancement and Voice Cloning](#rvc-quality-enhancement-and-voice-cloning)
    - [NISQA TTS Evaluation](#nisqa-tts-evaluation)
  - [Contributing](#contributing)
  - [Tips](#tips)
  - [To-do](#to-do)

![UI Demonstration Image](ui_demonstration.png)

## Samples
The samples were generated using the minimal settings - no LLM text processing, RVC or TTS evaluation, and no sentences were regenerated. Both XTTS and Silero generations were faster than playback speed. 

https://github.com/lukaszliniewicz/Pandrator/assets/75737665/76a97cf0-275d-4ea2-868e-95eecdc6f6ce

https://github.com/lukaszliniewicz/Pandrator/assets/75737665/bbb10512-79ed-43ea-bee3-e271b605580e

https://github.com/lukaszliniewicz/Pandrator/assets/75737665/118f5b9c-641b-4edd-8ef6-178dd924a883

## Requirments

### Hardware
#### XTTS
I was able to run all functionalities on a laptop with a Ryzen 5600h and a 3050 laptop GPU (4GB of VRAM). It's likely that you will need at least 16GB of RAM, a reasonably modern CPU, and ideally an NVIDIA GPU with 4 GB+ of VRAM for usable performance. Consult the requirments of the services listed below.
#### Silero
Silero runs on the CPU. It should perform well on almost all reasonably modern systems. 
#### VoiceCraft
You can run VoiceCraft on a cpu, but generation will be very slow. To achieve meaningful acceleration with a GPU (Nvidia), you need one with at least 8GB of VRAM. 

### Dependencies
This project relies on several APIs and services (running locally) and libraries, notably:

#### Required
- [XTTS API Server by daswer123](https://github.com/daswer123/xtts-api-server.git) for Text-to-Speech (TTS) generation using Coqui [XTTSv2](https://huggingface.co/coqui/XTTS-v2) OR [Silero API Server by ouoertheo](https://github.com/ouoertheo/silero-api-server) for TTS generaton using the [Silero models](https://github.com/snakers4/silero-models) OR [VoiceCraft by jasonppy](https://github.com/jasonppy/VoiceCraft). XTTS and VoiceCraft use the GPU (Nvidia), and Silero uses the CPU. Silero can be run on low-end systems.
- [FFmpeg](https://github.com/FFmpeg/FFmpeg) for audio encoding.
- [Sentence Splitter by mediacloud](https://github.com/mediacloud/sentence-splitter) for splitting `.txt ` files into sentences, [customtkinter by TomSchimansky](https://github.com/TomSchimansky/CustomTkinter), [num2words by savoirfairelinux](https://github.com/savoirfairelinux/num2words) for converting numbers to words (Silero requirs this), `pysrt`, `pydub` and others (see `requirements.txt`). 

#### Optional
- [Text Generation Webui API by oobabooga](https://github.com/oobabooga/text-generation-webui.git) for LLM-based text pre-processing.
- [RVC_CLI by blaise-tk](https://github.com/blaise-tk/RVC_CLI.git) for enhancing voice quality and cloning results with [Retrieval Based Voice Conversion](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI).
- [NISQA by gabrielmittag](https://github.com/gabrielmittag/NISQA.git) for evaluating TTS generations (using the [FastAPI implementation](https://github.com/lukaszliniewicz/NISQA-API)).

## Installation

### Minimal One-Click Installation Executables (Windows only):
Run `pandrator_start_minimal_xtts.exe`, `pandrator_start_minimal_silero.exe` or `pandrator_start_minimal_voicecraft.exe` with administrator priviliges. You will find them under [Releases](https://github.com/lukaszliniewicz/Pandrator/releases). The executables were created using [pyinstaller](https://github.com/pyinstaller/pyinstaller) from `pandrator_start_minimal_xtts.py`, `pandrator_start_minimal_silero.py` and `pandrator_start_minimal_voicecraft.py` in the repository.

**The file may be flagged as a threat by antivirus software, so you may have to add it as an exception.**

On first use the executable creates the Pandrator folder, installs `curl`, `git`, `ffmpeg` (using Chocolatey) and `Miniconda`, clones the XTTS Api Server respository, the Silero Api Server repository or the VoiceCraft API repository and the Pandrator repository, creates conda environments, installs dependencies and launches them. **You may use it to launch Pandrator later**. 

If you want to perform the setup again, remove the Pandrator folder it created. Please allow at least a couple of minutes for the initial setup process to download models and install dependencies (it takes about 7-10 minutes for me).

For additional functionality:
- Install Text Generation Webui and remember to enable the API (add `--api` to `CMD_FLAGS.txt` in the main directory of the Webui before starting it).
- Set up RVC_CLI for enhancing generations with RVC.
- Set up NISQA API for automatic evaluation of generations.

Please refer to the repositories linked under [Dependencies](#Dependencies) for detailed installation instructions. Remember that the APIs must be running to make use of the functionalities they offer.

### Manual Installation:
1. Make sure that Python 3, git and ffmpeg are installed and in PATH.
2. Install and run at least XTTS API Server, Silero API Server or VoiceCraft API Server. 
3. Clone this repository (`git clone https://github.com/lukaszliniewicz/Pandrator.git`).
4. `cd` to the repository directory.
5. Install requirements using `pip install -r requirements.txt`.
6. Run `python pandrator.py`.

## Features
- **Text Pre-processing:** Splits text into sentences and (attempts to) preserve paragraphs. Profiles for multiple languages are available.
- **LLM Text Pre-processing:** Utilizes a local LLM for text corrections and enhancements with up to three different prompts run sequentially, and an evaluation mechanism that asks the model to perform a task twice and then choose the better response. I've been using `openchat-3.5-0106.Q5_K_M.gguf` with good results, as well as for example `Mistral 7B Instruct 0.2`. Different models may perform different tasks well, so it's possible to choose a specific model for a specific prompt.
- **Audio Generation:** Converts processed text into speech, with options for voice cloning and quality enhancement. It currently supports `.txt`,  `.srt` and `.pdf` files. 
- **Audio Evaluation:** An experimental feature that predicts Mean Opinion Score (MOS) for generated sentences and sets a score threshold or chooses the best score from a set number of generations.
- **Generating and adding dubbing to video files:** Speech generated from subtitle files is synchronized with the SRT timestamps and can be saved as a file or mixed with an audio track of a video file, effectively producing dubbing. It handles cases where generated speech exceeds the time alloted for a subtitle and self-corrects synchronisation. It's possible to speed up or slow down generated audio. 
- **Session Management:** Supports creating, deleting, and loading sessions for organized workflow.
- **GUI:** Built with customtkinker for a user-friendly experience.

## Quick Start Guide

![Demonstration GIF](demonstration.gif)

### Basic Usage
If you don't want to use the additional functionalities, you have everything you need in the **Session tab**. 

1. Either create a new session or load an existing one (select a folder in `Outputs` to do that).
2. Choose your `.txt`, `.srt` or `.pdf` file. If you choose a PDF, a preview window will open with the text extracted from the file. You may edit it and disable paragraph detection if it doesn't work well. Very big PDF files can take a couple of minutes to load.
3. Select the TTS server you want to use - XTTS, Silero or VoiceCraft - and the language from the dropdown (VoiceCraft currently supports only English).
4. Choose the voice you want to use.
   1. **For XTTS**, voices are short, 8-12s `.wav` files in the `tts_voices` directory. The XTTS model uses the audio to clone the voice. It doesn't matter what language the sample is in, you will be able to generate in all supported languages, but the quality will be best if you provide a sample in your target language. You may use the sample one in the repository or upload your own. Please make sure that the audio is between 8 and 12s, mono, and the sample rate is 22050hz. You may use a tool like Audacity to prepare the files. The less noise, the better. You may use a tool like [Resemble AI](https://github.com/resemble-ai/resemble-enhance) for denoising and/or enhancement of your samples on [Hugging Face](https://huggingface.co/spaces/ResembleAI/resemble-enhance).
   2. **Silero** offers a number of voices for each language it supports. It doesn't support voice cloning. Simply sselect a voice from the dropdown after choosing the language.
   3. **VoiceCraft** works similarly to XTTS in that it clones the voice from a `.wav ` sample. However, it needs both a properly formatted `.wav` file (mono, 16000hz) and a `.txt` file with the transcription of what is said in the sample. The files must have the same name (apart from the extension, of course). You need to upload them to `tts_voices/VoiceCraft` and you will be able to select them in the GUI. Currently only English is supported. If you generate with a new voice for the first time, the server will perform the alignment procedure, so the first sentence will be generated with a delay. This won't happen when you use that voice again.
6. If you want, you can either slow down or speed up the generated audio (type in or choose a ratio, e.g. 1.1, which is 10% faster than generated; it may be especially useful for dubbing).
7. If you chose an `.srt` file, you will be given the option to select a video file and one of its audio tracks to mix with the synchronized output, as well as weather you want to lower the volume of the original audio when subtitle audio is playing.
8. Start the generation. You may stop and resume it later, or close the programme and load the session later.
9. You can play back the generated sentences, also as a playlist, and edit (the text for regeneration), regenerate or remove individual ones.
10. "Save Output" concatenates the sentences generated so far an encodes them as one file (default is `.opus` at 64k bitrate; you may change it in the Audio tab to `.wav` or `.mp3`).

### General Audio Settings
1. You can change the lenght of silence appended to the end of sentences and paragraphs.
2. You can enable a fade-in and -out effect and set the duration.
3. You can choose the output format and bitrate.

### General Text Pre-Processing Settings
1. You can disable/enable splitting long sentences and set the max lenght a text fragment sent for TTS generation may have (enabled by default; it tries to split sentences whose lenght exceeds the max lenght value; it looks for punctuation marks (, ; : -) and chooses the one closest to the midpoint of the sentence).
2. You can disable/enable appending short sentences to preceding or following sentences (disabled by default, may perhaps improve the flow as the lenght of text fragments sent to the TTS API is more uniform).
3. Remove diacritics (useful when generating a text that contains many foreign words or transliterations from foreign alphabets). Do not enable this if you generate in a language that needs diacritics! The pronounciation will be wrong then.

### LLM Pre-processing
- Enable LLM processing to use language models for preprocessing the text before sending it to the TTS API. For example, you may ask the LLM to remove OCR artifacts, spell out abbreviations, correct punctuation etc.
- You can define up to three prompts for text optimization. Each prompt is sent to the LLM API separately, and the output of the last prompt is used for TTS generation.
- For each prompt, you can enable/disable it, set the prompt text, choose the LLM model to use, and enable/disable evaluation (if enabled, the LLM API will be called twice for each prompt, and then again for the model to choose the better result).
- Load the available LLM models using the "Load LLM Models" button in the Session tab.

### RVC Quality Enhancement and Voice Cloning
- Enable RVC to enhance the generated audio quality and apply voice cloning.
- Select the RVC model file (.pth) and the corresponding index file using the "Select RVC Model" and "Select RVC Index" buttons in the Audio Processing tab.
- When RVC is enabled, the generated audio will be processed using the selected RVC model and index before being saved.

### NISQA TTS Evaluation
- Enable TTS evaluation to assess the quality of the generated audio using the NISQA (Non-Intrusive Speech Quality Assessment) model.
- Set the target MOS (Mean Opinion Score) value and the maximum number of attempts for each sentence.
- When TTS evaluation is enabled, the generated audio will be evaluated using the NISQA model, and the best audio (based on the MOS score) will be chosen for each sentence.
- If the target MOS value is not reached within the maximum number of attempts, the best audio generated so far will be used.

## Contributing
Contributions, suggestions for improvements, and bug reports are most welcome!

## Tips
- You can find a collection of voice sample for example [here](https://aiartes.com/voiceai). They are intended for use with ElevenLabs, so you will need to pick an 8-12s fragment and save it as 22050khz mono `.wav` usuing Audacity, for instance.
- You can find a collection of RVC models for example [here](https://voice-models.com/).

## To-do
- [ ] Add the other API servers to the setup script.
- [ ] Add importing/exporting settings.
- [ ] Add support for proprietary APIs for text pre-processing and TTS generation.
- [ ] Add support for HTML, XML, and Epub.
- [x] Include support for PDF files.
- [ ] Include OCR for PDFs.
- [x] Integrate editing capabilities for processed sentences within the UI.
- [ ] Add support for a higher quality local TTS model, Tortoise.
- [x] Add support for a lower quality but faster local TTS model that can easily run on CPU, e.g. Silero or Piper.
- [ ] Implement a better text segmentation method, e.g. NLP-based.
- [ ] Add option to record a voice sample and use it for TTS to the GUI.
- [x] Add workflow to create dubbing from `.srt` subtitle files.
