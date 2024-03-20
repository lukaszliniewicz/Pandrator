@echo off

REM Check if conda is installed
where conda >nul 2>nul
if %errorlevel% neq 0 (
    echo Conda not found. Installing Conda...
    REM Download Miniconda installer
    curl -O https://repo.anaconda.com/miniconda/Miniconda-latest-Windows-x86_64.exe
    REM Silently install Miniconda
    start /wait "" Miniconda-latest-Windows-x86_64.exe /InstallationType=JustMe /RegisterPython=0 /S /D=%UserProfile%\Miniconda3
    REM Remove the installer after installation
    del Miniconda-latest-Windows-x86_64.exe
)

REM Create Audiobook_Generator_and_Tools directory
mkdir Audiobook_Generator_and_Tools
cd Audiobook_Generator_and_Tools

REM Clone Audiobook-Generator repository
git clone https://github.com/lukaszliniewicz/Audiobook-Generator.git
cd Audiobook-Generator

REM Create and activate audiobook_generator conda environment
conda create -n audiobook_generator python=3.10 -y
call activate audiobook_generator

REM Install requirements for Audiobook-Generator
pip install -r requirements.txt

REM Go back to Audiobook_Generator_and_Tools directory
cd ..

REM Clone xtts-api-server repository
git clone https://github.com/daswer123/xtts-api-server.git
cd xtts-api-server

REM Create and activate xtts_api_server conda environment
conda create -n xtts_api_server python=3.10 -y
call activate xtts_api_server

REM Install xtts-api-server and PyTorch dependencies
pip install xtts-api-server
pip install torch==2.1.1+cu118 torchaudio==2.1.1+cu118 --index-url https://download.pytorch.org/whl/cu118

REM Copy sample_male.wav from generation directory to speakers directory
xcopy /y "..\Audiobook-Generator\generation\sample_male.wav" ".\speakers\"

REM Start the xtts server
start "xtts server" cmd /c "python -m xtts_api_server --deepspeed"

REM Go back to Audiobook-Generator directory
cd ..\Audiobook-Generator

REM Activate audiobook_generator environment
call activate audiobook_generator

REM Start the audiobook_generator.py script
start "audiobook generator" cmd /c "python audiobook_generator.py"
