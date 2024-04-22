import os
import subprocess
import logging
import time
import shutil
import requests
import traceback

# Configure logging
logging.basicConfig(filename='installation.log', level=logging.DEBUG,
                    format='%(asctime)s - %(levelname)s - %(message)s')

def run_command(command):
    try:
        logging.info(f"Running command: {' '.join(command)}")
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        logging.debug(f"Command output: {result.stdout}")
        return result.stdout
    except subprocess.CalledProcessError as e:
        logging.error(f"Error executing command: {command}")
        logging.error(f"Error message: {str(e)}")
        logging.error(f"Error output: {e.stderr}")
        raise

def check_program_installed(program):
    logging.info(f"Checking if {program} is installed...")
    try:
        result = shutil.which(program)
        logging.info(f"{program} is {'installed' if result else 'not installed'}")
        return result is not None
    except Exception as e:
        logging.error(f"Error checking if {program} is installed: {str(e)}")
        logging.error(traceback.format_exc())
        raise

def check_choco():
    return check_program_installed('choco')

def install_choco():
    logging.info("Installing Chocolatey...")
    try:
        run_command(['powershell', '-Command', "Set-ExecutionPolicy Bypass -Scope Process -Force; [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://chocolatey.org/install.ps1'))"])
    except Exception as e:
        logging.error(f"Error installing Chocolatey: {str(e)}")
        logging.error(traceback.format_exc())
        raise

def install_dependencies():
    dependencies = ['git', 'curl', 'ffmpeg', 'calibre']
    for dependency in dependencies:
        if not check_program_installed(dependency):
            logging.info(f"Installing {dependency}...")
            try:
                run_command(['choco', 'install', dependency, '-y'])
            except subprocess.CalledProcessError as e:
                logging.error(f"Failed to install {dependency}.")
                raise
        else:
            logging.info(f"{dependency} is already installed.")

def install_conda(install_path):
    logging.info("Installing Miniconda...")
    try:
        conda_installer = 'Miniconda3-latest-Windows-x86_64.exe'
        run_command(['curl', '-O', f'https://repo.anaconda.com/miniconda/{conda_installer}'])
        run_command([conda_installer, '/InstallationType=JustMe', '/RegisterPython=0', '/S', f'/D={install_path}'])
        os.remove(conda_installer)
    except Exception as e:
        logging.error(f"Error installing Miniconda: {str(e)}")
        logging.error(traceback.format_exc())
        raise

def check_conda(conda_path):
    logging.info(f"Checking if conda is installed at {conda_path}...")
    try:
        conda_exe = os.path.join(conda_path, 'Scripts', 'conda.exe')
        result = os.path.exists(conda_exe)
        logging.info(f"Conda is {'installed' if result else 'not installed'} at {conda_path}")
        return result
    except Exception as e:
        logging.error(f"Error checking if conda is installed at {conda_path}: {str(e)}")
        logging.error(traceback.format_exc())
        raise

def create_conda_env(conda_path, env_name, python_version):
    logging.info(f"Creating conda environment {env_name}...")
    try:
        run_command([f'{conda_path}\\Scripts\\conda.exe', 'create', '-n', env_name, f'python={python_version}', '-y'])
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to create conda environment {env_name}")
        logging.error(f"Error message: {str(e)}")
        raise

def install_requirements(conda_path, env_name, requirements_file):
    logging.info(f"Installing requirements for {env_name} from {requirements_file}...")
    try:
        run_command([f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'pip', 'install', '-r', requirements_file])
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to install requirements for {env_name} from {requirements_file}")
        logging.error(f"Error message: {str(e)}")
        raise

def install_voicecraft_api_dependencies(conda_path, env_name):
    logging.info(f"Installing VoiceCraft API dependencies in {env_name}...")
    try:
        run_command([f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'conda', 'install', 'pytorch==2.0.1', 'torchvision==0.15.2', 'torchaudio==2.0.2', 'pytorch-cuda=11.7', '-c', 'pytorch', '-c', 'nvidia', '-y'])
        run_command([f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'conda', 'install', '-c', 'conda-forge', 'montreal-forced-aligner=2.2.17', 'openfst=1.8.2', 'kaldi=5.5.1068', '-y'])
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to install VoiceCraft API dependencies in {env_name}")
        logging.error(f"Error message: {str(e)}")
        raise

