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

class ScrollableFrame(ctk.CTkScrollableFrame):
    def __init__(self, container, *args, **kwargs):
        super().__init__(container, *args, **kwargs)
        self.inner_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.inner_frame.pack(fill="both", expand=True)

    def get_inner_frame(self):
        return self.inner_frame

class PandratorInstaller(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.initial_working_dir = os.getcwd()
        # Define instance variables for checkboxes
        self.pandrator_var = ctk.BooleanVar(value=True)
        self.xtts_var = ctk.BooleanVar(value=False)
        self.xtts_cpu_var = ctk.BooleanVar(value=False)
        self.silero_var = ctk.BooleanVar(value=False)
        self.voicecraft_var = ctk.BooleanVar(value=False)
        self.rvc_var = ctk.BooleanVar(value=False)

        # Define instance variables for launch options
        self.launch_pandrator_var = ctk.BooleanVar(value=True)
        self.launch_xtts_var = ctk.BooleanVar(value=False)
        self.lowvram_var = ctk.BooleanVar(value=False)
        self.deepspeed_var = ctk.BooleanVar(value=False)
        self.xtts_cpu_launch_var = ctk.BooleanVar(value=False)
        self.launch_silero_var = ctk.BooleanVar(value=False)
        self.launch_voicecraft_var = ctk.BooleanVar(value=False)

        # Initialize process attributes
        self.xtts_process = None
        self.pandrator_process = None
        self.silero_process = None
        self.voicecraft_process = None

        self.title("Pandrator Installer & Launcher")
        
        # Calculate 92% of screen height
        screen_height = self.winfo_screenheight()
        window_height = int(screen_height * 0.92)
        self.geometry(f"900x{window_height}")

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # Create main scrollable frame
        self.main_frame = ScrollableFrame(self)
        self.main_frame.pack(fill="both", expand=True, padx=10, pady=10)

        # Content Frame (to align content to the top)
        self.content_frame = ctk.CTkFrame(self.main_frame.get_inner_frame(), fg_color="transparent")
        self.content_frame.pack(fill="both", expand=True)

        # Title
        self.title_label = ctk.CTkLabel(self.content_frame, text="Pandrator Installer & Launcher", font=("Arial", 32, "bold"))
        self.title_label.pack(pady=(20, 10))

        # Information Text Area
        self.info_text = ctk.CTkTextbox(self.content_frame, height=100, wrap="word", font=("Arial", 12))
        self.info_text.pack(fill="x", padx=20, pady=10)
        self.info_text.insert("1.0", "This tool will help you set up and run Pandrator and other TTS engines and tools. "
                              "It will install Pandrator, Miniconda, required Python packages, "
                              "and dependencies (Git, FFmpeg, Calibre, Visual Studio C++ Build Tools) using winget if not installed already.\n\n"
                              "To uninstall Pandrator, simply delete the Pandrator folder.\n\n"
                              "The installation will take about 6-9GB of disk space depending on the selected options.\n\n")
        self.info_text.configure(state="disabled")

        # Installation Frame
        self.installation_frame = ctk.CTkFrame(self.content_frame)
        self.installation_frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(self.installation_frame, text="Installation", font=("Arial", 18, "bold")).pack(anchor="w", padx=10, pady=(10, 5))

        self.pandrator_checkbox = ctk.CTkCheckBox(self.installation_frame, text="Pandrator", variable=self.pandrator_var)
        self.pandrator_checkbox.pack(anchor="w", padx=10, pady=(5, 0))

        ctk.CTkLabel(self.installation_frame, text="TTS Engines", font=("Arial", 14, "bold")).pack(anchor="w", padx=10, pady=(20, 0))
        ctk.CTkLabel(self.installation_frame, text="You can select and install new engines and tools after the initial installation.", font=("Arial", 10, "bold")).pack(anchor="w", padx=10, pady=(0, 10))

        engine_frame = ctk.CTkFrame(self.installation_frame)
        engine_frame.pack(fill="x", padx=10, pady=(0, 10))
        
        self.xtts_checkbox = ctk.CTkCheckBox(engine_frame, text="XTTS", variable=self.xtts_var, command=self.update_xtts_options)
        self.xtts_checkbox.pack(side="left", padx=(0, 20), pady=5)
        self.xtts_cpu_checkbox = ctk.CTkCheckBox(engine_frame, text="XTTS CPU only", variable=self.xtts_cpu_var, command=self.update_xtts_options)
        self.xtts_cpu_checkbox.pack(side="left", padx=(0, 20), pady=5)

        self.silero_checkbox = ctk.CTkCheckBox(engine_frame, text="Silero", variable=self.silero_var)
        self.silero_checkbox.pack(side="left", padx=(0, 20), pady=5)
        self.voicecraft_checkbox = ctk.CTkCheckBox(engine_frame, text="Voicecraft", variable=self.voicecraft_var)
        self.voicecraft_checkbox.pack(side="left", padx=(0, 20), pady=5)

        ctk.CTkLabel(self.installation_frame, text="Other tools", font=("Arial", 14, "bold")).pack(anchor="w", padx=10, pady=(20, 5))

        self.rvc_checkbox = ctk.CTkCheckBox(self.installation_frame, text="RVC Voice Cloning (RVC CLI)", variable=self.rvc_var)
        self.rvc_checkbox.pack(anchor="w", padx=10, pady=5)

        button_frame = ctk.CTkFrame(self.installation_frame)
        button_frame.pack(anchor="w", padx=10, pady=(20, 10))

        self.install_button = ctk.CTkButton(button_frame, text="Install", command=self.install_pandrator, width=200, height=40)
        self.install_button.pack(side="left", padx=(0, 10))

        self.open_log_button = ctk.CTkButton(button_frame, text="View Installation Log", command=self.open_log_file, width=200, height=40)
        self.open_log_button.pack(side="left", padx=10)
        self.open_log_button.configure(state="disabled")

        # Launch Frame
        self.launch_frame = ctk.CTkFrame(self.content_frame)
        self.launch_frame.pack(fill="x", padx=20, pady=10)
        ctk.CTkLabel(self.launch_frame, text="Launch", font=("Arial", 18, "bold")).grid(row=0, column=0, columnspan=4, sticky="w", padx=10, pady=(10, 5))
        ctk.CTkCheckBox(self.launch_frame, text="Pandrator", variable=self.launch_pandrator_var).grid(row=1, column=0, columnspan=4, sticky="w", padx=10, pady=5)

        # XTTS options in one row
        ctk.CTkCheckBox(self.launch_frame, text="XTTS", variable=self.launch_xtts_var).grid(row=2, column=0, sticky="w", padx=10, pady=5)
        self.xtts_cpu_checkbox = ctk.CTkCheckBox(self.launch_frame, text="Use CPU", variable=self.xtts_cpu_launch_var, command=self.update_xtts_launch_options)
        self.xtts_cpu_checkbox.grid(row=2, column=1, sticky="w", padx=10, pady=5)
        self.lowvram_checkbox = ctk.CTkCheckBox(self.launch_frame, text="Low VRAM", variable=self.lowvram_var)
        self.lowvram_checkbox.grid(row=2, column=2, sticky="w", padx=10, pady=5)
        self.deepspeed_checkbox = ctk.CTkCheckBox(self.launch_frame, text="DeepSpeed", variable=self.deepspeed_var)
        self.deepspeed_checkbox.grid(row=2, column=3, sticky="w", padx=10, pady=5)

        ctk.CTkCheckBox(self.launch_frame, text="Silero", variable=self.launch_silero_var).grid(row=3, column=0, columnspan=4, sticky="w", padx=10, pady=5)
        ctk.CTkCheckBox(self.launch_frame, text="Voicecraft", variable=self.launch_voicecraft_var).grid(row=4, column=0, columnspan=4, sticky="w", padx=10, pady=5)
        self.launch_button = ctk.CTkButton(self.launch_frame, text="Launch", command=self.launch_apps, width=200, height=40)
        self.launch_button.grid(row=5, column=0, columnspan=4, sticky="w", padx=10, pady=(20, 10))

        # Progress Bar and Status Label
        self.progress_bar = ctk.CTkProgressBar(self.content_frame)
        self.progress_bar.pack(fill="x", padx=20, pady=(20, 10))
        self.progress_bar.set(0)

        self.status_label = ctk.CTkLabel(self.content_frame, text="", font=("Arial", 14))
        self.status_label.pack(pady=(0, 10))

        # Schedule update_gpu_options and update_button_states to run after a short delay
        self.after(100, self.update_gpu_options)
        self.after(100, self.update_button_states)
        self.check_existing_installations()

        atexit.register(self.shutdown_apps)

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
        
    def update_xtts_options(self):
        if self.xtts_var.get():
            self.xtts_cpu_var.set(False)
        elif self.xtts_cpu_var.get():
            self.xtts_var.set(False)
        
        # Update launch options based on CPU selection
        self.update_xtts_launch_options()
        
    def update_xtts_launch_options(self):
        config_path = os.path.join(self.initial_working_dir, 'Pandrator', 'config.json')
        cuda_support = False
        
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config = json.load(f)
            cuda_support = config.get('cuda_support', False)

        if cuda_support:
            self.xtts_cpu_checkbox.configure(state="normal")
            if self.xtts_cpu_launch_var.get():
                self.lowvram_checkbox.configure(state="disabled")
                self.deepspeed_checkbox.configure(state="disabled")
            else:
                self.lowvram_checkbox.configure(state="normal")
                self.deepspeed_checkbox.configure(state="normal")
        else:
            self.xtts_cpu_checkbox.configure(state="normal")
            self.xtts_cpu_launch_var.set(True)
            self.lowvram_checkbox.configure(state="disabled")
            self.deepspeed_checkbox.configure(state="disabled")

    def disable_buttons(self):
        if self.install_button:
            self.install_button.configure(state="disabled")
        if self.pandrator_checkbox:
            self.pandrator_checkbox.configure(state="disabled")
        if self.xtts_checkbox:
            self.xtts_checkbox.configure(state="disabled")
        if self.xtts_cpu_checkbox:
            self.xtts_cpu_checkbox.configure(state="disabled")
        if self.silero_checkbox:
            self.silero_checkbox.configure(state="disabled")
        if self.voicecraft_checkbox:
            self.voicecraft_checkbox.configure(state="disabled")
        if self.rvc_checkbox:
            self.rvc_checkbox.configure(state="disabled")
        self.update_button_states()
        self.update_gpu_options()            

    def enable_buttons(self):
        if self.install_button:
            self.install_button.configure(state="normal")
        if self.pandrator_checkbox:
            self.pandrator_checkbox.configure(state="normal")
        if self.xtts_checkbox:
            self.xtts_checkbox.configure(state="normal")
        if self.silero_checkbox:
            self.silero_checkbox.configure(state="normal")
        if self.voicecraft_checkbox:
            self.voicecraft_checkbox.configure(state="normal")
        if self.rvc_checkbox:
            self.rvc_checkbox.configure(state="normal")
        self.update_button_states()
        self.update_gpu_options()

    def update_button_states(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        if os.path.exists(pandrator_path):
            if self.launch_button:
                self.launch_button.configure(state="normal")
            
            config_path = os.path.join(pandrator_path, 'config.json')
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = json.load(f)
                
                for widget in self.launch_frame.winfo_children():
                    if isinstance(widget, ctk.CTkCheckBox):
                        if widget.cget("text") == "XTTS":
                            if config.get('xtts_support', False):
                                widget.configure(state="normal")
                                self.launch_xtts_var.set(False)  # Uncheck the checkbox
                            else:
                                widget.configure(state="disabled")
                                self.launch_xtts_var.set(False)  # Uncheck the checkbox
                        elif widget.cget("text") == "Silero":
                            if config.get('silero_support', False):
                                widget.configure(state="normal")
                                self.launch_silero_var.set(False)  # Uncheck the checkbox
                            else:
                                widget.configure(state="disabled")
                                self.launch_silero_var.set(False)  # Uncheck the checkbox
                        elif widget.cget("text") == "Voicecraft":
                            if config.get('voicecraft_support', False):
                                widget.configure(state="normal")
                                self.launch_voicecraft_var.set(False)  # Uncheck the checkbox
                            else:
                                widget.configure(state="disabled")
                                self.launch_voicecraft_var.set(False)  # Uncheck the checkbox
                    elif isinstance(widget, ctk.CTkFrame):
                        for child in widget.winfo_children():
                            if isinstance(child, ctk.CTkCheckBox):
                                if child.cget("text") in ["Low VRAM", "DeepSpeed", "Use CPU"]:
                                    if config.get('xtts_support', False):
                                        child.configure(state="normal")
                                    else:
                                        child.configure(state="disabled")
        else:
            if self.launch_button:
                self.launch_button.configure(state="disabled")
            for widget in self.launch_frame.winfo_children():
                if isinstance(widget, ctk.CTkCheckBox):
                    widget.configure(state="disabled")
                elif isinstance(widget, ctk.CTkFrame):
                    for child in widget.winfo_children():
                        if isinstance(child, ctk.CTkCheckBox):
                            child.configure(state="disabled")
        
    def update_gpu_options(self):
        xtts_checkbox = None
        xtts_cpu_checkbox = None
        for widget in self.installation_frame.winfo_children():
            if isinstance(widget, ctk.CTkFrame):
                for child in widget.winfo_children():
                    if isinstance(child, ctk.CTkCheckBox):
                        if child.cget("text") == "XTTS":
                            xtts_checkbox = child
                        elif child.cget("text") == "XTTS CPU only":
                            xtts_cpu_checkbox = child
        
        if xtts_checkbox and xtts_cpu_checkbox:
            pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
            config_path = os.path.join(pandrator_path, 'config.json')
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = json.load(f)
                xtts_support = config.get('xtts_support', False)
                if xtts_support:
                    xtts_checkbox.configure(state="disabled")
                    xtts_cpu_checkbox.configure(state="disabled")
            else:
                xtts_cpu_checkbox.configure(state="normal")
                if xtts_checkbox.get():
                    xtts_cpu_checkbox.configure(state="disabled")
        
    def refresh_gui_state(self):
        # Read the latest config
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        config_path = os.path.join(pandrator_path, 'config.json')
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config = json.load(f)
            
            # Update installation checkboxes
            self.xtts_checkbox.configure(state="disabled" if config.get('xtts_support', False) else "normal")
            self.xtts_cpu_checkbox.configure(state="disabled" if config.get('xtts_support', False) else "normal")
            self.silero_checkbox.configure(state="disabled" if config.get('silero_support', False) else "normal")
            self.voicecraft_checkbox.configure(state="disabled" if config.get('voicecraft_support', False) else "normal")
            
            # Update launch checkboxes
            for widget in self.launch_frame.winfo_children():
                if isinstance(widget, ctk.CTkCheckBox):
                    if widget.cget("text") == "XTTS":
                        widget.configure(state="normal" if config.get('xtts_support', False) else "disabled")
                    elif widget.cget("text") == "Silero":
                        widget.configure(state="normal" if config.get('silero_support', False) else "disabled")
                    elif widget.cget("text") == "Voicecraft":
                        widget.configure(state="normal" if config.get('voicecraft_support', False) else "disabled")
                elif isinstance(widget, ctk.CTkFrame):
                    for child in widget.winfo_children():
                        if isinstance(child, ctk.CTkCheckBox):
                            if child.cget("text") in ["Low VRAM", "DeepSpeed", "Use CPU"]:
                                child.configure(state="normal" if config.get('xtts_support', False) else "disabled")
        
        
    def remove_directory(self, path):
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                shutil.rmtree(path)
                return True
            except PermissionError:
                time.sleep(1)  # Wait for a second before retrying
        return False

    def check_existing_installations(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')

        if os.path.exists(pandrator_path):
            self.pandrator_var.set(False)
            self.pandrator_checkbox.configure(state="disabled")

            config_path = os.path.join(pandrator_path, 'config.json')
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = json.load(f)

                xtts_support = config.get('xtts_support', False)

                if xtts_support:
                    self.xtts_var.set(False)
                    self.xtts_cpu_var.set(False)
                    self.xtts_checkbox.configure(state="disabled")
                    self.xtts_cpu_checkbox.configure(state="disabled")
                else:
                    self.xtts_var.set(False)
                    self.xtts_cpu_var.set(False)
                    self.xtts_checkbox.configure(state="normal")
                    self.xtts_cpu_checkbox.configure(state="normal")

                silero_support = config.get('silero_support', False)

                if silero_support:
                    self.silero_var.set(False)
                    self.silero_checkbox.configure(state="disabled")
                else:
                    self.silero_var.set(False)
                    self.silero_checkbox.configure(state="normal")

                voicecraft_support = config.get('voicecraft_support', False)

                if voicecraft_support:
                    self.voicecraft_var.set(False)
                    self.voicecraft_checkbox.configure(state="disabled")
                else:
                    self.voicecraft_var.set(False)
                    self.voicecraft_checkbox.configure(state="normal")

        self.update_button_states()
        self.update_gpu_options()
        self.update_xtts_launch_options() 
    
    def install_pandrator(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        pandrator_already_installed = os.path.exists(pandrator_path)
        
        if pandrator_already_installed and not self.pandrator_var.get():
            if not any([self.xtts_var.get(), self.xtts_cpu_var.get(), self.silero_var.get(), self.voicecraft_var.get(), self.rvc_var.get()]):
                messagebox.showinfo("Info", "No new components selected for installation.")
                return
        elif not pandrator_already_installed and not self.pandrator_var.get():
            messagebox.showerror("Error", "Pandrator must be installed first before adding new components.")
            return

        self.disable_buttons()
        self.progress_bar.set(0)
        self.status_label.configure(text="Installing...")

        self.initialize_logging()

        logging.info("Installation process started.")

        threading.Thread(target=self.install_process, daemon=True).start()

    def open_log_file(self):
        if hasattr(self, 'log_filename') and os.path.exists(self.log_filename):
            os.startfile(self.log_filename)
        else:
            self.status_label.configure(text="No log file available.")

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
            self.run_command(['winget', '--version'])
            logging.info("winget is already installed.")
        except FileNotFoundError:
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
                    
                    # Try to execute the PowerShell script
                    try:
                        self.run_command([
                            'powershell',
                            '-ExecutionPolicy',
                            'Bypass',
                            '-File',
                            script_path
                        ], use_shell=True)
                    except subprocess.CalledProcessError:
                        logging.warning("Failed to execute PowerShell script. Trying with explicit path...")
                        # Fallback to explicit PowerShell path
                        powershell_path = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
                        self.run_command([
                            powershell_path,
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
        except subprocess.CalledProcessError as e:
            logging.error(f"Error occurred while checking winget version: {str(e)}")
            logging.error(f"Error output: {e.stderr.decode('utf-8')}")
            raise
            
    def get_system_architecture(self):
        return 'x64' if sys.maxsize > 2**32 else 'x86'
           
    def get_program_path_from_registry(self, program_name):
        try:
            if program_name == 'git':
                key_path = r"SOFTWARE\GitForWindows"
                value_name = "InstallPath"
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
        dependencies = [('Git.Git', 'git')]
        for winget_id, program_name in dependencies:
            logging.info(f"Checking installation for {program_name}")
            if not self.check_program_installed(program_name):
                logging.info(f"Installing {program_name}...")
                try:
                    self.run_command(['winget', 'install', '--id', winget_id, '-e', '--accept-source-agreements', '--accept-package-agreements'])
                    
                    # Refresh environment variables in a new session
                    self.refresh_env_in_new_session()
                    
                    # Only verify installation for git
                    if program_name == 'git':
                        if not self.check_program_installed(program_name):
                            logging.warning(f"{program_name} installation not detected. Attempting to use absolute path.")
                            program_path = self.get_program_path_from_registry(program_name)
                            if program_path:
                                bin_path = os.path.join(program_path, 'bin')
                                os.environ['PATH'] = f"{bin_path};{os.environ['PATH']}"
                                logging.info(f"Added {program_name} to PATH: {bin_path}")
                            else:
                                absolute_path = self.get_program_path(program_name)
                                if absolute_path:
                                    os.environ[program_name.upper()] = absolute_path
                                    logging.info(f"Updated {program_name} path: {absolute_path}")
                                else:
                                    raise Exception(f"Failed to find {program_name} after installation.")
                    
                except subprocess.CalledProcessError as e:
                    logging.error(f"Failed to install {program_name}.")
                    logging.error(f"Error output: {e.stderr.decode('utf-8')}")
                    raise
            else:
                logging.info(f"{program_name} is already installed.")

        # Handle Calibre installation separately
        return self.install_calibre()

    def install_calibre(self):
        logging.info("Checking installation for Calibre")
        if not self.check_program_installed('calibre'):
            logging.info("Installing Calibre...")
            try:
                self.run_command(['winget', 'install', '--id', 'calibre.calibre', '-e', '--accept-source-agreements', '--accept-package-agreements'])
                self.refresh_env_in_new_session()
                if self.check_program_installed('calibre'):
                    logging.info("Calibre installed successfully.")
                    return True
                else:
                    logging.warning("Calibre installation not detected after installation attempt.")
                    return False
            except subprocess.CalledProcessError as e:
                logging.error("Failed to install Calibre.")
                logging.error(f"Error output: {e.stderr.decode('utf-8')}")
                return False
        else:
            logging.info("Calibre is already installed.")
            return True
            
    def show_calibre_installation_message(self):
        message = ("Calibre installation failed. Please install Calibre manually.\n"
                   "You can download it from: https://calibre-ebook.com/download_windows")
        messagebox.showwarning("Calibre Installation Required", message)

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
        url = f'https://repo.anaconda.com/miniconda/{conda_installer}'
        
        # Download the file
        response = requests.get(url)
        with open(conda_installer, 'wb') as f:
            f.write(response.content)
        
        self.run_command([conda_installer, '/InstallationType=JustMe', '/RegisterPython=0', '/S', f'/D={install_path}'])
        os.remove(conda_installer)

    def check_conda(self, conda_path):
        return os.path.exists(os.path.join(conda_path, 'Scripts', 'conda.exe'))

    def create_conda_env(self, conda_path, env_name, python_version, additional_packages=None):
        logging.info(f"Creating conda environment {env_name}...")
        try:
            # Create the environment with Python
            create_command = [
                os.path.join(conda_path, 'Scripts', 'conda.exe'),
                'create',
                '-n', env_name,
                f'python={python_version}',
                '-y'
            ]
            self.run_command(create_command)

            # If it's the pandrator_installer environment, install ffmpeg from conda-forge
            if env_name == 'pandrator_installer':
                logging.info("Installing ffmpeg from conda-forge for pandrator_installer...")
                ffmpeg_command = [
                    os.path.join(conda_path, 'Scripts', 'conda.exe'),
                    'install',
                    '-n', env_name,
                    'conda-forge::ffmpeg',
                    '-y'
                ]
                self.run_command(ffmpeg_command)

            # Install additional packages if specified
            if additional_packages:
                logging.info(f"Installing additional packages: {', '.join(additional_packages)}")
                install_command = [
                    os.path.join(conda_path, 'Scripts', 'conda.exe'),
                    'install',
                    '-n', env_name,
                    '-y'
                ] + additional_packages
                self.run_command(install_command)

        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to create or setup conda environment {env_name}")
            logging.error(f"Error output: {e.stderr.decode('utf-8')}")
            raise

    def install_requirements(self, conda_path, env_name, requirements_file):
        logging.info(f"Installing requirements for {env_name}...")
        self.run_command([os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-n', env_name, 'pip', 'install', '-r', requirements_file])

    def install_package(self, conda_path, env_name, package):
        logging.info(f"Installing {package} in {env_name}...")
        self.run_command([os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-n', env_name, 'pip', 'install', package])

    def download_pretrained_models(self, repo_path):
        pretrained_models_dir = os.path.join(repo_path, 'pretrained_models')
        os.makedirs(pretrained_models_dir, exist_ok=True)
        encodec_url = 'https://huggingface.co/pyp1/VoiceCraft/resolve/main/encodec_4cb2048_giga.th'
        voicecraft_model_dir = os.path.join(pretrained_models_dir, 'VoiceCraft_gigaHalfLibri330M_TTSEnhanced_max16s')
        os.makedirs(voicecraft_model_dir, exist_ok=True)
        
        config_url = 'https://huggingface.co/pyp1/VoiceCraft_gigaHalfLibri330M_TTSEnhanced_max16s/resolve/main/config.json'
        model_url = 'https://huggingface.co/pyp1/VoiceCraft_gigaHalfLibri330M_TTSEnhanced_max16s/resolve/main/model.safetensors'
        encodec_path = os.path.join(pretrained_models_dir, 'encodec_4cb2048_giga.th')
        config_path = os.path.join(voicecraft_model_dir, 'config.json')
        model_path = os.path.join(voicecraft_model_dir, 'model.safetensors')

        def download_file(url, path):
            if not os.path.exists(path):
                logging.info(f"Downloading {os.path.basename(path)}...")
                try:
                    response = requests.get(url, stream=True)
                    response.raise_for_status()
                    with open(path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
                    logging.info(f"Successfully downloaded {os.path.basename(path)}")
                except requests.RequestException as e:
                    logging.error(f"Failed to download {os.path.basename(path)}")
                    logging.error(f"Error message: {str(e)}")
                    raise
            else:
                logging.info(f"{os.path.basename(path)} already exists. Skipping download.")

        download_file(encodec_url, encodec_path)
        download_file(config_url, config_path)
        download_file(model_url, model_path)

    def install_pytorch_and_xtts_api_server(self, conda_path, env_name):
        logging.info(f"Installing PyTorch and xtts-api-server package in {env_name}...")
        
        try:
            # Install PyTorch
            if self.xtts_cpu_var.get():
                pytorch_cmd = [os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-n', env_name, 'pip', 'install', 'torch==2.1.1', 'torchaudio==2.1.1']
            else:
                pytorch_cmd = [os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-n', env_name, 'pip', 'install', 'torch==2.1.1+cu118', 'torchaudio==2.1.1+cu118', '--extra-index-url', 'https://download.pytorch.org/whl/cu118']
            self.run_command(pytorch_cmd)
            
            # Install xtts-api-server package
            xtts_cmd = [os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-n', env_name, 'pip', 'install', 'xtts-api-server']
            self.run_command(xtts_cmd)
            
            logging.info("PyTorch and xtts-api-server package installed successfully.")
        except subprocess.CalledProcessError as e:
            logging.error("Error installing PyTorch and xtts-api-server package.")
            logging.error(f"Error output: {e.stderr.decode('utf-8')}")
            raise

    def install_audiocraft(self, conda_path, env_name, voicecraft_repo_path):
        logging.info(f"Installing audiocraft package in {env_name}...")
        try:
            audiocraft_repo = 'https://github.com/facebookresearch/audiocraft.git'
            audiocraft_commit = 'c5157b5bf14bf83449c17ea1eeb66c19fb4bc7f0'
            
            # Change to the VoiceCraft repository directory
            os.chdir(voicecraft_repo_path)
            
            # Install audiocraft package
            self.run_command([os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-n', env_name, 'pip', 'install', '-e', f'git+{audiocraft_repo}@{audiocraft_commit}#egg=audiocraft'])
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to install audiocraft package in {env_name}")
            logging.error(f"Error message: {str(e)}")
            raise

    def install_voicecraft_api_dependencies(self, conda_path, env_name):
        logging.info(f"Installing VoiceCraft API dependencies in {env_name}...")
        try:
            self.run_command([os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-n', env_name, 'conda', 'install', 'pytorch==2.0.1', 'torchvision==0.15.2', 'torchaudio==2.0.2', 'pytorch-cuda=11.7', '-c', 'pytorch', '-c', 'nvidia', '-y'])
            self.run_command([os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-n', env_name, 'conda', 'install', '-c', 'conda-forge', 'montreal-forced-aligner=2.2.17', 'openfst=1.8.2', 'kaldi=5.5.1068', '-y'])
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to install VoiceCraft API dependencies in {env_name}")
            logging.error(f"Error message: {str(e)}")
            raise

    def download_mfa_models(self, conda_path, env_name):
        logging.info(f"Downloading MFA models in {env_name}...")
        try:
            self.run_command([os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-n', env_name, 'mfa', 'model', 'download', 'dictionary', 'english_us_arpa'])
            self.run_command([os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-n', env_name, 'mfa', 'model', 'download', 'acoustic', 'english_us_arpa'])
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to download MFA models in {env_name}")
            logging.error(f"Error message: {str(e)}")
            raise

    def replace_files(self, repo_path, file_mappings):
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

    def install_silero_api_server(self, conda_path, env_name):
        logging.info(f"Installing Silero API server in {env_name}...")
        try:
            self.install_package(conda_path, env_name, 'requests')
            self.install_package(conda_path, env_name, 'silero-api-server')
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to install Silero API server in {env_name}")
            logging.error(f"Error message: {str(e)}")
            raise

    def check_and_update_numpy(self, conda_path, env_name):
        logging.info(f"Checking NumPy version in {env_name}...")
        try:
            # Check current NumPy version
            numpy_version = subprocess.check_output([os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-n', env_name, 'python', '-c', 'import numpy; print(numpy.__version__)'], universal_newlines=True).strip()
            logging.info(f"Current NumPy version: {numpy_version}")
            
            # If NumPy version is 2.x, downgrade to 1.24.3
            if numpy_version.startswith('2.'):
                logging.info("Downgrading NumPy to version 1.24.3...")
                self.run_command([os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-n', env_name, 'pip', 'install', 'numpy==1.24.3'])
                logging.info("NumPy downgraded successfully.")
            else:
                logging.info("NumPy version is compatible. No changes needed.")
        except subprocess.CalledProcessError as e:
            logging.error("Error checking or updating NumPy version.")
            logging.error(f"Error message: {str(e)}")
            raise

    def install_pytorch(self, conda_path, env_name):
        logging.info(f"Installing PyTorch 1.13.1 in {env_name}...")
        try:
            self.run_command([os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-n', env_name, 'pip', 'install', 'torch==1.13.1', 'torchvision==0.14.1', 'torchaudio==0.13.1'])
            logging.info("PyTorch 1.13.1 installed successfully.")
        except subprocess.CalledProcessError as e:
            logging.error("Error installing PyTorch.")
            logging.error(f"Error message: {str(e)}")
            raise

    def install_rvc_cli(self, conda_path, env_name):
        logging.info("Starting RVC_CLI installation")
        rvc_cli_path = os.path.join(os.path.dirname(conda_path), 'rvc-cli')

        try:
            # Clone RVC_CLI repository
            logging.info("Cloning RVC_CLI repository...")
            clone_cmd = ['git', 'clone', 'https://github.com/blaisewf/rvc-cli.git', rvc_cli_path]
            logging.info(f"Running command: {' '.join(clone_cmd)}")
            clone_result = subprocess.run(clone_cmd, capture_output=True, text=True, check=True)
            logging.info(clone_result.stdout)

            # Run install.bat
            install_bat_path = os.path.join(rvc_cli_path, 'install.bat')
            logging.info("Running install.bat for RVC_CLI...")
            logging.info(f"Running command: {install_bat_path}")
            
            install_process = subprocess.Popen(install_bat_path, cwd=rvc_cli_path, shell=True, 
                                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            
            installation_successful = False
            for line in iter(install_process.stdout.readline, ''):
                stripped_line = line.strip()
                logging.info(stripped_line)
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
            logging.info("Running prerequisites...")
            python_exe = os.path.join(rvc_cli_path, 'env', 'python.exe')
            rvc_cli_script = os.path.join(rvc_cli_path, 'rvc_cli.py')
            
            if not os.path.exists(python_exe):
                raise FileNotFoundError(f"Python executable not found at: {python_exe}")
            if not os.path.exists(rvc_cli_script):
                raise FileNotFoundError(f"RVC CLI script not found at: {rvc_cli_script}")

            prerequisites_cmd = [python_exe, rvc_cli_script, 'prerequisites']
            logging.info(f"Running command: {' '.join(prerequisites_cmd)}")
            
            prereq_process = subprocess.Popen(prerequisites_cmd, cwd=rvc_cli_path, 
                                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            
            for line in iter(prereq_process.stdout.readline, ''):
                logging.info(line.strip())

            prereq_process.wait(timeout=1800)  # 30 minutes timeout
            if prereq_process.returncode != 0:
                raise Exception(f"Prerequisites process exited with non-zero return code: {prereq_process.returncode}")

            logging.info("RVC_CLI installation and prerequisites download completed successfully.")

        except Exception as e:
            error_msg = f"An error occurred during RVC_CLI installation or prerequisites download: {str(e)}"
            logging.error(error_msg)
            logging.error(traceback.format_exc())
            raise

    def install_process(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        conda_path = os.path.join(pandrator_path, 'conda')
        pandrator_already_installed = os.path.exists(pandrator_path)
        
        try:
            self.update_progress(0.1)
            self.update_status("Installing winget...")
            self.install_winget()

            self.update_progress(0.2)
            self.update_status("Installing dependencies...")
            try:
                self.install_dependencies()
            except Exception as e:
                logging.error(f"Error during dependency installation: {str(e)}")
                self.show_calibre_installation_message()

            self.update_progress(0.3)
            self.update_status("Installing Visual C++ Build Tools...")
            self.install_visual_cpp_build_tools()
            
            self.update_progress(0.4)
            self.update_status("Cloning repositories...")
            
            if self.pandrator_var.get() or not pandrator_already_installed:
                self.run_command(['git', 'clone', 'https://github.com/lukaszliniewicz/Pandrator.git', os.path.join(pandrator_path, 'Pandrator')])
            
            if self.xtts_var.get() or self.xtts_cpu_var.get():
                self.run_command(['git', 'clone', 'https://github.com/daswer123/xtts-api-server.git', os.path.join(pandrator_path, 'xtts-api-server')])
            
            if self.voicecraft_var.get():
                self.run_command(['git', 'clone', 'https://github.com/lukaszliniewicz/VoiceCraft_API.git', os.path.join(pandrator_path, 'VoiceCraft_API')])

            self.update_progress(0.5)
            self.update_status("Installing Miniconda...")
            if not self.check_conda(conda_path):
                self.install_conda(conda_path)

            if not self.check_conda(conda_path):
                self.update_status("Conda installation failed")
                logging.error("Conda installation failed")
                self.enable_buttons()
                return

            if self.pandrator_var.get() or not pandrator_already_installed:
                self.update_progress(0.6)
                self.update_status("Creating Pandrator Conda environment...")
                self.create_conda_env(conda_path, 'pandrator_installer', '3.10')

                self.update_progress(0.7)
                self.update_status("Installing Pandrator requirements...")
                pandrator_repo_path = os.path.join(pandrator_path, 'Pandrator')
                self.install_requirements(conda_path, 'pandrator_installer', os.path.join(pandrator_repo_path, 'requirements.txt'))

            if self.xtts_var.get() or self.xtts_cpu_var.get():
                self.update_progress(0.8)
                self.update_status("Creating XTTS Conda environment...")
                self.create_conda_env(conda_path, 'xtts_api_server_installer', '3.10')
                self.update_progress(0.9)
                self.update_status("Installing PyTorch and xtts-api-server...")
                self.install_pytorch_and_xtts_api_server(conda_path, 'xtts_api_server_installer')

            if self.silero_var.get():
                self.update_progress(0.8)
                self.update_status("Creating Silero Conda environment...")
                self.create_conda_env(conda_path, 'silero_api_server_installer', '3.10')

                self.update_progress(0.9)
                self.update_status("Installing Silero API server...")
                self.install_silero_api_server(conda_path, 'silero_api_server_installer')
                self.check_and_update_numpy(conda_path, 'silero_api_server_installer')
                self.install_pytorch(conda_path, 'silero_api_server_installer')

            if self.voicecraft_var.get():
                self.update_progress(0.8)
                self.update_status("Creating VoiceCraft Conda environment...")
                self.create_conda_env(conda_path, 'voicecraft_api_installer', '3.9.16')

                self.update_progress(0.9)
                self.update_status("Installing VoiceCraft API dependencies...")
                voicecraft_repo_path = os.path.join(pandrator_path, 'VoiceCraft_API')
                self.install_requirements(conda_path, 'voicecraft_api_installer', os.path.join(voicecraft_repo_path, 'requirements.txt'))
                self.install_voicecraft_api_dependencies(conda_path, 'voicecraft_api_installer')
                self.download_mfa_models(conda_path, 'voicecraft_api_installer')
                self.install_audiocraft(conda_path, 'voicecraft_api_installer', voicecraft_repo_path)

                # Replace files in the VoiceCraft repo
                file_mappings = {
                    'audiocraft_windows/cluster.py': 'src/audiocraft/audiocraft/utils/cluster.py',
                    'audiocraft_windows/environment.py': 'src/audiocraft/audiocraft/environment.py',
                    'audiocraft_windows/checkpoint.py': 'src/audiocraft/audiocraft/utils/checkpoint.py'
                }
                self.replace_files(voicecraft_repo_path, file_mappings)

                # Download pretrained models
                self.download_pretrained_models(voicecraft_repo_path)

            if self.rvc_var.get():
                self.update_progress(0.8)
                self.update_status("Installing RVC_CLI...")
                self.install_rvc_cli(conda_path, 'rvc_cli_installer')

            # Create or update config file
            config_path = os.path.join(pandrator_path, 'config.json')
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = json.load(f)
            else:
                config = {}

            # Update config based on what was installed or already exists
            config['cuda_support'] = config.get('cuda_support', False) or self.xtts_var.get()
            config['xtts_support'] = config.get('xtts_support', False) or self.xtts_var.get() or self.xtts_cpu_var.get()
            config['silero_support'] = config.get('silero_support', False) or self.silero_var.get()
            config['voicecraft_support'] = config.get('voicecraft_support', False) or self.voicecraft_var.get()

            with open(config_path, 'w') as f:
                json.dump(config, f)

            self.update_progress(1.0)
            self.update_status("Installation complete!")
            logging.info("Installation completed successfully.")

        except Exception as e:
            logging.error(f"Installation failed: {str(e)}")
            logging.error(traceback.format_exc())
            self.update_status("Installation failed. Check the log for details.")
        finally:
            self.after(100, self.update_gpu_options)
            self.after(100, self.update_button_states)
            self.check_existing_installations()


    def launch_apps(self):
        base_path = os.path.abspath(self.initial_working_dir)
        pandrator_path = os.path.join(base_path, 'Pandrator')
        conda_path = os.path.join(pandrator_path, 'conda')

        self.update_progress(0.3)
        self.update_status("Preparing to launch...")
        logging.info(f"Launch process started. Base directory: {base_path}")
        logging.info(f"Pandrator path: {pandrator_path}")
        logging.info(f"Conda path: {conda_path}")

        if self.launch_xtts_var.get():
            self.update_progress(0.4)
            self.update_status("Starting XTTS server...")
            xtts_server_path = os.path.join(pandrator_path, 'xtts-api-server')
            logging.info(f"XTTS server path: {xtts_server_path}")
            
            if not os.path.exists(xtts_server_path):
                error_msg = f"XTTS server path not found: {xtts_server_path}"
                self.update_status(error_msg)
                logging.error(error_msg)
                return
            
            try:
                use_cpu = self.xtts_cpu_launch_var.get()
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

        if self.launch_silero_var.get():
            self.update_progress(0.6)
            self.update_status("Starting Silero server...")
            
            try:
                self.silero_process = self.run_silero_api_server(conda_path, 'silero_api_server_installer')
            except Exception as e:
                error_msg = f"Failed to start Silero server: {str(e)}"
                self.update_status(error_msg)
                logging.error(error_msg)
                logging.exception("Exception details:")
                return
            
            silero_server_url = 'http://127.0.0.1:8001/docs'
            if not self.check_silero_server_online(silero_server_url):
                error_msg = "Silero server failed to come online"
                self.update_status(error_msg)
                logging.error(error_msg)
                self.shutdown_silero()
                return

        if self.launch_voicecraft_var.get():
            self.update_progress(0.8)
            self.update_status("Starting VoiceCraft server...")
            voicecraft_repo_path = os.path.join(pandrator_path, 'VoiceCraft_API')
            api_script_path = os.path.join(voicecraft_repo_path, 'api.py')
            
            try:
                self.voicecraft_process = self.run_voicecraft_api_server(conda_path, 'voicecraft_api_installer', api_script_path, voicecraft_repo_path)
            except Exception as e:
                error_msg = f"Failed to start VoiceCraft server: {str(e)}"
                self.update_status(error_msg)
                logging.error(error_msg)
                logging.exception("Exception details:")
                return
            
            voicecraft_server_url = 'http://127.0.0.1:8245/docs'
            if not self.check_voicecraft_server_online(voicecraft_server_url):
                error_msg = "VoiceCraft server failed to come online"
                self.update_status(error_msg)
                logging.error(error_msg)
                self.shutdown_voicecraft()
                return

        if self.launch_pandrator_var.get():
            self.update_progress(0.9)
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
        self.update_status("Apps are running!")
        self.update_button_states()
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

    def run_silero_api_server(self, conda_path, env_name):
        logging.info(f"Running Silero API server in {env_name}...")

        # Create log file for silero server output
        silero_log_file = os.path.join(os.getcwd(), 'silero_server.log')

        # Run silero server command with output redirection
        silero_server_command = f'"{os.path.join(conda_path, "Scripts", "conda.exe")}" run -n {env_name} python -m silero_api_server > "{silero_log_file}" 2>&1'
        process = subprocess.Popen(silero_server_command, shell=True)
        self.silero_process = process
        return process

    def check_silero_server_online(self, url, max_attempts=30, wait_interval=10):
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

    def run_voicecraft_api_server(self, conda_path, env_name, api_script_path, voicecraft_repo_path):
        logging.info(f"Running VoiceCraft API server in {env_name}...")
        try:
            # Change to the VoiceCraft repository directory
            os.chdir(voicecraft_repo_path)
            
            voicecraft_server_command = [os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-n', env_name, 'python', api_script_path]
            process = subprocess.Popen(voicecraft_server_command, creationflags=subprocess.CREATE_NEW_CONSOLE)
            self.voicecraft_process = process
            return process
        except Exception as e:
            logging.error(f"Failed to run VoiceCraft API server in {env_name}")
            logging.error(f"Error message: {str(e)}")
            logging.error(traceback.format_exc())
            raise

    def check_voicecraft_server_online(self, url, max_attempts=30, wait_interval=10):
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

    def check_processes_status(self):
        if self.pandrator_process and self.pandrator_process.poll() is not None:
            # Pandrator has exited
            self.pandrator_process = None
            self.shutdown_apps()  # Shut down other apps when Pandrator exits
        if self.xtts_process and self.xtts_process.poll() is not None:
            # XTTS has exited
            self.xtts_process = None
        if self.silero_process and self.silero_process.poll() is not None:
            # Silero has exited
            self.silero_process = None
        if self.voicecraft_process and self.voicecraft_process.poll() is not None:
            # VoiceCraft has exited
            self.voicecraft_process = None
        
        if not self.pandrator_process and not self.xtts_process and not self.silero_process and not self.voicecraft_process:
            self.update_status("All processes have exited.")
            self.update_button_states()
        else:
            self.after(5000, self.check_processes_status)  # Schedule next check

    def shutdown_apps(self):
        self.shutdown_xtts()
        self.shutdown_silero()
        self.shutdown_voicecraft()

    def shutdown_xtts(self):
        if self.xtts_process:
            logging.info(f"Terminating XTTS process with PID: {self.xtts_process.pid}")
            try:
                parent = psutil.Process(self.xtts_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.terminate()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when terminating child process with PID: {child.pid}")
                parent.terminate()
                self.xtts_process.wait(timeout=10)
            except psutil.NoSuchProcess:
                logging.info("XTTS process already terminated.")
            except psutil.TimeoutExpired:
                logging.warning("XTTS process did not terminate, forcing kill")
                parent = psutil.Process(self.xtts_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when killing child process with PID: {child.pid}")
                parent.kill()
            self.xtts_process = None

        # Check if any process is using port 8020 and kill it
        for conn in psutil.net_connections():
            if conn.laddr.port == 8020:
                try:
                    process = psutil.Process(conn.pid)
                    if process.pid != 0:  # Skip System Idle Process
                        process.terminate()
                        logging.info(f"Terminated process using port 8020: PID {conn.pid}")
                except psutil.NoSuchProcess:
                    logging.info(f"Process using port 8020 (PID {conn.pid}) no longer exists")
                except psutil.AccessDenied:
                    logging.warning(f"Access denied when terminating process with PID: {conn.pid}")

    def shutdown_silero(self):
        if self.silero_process:
            logging.info(f"Terminating Silero process with PID: {self.silero_process.pid}")
            try:
                parent = psutil.Process(self.silero_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.terminate()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when terminating child process with PID: {child.pid}")
                parent.terminate()
                self.silero_process.wait(timeout=10)
            except psutil.NoSuchProcess:
                logging.info("Silero process already terminated.")
            except psutil.TimeoutExpired:
                logging.warning("Silero process did not terminate, forcing kill")
                parent = psutil.Process(self.silero_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when killing child process with PID: {child.pid}")
                parent.kill()
            self.silero_process = None

        # Check if any process is using port 8001 and kill it
        for conn in psutil.net_connections():
            if conn.laddr.port == 8001:
                try:
                    process = psutil.Process(conn.pid)
                    if process.pid != 0:  # Skip System Idle Process
                        process.terminate()
                        logging.info(f"Terminated process using port 8001: PID {conn.pid}")
                except psutil.NoSuchProcess:
                    logging.info(f"Process using port 8001 (PID {conn.pid}) no longer exists")
                except psutil.AccessDenied:
                    logging.warning(f"Access denied when terminating process with PID: {conn.pid}")

    def shutdown_voicecraft(self):
        if self.voicecraft_process:
            logging.info(f"Terminating VoiceCraft process with PID: {self.voicecraft_process.pid}")
            try:
                parent = psutil.Process(self.voicecraft_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.terminate()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when terminating child process with PID: {child.pid}")
                parent.terminate()
                self.voicecraft_process.wait(timeout=10)
            except psutil.NoSuchProcess:
                logging.info("VoiceCraft process already terminated.")
            except psutil.TimeoutExpired:
                logging.warning("VoiceCraft process did not terminate, forcing kill")
                parent = psutil.Process(self.voicecraft_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when killing child process with PID: {child.pid}")
                parent.kill()
            self.voicecraft_process = None

        # Check if any process is using port 8245 and kill it
        for conn in psutil.net_connections():
            if conn.laddr.port == 8245:
                try:
                    process = psutil.Process(conn.pid)
                    if process.pid != 0:  # Skip System Idle Process
                        process.terminate()
                        logging.info(f"Terminated process using port 8245: PID {conn.pid}")
                except psutil.NoSuchProcess:
                    logging.info(f"Process using port 8245 (PID {conn.pid}) no longer exists")
                except psutil.AccessDenied:
                    logging.warning(f"Access denied when terminating process with PID: {conn.pid}")

    def destroy(self):
        self.shutdown_apps()
        super().destroy()

if __name__ == "__main__":
    app = PandratorInstaller()
    app.mainloop()
