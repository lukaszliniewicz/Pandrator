import os
import subprocess
import logging
import time
import shutil
import requests

# Configure logging
logging.basicConfig(filename='installation.log', level=logging.DEBUG,
                    format='%(asctime)s - %(levelname)s - %(message)s')

def run_command(command):
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as e:
        logging.error(f"Error executing command: {command}")
        logging.error(f"Error message: {str(e)}")
        raise

def check_program_installed(program):
    return shutil.which(program) is not None

def check_choco():
    return check_program_installed('choco')

def install_choco():
    logging.info("Installing Chocolatey...")
    run_command(['powershell', '-Command', "Set-ExecutionPolicy Bypass -Scope Process -Force; [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://chocolatey.org/install.ps1'))"])

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

def install_conda(install_path):
    logging.info("Installing Miniconda...")
    conda_installer = 'Miniconda3-latest-Windows-x86_64.exe'
    run_command(['curl', '-O', f'https://repo.anaconda.com/miniconda/{conda_installer}'])
    run_command([conda_installer, '/InstallationType=JustMe', '/RegisterPython=0', '/S', f'/D={install_path}'])
    os.remove(conda_installer)

def check_conda(conda_path):
    return os.path.exists(os.path.join(conda_path, 'Scripts', 'conda.exe'))

def create_conda_env(conda_path, env_name, python_version):
    logging.info(f"Creating conda environment {env_name}...")
    try:
        run_command([f'{conda_path}\\Scripts\\conda.exe', 'create', '-n', env_name, f'python={python_version}', '-y'])
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to create conda environment {env_name}")
        logging.error(f"Error message: {str(e)}")
        raise

def install_requirements(conda_path, env_name, requirements_file):
    logging.info(f"Installing requirements for {env_name}...")
    run_command([f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'pip', 'install', '-r', requirements_file])

def install_package(conda_path, env_name, package):
    logging.info(f"Installing {package} in {env_name}...")
    run_command([f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'pip', 'install', package])

def install_silero_api_server(conda_path, env_name):
    logging.info(f"Installing Silero API server in {env_name}...")
    install_package(conda_path, env_name, 'requests')
    install_package(conda_path, env_name, 'silero-api-server')

def run_script(conda_path, env_name, script_path):
    logging.info(f"Running script {script_path} in {env_name}...")
    
    # Change to the directory of the pandrator script
    script_dir = os.path.dirname(script_path)
    os.chdir(script_dir)
    
    subprocess.Popen([f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'python', script_path], creationflags=subprocess.DETACHED_PROCESS)

def run_silero_api_server(conda_path, env_name):
    logging.info(f"Running Silero API server in {env_name}...")

    # Create log file for silero server output
    silero_log_file = os.path.join(os.getcwd(), 'silero_server.log')

    # Run silero server command with output redirection
    silero_server_command = f'"{conda_path}\\Scripts\\conda.exe" run -n {env_name} python -m silero_api_server > "{silero_log_file}" 2>&1'
    subprocess.Popen(silero_server_command, shell=True)

def check_silero_server_online(url, max_attempts=30, wait_interval=10):
    attempt = 1
    while attempt <= max_attempts:
        try:
            response = requests.get(url)
            if response.status_code == 200:
                logging.info("Silero server is online.")
                return True
        except requests.exceptions.RequestException as e:
            logging.info(f"Silero server is not online. Waiting... (Attempt {attempt}/{max_attempts})")
        
        time.sleep(wait_interval)
        attempt += 1
    
    logging.error("Silero server failed to come online within the specified attempts.")
    return False

def check_and_update_numpy(conda_path, env_name):
    logging.info(f"Checking NumPy version in {env_name}...")
    try:
        # Check current NumPy version
        numpy_version = subprocess.check_output([f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'python', '-c', 'import numpy; print(numpy.__version__)'], universal_newlines=True).strip()
        logging.info(f"Current NumPy version: {numpy_version}")
        
        # If NumPy version is 2.x, downgrade to 1.24.3
        if numpy_version.startswith('2.'):
            logging.info("Downgrading NumPy to version 1.24.3...")
            run_command([f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'pip', 'install', 'numpy==1.24.3'])
            logging.info("NumPy downgraded successfully.")
        else:
            logging.info("NumPy version is compatible. No changes needed.")
    except subprocess.CalledProcessError as e:
        logging.error("Error checking or updating NumPy version.")
        logging.error(f"Error message: {str(e)}")
        raise

def install_pytorch(conda_path, env_name):
    logging.info(f"Installing PyTorch 1.13.1 in {env_name}...")
    try:
        run_command([f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'pip', 'install', 'torch==1.13.1', 'torchvision==0.14.1', 'torchaudio==0.13.1'])
        logging.info("PyTorch 1.13.1 installed successfully.")
    except subprocess.CalledProcessError as e:
        logging.error("Error installing PyTorch.")
        logging.error(f"Error message: {str(e)}")
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
        run_command(['git', 'clone', 'https://github.com/lukaszliniewicz/Pandrator.git', os.path.join(pandrator_path, 'Pandrator')])

        # Install Miniconda
        conda_path = os.path.join(pandrator_path, 'conda')
        install_conda(conda_path)

        # Check if conda is installed correctly
        if not check_conda(conda_path):
            logging.error("Conda installation failed. Please check the installation logs.")
            return

        # Create conda environments
        create_conda_env(conda_path, 'silero_api_server_installer', '3.10')
        create_conda_env(conda_path, 'pandrator_installer', '3.10')

        # Install Silero API server
        install_silero_api_server(conda_path, 'silero_api_server_installer')

        # Install requirements for pandrator
        pandrator_repo_path = os.path.join(pandrator_path, 'Pandrator')
        install_requirements(conda_path, 'pandrator_installer', os.path.join(pandrator_repo_path, 'requirements.txt'))
    else:
        logging.info("Pandrator folder exists. Skipping installation steps.")
        
    # Get the conda path
    conda_path = os.path.join(pandrator_path, 'conda')

    # Check and update NumPy version
    check_and_update_numpy(conda_path, 'silero_api_server_installer')

    # Install PyTorch 1.13.1
    install_pytorch(conda_path, 'silero_api_server_installer')

    # Run Silero API server
    run_silero_api_server(conda_path, 'silero_api_server_installer')
    
    # Wait for Silero server to come online
    silero_server_url = 'http://127.0.0.1:8001/docs'
    if not check_silero_server_online(silero_server_url):
        logging.error("Silero server failed to come online. Exiting...")
        return

    # Run pandrator script
    pandrator_script_path = os.path.join(pandrator_path, 'Pandrator', 'pandrator.py')
    run_script(conda_path, 'pandrator_installer', pandrator_script_path)
    
    # Keep the main script running to prevent immediate exit
    while True:
        time.sleep(1)

if __name__ == '__main__':
    main()
