<p align="left">
  <img src="pandrator.png" alt="Icon" width="200" height="200"/>
</p>

# Pandrator, an audiobook generator

Pandrator is a tool designed to transform text into spoken audio using a variety of APIs and processing techniques. 
It is still in alpha stage and I'm not an experience developer, so the code is far from perfect in terms of optimisation, features and reliability. Please keep this in mind.
It leverages the XTTS model(s) for text-to-speech conversion, enhanced by RVC_CLI for quality improvement and better voice cloning results, and NISQA for audio quality evaluation. Additionally, it incorporates Text Generation Webui's API for local LLM-based text pre-processing, enabling a wide range of text manipulations before audio generation.

## Requirments

### Hardware
I was able to run all functionalities on a laptop with a Ryzen 5600h and a 3050 (4GB of VRAM). It's likely that you will need at least 16GB of RAM, a reasonably modern CPU, and ideally an NVIDIA GPU with 4 GB+ of VRAM for usable performance. Consult the requirments of the services listed below.

### Dependencies
This project relies on several APIs and services (running locally), including:
- [XTTS API Server by daswer123](https://github.com/daswer123/xtts-api-server.git) (required) 
- [Text Generation Webui API by oobabooga](https://github.com/oobabooga/text-generation-webui.git) (optional)
- [RVC_CLI by blaise-tk](https://github.com/blaise-tk/RVC_CLI.git) (optional) 
- [NISQA by gabrielmittag](https://github.com/gabrielmittag/NISQA.git) (optional)

## Installation

### Minimal One-Click Installation Executable (Windows with an Nvidia GPU only):
Run `pandrator_start_minimal.exe` with administrator priviliges. The executable was created usinng `pyinstaller` from `pandrator_start_minimal.py` in the repository.

**It may be flagged as a threat by antivirus software, so you may have to add it as an exception.**

It creates a Pandrator folder, installs `curl`, `git`, `ffmpeg` and `Miniconda`, creates an environment, clones the XTTS Api Server repository and the Pandrator repository, and launches them. You may use it to launch Pandrator later. If you want to perform the setup again, you have to remove the Pandrator folder it created. 

For additional functionality:
- Install Text Generation Webui and remember to enable the API.
- Set up `RVC_CLI` for enhancing generations with RVC.
- Set up `NISQA API` for automatic evaluation of generations.
Please refer to the repositories linked above for detailed installation instructions. Remember that the APIs must be running to make use of the functionalities they offer.

### Manual Installation:
1. Make sure that Python 3 is installed.
2. Install and run at last XTTS API Server. 
3. Clone this repository.
4. `cd` to the repository directory.
5. Install requirements using `pip install -r requirements.txt`.
6. Run `python pandrator.py`.

## Features
- **Text Pre-processing:** Splits text into sentences and (attempts to) preserve paragraphs. Profiles for multiple languages are available.
- **LLM Text Pre-processing:** Utilizes a local LLM for text corrections and enhancements with up to three different prompts run sequentially, and an evaluation mechanism that asks the model to perform a task twice and then choose the better response. I've been using 'openchat-3.5-0106.Q5_K_M.gguf' with good results, as well as for example 'Mistral 7B Instruct 0.2'. Different models may perform different tasks well, so it's possible to choose a specific model for a specific prompt.
- **Audio Generation:** Converts processed text into speech, with options for voice cloning and quality enhancement.
- **Audio Evaluation:** An experimental feature that predicts Mean Opinion Score (MOS) for generated sentences and sets a score threshold or chooses the best score from a set number of generations.
- **Session Management:** Supports creating, deleting, and loading sessions for organized workflow.
- **GUI:** Built with customtkinker for a user-friendly experience.

## Usage
Follow the manual or minimal installation steps to set up the Audiobook Generator. Once the setup is complete, the GUI will guide you through the process of converting text files into audiobooks.

## Contributing
Contributions, suggestions for improvements, and bug reports are welcome. Please refer to the contributing guidelines for more information.

## To-do
- [ ] Add the other APIs to the setup script.
- [ ] Add importing/exporting settings.
- [ ] Add support for proprietary APIs for text pre-processing and TTS generation.
- [ ] Enhance file format support (e.g., HTML, XML, PDF, Epub) including direct PDF to TXT conversion with OCR.
- [ ] Integrate editing capabilities for processed sentences within the UI.
- [ ] Add support for a higher quality local TTS model, Tortoise.
- [ ] Add support for a lower quality but faster local TTS model that can easily run on CPU, e.g. Silero or Piper.
- [ ] Implement a better text segmentation method, e.g. NLP-based.
- [ ] Add option to record a voice sample and use it for TTS.
