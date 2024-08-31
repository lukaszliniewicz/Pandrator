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
                              "and dependencies (Git, Curl, FFmpeg, Calibre) using Chocolatey.\n\n"
                              "Note: To install dependencies automatically, run this installer as Administrator. "
                              "To uninstall Pandrator, simply delete the Pandrator folder.\n\n"
                              "Select your options below and click the appropriate button to begin.")
        self.info_text.configure(state="disabled")

        # Install Section
        self.install_frame = ctk.CTkFrame(self)
        self.install_frame.grid(row=2, column=0, padx=20, pady=10, sticky="ew")
        self.install_frame.grid_columnconfigure(0, weight=1)

        self.install_label = ctk.CTkLabel(self.install_frame, text="Install", font=("Arial", 18, "bold"))
        self.install_label.grid(row=0, column=0, columnspan=3, pady=(10, 5), sticky="w", padx=10)

        button_frame = ctk.CTkFrame(self.install_frame)
        button_frame.grid(row=1, column=0, sticky="w", padx=10, pady=10)

        self.install_button = ctk.CTkButton(button_frame, text="Install Pandrator & XTTS", 
                                            command=self.install_pandrator_xtts, width=200, height=40)
        self.install_button.grid(row=0, column=0, padx=(0, 10))

        self.cuda_var = ctk.BooleanVar(value=True)
        self.cuda_checkbox = ctk.CTkCheckBox(button_frame, text="Install CUDA PyTorch", variable=self.cuda_var, command=self.update_gpu_options)
        self.cuda_checkbox.grid(row=0, column=1, padx=10)

        self.open_log_button = ctk.CTkButton(button_frame, text="Open Installation Log", 
                                             command=self.open_log_file, width=200, height=40)
        self.open_log_button.grid(row=0, column=2, padx=(10, 0))
        self.open_log_button.configure(state="disabled")

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

        self.lowvram_var = ctk.BooleanVar(value=True)
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

    def update_button_states(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        if os.path.exists(pandrator_path):
            self.launch_cpu_button.configure(state="normal")
            self.launch_pandrator_button.configure(state="normal")
            
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
        
        os.makedirs(pandrator_path, exist_ok=True)
        logs_path = os.path.join(pandrator_path, 'Logs')
        os.makedirs(logs_path, exist_ok=True)
        
        # Configure logging
        current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_filename = os.path.join(logs_path, f'pandrator_installation_log_{current_time}.log')
        logging.basicConfig(filename=self.log_filename, level=logging.DEBUG,
                            format='%(asctime)s - %(levelname)s - %(message)s')
        
        self.open_log_button.configure(state="normal")
        
        threading.Thread(target=self.install_process, daemon=True).start()

    def launch_pandrator_xtts_gpu(self):
        self.progress_bar.set(0)
        self.status_label.configure(text="Launching...")
        threading.Thread(target=self.launch_process, args=(False,), daemon=True).start()

    def launch_pandrator_xtts_cpu(self):
        self.progress_bar.set(0)
        self.status_label.configure(text="Launching...")
        threading.Thread(target=self.launch_process, args=(True,), daemon=True).start()

    def launch_pandrator_only(self):
        self.progress_bar.set(0)
        self.status_label.configure(text="Launching Pandrator...")
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

    def enable_buttons(self):
        self.install_button.configure(state="normal")
        self.cuda_checkbox.configure(state="normal")
        self.update_button_states()
        self.update_gpu_options()

    def update_progress(self, value):
        self.progress_bar.set(value)

    def update_status(self, text):
        self.status_label.configure(text=text)

    def run_command(self, command):
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

    def check_program_installed(self, program):
        return shutil.which(program) is not None

    def check_choco(self):
        return self.check_program_installed('choco')

    def install_choco(self):
        logging.info("Installing Chocolatey...")
        self.run_command(['powershell', '-Command', "Set-ExecutionPolicy Bypass -Scope Process -Force; [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://chocolatey.org/install.ps1'))"])

    def install_dependencies(self):
        dependencies = ['git', 'curl', 'ffmpeg', 'calibre']
        for dependency in dependencies:
            if not self.check_program_installed(dependency):
                logging.info(f"Installing {dependency}...")
                try:
                    self.run_command(['choco', 'install', dependency, '-y'])
                except subprocess.CalledProcessError as e:
                    logging.error(f"Failed to install {dependency}.")
                    raise

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
            raise

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

    def install_process(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        
        self.update_progress(0.1)
        self.update_status("Installing Chocolatey...")
        if not self.check_choco():
            self.install_choco()
        
        self.update_progress(0.2)
        self.update_status("Installing dependencies...")
        self.install_dependencies()
        
        self.update_progress(0.3)
        self.update_status("Cloning repositories...")
        self.run_command(['git', 'clone', 'https://github.com/daswer123/xtts-api-server.git', os.path.join(pandrator_path, 'xtts-api-server')])
        self.run_command(['git', 'clone', 'https://github.com/lukaszliniewicz/Pandrator.git', os.path.join(pandrator_path, 'Pandrator')])

        self.update_progress(0.4)
        self.update_status("Installing Miniconda...")
        conda_path = os.path.join(pandrator_path, 'conda')
        self.install_conda(conda_path)

        if not self.check_conda(conda_path):
            self.update_status("Conda installation failed")
            self.enable_buttons()
            return

        self.update_progress(0.5)
        self.update_status("Creating Conda environments...")
        self.create_conda_env(conda_path, 'xtts_api_server_installer', '3.10')
        self.create_conda_env(conda_path, 'pandrator_installer', '3.10')

        self.update_progress(0.6)
        self.update_status("Installing PyTorch and xtts-api-server...")
        self.install_pytorch_and_xtts_api_server(conda_path, 'xtts_api_server_installer')

        self.update_progress(0.8)
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
        self.enable_buttons()

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

if __name__ == "__main__":
    app = PandratorInstaller()
    app.mainloop()
