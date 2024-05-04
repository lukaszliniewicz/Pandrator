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

def install_pytorch_and_xtts_api_server(conda_path, env_name):
    logging.info(f"Installing PyTorch and xtts_api_server package in {env_name}...")
    
    try:
        # Install PyTorch
        pytorch_cmd = [f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'pip', 'install', 'torch==2.1.1+cu118', 'torchaudio==2.1.1+cu118', '--extra-index-url', 'https://download.pytorch.org/whl/cu118']
        pytorch_result = subprocess.run(pytorch_cmd, capture_output=True, text=True)
        
        logging.info("PyTorch installation output:")
        logging.info(pytorch_result.stdout)
        
        if pytorch_result.returncode != 0:
            logging.error("Error installing PyTorch:")
            logging.error(pytorch_result.stderr)
            raise subprocess.CalledProcessError(pytorch_result.returncode, pytorch_cmd, output=pytorch_result.stdout, stderr=pytorch_result.stderr)
        
        # Install xtts_api_server package
        xtts_cmd = [f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'pip', 'install', 'xtts_api_server']
        xtts_result = subprocess.run(xtts_cmd, capture_output=True, text=True)
        
        logging.info("xtts_api_server installation output:")
        logging.info(xtts_result.stdout)
        
        if xtts_result.returncode != 0:
            logging.error("Error installing xtts_api_server package:")
            logging.error(xtts_result.stderr)
            raise subprocess.CalledProcessError(xtts_result.returncode, xtts_cmd, output=xtts_result.stdout, stderr=xtts_result.stderr)
        
        logging.info("PyTorch and xtts_api_server package installed successfully.")
    except subprocess.CalledProcessError as e:
        logging.error(f"Error executing command: {e.cmd}")
        logging.error(f"Error message: {str(e)}")
        logging.error(f"Output: {e.output}")
        logging.error(f"Error: {e.stderr}")
        raise

def run_script(conda_path, env_name, script_path):
    logging.info(f"Running script {script_path} in {env_name}...")
    
    # Change to the directory of the pandrator script
    script_dir = os.path.dirname(script_path)
    os.chdir(script_dir)
    
    subprocess.Popen([f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'python', script_path], creationflags=subprocess.DETACHED_PROCESS)

def run_xtts_api_server(conda_path, env_name, xtts_server_path):
    logging.info(f"Running xtts_api_server in {env_name}...")
    os.chdir(xtts_server_path)

    # Create log file for xtts server output
    xtts_log_file = os.path.join(xtts_server_path, 'xtts_server.log')

    # Run xtts server command with output redirection
    xtts_server_command = f'"{conda_path}\\Scripts\\conda.exe" run -n {env_name} python -m xtts_api_server --lowvram --deepspeed > "{xtts_log_file}" 2>&1'
    subprocess.Popen(xtts_server_command, cwd=xtts_server_path, shell=True)

def check_xtts_server_online(url, max_attempts=30, wait_interval=10):
    attempt = 1
    while attempt <= max_attempts:
        try:
            response = requests.get(url)
            if response.status_code == 200:
                logging.info("xtts server is online.")
                return True
        except requests.exceptions.RequestException as e:
            logging.info(f"xtts server is not online. Waiting... (Attempt {attempt}/{max_attempts})")
        
        time.sleep(wait_interval)
        attempt += 1
    
    logging.error("xtts server failed to come online within the specified attempts.")
    return False

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
        run_command(['git', 'clone', 'https://github.com/daswer123/xtts-api-server.git', os.path.join(pandrator_path, 'xtts-api-server')])
        run_command(['git', 'clone', 'https://github.com/lukaszliniewicz/Pandrator.git', os.path.join(pandrator_path, 'Pandrator')])

        # Install Miniconda
        conda_path = os.path.join(pandrator_path, 'conda')
        install_conda(conda_path)

        # Check if conda is installed correctly
        if not check_conda(conda_path):
            logging.error("Conda installation failed. Please check the installation logs.")
            return

        # Create conda environments
        create_conda_env(conda_path, 'xtts_api_server_installer', '3.10')
        create_conda_env(conda_path, 'pandrator_installer', '3.10')

        # Install PyTorch and xtts_api_server package
        install_pytorch_and_xtts_api_server(conda_path, 'xtts_api_server_installer')

        # Install requirements for pandrator
        pandrator_repo_path = os.path.join(pandrator_path, 'Pandrator')
        install_requirements(conda_path, 'pandrator_installer', os.path.join(pandrator_repo_path, 'requirements.txt'))
    else:
        logging.info("Pandrator folder exists. Skipping installation steps.")
        
    # Get the conda path
    conda_path = os.path.join(pandrator_path, 'conda')

    # Run xtts_api_server
    xtts_server_path = os.path.join(pandrator_path, 'xtts-api-server')
    run_xtts_api_server(conda_path, 'xtts_api_server_installer', xtts_server_path)
    
    # Wait for xtts server to come online
    xtts_server_url = 'http://127.0.0.1:8020/docs'
    if not check_xtts_server_online(xtts_server_url):
        logging.error("xtts server failed to come online. Exiting...")
        return

    # Run pandrator script
    pandrator_script_path = os.path.join(pandrator_path, 'Pandrator', 'pandrator.py')
    run_script(conda_path, 'pandrator_installer', pandrator_script_path)
    
    # Keep the main script running to prevent immediate exit
    while True:
        time.sleep(1)

if __name__ == '__main__':
    main()