def download_mfa_models(conda_path, env_name):
    logging.info(f"Downloading MFA models in {env_name}...")
    try:
        run_command([f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'mfa', 'model', 'download', 'dictionary', 'english_us_arpa'])
        run_command([f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'mfa', 'model', 'download', 'acoustic', 'english_us_arpa'])
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to download MFA models in {env_name}")
        logging.error(f"Error message: {str(e)}")
        raise

def install_audiocraft(conda_path, env_name, voicecraft_repo_path):
    logging.info(f"Installing audiocraft package in {env_name}...")
    try:
        audiocraft_repo = 'https://github.com/facebookresearch/audiocraft.git'
        audiocraft_commit = 'c5157b5bf14bf83449c17ea1eeb66c19fb4bc7f0'
        
        # Change to the VoiceCraft repository directory
        os.chdir(voicecraft_repo_path)
        
        # Install audiocraft package
        run_command([f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'pip', 'install', '-e', f'git+{audiocraft_repo}@{audiocraft_commit}#egg=audiocraft'])
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to install audiocraft package in {env_name}")
        logging.error(f"Error message: {str(e)}")
        raise

def run_script(conda_path, env_name, script_path):
    logging.info(f"Running script {script_path} in {env_name}...")
    try:
        # Change to the directory of the pandrator script
        script_dir = os.path.dirname(script_path)
        os.chdir(script_dir)
        
        subprocess.Popen([f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'python', script_path])
    except Exception as e:
        logging.error(f"Failed to run script {script_path} in {env_name}")
        logging.error(f"Error message: {str(e)}")
        logging.error(traceback.format_exc())
        raise

def run_voicecraft_api_server(conda_path, env_name, api_script_path, voicecraft_repo_path):
    logging.info(f"Running VoiceCraft API server in {env_name}...")
    try:
        # Change to the VoiceCraft repository directory
        os.chdir(voicecraft_repo_path)
        
        voicecraft_server_command = [f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'python', api_script_path]
        subprocess.Popen(voicecraft_server_command, creationflags=subprocess.CREATE_NEW_CONSOLE)
    except Exception as e:
        logging.error(f"Failed to run VoiceCraft API server in {env_name}")
        logging.error(f"Error message: {str(e)}")
        logging.error(traceback.format_exc())
        raise

def check_voicecraft_server_online(url, max_attempts=30, wait_interval=10):
    attempt = 1
    while attempt <= max_attempts:
        try:
            logging.info(f"Checking if VoiceCraft server is online at {url} (Attempt {attempt}/{max_attempts})...")
            response = requests.get(url)
            if response.status_code == 200:
                logging.info("VoiceCraft server is online.")
                return True
        except requests.exceptions.RequestException as e:
            logging.warning(f"VoiceCraft server is not online. Waiting... (Attempt {attempt}/{max_attempts})")
        
        time.sleep(wait_interval)
        attempt += 1
    
    logging.error("VoiceCraft server failed to come online within the specified attempts.")
    return False


def replace_files(repo_path, file_mappings):
    for src_file, dest_file in file_mappings.items():
        src_path = os.path.join(repo_path, src_file)
        dest_path = os.path.join(repo_path, dest_file)
        try:
            shutil.copy2(src_path, dest_path)
            logging.info(f"Replaced file: {dest_file}")
        except Exception as e:
            logging.error(f"Failed to replace file: {dest_file}")
            logging.error(f"Error message: {str(e)}")
            logging.error(traceback.format_exc())
            raise

