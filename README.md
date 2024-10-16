<p align="left">
  <img src="pandrator.png" alt="Icon" width="200" height="200"/>
</p>

# Pandrator, a GUI audiobook and dubbing generator with voice cloning and AI text optimisation

>[!TIP]
>**TL;DR:**
> - Pandrator is not an AI model itself, but a GUI framework for Text-to-Speech projects. It can generate audiobooks and dubbing by leveraging several AI tools, custom workflows and algorithms. It works on Windows out of the box. It does work on Linux, but you have to perform a manual installation at the moment.
> - The easiest way to use it is to download one of the precompiled **[archives](https://1drv.ms/f/s!AgSiDu9lV3iMnPFKPO5BB_c72OLjtQ?e=3fRZMG)** - simply unpack them and use the included launcher. See **[this table](#self-contained-packages)** for their contents and sizes.

## Quick Demonstration
This video shows the process of launching Pandrator, selecting a source file, starting generation, stopping it and previewing the saved file. It has not been sped up as it's intended to illustrate the real performance (you may skip the first 35s when the XTTS server is launching, and please remember to turn on the sound). 

https://github.com/user-attachments/assets/7cab141a-e043-4057-8166-72cb29281c50

## About Pandrator

Pandrator aspires to be easy to use and install - it has a one-click installer and a graphical user interface. It is a tool designed to perform two tasks: 
- transform text, PDF, EPUB and SRT files into spoken audio in multiple languages based chiefly on open source software run locally, including preprocessing to make the generated speech sound as natural as possible by, among other things, splitting the text into paragraphs, sentences and smaller logical text blocks (clauses), which the TTS models can process with minimal artifacts. Each sentence can be regenerated if the first attempt is not satisfacory. Voice cloning is possible for models that support it, and text can be additionally preprocessed using LLMs (to remove OCR artifacts or spell out things that the TTS models struggle with, like Roman numerals and abbreviations, for example),
- generate dubbing either directly from a video file, including transcription (using [WhisperX](https://github.com/m-bain/whisperX)), or from an .srt file. It includes a complete workflow from a video file to a dubbed video file with subtitles - including translation using a variety of APIs and techniques to improve the quality of translation. [Subdub](https://github.com/lukaszliniewicz/Subdub), a companion app developed for this purpose, can also be used on its own.  

It leverages the [XTTS](https://huggingface.co/coqui/XTTS-v2), [Silero](https://github.com/snakers4/silero-models) and [VoiceCraft](https://github.com/jasonppy/VoiceCraft) model(s) for text-to-speech conversion and voice cloning, enhanced by [RVC_CLI](https://github.com/blaisewf/rvc-cli) for quality improvement and better voice cloning results, and NISQA for audio quality evaluation. Additionally, it incorporates [Text Generation Webui's](https://github.com/oobabooga/text-generation-webui) API for local LLM-based text pre-processing, enabling a wide range of text manipulations before audio generation.

>[!NOTE]
> Please note that Pandrator is still in an alpha stage and I'm not an experienced developer (I'm a noob, in fact), so the code is far from perfect in terms of optimisation, features and reliability. Please keep this in mind and contribute, if you want to help me make it better.

## Samples
The samples were generated using the minimal settings - no LLM text processing, RVC or TTS evaluation, and no sentences were regenerated. Both XTTS and Silero generations were faster than playback speed. 

https://github.com/user-attachments/assets/1c763c94-c66b-4c22-a698-6c4bcf3e875d

https://github.com/lukaszliniewicz/Pandrator/assets/75737665/bbb10512-79ed-43ea-bee3-e271b605580e

https://github.com/lukaszliniewicz/Pandrator/assets/75737665/118f5b9c-641b-4edd-8ef6-178dd924a883

Dubbing sample, including translation ([video source](https://www.youtube.com/watch?v=_SwUpU0E2Eg&t=61s&pp=ygUn0LLRi9GB0YLRg9C_0LvQtdC90LjQtSDQu9C10LPQsNGB0L7QstCw)):

https://github.com/user-attachments/assets/1ba8068d-986e-4dec-a162-3b7cc49052f4

## Requirements

### Hardware Requirements

| Tool       | CPU Requirements                                              | GPU Requirements                                                       |
|------------|---------------------------------------------------------------|-------------------------------------------------------------------------|
| XTTS       | A reasonably modern CPU (for CPU-only generation)              | NVIDIA GPU with 4GB+ of VRAM for good performance                        |
| Silero     | Performs well on most CPUs                   | N/A                                                                     |
| VoiceCraft | Usable on CPU, but generation will be slow                     | NVIDIA GPU with 8GB+ of VRAM for acceleration (4GB VRAM requires kv cache disabled) |

### Dependencies
This project relies on several APIs and services (running locally) and libraries, notably:

#### Required
- [XTTS API Server by daswer123](https://github.com/daswer123/xtts-api-server.git) for Text-to-Speech (TTS) generation using Coqui [XTTSv2](https://huggingface.co/coqui/XTTS-v2) OR [Silero API Server by ouoertheo](https://github.com/ouoertheo/silero-api-server) for TTS generaton using the [Silero models](https://github.com/snakers4/silero-models) OR [VoiceCraft by jasonppy](https://github.com/jasonppy/VoiceCraft).
- [FFmpeg](https://github.com/FFmpeg/FFmpeg) for audio encoding.
- [Sentence Splitter by mediacloud](https://github.com/mediacloud/sentence-splitter) for splitting `.txt ` files into sentences, [customtkinter by TomSchimansky](https://github.com/TomSchimansky/CustomTkinter), [num2words by savoirfairelinux](https://github.com/savoirfairelinux/num2words), and many others. For a full list, see `requirements.txt`.

#### Optional
- [Subdub](https://github.com/lukaszliniewicz/Subdub), a command line app that transcribes video files, translates subtitles and synchronises the generated speech with the video, made specially for Pandrator.
- [WhisperX by m-bain](https://github.com/m-bain/whisperX), an enhanced implementation of OpenAI's Whisper model with improved alignment, used for dubbing and XTTS training. 
- [Easy XTTS Trainer](https://github.com/lukaszliniewicz/easy_xtts_trainer), a command line app that enables XTTS fine-tuning using one or more audio files, made specially for Pandrator.
- [RVC Python by daswer123](https://github.com/daswer123/rvc-python) for enhancing voice quality and cloning results with [Retrieval Based Voice Conversion](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI).
- [Text Generation Webui API by oobabooga](https://github.com/oobabooga/text-generation-webui.git) for LLM-based text pre-processing.
- [NISQA by gabrielmittag](https://github.com/gabrielmittag/NISQA.git) for evaluating TTS generations (using the [FastAPI implementation](https://github.com/lukaszliniewicz/NISQA-API)).

## Installation

### Self-contained packages
I've prepared packages (archives) that you can simply unpack - everything is preinstalled in its own portable conda environment. You can use the launcher to start Pandrator, update it and install new features, depending on the version of the package you downloaded. 

| Package | Contents                                                   | Unpacked Size | Link     |
|---------|-------------------------------------------------------------|---------------|----------|
| 1       | Pandrator and Silero                                        | 4GB           | [Download](https://1drv.ms/u/s!AgSiDu9lV3iMnoVipZuCpbxCWkfaCA?e=Xqbvsl) |
| 2       | Pandrator and XTTS                                          | 14GB          | [Download](https://1drv.ms/u/s!AgSiDu9lV3iMnoVtKm77JrJYOrqEGw?e=sjcAMr) |
| 3       | Pandrator, XTTS, RVC, WhisperX (for dubbing) and XTTS fine-tuning | 36GB          | [Download](https://1drv.ms/u/s!AgSiDu9lV3iMnoVuGyd8Q_thLlS1nQ?e=vfON94) |

### GUI Installer and Launcher (Windows)

![pandrator_installer_launcher_KLoHrNDIps](https://github.com/user-attachments/assets/2be46b49-9e79-4281-89ed-5797bdfbe28b)

Run `pandrator_installer_launcher.exe` with administrator priviliges. You will find it under [Releases](https://github.com/lukaszliniewicz/Pandrator/releases). The executable was created using [pyinstaller](https://github.com/pyinstaller/pyinstaller) from `pandrator_installer_launcher.py` in the repository.

**The file may be flagged as a threat by antivirus software, so you may have to add it as an exception.**

You can choose which TTS engines to install and whether to install the software that enables RVC voice cloning (RVC Python), dubbing (WhisperX) and XTTS fine-tuning (Easy XTTS Trainer). You may install more components later. 

The Installer/Launcher performs the following tasks:

1. Creates the Pandrator folder
2. Installs necessary tools if not already present:
   - C++ Build Tools
   - Calibre
   - winget (if necessary)
3. Installs Miniconda
4. Clones the following repositories:
   - Pandrator
   - Subdub
   - XTTS API Server (if selected)
   - Silero API Server (if selected)
   - VoiceCraft API (if selected)
5. Creates required conda environments
6. Installs all necessary dependencies

**Note:** You can use the Installer/Launcher to launch Pandrator and all the tools at any moment.

If you want to perform the setup again, remove the Pandrator folder it created. Please allow at least a couple of minutes for the initial setup process to download models and install dependencies. Depending on the options you've chosen, it may take up to 30 minutes.

For additional functionality not yet included in the installer:
- Install Text Generation Webui and remember to enable the API (add `--api` to `CMD_FLAGS.txt` in the main directory of the Webui before starting it).
- Set up NISQA API for automatic evaluation of generations.

Please refer to the repositories linked under [Dependencies](#Dependencies) for detailed installation instructions. Remember that the APIs must be running to make use of the functionalities they offer.

### Manual Installation

#### Prerequisites

- Git
- Miniconda or Anaconda (installed system-wide)
- Microsoft Visual C++ Build Tools
- Calibre

#### Installation Steps

1. Install dependencies:
   - Calibre: Download and install from [https://calibre-ebook.com/download_windows](https://calibre-ebook.com/download_windows)
   - Microsoft Visual C++ Build Tools: 
     ```
     winget install --id Microsoft.VisualStudio.2022.BuildTools --override "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended" --accept-package-agreements --accept-source-agreements
     ```

2. Clone the repositories:
   ```
   mkdir Pandrator
   cd Pandrator
   git clone https://github.com/lukaszliniewicz/Pandrator.git
   git clone https://github.com/lukaszliniewicz/Subdub.git
   ```

3. Create and activate a conda environment:
   ```
   conda create -n pandrator_installer python=3.10 -y
   conda activate pandrator_installer
   ```

4. Install Pandrator and Subdub requirements:
   ```
   cd Pandrator
   pip install -r requirements.txt
   cd ../Subdub
   pip install -r requirements.txt
   cd ..
   ```

5. (Optional) Install XTTS:
   ```
   git clone https://github.com/daswer123/xtts-api-server.git
   conda create -n xtts_api_server_installer python=3.10 -y
   conda activate xtts_api_server_installer
   pip install torch==2.1.1+cu118 torchaudio==2.1.1+cu118 --extra-index-url https://download.pytorch.org/whl/cu118
   pip install xtts-api-server
   ```

6. (Optional) Install Silero:
   ```
   conda create -n silero_api_server_installer python=3.10 -y
   conda activate silero_api_server_installer
   pip install silero-api-server
   ```

7. (Optional) Install RVC (Retrieval-based Voice Conversion):
   ```
   conda activate pandrator_installer
   pip install pip==24
   pip install rvc-python
   pip install torch==2.1.1+cu118 torchaudio==2.1.1+cu118 --index-url https://download.pytorch.org/whl/cu118
   ```

8. (Optional) Install WhisperX:
   ```
   conda create -n whisperx_installer python=3.10 -y
   conda activate whisperx_installer
   conda install git -c conda-forge -y
   pip install torch==2.0.1 torchvision==0.15.2 torchaudio==2.0.2 --index-url https://download.pytorch.org/whl/cu118
   conda install cudnn=8.9.7.29 -c conda-forge -y
   conda install ffmpeg -c conda-forge -y
   pip install git+https://github.com/m-bain/whisperx.git
   ```

9. (Optional) Install XTTS Fine-tuning:
   ```
   git clone https://github.com/lukaszliniewicz/easy_xtts_trainer.git
   conda create -n easy_xtts_trainer python=3.10 -y
   conda activate easy_xtts_trainer
   cd easy_xtts_trainer
   pip install -r requirements.txt
   pip install torch==2.1.1+cu118 torchaudio==2.1.1+cu118 --index-url https://download.pytorch.org/whl/cu118
   cd ..
   ```

#### Running the Components

1. Run Pandrator:
   ```
   conda activate pandrator_installer
   cd Pandrator
   python pandrator.py
   ```

2. Run XTTS API Server (if installed):
   ```
   conda activate xtts_api_server_installer
   python -m xtts_api_server
   ```
   Additional options:
   - For CPU only: Add `--device cpu`
   - For low VRAM: Add `--lowvram`
   - To use DeepSpeed: Add `--deepspeed`

3. Run Silero API Server (if installed):
   ```
   conda activate silero_api_server_installer
   python -m silero_api_server
   ```

#### Folder Structure

After installation, your folder structure should look like this:

```
Pandrator/
├── Pandrator/
├── Subdub/
├── xtts-api-server/ (if XTTS is installed)
├── easy_xtts_trainer/ (if XTTS Fine-tuning is installed)
```

For more detailed information on using specific components or troubleshooting, please refer to the documentation of each individual repository.

## Quick Start Guide
### Basic Usage
If you don't want to use the additional functionalities, you have everything you need in the **Session tab**. 

1. Either create a new session or load an existing one (select a folder in `Outputs` to do that).
2. Choose your `.txt`, `.srt`, `.pdf` or `epub` file. If you choose a PDF or EPUB file, a preview window will open with the extracted text. You may edit it (OCRed books often have poorly recognized text from the title page, for example). Files that contain a lot of text, regardless of format, can take a long time to finish preprocessing before generation begins. The GUI will freeze, but as long as there is processor activity, it's simply working. For whole books, expect 10m+ for preprocessing.
3. Select the TTS server you want to use - XTTS, Silero or VoiceCraft - and the language from the dropdown (VoiceCraft currently supports only English).
4. Choose the voice you want to use.
   1. **XTTS**, voices are short, 6-12s `.wav` files (22050hz sample rate, mono) stored in the `tts_voices` directory. The XTTS model uses the audio to clone the voice. It doesn't matter what language the sample is in, you will be able to generate speech in all supported languages, but the quality will be best if you provide a sample in your target language. You may use the sample one in the repository or upload your own. Please make sure that the audio is between 6 and 12s, mono, and the sample rate is 22050hz. You may use a tool like Audacity to prepare the files. The less noise, the better. You may use a tool like [Resemble AI](https://github.com/resemble-ai/resemble-enhance) for denoising and/or enhancement of your samples on [Hugging Face](https://huggingface.co/spaces/ResembleAI/resemble-enhance).
   2. **Silero** offers a number of voices for each language it supports. It doesn't support voice cloning. Simply select a voice from the dropdown after choosing the language.
   3. **VoiceCraft** works similarly to XTTS in that it clones the voice from a `.wav ` sample. However, it needs both a properly formatted `.wav` file (mono, 16000hz) and a `.txt` file with the transcription of what is said in the sample. The files must have the same name (apart from the extension, of course). You need to upload them to `tts_voices/VoiceCraft` and you will be able to select them in the GUI. Currently only English is supported. If you generate with a new voice for the first time, the server will perform the alignment procedure, so the first sentence will be generated with a delay. This won't happen when you use that voice again.
6. If you want, you can either slow down or speed up the generated audio (type in or choose a ratio, e.g. 1.1, which is 10% faster than generated; it may be especially useful for dubbing).
7. If you chose an `.srt` file, you will be given the option to select a video file and one of its audio tracks to mix with the synchronized output, as well as weather you want to lower the volume of the original audio when subtitle audio is playing.
8. Start the generation. You may stop and resume it later, or close the programme and load the session later.
9. You can play back the generated sentences, also as a playlist, edit them (the text for regeneration), regenerate or remove individual ones.
10. "Save Output" concatenates the sentences generated so far an encodes them as one file (default is `.opus` at 64k bitrate; you may change it in the Audio tab to `.wav` or `.mp3`).

### General Audio Settings
1. You can change the lenght of silence appended to the end of sentences and paragraphs.
2. You can enable a fade-in and -out effect and set the duration.
3. You can choose the output format and bitrate.

### General Text Pre-Processing Settings
1. You can disable/enable splitting long sentences and set the max lenght a text fragment sent for TTS generation may have (enabled by default; it tries to split sentences whose lenght exceeds the max lenght value; it looks for punctuation marks (, ; : -) and chooses the one closest to the midpoint of the sentence; if there are no punctuation marks, it looks for conjunctions like "and"); it performs this operation twice as some sentence fragments may still be too long after just one split.
2. You can disable/enable appending short sentences (to preceding or following sentences; disabled by default, may perhaps improve the flow as the lenght of text fragments sent to the model is more uniform).
3. Remove diacritics (useful when generating a text that contains many foreign words or transliterations from foreign alphabets, e.g. Japanese). Do not enable this if you generate in a language that needs diacritics, like German or Polish! The pronounciation will be wrong then.

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
- [ ] Add support for Surya for PDF OCR, layout and redeaing order detection, plus preprocessing of chapters, headers, footers, footnotes and tables. 
- [ ] Add support for StyleTTS2
- [ ] Add importing/exporting settings.
- [ ] Add support for proprietary APIs for text pre-processing and TTS generation.
- [ ] Include OCR for PDFs.
- [ ] Add support for a higher quality local TTS model, Tortoise.
- [ ] Add option to record a voice sample and use it for TTS to the GUI.
- [x] Add support for chapter segmentation
- [x] Add all API servers to the setup script.
- [x] Add support for custom XTTS models 
- [x] Add workflow to create dubbing from `.srt` subtitle files.
- [x] Include support for PDF files.
- [x] Integrate editing capabilities for processed sentences within the UI.
- [x] Add support for a lower quality but faster local TTS model that can easily run on CPU, e.g. Silero or Piper.
- [x] Add support for EPUB.


