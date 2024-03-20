@echo off

REM Set the base directory path
set BASE_DIR=%cd%

REM Check if conda is available in path
where conda >nul 2>nul
if %errorlevel% neq 0 (
    echo Conda not found. Installing Miniconda...
    REM Download Miniconda installer
    curl -O https://repo.anaconda.com/miniconda/Miniconda-latest-Windows-x86_64.exe
    REM Install Miniconda silently in a subfolder
    start /wait "" Miniconda-latest-Windows-x86_64.exe /InstallationType=JustMe /RegisterPython=0 /S /D=%BASE_DIR%\miniconda
    REM Add Miniconda to PATH
    set PATH=%BASE_DIR%\miniconda;%BASE_DIR%\miniconda\Scripts;%PATH%
)

REM Create and activate audiobook_generator conda environment
if not exist audiobook_generator\audiobook_generator (
    conda create -y -n audiobook_generator python=3.10
)
call conda activate audiobook_generator

REM Install requirements for audiobook_generator
pip install -r audiobook_generator\requirements.txt

REM Clone the xtts-api-server repository
if not exist Tools\xtts-api-server (
    mkdir Tools
    cd Tools
    git clone https://github.com/daswer123/xtts-api-server.git
    cd ..
)

REM Create and activate xtts_api_server conda environment
if not exist Tools\xtts-api-server\xtts_api_server (
    conda create -y -n xtts_api_server python=3.10
)
call conda activate xtts_api_server

REM Install xtts-api-server and PyTorch
pip install xtts-api-server

REM Start the xtts server
start "XTTS Server" cmd /c "call conda activate xtts_api_server & cd Tools\xtts-api-server & python -m xtts_api_server"

REM Start the audiobook_generator
call conda activate audiobook_generator
cd audiobook_generator
python audiobook_generator.py