def main():
    # Create Pandrator folder
    pandrator_path = os.path.join(os.getcwd(), 'Pandrator')
    
    if not os.path.exists(pandrator_path):
        # Check if Chocolatey is installed, if not, install it
        if not check_choco():
            logging.info("Chocolatey is not installed.")
            install_choco()
        
        # Check and install dependencies
        install_dependencies()
        
        os.makedirs(pandrator_path, exist_ok=True)
        logging.info(f"Created Pandrator folder at {pandrator_path}")

        # Clone repositories
        logging.info("Cloning repositories...")
        try:
            run_command(['git', 'clone', 'https://github.com/lukaszliniewicz/VoiceCraft_API.git', os.path.join(pandrator_path, 'VoiceCraft_API')])
            run_command(['git', 'clone', 'https://github.com/lukaszliniewicz/Pandrator.git', os.path.join(pandrator_path, 'Pandrator')])
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to clone repositories")
            logging.error(f"Error message: {str(e)}")
            raise

        # Install Miniconda
        conda_path = os.path.join(pandrator_path, 'conda')
        install_conda(conda_path)

        # Check if conda is installed correctly
        if not check_conda(conda_path):
            logging.error("Conda installation failed. Please check the installation logs.")
            return

        voicecraft_env_name = 'voicecraft_api_installer'
        voicecraft_repo_path = os.path.join(pandrator_path, 'VoiceCraft_API')

        if not os.path.exists(os.path.join(conda_path, 'envs', voicecraft_env_name)):
            # Create voicecraft_api_installer environment
            create_conda_env(conda_path, voicecraft_env_name, '3.9.16')
            
            # Install VoiceCraft API dependencies
            install_requirements(conda_path, voicecraft_env_name, os.path.join(voicecraft_repo_path, 'requirements.txt'))
            install_voicecraft_api_dependencies(conda_path, voicecraft_env_name)
            download_mfa_models(conda_path, voicecraft_env_name)
            
            # Install audiocraft package
            install_audiocraft(conda_path, voicecraft_env_name, voicecraft_repo_path)
        else:
            logging.info(f"Environment {voicecraft_env_name} already exists. Skipping installation.")

        # Create pandrator_installer environment
        create_conda_env(conda_path, 'pandrator_installer', '3.10')

        # Replace files in the VoiceCraft repo
        file_mappings = {
            'audiocraft_windows/cluster.py': 'src/audiocraft/audiocraft/utils/cluster.py',
            'audiocraft_windows/environment.py': 'src/audiocraft/audiocraft/environment.py',
            'audiocraft_windows/checkpoint.py': 'src/audiocraft/audiocraft/utils/checkpoint.py'
        }
        replace_files(voicecraft_repo_path, file_mappings)

        # Download pretrained models
        download_pretrained_models(voicecraft_repo_path)

        # Install requirements for pandrator
        pandrator_repo_path = os.path.join(pandrator_path, 'Pandrator')
        install_requirements(conda_path, 'pandrator_installer', os.path.join(pandrator_repo_path, 'requirements.txt'))
    else:
        logging.info("Pandrator folder exists. Skipping installation steps.")
        
    # Get the conda path
    conda_path = os.path.join(pandrator_path, 'conda')
    voicecraft_repo_path = os.path.join(pandrator_path, 'VoiceCraft_API')

    # Run VoiceCraft API server
    api_script_path = os.path.join(voicecraft_repo_path, 'api.py')
    run_voicecraft_api_server(conda_path, 'voicecraft_api_installer', api_script_path, voicecraft_repo_path)
    
    # Wait for VoiceCraft server to come online
    voicecraft_server_url = 'http://127.0.0.1:8245/docs'
    if not check_voicecraft_server_online(voicecraft_server_url):
        logging.error("VoiceCraft server failed to come online. Exiting...")
        return

    # Run pandrator script
    pandrator_script_path = os.path.join(pandrator_path, 'Pandrator', 'pandrator.py')
    run_script(conda_path, 'pandrator_installer', pandrator_script_path)
    
    # Keep the main script running to prevent immediate exit
    while True:
        time.sleep(1)
if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logging.error(f"An error occurred during execution: {str(e)}")
        logging.error(traceback.format_exc())
   
