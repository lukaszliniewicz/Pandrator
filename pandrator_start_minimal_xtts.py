import os
import subprocess
import logging
import time
import shutil
import requests
from datetime import datetime
import threading

# Configure logging
current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
log_filename = f'pandrator_installation_log_{current_time}.log'
logging.basicConfig(filename=log_filename, level=logging.DEBUG,
                    format='%(asctime)s - %(levelname)s - %(message)s')

def print_stage(message):
    print(f"\n=== {message} ===")

def print_error(message):
    print(f"\nERROR: {message}")
    print(f"Please check the log file '{log_filename}' for more details.")

def run_command(command):
    try:
        result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        logging.info(f"Command executed: {' '.join(command)}")
        logging.debug(f"STDOUT: {result.stdout}")
        logging.debug(f"STDERR: {result.stderr}")
    except subprocess.CalledProcessError as e:
        logging.error(f"Error executing command: {' '.join(command)}")
        logging.error(f"Error message: {str(e)}")
        logging.error(f"STDOUT: {e.stdout}")
        logging.error(f"STDERR: {e.stderr}")
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
        raise

def install_requirements(conda_path, env_name, requirements_file):
    logging.info(f"Installing requirements for {env_name}...")
    run_command([f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'pip', 'install', '-r', requirements_file])

def install_pytorch_and_xtts_api_server(conda_path, env_name):
    logging.info(f"Installing PyTorch and xtts-api-server package in {env_name}...")
    
    try:
        # Install PyTorch
        pytorch_cmd = [f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'pip', 'install', 'torch==2.1.1+cu118', 'torchaudio==2.1.1+cu118', '--extra-index-url', 'https://download.pytorch.org/whl/cu118']
        run_command(pytorch_cmd)
        
        # Install xtts-api-server package
        xtts_cmd = [f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'pip', 'install', 'xtts-api-server']
        run_command(xtts_cmd)
        
        logging.info("PyTorch and xtts-api-server package installed successfully.")
    except subprocess.CalledProcessError as e:
        logging.error("Error installing PyTorch and xtts-api-server package.")
        raise

def run_script(conda_path, env_name, script_path):
    logging.info(f"Running script {script_path} in {env_name}...")
    
    script_dir = os.path.dirname(script_path)
    os.chdir(script_dir)
    
    subprocess.Popen([f'{conda_path}\\Scripts\\conda.exe', 'run', '-n', env_name, 'python', script_path], creationflags=subprocess.DETACHED_PROCESS)

def run_xtts_api_server(conda_path, env_name, xtts_server_path):
    logging.info(f"Running xtts_api_server in {env_name}...")
    os.chdir(xtts_server_path)

    xtts_log_file = os.path.join(xtts_server_path, 'xtts_server.log')

    xtts_server_command = [
        f'{conda_path}\\Scripts\\conda.exe',
        'run',
        '-n', env_name,
        'python', '-m', 'xtts_api_server',
        '--lowvram',
        '--deepspeed'
    ]

    def log_output(pipe, logfile):
        with open(logfile, 'a') as f:
            for line in iter(pipe.readline, b''):
                decoded_line = line.decode('utf-8').strip()
                f.write(decoded_line + '\n')
                f.flush()
                logging.info(f"XTTS: {decoded_line}")

    try:
        process = subprocess.Popen(xtts_server_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=xtts_server_path)
        
        stdout_thread = threading.Thread(target=log_output, args=(process.stdout, xtts_log_file), daemon=True)
        stderr_thread = threading.Thread(target=log_output, args=(process.stderr, xtts_log_file), daemon=True)
        
        stdout_thread.start()
        stderr_thread.start()

        logging.info(f"xtts_api_server process started with PID: {process.pid}")
        return process
    except Exception as e:
        logging.error(f"Failed to start xtts_api_server: {str(e)}")
        raise

