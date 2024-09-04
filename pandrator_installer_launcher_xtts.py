import os
import subprocess
import logging
import time
import shutil
import requests
import threading
import customtkinter as ctk
from datetime import datetime
import atexit
import psutil
import json
import tkinter.messagebox as messagebox
import traceback
import tempfile
import sys
import ctypes
import winreg
import queue
import msvcrt


class PandratorInstaller(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.initial_working_dir = os.getcwd()

        self.title("Pandrator Installer & Launcher")
        self.geometry("900x700")

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(6, weight=1)

        # Title
        self.title_label = ctk.CTkLabel(self, text="Pandrator Installer & Launcher", font=("Arial", 32, "bold"))
        self.title_label.grid(row=0, column=0, pady=(20, 10))

        # Description
        self.info_text = ctk.CTkTextbox(self, height=120, wrap="word", font=("Arial", 12))
        self.info_text.grid(row=1, column=0, sticky="ew", padx=20, pady=10)
        self.info_text.insert("1.0", "This tool will help you set up and run Pandrator and XTTS. "
                              "It will install Pandrator, XTTS, Miniconda, required Python packages, "
                              "and dependencies, if not installed already (Git, Curl, FFmpeg, Calibre, Visual Studio C++ Build Tools) using winget.\n\n"
                              "This installer will automatically install winget if it's not already installed on your system.\n\n"
                              "To uninstall Pandrator, simply delete the Pandrator folder.\n\n"
                              "The installation will take about 6GB of disk space without CUDA support and about 9GB with CUDA support.\n\n"
                              "Select your options below and click the appropriate button to begin.")
        self.info_text.configure(state="disabled")

        # Install Section
        self.install_frame = ctk.CTkFrame(self)
        self.install_frame.grid(row=2, column=0, padx=20, pady=10, sticky="ew")
        self.install_frame.grid_columnconfigure(0, weight=1)

        self.install_label = ctk.CTkLabel(self.install_frame, text="Install", font=("Arial", 18, "bold"))
        self.install_label.grid(row=0, column=0, columnspan=4, pady=(10, 5), sticky="w", padx=10)

        button_frame = ctk.CTkFrame(self.install_frame)
        button_frame.grid(row=1, column=0, sticky="w", padx=10, pady=10)

        self.install_button = ctk.CTkButton(button_frame, text="Install Pandrator & XTTS", 
                                            command=self.install_pandrator_xtts, width=200, height=40)
        self.install_button.grid(row=0, column=0, padx=(0, 10))

        self.open_log_button = ctk.CTkButton(button_frame, text="Open Installation Log", 
                                             command=self.open_log_file, width=200, height=40)
        self.open_log_button.grid(row=0, column=1, padx=10)
        self.open_log_button.configure(state="disabled")

        self.install_rvc_button = ctk.CTkButton(button_frame, text="Install RVC_CLI", 
                                                command=self.install_rvc_cli, width=200, height=40)
        self.install_rvc_button.grid(row=0, column=2, padx=(10, 0))

        self.cuda_var = ctk.BooleanVar(value=True)
        self.cuda_checkbox = ctk.CTkCheckBox(self.install_frame, text="Install CUDA PyTorch (uncheck if you don't have an Nvidia GPU)", variable=self.cuda_var, command=self.update_gpu_options)
        self.cuda_checkbox.grid(row=2, column=0, pady=(10, 0), sticky="w", padx=10)

        # Launch Section
        self.launch_frame = ctk.CTkFrame(self)
        self.launch_frame.grid(row=3, column=0, padx=20, pady=10, sticky="ew")
        self.launch_frame.grid_columnconfigure(0, weight=1)

        self.launch_label = ctk.CTkLabel(self.launch_frame, text="Launch", font=("Arial", 18, "bold"))
        self.launch_label.grid(row=0, column=0, columnspan=2, pady=(10, 5), sticky="w", padx=10)

        launch_button_frame = ctk.CTkFrame(self.launch_frame)
        launch_button_frame.grid(row=1, column=0, sticky="w", padx=10, pady=10)

        self.launch_gpu_button = ctk.CTkButton(launch_button_frame, text="Launch Pandrator & XTTS (GPU)", 
                                               command=self.launch_pandrator_xtts_gpu, width=200, height=40)
        self.launch_gpu_button.grid(row=0, column=0, padx=(0, 10))

        self.launch_cpu_button = ctk.CTkButton(launch_button_frame, text="Launch Pandrator & XTTS (CPU)", 
                                               command=self.launch_pandrator_xtts_cpu, width=200, height=40)
        self.launch_cpu_button.grid(row=0, column=1, padx=10)

        self.launch_pandrator_button = ctk.CTkButton(launch_button_frame, text="Launch Pandrator Only", 
                                                     command=self.launch_pandrator_only, width=200, height=40)
        self.launch_pandrator_button.grid(row=0, column=2, padx=(10, 0))

        # XTTS GPU Options
        self.xtts_gpu_frame = ctk.CTkFrame(self)
        self.xtts_gpu_frame.grid(row=4, column=0, padx=20, pady=10, sticky="ew")
        self.xtts_gpu_frame.grid_columnconfigure(0, weight=1)

        self.xtts_gpu_label = ctk.CTkLabel(self.xtts_gpu_frame, text="XTTS GPU Options (disregard if using CPU)", font=("Arial", 18, "bold"))
        self.xtts_gpu_label.grid(row=0, column=0, pady=(10, 5), sticky="w", padx=10)

        xtts_options_frame = ctk.CTkFrame(self.xtts_gpu_frame)
        xtts_options_frame.grid(row=1, column=0, sticky="w", padx=10, pady=10)

        self.lowvram_var = ctk.BooleanVar(value=False)
        self.lowvram_checkbox = ctk.CTkCheckBox(xtts_options_frame, text="Low VRAM mode", variable=self.lowvram_var)
        self.lowvram_checkbox.grid(row=0, column=0, padx=(0, 10))

        self.deepspeed_var = ctk.BooleanVar(value=True)
        self.deepspeed_checkbox = ctk.CTkCheckBox(xtts_options_frame, text="Use DeepSpeed", variable=self.deepspeed_var)
        self.deepspeed_checkbox.grid(row=0, column=1, padx=10)

        # Progress and Status
        self.progress_bar = ctk.CTkProgressBar(self)
        self.progress_bar.grid(row=5, column=0, sticky="ew", padx=20, pady=(20, 10))
        self.progress_bar.set(0)

        self.status_label = ctk.CTkLabel(self, text="", font=("Arial", 14))
        self.status_label.grid(row=6, column=0, pady=(0, 10))

        self.xtts_process = None
        self.pandrator_process = None
        atexit.register(self.shutdown_xtts)

        self.update_button_states()
        self.update_gpu_options()

    def initialize_logging(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        os.makedirs(pandrator_path, exist_ok=True)
        logs_path = os.path.join(pandrator_path, 'Logs')
        os.makedirs(logs_path, exist_ok=True)

        current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_filename = os.path.join(logs_path, f'pandrator_installation_log_{current_time}.log')
        logging.basicConfig(filename=self.log_filename, level=logging.DEBUG,
                            format='%(asctime)s - %(levelname)s - %(message)s')

        self.open_log_button.configure(state="normal")

    def update_button_states(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        if os.path.exists(pandrator_path):
            self.launch_cpu_button.configure(state="normal")
            self.launch_pandrator_button.configure(state="normal")
            self.install_rvc_button.configure(state="normal")
            
            config_path = os.path.join(pandrator_path, 'config.json')
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = json.load(f)
                if config.get('cuda_support', False):
                    self.launch_gpu_button.configure(state="normal")
                    self.xtts_gpu_frame.grid()
                    self.lowvram_checkbox.configure(state="normal")
                    self.deepspeed_checkbox.configure(state="normal")
                else:
                    self.launch_gpu_button.configure(state="disabled")
                    self.xtts_gpu_frame.grid_remove()
            else:
                self.launch_gpu_button.configure(state="disabled")
                self.xtts_gpu_frame.grid_remove()
        else:
            self.launch_gpu_button.configure(state="disabled")
            self.launch_cpu_button.configure(state="disabled")
            self.launch_pandrator_button.configure(state="disabled")
            self.install_rvc_button.configure(state="disabled")
            self.xtts_gpu_frame.grid_remove()

    def update_gpu_options(self):
        if self.cuda_var.get():
            self.lowvram_checkbox.configure(state="normal")
            self.deepspeed_checkbox.configure(state="normal")
        else:
            self.lowvram_checkbox.configure(state="disabled")
            self.deepspeed_checkbox.configure(state="disabled")

    def remove_directory(self, path):
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                shutil.rmtree(path)
                return True
            except PermissionError:
                time.sleep(1)  # Wait for a second before retrying
        return False

    def install_pandrator_xtts(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        if os.path.exists(pandrator_path):
            if messagebox.askyesno("Warning", "The Pandrator folder already exists. Do you want to remove it and reinstall?"):
                if not self.remove_directory(pandrator_path):
                    messagebox.showerror("Error", "Unable to remove the Pandrator folder. Please close any applications that might be using files in this folder, then try again. If the problem persists, please manually delete the Pandrator folder and try again.")
                    return
            else:
                return

        self.disable_buttons()
        self.progress_bar.set(0)
        self.status_label.configure(text="Installing...")

        self.initialize_logging()

        logging.info("Installation process started.")

        threading.Thread(target=self.install_process, daemon=True).start()

    def launch_pandrator_xtts_gpu(self):
        self.progress_bar.set(0)
        self.status_label.configure(text="Launching...")
        logging.info("Launching Pandrator and XTTS with GPU.")
        threading.Thread(target=self.launch_process, args=(False,), daemon=True).start()

    def launch_pandrator_xtts_cpu(self):
        self.progress_bar.set(0)
        self.status_label.configure(text="Launching...")
        logging.info("Launching Pandrator and XTTS with CPU.")
        threading.Thread(target=self.launch_process, args=(True,), daemon=True).start()

    def launch_pandrator_only(self):
        self.progress_bar.set(0)
        self.status_label.configure(text="Launching Pandrator...")
        logging.info("Launching Pandrator only.")
        threading.Thread(target=self.launch_pandrator_process, daemon=True).start()

    def open_log_file(self):
        if hasattr(self, 'log_filename') and os.path.exists(self.log_filename):
            os.startfile(self.log_filename)
        else:
            self.status_label.configure(text="No log file available.")

    def disable_buttons(self):
        self.install_button.configure(state="disabled")
        self.cuda_checkbox.configure(state="disabled")
        self.lowvram_checkbox.configure(state="disabled")
        self.deepspeed_checkbox.configure(state="disabled")
        self.install_rvc_button.configure(state="disabled")

    def enable_buttons(self):
        self.install_button.configure(state="normal")
        self.cuda_checkbox.configure(state="normal")
        self.update_button_states()
        self.update_gpu_options()

    def update_progress(self, value):
        self.progress_bar.set(value)

    def update_status(self, text):
        self.status_label.configure(text=text)
        logging.info(text)

    def run_command(self, command, use_shell=False, cwd=None):
        try:
            if use_shell:
                process = subprocess.Popen(
                    command if isinstance(command, str) else " ".join(command),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=True,
                    cwd=cwd
                )
            else:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd
                )
            
            stdout, stderr = process.communicate()
            
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
            
            logging.info(f"Command executed: {command if isinstance(command, str) else ' '.join(command)}")
            logging.debug(f"STDOUT: {stdout.decode('utf-8')}")
            logging.debug(f"STDERR: {stderr.decode('utf-8')}")
            
            return stdout.decode('utf-8'), stderr.decode('utf-8')
        except subprocess.CalledProcessError as e:
            logging.error(f"Error executing command: {command if isinstance(command, str) else ' '.join(command)}")
            logging.error(f"Error message: {str(e)}")
            logging.error(f"STDOUT: {e.stdout.decode('utf-8')}")
            logging.error(f"STDERR: {e.stderr.decode('utf-8')}")
            raise

    def check_program_installed(self, program):
        try:
            self.run_command(['where', program])
            return True
        except subprocess.CalledProcessError:
            return False

    def refresh_environment_variables(self):
        """Refresh the environment variables for the current session."""
        try:
            # Refresh environment variables for the current session
            logging.info("Refreshing environment variables...")
            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x001A
            SMTO_ABORTIFHUNG = 0x0002
            result = ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
                SMTO_ABORTIFHUNG, 5000, ctypes.byref(ctypes.c_long())
            )
            if result == 0:
                logging.warning("Environment variables refresh timed out.")
            else:
                logging.info("Environment variables refreshed successfully.")
        except Exception as e:
            logging.error(f"Failed to refresh environment variables: {str(e)}")
            logging.error(traceback.format_exc())
            raise

    def install_winget(self):
        try:
            logging.info("Checking if winget is installed...")
            self.run_command(['where', 'winget'])
            logging.info("winget is already installed.")
            
            # Verify winget version
            version_output, _ = self.run_command(['winget', '--version'])
            logging.info(f"Installed winget version: {version_output.strip()}")
        except subprocess.CalledProcessError:
            logging.info("winget is not installed. Proceeding with installation...")

            try:
                with tempfile.TemporaryDirectory() as temp_dir:
                    script_path = os.path.join(temp_dir, "winget-install.ps1")
                    
                    # Download the PowerShell script
                    self.run_command([
                        'powershell',
                        '-Command',
                        f'Invoke-WebRequest -Uri "https://github.com/asheroto/winget-install/releases/latest/download/winget-install.ps1" -OutFile "{script_path}"'
                    ], use_shell=True)
                    
                    # Execute the PowerShell script
                    self.run_command([
                        'powershell',
                        '-ExecutionPolicy',
                        'Bypass',
                        '-File',
                        script_path
                    ], use_shell=True)
                
                logging.info("winget has been installed.")
                
                # Refresh environment variables
                self.refresh_environment_variables()
                
                # Verify installation
                version_output, _ = self.run_command(['winget', '--version'])
                logging.info(f"Installed winget version: {version_output.strip()}")
            except Exception as e:
                logging.error(f"Failed to install winget: {str(e)}")
                logging.error(traceback.format_exc())
                raise
        except Exception as e:
            logging.error(f"Unexpected error while checking or installing winget: {str(e)}")
            logging.error(traceback.format_exc())
            raise
            
    def get_system_architecture(self):
        return 'x64' if sys.maxsize > 2**32 else 'x86'
           
    def get_program_path_from_registry(self, program_name):
        try:
            if program_name == 'git':
                key_path = r"SOFTWARE\GitForWindows"
                value_name = "InstallPath"
            elif program_name == 'curl':
                key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\curl.exe"
                value_name = None  # Default value
            else:
                return None

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                if value_name:
                    return winreg.QueryValueEx(key, value_name)[0]
                else:
                    return winreg.QueryValueEx(key, "")[0]  # Default value
        except WindowsError:
            return None

    def install_dependencies(self):
        logging.info("Starting install_dependencies method")
        dependencies = [('Git.Git', 'git'), ('cURL', 'curl'), ('Gyan.FFmpeg', 'ffmpeg'), ('calibre.calibre', 'calibre')]
        for winget_id, program_name in dependencies:
            logging.info(f"Checking installation for {program_name}")
            if not self.check_program_installed(program_name):
                logging.info(f"Installing {program_name}...")
                try:
                    self.run_command(['winget', 'install', '--id', winget_id, '-e', '--accept-source-agreements', '--accept-package-agreements'])
                    
                    # Refresh environment variables in a new session
                    self.refresh_env_in_new_session()
                    
                    # Only verify installation for git and curl
                    if program_name in ['git', 'curl']:
                        if not self.check_program_installed(program_name):
                            logging.warning(f"{program_name} installation not detected. Attempting to use absolute path.")
                            program_path = self.get_program_path_from_registry(program_name)
                            if program_path:
                                if program_name == 'git':
                                    bin_path = os.path.join(program_path, 'bin')
                                    os.environ['PATH'] = f"{bin_path};{os.environ['PATH']}"
                                    logging.info(f"Added {program_name} to PATH: {bin_path}")
                                else:
                                    program_dir = os.path.dirname(program_path)
                                    os.environ['PATH'] = f"{program_dir};{os.environ['PATH']}"
                                    logging.info(f"Added {program_name} to PATH: {program_dir}")
                            else:
                                absolute_path = self.get_program_path(program_name)
                                if absolute_path:
                                    os.environ[program_name.upper()] = absolute_path
                                    logging.info(f"Updated {program_name} path: {absolute_path}")
                                else:
                                    raise Exception(f"Failed to find {program_name} after installation.")
                    else:
                        logging.info(f"Skipping post-installation check for {program_name}")
                    
                except subprocess.CalledProcessError as e:
                    logging.error(f"Failed to install {program_name}.")
                    logging.error(f"Error output: {e.stderr.decode('utf-8')}")
                    raise
            else:
                logging.info(f"{program_name} is already installed.")

    def check_program_installed(self, program):
        try:
            self.run_command(['where', program])
            return True
        except subprocess.CalledProcessError:
            return False

    def get_program_path(self, program_name):
        try:
            output, _ = self.run_command(['where', program_name])
            return output.strip().split('\n')[0]
        except subprocess.CalledProcessError:
            logging.error(f"Failed to find {program_name} path")
            return None

    def refresh_env_in_new_session(self):
        refresh_cmd = 'powershell -Command "[System.Environment]::GetEnvironmentVariables([System.EnvironmentVariableTarget]::Machine)"'
        output, _ = self.run_command(refresh_cmd, use_shell=True)
        new_env = dict(line.split('=', 1) for line in output.strip().split('\n') if '=' in line)
        os.environ.update(new_env)
        logging.info("Refreshed environment variables in a new session")

    def install_visual_cpp_build_tools(self):
        logging.info("Installing/Updating Microsoft Visual C++ Build Tools...")
        self.update_status("Installing/Updating Microsoft Visual C++ Build Tools...")
        
        winget_command = [
            "winget", "install", 
            "--id", "Microsoft.VisualStudio.2022.BuildTools",
            "--override", "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
        ]
        
        try:
            process = subprocess.Popen(winget_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            
            output = []
            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                if line:
                    output.append(line.strip())
                    logging.info(line.strip())
            
            error_output = process.stderr.read().strip()
            if error_output:
                logging.debug(f"STDERR: {error_output}")
            
            returncode = process.poll()
            
            if returncode == 0 or any("No available upgrade found" in line for line in output):
                logging.info("Microsoft Visual C++ Build Tools are up to date or successfully installed.")
                self.update_status("Build Tools are ready.")
                return True
            else:
                logging.error(f"Failed to install/update Microsoft Visual C++ Build Tools. Return code: {returncode}")
                if error_output:
                    logging.error(f"Error output: {error_output}")
                self.update_status("Error during Build Tools installation/update. Check the log for details.")
                return False
            
        except Exception as e:
            logging.error(f"An error occurred during Visual C++ Build Tools installation: {str(e)}")
            logging.error(traceback.format_exc())
            self.update_status("Error during Build Tools installation/update. Check the log for details.")
            return False

    def install_conda(self, install_path):
        logging.info("Installing Miniconda...")
        conda_installer = 'Miniconda3-latest-Windows-x86_64.exe'
        self.run_command(['curl', '-O', f'https://repo.anaconda.com/miniconda/{conda_installer}'])
        self.run_command([conda_installer, '/InstallationType=JustMe', '/RegisterPython=0', '/S', f'/D={install_path}'])
        os.remove(conda_installer)

    def check_conda(self, conda_path):
        return os.path.exists(os.path.join(conda_path, 'Scripts', 'conda.exe'))

    def create_conda_env(self, conda_path, env_name, python_version):
        logging.info(f"Creating conda environment {env_name}...")
        try:
            self.run_command([os.path.join(conda_path, 'Scripts', 'conda.exe'), 'create', '-n', env_name, f'python={python_version}', '-y'])
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to create conda environment {env_name}")
            logging.error(f"Error output: {e.stderr.decode('utf-8')}")
            raise

    def install_requirements(self, conda_path, env_name, requirements_file):
        logging.info(f"Installing requirements for {env_name}...")
        self.run_command([os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-n', env_name, 'pip', 'install', '-r', requirements_file])

    def install_pytorch_and_xtts_api_server(self, conda_path, env_name):
        logging.info(f"Installing PyTorch and xtts-api-server package in {env_name}...")
        
        try:
            # Install PyTorch
            if self.cuda_var.get():
                pytorch_cmd = [os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-n', env_name, 'pip', 'install', 'torch==2.1.1+cu118', 'torchaudio==2.1.1+cu118', '--extra-index-url', 'https://download.pytorch.org/whl/cu118']
            else:
                pytorch_cmd = [os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-n', env_name, 'pip', 'install', 'torch==2.1.1', 'torchaudio==2.1.1']
            self.run_command(pytorch_cmd)
            
            # Install xtts-api-server package
            xtts_cmd = [os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-n', env_name, 'pip', 'install', 'xtts-api-server']
            self.run_command(xtts_cmd)
            
            logging.info("PyTorch and xtts-api-server package installed successfully.")
        except subprocess.CalledProcessError as e:
            logging.error("Error installing PyTorch and xtts-api-server package.")
            logging.error(f"Error output: {e.stderr.decode('utf-8')}")
            raise

    def install_process(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        
        try:
            self.update_progress(0.1)
            self.update_status("Installing winget...")
            self.install_winget()

            self.update_progress(0.2)
            self.update_status("Installing dependencies...")
            self.install_dependencies()
            
            self.update_progress(0.3)
            self.update_status("Installing Visual C++ Build Tools...")
            self.install_visual_cpp_build_tools()
            
            self.update_progress(0.4)
            self.update_status("Cloning repositories...")
            self.run_command(['git', 'clone', 'https://github.com/daswer123/xtts-api-server.git', os.path.join(pandrator_path, 'xtts-api-server')])
            self.run_command(['git', 'clone', 'https://github.com/lukaszliniewicz/Pandrator.git', os.path.join(pandrator_path, 'Pandrator')])

            self.update_progress(0.5)
            self.update_status("Installing Miniconda...")
            conda_path = os.path.join(pandrator_path, 'conda')
            self.install_conda(conda_path)

            if not self.check_conda(conda_path):
                self.update_status("Conda installation failed")
                logging.error("Conda installation failed")
                self.enable_buttons()
                return

            self.update_progress(0.6)
            self.update_status("Creating Conda environments...")
            self.create_conda_env(conda_path, 'xtts_api_server_installer', '3.10')
            self.create_conda_env(conda_path, 'pandrator_installer', '3.10')

            self.update_progress(0.8)
            self.update_status("Installing PyTorch and xtts-api-server...")
            self.install_pytorch_and_xtts_api_server(conda_path, 'xtts_api_server_installer')

            self.update_progress(0.9)
            self.update_status("Installing Pandrator requirements...")
            pandrator_repo_path = os.path.join(pandrator_path, 'Pandrator')
            self.install_requirements(conda_path, 'pandrator_installer', os.path.join(pandrator_repo_path, 'requirements.txt'))

            # Create config file
            config = {
                'cuda_support': self.cuda_var.get()
            }
            with open(os.path.join(pandrator_path, 'config.json'), 'w') as f:
                json.dump(config, f)

            self.update_progress(1.0)
            self.update_status("Installation complete!")
            logging.info("Installation completed successfully.")
            self.enable_buttons()
        except Exception as e:
            logging.error(f"Installation failed: {str(e)}")
            logging.error(traceback.format_exc())
            self.update_status("Installation failed. Check the log for details.")
            self.enable_buttons()

    def run_xtts_api_server(self, conda_path, env_name, xtts_server_path, use_cpu=False):
        logging.info(f"Attempting to run XTTS API server...")
        logging.info(f"Conda path: {conda_path}")
        logging.info(f"Environment name: {env_name}")
        logging.info(f"XTTS server path: {xtts_server_path}")
        logging.info(f"Use CPU: {use_cpu}")

        if not os.path.exists(xtts_server_path):
            raise FileNotFoundError(f"XTTS server path not found: {xtts_server_path}")

        xtts_log_file = os.path.join(xtts_server_path, 'xtts_server.log')

        xtts_server_command = [
            os.path.join(conda_path, 'Scripts', 'conda.exe'),
            'run',
            '-n', env_name,
            'python', '-m', 'xtts_api_server',
        ]

        if use_cpu:
            xtts_server_command.extend(['--device', 'cpu'])
        else:
            if self.lowvram_var.get():
                xtts_server_command.append('--lowvram')
            if self.deepspeed_var.get():
                xtts_server_command.append('--deepspeed')

        logging.info(f"XTTS command: {' '.join(xtts_server_command)}")

        def log_output(pipe, logfile):
            with open(logfile, 'a') as f:
                for line in iter(pipe.readline, b''):
                    decoded_line = line.decode('utf-8').strip()
                    f.write(decoded_line + '\n')
                    f.flush()
                    logging.info(f"XTTS: {decoded_line}")

        try:
            process = subprocess.Popen(xtts_server_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=xtts_server_path)
            self.xtts_process = process
            
            stdout_thread = threading.Thread(target=log_output, args=(process.stdout, xtts_log_file), daemon=True)
            stderr_thread = threading.Thread(target=log_output, args=(process.stderr, xtts_log_file), daemon=True)
            
            stdout_thread.start()
            stderr_thread.start()

            logging.info(f"XTTS API server process started with PID: {process.pid}")
            return process
        except Exception as e:
            logging.error(f"Failed to start XTTS API server: {str(e)}")
            logging.exception("Exception details:")
            raise

    def check_xtts_server_online(self, url, max_attempts=60, wait_interval=10):
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

    def launch_process(self, use_cpu=False):
        base_path = os.path.abspath(self.initial_working_dir)
        pandrator_path = os.path.join(base_path, 'Pandrator')
        conda_path = os.path.join(pandrator_path, 'conda')

        self.update_progress(0.3)
        self.update_status("Preparing to launch...")
        logging.info(f"Launch process started. Base directory: {base_path}")
        logging.info(f"Pandrator path: {pandrator_path}")
        logging.info(f"Conda path: {conda_path}")

        xtts_server_path = os.path.join(pandrator_path, 'xtts-api-server')
        logging.info(f"XTTS server path: {xtts_server_path}")

        if not os.path.exists(xtts_server_path):
            error_msg = f"XTTS server path not found: {xtts_server_path}"
            self.update_status(error_msg)
            logging.error(error_msg)
            return

        self.update_status("Starting XTTS server...")
        try:
            xtts_process = self.run_xtts_api_server(conda_path, 'xtts_api_server_installer', xtts_server_path, use_cpu)
        except Exception as e:
            error_msg = f"Failed to start XTTS server: {str(e)}"
            self.update_status(error_msg)
            logging.error(error_msg)
            logging.exception("Exception details:")
            return

        xtts_server_url = 'http://127.0.0.1:8020/docs'
        if not self.check_xtts_server_online(xtts_server_url):
            error_msg = "XTTS server failed to come online"
            self.update_status(error_msg)
            logging.error(error_msg)
            self.shutdown_xtts()
            return

        self.update_progress(0.7)
        self.update_status("Starting Pandrator...")
        pandrator_script_path = os.path.join(pandrator_path, 'Pandrator', 'pandrator.py')
        logging.info(f"Pandrator script path: {pandrator_script_path}")

        if not os.path.exists(pandrator_script_path):
            error_msg = f"Pandrator script not found: {pandrator_script_path}"
            self.update_status(error_msg)
            logging.error(error_msg)
            return

        try:
            self.pandrator_process = self.run_script(conda_path, 'pandrator_installer', pandrator_script_path)
        except Exception as e:
            error_msg = f"Failed to start Pandrator: {str(e)}"
            self.update_status(error_msg)
            logging.error(error_msg)
            logging.exception("Exception details:")
            return

        self.update_progress(1.0)
        self.update_status("Pandrator and XTTS are running!")
        self.after(5000, self.check_processes_status)

    def launch_pandrator_process(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        conda_path = os.path.join(pandrator_path, 'conda')

        self.update_progress(0.5)
        self.update_status("Starting Pandrator...")
        pandrator_script_path = os.path.join(pandrator_path, 'Pandrator', 'pandrator.py')
        self.pandrator_process = self.run_script(conda_path, 'pandrator_installer', pandrator_script_path)
        
        self.update_progress(1.0)
        self.update_status("Pandrator is running!")
        self.after(5000, self.check_processes_status)

    def run_script(self, conda_path, env_name, script_path):
        logging.info(f"Running script {script_path} in {env_name}...")
        
        script_dir = os.path.dirname(script_path)
        
        process = subprocess.Popen([
            os.path.join(conda_path, 'Scripts', 'conda.exe'),
            'run',
            '-n', env_name,
            'python',
            script_path
        ], cwd=script_dir, creationflags=subprocess.DETACHED_PROCESS)
        return process

    def check_processes_status(self):
        if self.pandrator_process and self.pandrator_process.poll() is not None:
            # Pandrator has exited
            self.pandrator_process = None
            self.shutdown_xtts()  # Shut down XTTS when Pandrator exits
        if self.xtts_process and self.xtts_process.poll() is not None:
            # XTTS has exited
            self.xtts_process = None
        
        if not self.pandrator_process and not self.xtts_process:
            self.update_status("All processes have exited.")
            self.update_button_states()
        else:
            self.after(5000, self.check_processes_status)  # Schedule next check

    def shutdown_xtts(self):
        if self.xtts_process:
            logging.info(f"Terminating XTTS process with PID: {self.xtts_process.pid}")
            try:
                parent = psutil.Process(self.xtts_process.pid)
                for child in parent.children(recursive=True):
                    child.terminate()
                parent.terminate()
                self.xtts_process.wait(timeout=10)
            except psutil.NoSuchProcess:
                logging.info("XTTS process already terminated.")
            except psutil.TimeoutExpired:
                logging.warning("XTTS process did not terminate, forcing kill")
                parent = psutil.Process(self.xtts_process.pid)
                for child in parent.children(recursive=True):
                    child.kill()
                parent.kill()
            self.xtts_process = None

        # Check if any process is using port 8020 and kill it
        for conn in psutil.net_connections():
            if conn.laddr.port == 8020:
                try:
                    process = psutil.Process(conn.pid)
                    process.terminate()
                    logging.info(f"Terminated process using port 8020: PID {conn.pid}")
                except psutil.NoSuchProcess:
                    logging.info(f"Process using port 8020 (PID {conn.pid}) no longer exists")

    def is_xtts_running(self):
        for conn in psutil.net_connections():
            if conn.laddr.port == 8020:
                return True
        return False

    def destroy(self):
        self.shutdown_xtts()
        super().destroy()

    def install_rvc_cli(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        rvc_cli_path = os.path.join(pandrator_path, 'rvc-cli')

        if not os.path.exists(pandrator_path):
            messagebox.showerror("Error", "Pandrator folder does not exist. Please install Pandrator first.")
            return

        self.update_status("Installing RVC_CLI...")
        logging.info("Starting RVC_CLI installation")

        # Ensure logging is properly set up
        if not hasattr(self, 'log_filename'):
            self.initialize_logging()

        # Create a queue for communication between threads
        self.output_queue = queue.Queue()

        # Start the installation process in a separate thread
        threading.Thread(target=self._install_rvc_cli_thread, args=(rvc_cli_path,), daemon=True).start()

        # Start checking the queue for updates
        self.after(100, self._check_install_queue)

    def _install_rvc_cli_thread(self, rvc_cli_path):
        try:
            # Clone RVC_CLI repository
            self._log_and_update("Cloning RVC_CLI repository...")
            clone_cmd = ['git', 'clone', 'https://github.com/blaisewf/rvc-cli.git', rvc_cli_path]
            self._log_and_update(f"Running command: {' '.join(clone_cmd)}")
            clone_result = subprocess.run(clone_cmd, capture_output=True, text=True, check=True)
            self._log_and_update(clone_result.stdout)

            # Run install.bat
            install_bat_path = os.path.join(rvc_cli_path, 'install.bat')
            self._log_and_update("Running install.bat for RVC_CLI...")
            self._log_and_update(f"Running command: {install_bat_path}")
            
            install_process = subprocess.Popen(install_bat_path, cwd=rvc_cli_path, shell=True, 
                                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            
            installation_successful = False
            for line in iter(install_process.stdout.readline, ''):
                stripped_line = line.strip()
                self._log_and_update(stripped_line)
                if "RVC CLI has been installed successfully" in stripped_line:
                    installation_successful = True
                    break

            if not installation_successful:
                install_process.wait(timeout=900)  # 15 minutes timeout
                if install_process.returncode != 0:
                    raise Exception(f"Installation process exited with non-zero return code: {install_process.returncode}")
            else:
                install_process.terminate()
                install_process.wait(timeout=30)  # Give it 30 seconds to terminate gracefully

            # Run the prerequisites command
            self._log_and_update("Running prerequisites...")
            python_exe = os.path.join(rvc_cli_path, 'env', 'python.exe')
            rvc_cli_script = os.path.join(rvc_cli_path, 'rvc_cli.py')
            
            if not os.path.exists(python_exe):
                raise FileNotFoundError(f"Python executable not found at: {python_exe}")
            if not os.path.exists(rvc_cli_script):
                raise FileNotFoundError(f"RVC CLI script not found at: {rvc_cli_script}")

            prerequisites_cmd = [python_exe, rvc_cli_script, 'prerequisites']
            self._log_and_update(f"Running command: {' '.join(prerequisites_cmd)}")
            
            prereq_process = subprocess.Popen(prerequisites_cmd, cwd=rvc_cli_path, 
                                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            
            for line in iter(prereq_process.stdout.readline, ''):
                self._log_and_update(line.strip())

            prereq_process.wait(timeout=1800)  # 30 minutes timeout
            if prereq_process.returncode != 0:
                raise Exception(f"Prerequisites process exited with non-zero return code: {prereq_process.returncode}")

            self._log_and_update("RVC_CLI installation and prerequisites download completed successfully.")
            self.output_queue.put(("done", "RVC_CLI is now installed and ready to use."))

        except Exception as e:
            error_msg = f"An error occurred during RVC_CLI installation or prerequisites download: {str(e)}"
            self._log_and_update(error_msg, level=logging.ERROR)
            self.output_queue.put(("error", error_msg))

    def _check_install_queue(self):
        try:
            message_type, message = self.output_queue.get_nowait()
            if message_type == "status":
                self.update_status(message)
            elif message_type == "done":
                self.update_status(message)
                messagebox.showinfo("Installation Complete", message)
            elif message_type == "error":
                self.update_status(message)
                messagebox.showerror("Installation Error", message)
        except queue.Empty:
            pass
        finally:
            # Schedule the next check
            self.after(100, self._check_install_queue)

    def _log_and_update(self, message, level=logging.INFO):
        """Log the message to file and put it in the queue for GUI update"""
        if hasattr(self, 'log_filename'):
            with open(self.log_filename, 'a', encoding='utf-8') as log_file:
                log_file.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {message}\n")
        logging.log(level, message)
        self.output_queue.put(("status", message))

    def update_status(self, message):
        self.status_label.configure(text=message)
        self.update()
if __name__ == "__main__":
    app = PandratorInstaller()
    app.mainloop()
