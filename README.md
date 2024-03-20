# Audiobook Generator

Audiobook Generator is a tool designed to transform text files into spoken audio using a variety of APIs and processing techniques. 
It is still in alpha stage and I'm not an experience developer, so the code is far from perfect, both in terms of optimisation, features and reliability. Please keep this in mind.
It leverages the the XTTS model(s) for text-to-speech conversion, enhanced by RVC_CLI for voice cloning and NISQA for audio quality evaluation. Additionally, it incorporates Text Generation Webui's API for additional local LLM-based text pre-processing, enabling a wide range of text manipulations before audio generation.

## Requirments

### Hardware
I was able to run all functionalities on a laptop with a Ryzen 5600h and a 3050 (4GB of VRAM). It's likely that you will need 16GB of RAM, a processor with 8+ threads, and ideally an NVIDIA GPU with 6 GB+ of VRAM for optimal performance. 

### Dependencies
This project relies on several APIs and services (running locally), including:
- [XTTS API Server by daswer123](https://github.com/daswer123/xtts-api-server.git)
- [Text Generation Webui API by oobabooga](https://github.com/oobabooga/text-generation-webui.git)
- [RVC_CLI by blaise-tk](https://github.com/blaise-tk/RVC_CLI.git)
- [NISQA by gabrielmittag](https://github.com/gabrielmittag/NISQA.git)

## Installation

### Minimal One-Click Installation Script:
Run `min_start.bat` from the command line. This script automates the setup by installing Conda, creating an environment, installing XTTS API Server, placing a sample voice file in its speakers directory, starting it and the Audiobook Generator. 

For additional functionality:
- Enable the 'Text Generation Webui API'.
- Set up 'RVC_CLI' for enhancing generations with RVC.
- Set up 'NISQA API' for automatic evaluation of generations.
Please refer to the repositories linked above for excellent installation instructions. Remember that the APIs must be running to make use of the functionalities they offer.

### Manual Installation:
1. Make sure that Python 3 is installed.
2. Clone this repository (git clone https://github.com/lukaszliniewicz/Audiobook-Generator/) using for example Windows Terminal.
3. `cd` to the repository directory.
4. Install requirements using `pip install -r requirements.txt`.
5. Run `python Audiobook_Generator/audiobook_generator.py`.

## Features
- **Text Pre-processing:** Splits text into sentences while preserving paragraphs. Profiles for multiple languages are available.
- **LLM Text Pre-processing:** Utilizes a local LLM for text corrections and enhancements with up to three different prompts run sequentially, and an evaluation mechanism that asks the model to perform a tas twice and then choose the better response. I've been using 'openchat-3.5-0106.Q5_K_M.gguf' with good results, as well as for example 'Mistral 7B Instruct 0.2'.
- **Audio Generation:** Converts processed text into speech, with options for voice cloning and quality enhancement.
- **Audio Evaluation:** An experimental feature that predicts Mean Opinion Score (MOS) for generated sentences and sets a score threshold or chooses the best score from a set number of generations.
- **Session Management:** Supports creating, deleting, and loading sessions for organized workflow.
- **GUI:** Built with customtkinker for a user-friendly experience.

## Usage
Follow the manual or minimal installation steps to set up the Audiobook Generator. Once the setup is complete, the GUI will guide you through the process of converting text files into audiobooks.

## Contributing
Contributions, suggestions for improvements, and bug reports are welcome. Please refer to the contributing guidelines for more information.

## To-do
- [ ] Develop a robust installation and startup script.
- [ ] Add support for proprietary APIs for text and LLM pre-processing.
- [ ] Enhance file format support (e.g., HTML, XML, PDF) including direct PDF to TXT conversion with OCR.
- [ ] Integrate editing capabilities for processed sentences within the UI.
- [ ] Add other local TTS models, e.g. Tortoise. 