def check_xtts_server_online(url, max_attempts=60, wait_interval=10):
    print("Waiting for xtts server to come online...")
    attempt = 1
    while attempt <= max_attempts:
        try:
            response = requests.get(url)
            if response.status_code == 200:
                print("xtts server is online.")
                logging.info("xtts server is online.")
                return True
        except requests.exceptions.RequestException as e:
            print(f"Attempt {attempt}/{max_attempts}: xtts server is not online yet. Waiting...")
            logging.info(f"xtts server is not online. Waiting... (Attempt {attempt}/{max_attempts})")
        
        time.sleep(wait_interval)
        attempt += 1
    
    logging.error("xtts server failed to come online within the specified attempts.")
    return False

def main():
    print_stage("Starting Pandrator Installation")
    
    pandrator_path = os.path.join(os.getcwd(), 'Pandrator')
    
    if not os.path.exists(pandrator_path):
        if not check_choco():
            print_stage("Installing Chocolatey")
            try:
                install_choco()
            except Exception as e:
                print_error("Failed to install Chocolatey")
                return
        
        print_stage("Installing Dependencies")
        try:
            install_dependencies()
        except Exception as e:
            print_error("Failed to install dependencies")
            return
        
        os.makedirs(pandrator_path, exist_ok=True)
        logging.info(f"Created Pandrator folder at {pandrator_path}")

        print_stage("Cloning Repositories")
        try:
            run_command(['git', 'clone', 'https://github.com/daswer123/xtts-api-server.git', os.path.join(pandrator_path, 'xtts-api-server')])
            run_command(['git', 'clone', 'https://github.com/lukaszliniewicz/Pandrator.git', os.path.join(pandrator_path, 'Pandrator')])
        except Exception as e:
            print_error("Failed to clone repositories")
            return

        print_stage("Installing Miniconda")
        conda_path = os.path.join(pandrator_path, 'conda')
        try:
            install_conda(conda_path)
        except Exception as e:
            print_error("Failed to install Miniconda")
            return

        if not check_conda(conda_path):
            print_error("Conda installation failed")
            return

        print_stage("Creating Conda Environments")
        try:
            create_conda_env(conda_path, 'xtts_api_server_installer', '3.10')
            create_conda_env(conda_path, 'pandrator_installer', '3.10')
        except Exception as e:
            print_error("Failed to create Conda environments")
            return

        print_stage("Installing PyTorch and xtts-api-server")
        try:
            install_pytorch_and_xtts_api_server(conda_path, 'xtts_api_server_installer')
        except Exception as e:
            print_error("Failed to install PyTorch and xtts-api-server")
            return

        print_stage("Installing Pandrator Requirements")
        pandrator_repo_path = os.path.join(pandrator_path, 'Pandrator')
        try:
            install_requirements(conda_path, 'pandrator_installer', os.path.join(pandrator_repo_path, 'requirements.txt'))
        except Exception as e:
            print_error("Failed to install Pandrator requirements")
            return
    else:
        print("Pandrator folder exists. Skipping installation steps.")
        
    conda_path = os.path.join(pandrator_path, 'conda')

    print_stage("Starting xtts_api_server")
    xtts_server_path = os.path.join(pandrator_path, 'xtts-api-server')
    xtts_process = run_xtts_api_server(conda_path, 'xtts_api_server_installer', xtts_server_path)
    
    xtts_server_url = 'http://127.0.0.1:8020/docs'
    if not check_xtts_server_online(xtts_server_url):
        print_error("xtts server failed to come online")
        xtts_process.terminate()
        return

    print_stage("Starting Pandrator")
    pandrator_script_path = os.path.join(pandrator_path, 'Pandrator', 'pandrator.py')
    run_script(conda_path, 'pandrator_installer', pandrator_script_path)
    
    print("\nInstallation complete. Pandrator is now running.")
    print(f"Log file: {log_filename}")
    print("You can close this window when you're done using Pandrator.")
    
    try:
        while True:
            time.sleep(1)
            if xtts_process.poll() is not None:
                print_error("xtts server process has stopped unexpectedly.")
                logging.error("xtts server process has stopped unexpectedly.")
                break
    except KeyboardInterrupt:
        print("\nStopping Pandrator...")
    finally:
        if xtts_process.poll() is None:
            xtts_process.terminate()
            xtts_process.wait()
        print("Pandrator stopped.")

if __name__ == '__main__':
    main()
