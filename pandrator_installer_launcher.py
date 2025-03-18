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
from dulwich import porcelain
import packaging.version
from CTkMessagebox import CTkMessagebox


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
        
        # Check for spaces in the working directory
        if ' ' in self.initial_working_dir:
            self.title("Warning: Path Contains Spaces")
            warning_message = (
                f"⚠️ WARNING: Your installation path contains spaces:\n\n"
                f"{self.initial_working_dir}\n\n"
                f"This will likely cause problems with Conda and may prevent Pandrator from installing correctly.\n\n"
                f"It's strongly recommended to move this installer to a path without spaces, such as:\n"
                f"C:\\Pandrator\n\n"
                f"Would you like to exit the installer so you can move it to a better location?"
            )
            
            result = messagebox.askyesno("Path Contains Spaces", warning_message, icon="warning")
            if result:
                sys.exit(0)
            else:
                messagebox.showinfo(
                    "Continuing With Risk", 
                    "Installation will continue, but you may encounter errors.\n"
                    "If installation fails, please restart the installer from a path without spaces."
                )
        # Define instance variables for checkboxes
        self.pandrator_var = ctk.BooleanVar(value=True)
        self.xtts_var = ctk.BooleanVar(value=False)
        self.xtts_cpu_var = ctk.BooleanVar(value=False)
        self.silero_var = ctk.BooleanVar(value=False)
        self.rvc_var = ctk.BooleanVar(value=False)

        # Define instance variables for launch options
        self.launch_pandrator_var = ctk.BooleanVar(value=True)
        self.launch_xtts_var = ctk.BooleanVar(value=False)
        self.lowvram_var = ctk.BooleanVar(value=False)
        self.deepspeed_var = ctk.BooleanVar(value=False)
        self.xtts_cpu_launch_var = ctk.BooleanVar(value=False)
        self.launch_silero_var = ctk.BooleanVar(value=False)


        # Initialize process attributes
        self.xtts_process = None
        self.pandrator_process = None
        self.silero_process = None

        self.title("Pandrator Installer & Launcher")
        
        # Calculate 92% of screen height and get full screen width
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        window_height = int(screen_height * 0.92)

        # Set the window geometry to full width and 92% height, positioned at the top
        self.geometry(f"{screen_width}x{window_height}+0+0")

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
        self.info_text.insert("1.0", "This tool will help you set up and run Pandrator as well as TTS engines and tools. "
                            "It will install Pandrator, Miniconda, required Python packages, "
                            "and dependencies (Calibre, Visual Studio C++ Build Tools)."
                            "To uninstall Pandrator, simply delete the Pandrator folder.\n\n"
                            "The installation will take between 3 and 30GB of disk space depending on the number of selected options.")
        self.info_text.configure(state="disabled")

        # New frame to contain installation and launch frames
        self.main_options_frame = ctk.CTkFrame(self.content_frame)
        self.main_options_frame.pack(fill="both", expand=True, padx=20, pady=10)
        self.main_options_frame.grid_columnconfigure(0, weight=1)
        self.main_options_frame.grid_columnconfigure(1, weight=1)

        # Installation Frame
        self.installation_frame = ctk.CTkFrame(self.main_options_frame)
        self.installation_frame.grid(row=0, column=0, padx=(0, 10), pady=10, sticky="nsew")

        ctk.CTkLabel(self.installation_frame, text="Install", font=("Arial", 20, "bold")).pack(anchor="w", padx=10, pady=(10, 5))
        # Build Tools notice and button section
        build_tools_frame = ctk.CTkFrame(self.installation_frame, fg_color="#2C2C2C")
        build_tools_frame.pack(fill="x", padx=10, pady=(5, 15))

        admin_warning = ctk.CTkLabel(
            build_tools_frame, 
            text="⚠️ IMPORTANT: Pandrator Installer must be run as administrator to install Build Tools!",
            font=("Arial", 11, "bold"),
            text_color="#FF9900",
            justify="left"
        )
        admin_warning.pack(anchor="w", padx=10, pady=(10, 5))

        build_tools_label = ctk.CTkLabel(
            build_tools_frame, 
            text="Visual C++ Build Tools are required for installation.\nIf not already installed:",
            font=("Arial", 11),
            justify="left"
        )
        build_tools_label.pack(anchor="w", padx=10, pady=(0, 5))

        build_tools_steps = ctk.CTkLabel(
            build_tools_frame, 
            text="1. Click the button below to install them\n2. After installation, close all command prompt/PowerShell windows\n3. Restart this installer",
            font=("Arial", 10),
            justify="left"
        )
        build_tools_steps.pack(anchor="w", padx=20, pady=(0, 5))

        self.build_tools_button = ctk.CTkButton(
            build_tools_frame, 
            text="Install Visual C++ Build Tools", 
            command=self.open_build_tools_installer,
            width=200
        )
        self.build_tools_button.pack(anchor="w", padx=10, pady=(0, 10))
        self.pandrator_checkbox = ctk.CTkCheckBox(self.installation_frame, text="Pandrator", variable=self.pandrator_var)
        self.pandrator_checkbox.pack(anchor="w", padx=10, pady=(5, 0))

        ctk.CTkLabel(self.installation_frame, text="TTS Engines", font=("Arial", 14, "bold")).pack(anchor="w", padx=10, pady=(20, 0))
        ctk.CTkLabel(self.installation_frame, text="You can select and install new engines and tools after the initial installation.", font=("Arial", 10, "bold")).pack(anchor="w", padx=10, pady=(0, 10))

        engine_frame = ctk.CTkFrame(self.installation_frame, fg_color="transparent")
        engine_frame.pack(fill="x", padx=10, pady=(0, 10))
        
        self.xtts_checkbox = ctk.CTkCheckBox(engine_frame, text="XTTS", variable=self.xtts_var)
        self.xtts_checkbox.pack(side="left", padx=(0, 20), pady=5)
        self.xtts_cpu_checkbox = ctk.CTkCheckBox(engine_frame, text="XTTS CPU only", variable=self.xtts_cpu_var)
        self.xtts_cpu_checkbox.pack(side="left", padx=(0, 20), pady=5)

        self.silero_checkbox = ctk.CTkCheckBox(engine_frame, text="Silero", variable=self.silero_var)
        self.silero_checkbox.pack(side="left", padx=(0, 20), pady=5)

        ctk.CTkLabel(self.installation_frame, text="Other tools", font=("Arial", 14, "bold")).pack(anchor="w", padx=10, pady=(20, 5))

        self.rvc_checkbox = ctk.CTkCheckBox(self.installation_frame, text="RVC (rvc-python)", variable=self.rvc_var)
        self.rvc_checkbox.pack(anchor="w", padx=10, pady=5)
        self.whisperx_var = ctk.BooleanVar(value=False)
        self.whisperx_checkbox = ctk.CTkCheckBox(self.installation_frame, text="WhisperX (needed for dubbing and XTTS training)", variable=self.whisperx_var)
        self.whisperx_checkbox.pack(anchor="w", padx=10, pady=5)
        self.xtts_finetuning_var = ctk.BooleanVar(value=False)
        self.xtts_finetuning_checkbox = ctk.CTkCheckBox(self.installation_frame, text="XTTS Fine-tuning", variable=self.xtts_finetuning_var, command=self.update_whisperx_checkbox)
        self.xtts_finetuning_checkbox.pack(anchor="w", padx=10, pady=5)
        button_frame = ctk.CTkFrame(self.installation_frame, fg_color="transparent")
        button_frame.pack(anchor="w", padx=10, pady=(20, 10))

        self.install_button = ctk.CTkButton(button_frame, text="Install", command=self.install_pandrator, width=200, height=40)
        self.install_button.pack(side="left", padx=(0, 10))
        self.update_button = ctk.CTkButton(button_frame, text="Update Pandrator", command=self.update_pandrator, width=200, height=40)
        self.update_button.pack(side="left", padx=10)
        self.open_log_button = ctk.CTkButton(button_frame, text="View Installation Log", command=self.open_log_file, width=200, height=40)
        self.open_log_button.pack(side="left", padx=10)
        self.open_log_button.configure(state="disabled")

        # Progress Bar and Status Label (now inside installation frame)
        self.progress_bar = ctk.CTkProgressBar(self.installation_frame)
        self.progress_bar.pack(fill="x", padx=20, pady=(20, 10))
        self.progress_bar.set(0)

        self.status_label = ctk.CTkLabel(self.installation_frame, text="", font=("Arial", 14))
        self.status_label.pack(pady=(0, 10))

        # Launch Frame
        self.launch_frame = ctk.CTkFrame(self.main_options_frame)
        self.launch_frame.grid(row=0, column=1, padx=(10, 0), pady=10, sticky="nsew")
        ctk.CTkLabel(self.launch_frame, text="Launch", font=("Arial", 20, "bold")).grid(row=0, column=0, columnspan=4, sticky="w", padx=10, pady=(10, 5))
        ctk.CTkCheckBox(self.launch_frame, text="Pandrator", variable=self.launch_pandrator_var).grid(row=1, column=0, columnspan=4, sticky="w", padx=10, pady=5)

        # XTTS options in one row
        ctk.CTkCheckBox(self.launch_frame, text="XTTS", variable=self.launch_xtts_var).grid(row=2, column=0, sticky="w", padx=10, pady=5)
        self.xtts_cpu_checkbox = ctk.CTkCheckBox(self.launch_frame, text="Use CPU", variable=self.xtts_cpu_launch_var)
        self.xtts_cpu_checkbox.grid(row=2, column=1, sticky="w", padx=10, pady=5)
        self.lowvram_checkbox = ctk.CTkCheckBox(self.launch_frame, text="Low VRAM", variable=self.lowvram_var)
        self.lowvram_checkbox.grid(row=2, column=2, sticky="w", padx=10, pady=5)
        self.deepspeed_checkbox = ctk.CTkCheckBox(self.launch_frame, text="DeepSpeed", variable=self.deepspeed_var)
        self.deepspeed_checkbox.grid(row=2, column=3, sticky="w", padx=10, pady=5)

        ctk.CTkCheckBox(self.launch_frame, text="Silero", variable=self.launch_silero_var).grid(row=3, column=0, columnspan=4, sticky="w", padx=10, pady=5)
        self.launch_button = ctk.CTkButton(self.launch_frame, text="Launch", command=self.launch_apps, width=200, height=40)
        self.launch_button.grid(row=5, column=0, columnspan=4, sticky="w", padx=10, pady=(20, 10))

        self.refresh_ui_state()
        atexit.register(self.shutdown_apps)

    def initialize_logging(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        os.makedirs(pandrator_path, exist_ok=True)
        logs_path = os.path.join(pandrator_path, 'Logs')
        os.makedirs(logs_path, exist_ok=True)

        current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_filename = os.path.join(logs_path, f'pandrator_installation_log_{current_time}.log')
        
        # Configure logging with explicit encoding handling
        handler = logging.FileHandler(self.log_filename, encoding='utf-8')
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        
        logger = logging.getLogger()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        self.open_log_button.configure(state="normal")

    def is_admin(self):
        """Check if the current process has admin privileges."""
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except:
            return False

    def is_build_tools_installed(self):
        """Check if Visual Studio Build Tools are installed."""
        try:
            # Method 1: Check common installation paths
            vs_paths = [
                r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools",
                r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools"
            ]
            
            for path in vs_paths:
                vc_tools_path = os.path.join(path, "VC", "Tools", "MSVC")
                if os.path.exists(vc_tools_path) and os.listdir(vc_tools_path):
                    logging.info(f"Found Build Tools at {vc_tools_path}")
                    return True
            
            # Method 2: Check registry
            try:
                # Check if Visual C++ Build Tools registry key exists
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, 
                                r"SOFTWARE\Microsoft\VisualStudio\SxS\VS7", 
                                0, winreg.KEY_READ) as key:
                    # Just checking if the key exists and has values
                    if winreg.QueryValueEx(key, "17.0")[0]:
                        logging.info("Found Build Tools in registry")
                        return True
            except FileNotFoundError:
                # Registry key not found
                pass
            except Exception as reg_error:
                logging.warning(f"Error checking registry for Build Tools: {str(reg_error)}")
                
            # Method 3: Try to use cl.exe (MSVC compiler)
            try:
                subprocess.run(["cl"], 
                            stdout=subprocess.PIPE, 
                            stderr=subprocess.PIPE, 
                            shell=True)
                logging.info("Found cl.exe in PATH, Build Tools appear to be installed")
                return True
            except:
                # cl.exe not found in PATH
                pass
                
            return False
        except Exception as e:
            logging.error(f"Error checking Build Tools installation: {str(e)}")
            return False

    def open_build_tools_installer(self):
        """Check for and install Visual Studio Build Tools."""
        # Check for admin privileges
        if not self.is_admin():
            message = ("Administrator privileges required!\n\n"
                    "The Pandrator Installer must be run as administrator to install Visual C++ Build Tools.\n\n"
                    "Please right-click on the Pandrator Installer and select 'Run as administrator'.")
            messagebox.showerror("Admin Rights Required", message)
            return
        
        # First check if Build Tools are already installed
        if self.is_build_tools_installed():
            # Build Tools are already installed, ask if user wants to force reinstall
            result = messagebox.askyesno(
                "Build Tools Already Installed", 
                "Visual C++ Build Tools appear to be already installed on your system.\n\n"
                "Do you want to force a reinstallation anyway?\n\n"
                "• Select 'Yes' to reinstall\n"
                "• Select 'No' to continue with Pandrator installation",
                icon="info"
            )
            
            if not result:
                # User chose not to reinstall, update status and return
                self.update_status("Using existing Visual C++ Build Tools installation.")
                return
            
            # User chose to reinstall, continue with installation
            logging.info("User chose to reinstall Build Tools despite existing installation")
            
        self.update_status("Preparing Visual C++ Build Tools installation...")
        logging.info("Starting Visual C++ Build Tools installation process...")
        
        try:
            # URL for VS Build Tools 2022
            url = "https://aka.ms/vs/17/release/vs_buildtools.exe"
            
            # Download the installer
            self.update_status("Downloading VS Build Tools installer...")
            
            response = requests.get(url, stream=True)
            if response.status_code != 200:
                raise Exception(f"Failed to download installer, status code: {response.status_code}")
                
            # Save the installer to a temporary file
            installer_path = os.path.join(tempfile.gettempdir(), "vs_buildtools.exe")
            with open(installer_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            
            # Run the installer silently with required components
            self.update_status("Installing VS Build Tools (running silently)...")
            
            # Command arguments with quiet mode
            install_cmd = [
                installer_path,
                "--quiet", "--norestart",
                "--add", "Microsoft.VisualStudio.Workload.VCTools",
                "--includeRecommended"
            ]
            
            # Launch the installer
            process = subprocess.Popen(install_cmd)
            
            # Start a background thread to monitor installation
            threading.Thread(target=self.monitor_build_tools_installation, 
                            args=(process,), daemon=True).start()
            
            message = ("Visual Studio Build Tools installation has started and is running silently in the background.\n\n"
                    "⏱️ This may take 5-15 minutes to complete.\n\n"
                    "The Pandrator Installer will notify you when the installation is complete or if it fails.\n\n"
                    "AFTER INSTALLATION:\n"
                    "1. Close all command prompt/PowerShell windows\n"
                    "2. Restart the Pandrator Installer")
            
            messagebox.showinfo("Build Tools Installation", message)
            
        except Exception as e:
            logging.error(f"Failed to run VS Build Tools installer: {str(e)}")
            logging.error(traceback.format_exc())
            
            message = ("Failed to download or start the Visual Studio Build Tools installer.\n\n"
                    "Please install it manually:\n"
                    "1. Download from: https://visualstudio.microsoft.com/downloads/#build-tools-for-visual-studio-2022\n"
                    "2. Run the installer and select 'Desktop development with C++'\n"
                    "3. Complete the installation\n"
                    "4. Close all terminal windows and restart Pandrator Installer")
            
            messagebox.showerror("Build Tools Installation Error", message)

    def monitor_build_tools_installation(self, process):
        """Monitor the Build Tools installation process in a background thread."""
        try:
            # Wait for the initial process to complete
            return_code = process.wait()
            
            if return_code == 0:
                # Initial process succeeded, now check for actual installation
                # Wait for installation to complete (can take several minutes)
                for attempt in range(30):  # Check for 5 minutes (30 x 10 seconds)
                    if self.is_build_tools_installed():
                        # Schedule UI updates on the main thread
                        self.after(0, lambda: self.update_status("VS Build Tools installation completed successfully!"))
                        self.after(0, lambda: messagebox.showinfo("Installation Complete", 
                            "Visual Studio Build Tools installation completed successfully!\n\n"
                            "Please:\n"
                            "1. Close all command prompt/PowerShell windows\n"
                            "2. Restart the Pandrator Installer"))
                        return
                    time.sleep(10)  # Wait 10 seconds between checks
                
                # If we get here, we couldn't confirm the installation
                self.after(0, lambda: self.update_status("Could not confirm VS Build Tools installation"))
                self.after(0, lambda: messagebox.showwarning("Installation Status Unknown", 
                    "The Visual Studio Build Tools installer completed, but we couldn't confirm if all components were installed correctly.\n\n"
                    "Please restart your computer and then run the Pandrator Installer again."))
            else:
                # Initial process failed
                self.after(0, lambda: self.update_status(f"VS Build Tools installation failed with code {return_code}"))
                self.after(0, lambda: messagebox.showerror("Installation Failed", 
                    f"Visual Studio Build Tools installation failed with code {return_code}.\n\n"
                    "Please try installing manually:\n"
                    "1. Download from: https://visualstudio.microsoft.com/downloads/#build-tools-for-visual-studio-2022\n"
                    "2. Run the installer and select 'Desktop development with C++'\n"
                    "3. Complete the installation\n"
                    "4. Close all terminal windows and restart Pandrator Installer"))
        except Exception as e:
            logging.error(f"Error monitoring Build Tools installation: {str(e)}")
            self.after(0, lambda: self.update_status("Error monitoring VS Build Tools installation"))

    def install_pytorch_for_xtts_finetuning(self, conda_path, env_name):
        logging.info(f"Installing PyTorch for XTTS Fine-tuning in {env_name}...")
        env_path = os.path.join(conda_path, 'envs', env_name)
        try:
            self.run_command([
                os.path.join(conda_path, 'Scripts', 'conda.exe'),
                'run', '-p', env_path, 'python', '-m',
                'pip', 'install', 'torch==2.2.0+cu118', 'torchaudio==2.2.0+cu118',
                '--index-url', 'https://download.pytorch.org/whl/cu118'
            ])
            logging.info("PyTorch for XTTS Fine-tuning installed successfully.")
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to install PyTorch for XTTS Fine-tuning in {env_name}")
            logging.error(f"Error message: {str(e)}")
            raise

    def disable_buttons(self):
        for widget in self.installation_frame.winfo_children():
            if isinstance(widget, (ctk.CTkCheckBox, ctk.CTkButton)):
                widget.configure(state="disabled")
        self.launch_button.configure(state="disabled")

    def enable_buttons(self):
        self.refresh_ui_state()

    def refresh_ui_state(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        config_path = os.path.join(pandrator_path, 'config.json')
        
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config = json.load(f)
        else:
            config = {}

        # Helper function
        def set_widget_state(widget, state, value=None):
            widget.configure(state=state)
            if isinstance(widget, ctk.CTkCheckBox) and value is not None:
                if value:
                    widget.select()
                else:
                    widget.deselect()

        # Pandrator
        pandrator_installed = os.path.exists(pandrator_path)
        set_widget_state(self.pandrator_checkbox, "disabled" if pandrator_installed else "normal", False)
        set_widget_state(self.launch_frame.winfo_children()[1], "normal" if pandrator_installed else "disabled", pandrator_installed)

        # XTTS
        xtts_support = config.get('xtts_support', False)
        xtts_cuda_support = config.get('cuda_support', False)
        
        # RVC
        rvc_support = config.get('rvc_support', False)
        set_widget_state(self.rvc_checkbox, "disabled" if rvc_support else "normal", False)
        
        # Disable both XTTS and XTTS CPU checkboxes if XTTS is installed in any form
        set_widget_state(self.xtts_checkbox, "disabled" if xtts_support else "normal", False)
        set_widget_state(self.xtts_cpu_checkbox, "disabled" if xtts_support else "normal", False)
        
        xtts_launch_checkbox = next(widget for widget in self.launch_frame.winfo_children() if isinstance(widget, ctk.CTkCheckBox) and widget.cget("text") == "XTTS")
        set_widget_state(xtts_launch_checkbox, "normal" if xtts_support else "disabled", False)
        
        cpu_checkbox = next(widget for widget in self.launch_frame.winfo_children() if isinstance(widget, ctk.CTkCheckBox) and widget.cget("text") == "Use CPU")
        lowvram_checkbox = next(widget for widget in self.launch_frame.winfo_children() if isinstance(widget, ctk.CTkCheckBox) and widget.cget("text") == "Low VRAM")
        deepspeed_checkbox = next(widget for widget in self.launch_frame.winfo_children() if isinstance(widget, ctk.CTkCheckBox) and widget.cget("text") == "DeepSpeed")
        
        if xtts_support:
            if xtts_cuda_support:
                set_widget_state(cpu_checkbox, "normal", False)
                set_widget_state(lowvram_checkbox, "normal", False)
                set_widget_state(deepspeed_checkbox, "normal", True)
            else:
                set_widget_state(cpu_checkbox, "normal", True)
                set_widget_state(lowvram_checkbox, "disabled", False)
                set_widget_state(deepspeed_checkbox, "disabled", False)
        else:
            set_widget_state(cpu_checkbox, "disabled", False)
            set_widget_state(lowvram_checkbox, "disabled", False)
            set_widget_state(deepspeed_checkbox, "disabled", False)

        # Silero
        silero_support = config.get('silero_support', False)
        set_widget_state(self.silero_checkbox, "disabled" if silero_support else "normal", False)
        silero_launch_checkbox = next(widget for widget in self.launch_frame.winfo_children() if isinstance(widget, ctk.CTkCheckBox) and widget.cget("text") == "Silero")
        set_widget_state(silero_launch_checkbox, "normal" if silero_support else "disabled", False)

        # RVC
        rvc_support = config.get('rvc_support', False)
        set_widget_state(self.rvc_checkbox, "disabled" if rvc_support else "normal", False)

        # XTTS Fine-tuning
        xtts_finetuning_support = config.get('xtts_finetuning_support', False)
        set_widget_state(self.xtts_finetuning_checkbox, "disabled" if xtts_finetuning_support else "normal", False)

        # WhisperX
        whisperx_support = config.get('whisperx_support', False)
        if whisperx_support:
            set_widget_state(self.whisperx_checkbox, "disabled", False)
        elif xtts_finetuning_support:
            # XTTS Fine-tuning is installed
            set_widget_state(self.whisperx_checkbox, "disabled", False)
        elif self.xtts_finetuning_var.get():
            # XTTS Fine-tuning is not installed but selected
            set_widget_state(self.whisperx_checkbox, "disabled", True)
        else:
            set_widget_state(self.whisperx_checkbox, "normal", False)

        # Update launch and install buttons state
        self.launch_button.configure(state="normal" if pandrator_installed else "disabled")
        self.install_button.configure(state="normal")
        self.update_button.configure(state="normal" if pandrator_installed else "disabled")

    def get_installed_components(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        config_path = os.path.join(pandrator_path, 'config.json')
        
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config = json.load(f)
        else:
            config = {}
        
        return {
            'xtts': config.get('xtts_support', False),
            'silero': config.get('silero_support', False),
            'rvc': config.get('rvc_support', False),
            'whisperx': config.get('whisperx_support', False),
            'xtts_finetuning': config.get('xtts_finetuning_support', False)
        }

    def install_whisperx(self, conda_path, env_name):
        logging.info(f"Installing WhisperX in {env_name}...")
        env_path = os.path.join(conda_path, 'envs', env_name)
        try:
            ## Install Git through Conda
            #self.run_command([
            #    os.path.join(conda_path, 'Scripts', 'conda.exe'),
            #    'install', '-p', env_path,
            #    'git', '-c', 'conda-forge', '-y'
            #])
            
            # Install PyTorch
            self.run_command([
                os.path.join(conda_path, 'Scripts', 'conda.exe'),
                'run', '-p', env_path,
                'python', '-m', 'pip', 'install',
                'torch==2.5.1', 'torchvision==0.20.1', 'torchaudio==2.5.1',
                '--index-url', 'https://download.pytorch.org/whl/cu118'
            ])
            
            # Install cuDNN
            self.run_command([
                os.path.join(conda_path, 'Scripts', 'conda.exe'),
                'install', '-p', env_path,
                'cudnn=8.9.7.29', '-c', 'conda-forge', '-y'
            ])
            
            # Install ffmpeg
            self.run_command([
                os.path.join(conda_path, 'Scripts', 'conda.exe'),
                'install', '-p', env_path,
                'ffmpeg', '-c', 'conda-forge', '-y'
            ])
            
            # Install CTranslate2
            self.run_command([
               os.path.join(conda_path, 'Scripts', 'conda.exe'),
               'run', '-p', env_path, 'python', '-m', 
               'pip', 'install',
               'pip<24'
            ])

            # Install WhisperX
            self.run_command([
                os.path.join(conda_path, 'Scripts', 'conda.exe'),
                'run', '-p', env_path, 'python', '-m',
                'pip', 'install', 'whisperx'
            ])
            
            # Install CTranslate2
            self.run_command([
               os.path.join(conda_path, 'Scripts', 'conda.exe'),
               'run', '-p', env_path, 'python', '-m', 
               'pip', 'install',
               'ctranslate2==4.4.0'
            ])
            
            logging.info("WhisperX installation completed successfully.")
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to install WhisperX in {env_name}")
            logging.error(f"Error message: {str(e)}")
            raise

    def remove_directory(self, path):
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                shutil.rmtree(path)
                return True
            except PermissionError:
                time.sleep(1)  # Wait for a second before retrying
        return False
    
    def install_pandrator(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        pandrator_already_installed = os.path.exists(pandrator_path)
        
        installed_components = self.get_installed_components()
        
        new_components_selected = (
            (self.xtts_var.get() or self.xtts_cpu_var.get()) and not installed_components['xtts'] or
            self.silero_var.get() and not installed_components['silero'] or
            self.rvc_var.get() and not installed_components['rvc'] or
            self.whisperx_var.get() and not installed_components['whisperx']
        )
        
        if pandrator_already_installed and not self.pandrator_var.get():
            if not new_components_selected:
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
                    cwd=cwd,
                    encoding='utf-8',  # Add explicit encoding
                    errors='replace'   # Replace invalid characters instead of failing
                )
            else:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    encoding='utf-8',  # Add explicit encoding
                    errors='replace'   # Replace invalid characters instead of failing
                )
            
            stdout, stderr = process.communicate()
            
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
            
            logging.info(f"Command executed: {command if isinstance(command, str) else ' '.join(command)}")
            logging.debug(f"STDOUT: {stdout}")
            logging.debug(f"STDERR: {stderr}")
            
            return stdout, stderr
        except subprocess.CalledProcessError as e:
            logging.error(f"Error executing command: {command if isinstance(command, str) else ' '.join(command)}")
            logging.error(f"Error message: {str(e)}")
            logging.error(f"STDOUT: {e.stdout}")
            logging.error(f"STDERR: {e.stderr}")
            raise

    def check_program_installed(self, program):
        try:
            self.run_command(['where', program])
            return True
        except subprocess.CalledProcessError:
            return False

    def install_chocolatey(self):
        logging.info("Installing Chocolatey...")
        try:
            powershell_command = """
            Set-ExecutionPolicy Bypass -Scope Process -Force; 
            [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; 
            iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
            """
            
            # Run the installation command
            process = subprocess.Popen(
                ["powershell", "-Command", powershell_command],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            
            # Capture and log the output
            for line in process.stdout:
                logging.info(line.strip())
            
            process.wait()
            
            if process.returncode == 0:
                logging.info("Chocolatey installed successfully.")
                
                # Enable global confirmation
                subprocess.run(["powershell", "-Command", "choco feature enable -n=allowGlobalConfirmation"], 
                               check=True, capture_output=True, text=True)
                logging.info("Global confirmation enabled for Chocolatey.")
                
                # Refresh environment variables
                self.refresh_environment_variables()
                return True
            else:
                error_output = process.stderr.read()
                logging.error(f"Failed to install Chocolatey: {error_output}")
                return False
        except Exception as e:
            logging.error(f"An error occurred during Chocolatey installation: {str(e)}")
            logging.error(traceback.format_exc())
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

    def install_dependencies(self):
        return self.install_calibre()
            
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

    def install_with_chocolatey(self, package_name, args=""):
        logging.info(f"Attempting to install {package_name} with Chocolatey...")
        
        # First, try using 'choco' command
        try:
            process = subprocess.Popen(
                f"choco install {package_name} -y {args}",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=True,
                text=True
            )
            
            stdout, stderr = process.communicate()
            logging.info(stdout)
            
            if process.returncode == 0:
                logging.info(f"{package_name} installed successfully using 'choco' command.")
                return True
        except Exception as e:
            logging.error(f"Error using 'choco' command: {str(e)}")
        
        # If 'choco' command fails, try using the Chocolatey executable directly
        try:
            choco_exe = os.path.join(os.environ.get('ProgramData', ''), 'chocolatey', 'bin', 'choco.exe')
            if os.path.exists(choco_exe):
                process = subprocess.Popen(
                    f'"{choco_exe}" install {package_name} -y {args}',
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=True,
                    text=True
                )
                
                stdout, stderr = process.communicate()
                logging.info(stdout)
                
                if process.returncode == 0:
                    logging.info(f"{package_name} installed successfully using Chocolatey executable.")
                    return True
            else:
                logging.error("Chocolatey executable not found.")
        except Exception as e:
            logging.error(f"Error using Chocolatey executable: {str(e)}")
        
        logging.error(f"Failed to install {package_name} using Chocolatey.")
        return False

    def install_calibre(self):
        logging.info("Checking installation for Calibre")
        if not self.check_program_installed('calibre'):
            logging.info("Installing Calibre...")
            if self.install_with_chocolatey('calibre'):
                self.refresh_env_in_new_session()
                if self.check_program_installed('calibre'):
                    logging.info("Calibre installed successfully.")
                    return True
                else:
                    logging.warning("Calibre installation not detected after installation attempt.")
                    return False
            else:
                return False
        else:
            logging.info("Calibre is already installed.")
            return True

    def install_conda(self, install_path):
        logging.info("Installing Miniconda...")
        conda_installer = 'Miniconda3-latest-Windows-x86_64.exe'
        url = f'https://repo.anaconda.com/miniconda/{conda_installer}'
        
        # Download the file
        response = requests.get(url)
        with open(conda_installer, 'wb') as f:
            f.write(response.content)
        
        self.run_command([
            conda_installer,
            '/InstallationType=JustMe',
            '/AddToPath=0',
            '/RegisterPython=0',
            '/NoRegistry=1',
            '/S',
            f'/D={install_path}'
        ])
        os.remove(conda_installer)

    def check_conda(self, conda_path):
        return os.path.exists(os.path.join(conda_path, 'Scripts', 'conda.exe'))

    def create_conda_env(self, conda_path, env_name, python_version, additional_packages=None):
        logging.info(f"Creating conda environment {env_name}...")
        env_path = os.path.join(conda_path, 'envs', env_name)
        try:
            # Create the environment with Python
            create_command = [
                os.path.join(conda_path, 'Scripts', 'conda.exe'),
                'create',
                '-p', env_path,
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
                    '-p', env_path,
                    'ffmpeg',
                    '-c',
                    'conda-forge',
                    '-y'
                ]
                self.run_command(ffmpeg_command)

            # Install additional packages if specified
            if additional_packages:
                logging.info(f"Installing additional packages: {', '.join(additional_packages)}")
                install_command = [
                    os.path.join(conda_path, 'Scripts', 'conda.exe'),
                    'install',
                    '-p', env_path,
                    '-y'
                ] + additional_packages
                self.run_command(install_command)

        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to create or setup conda environment {env_name}")
            logging.error(f"Error output: {e.stderr.decode('utf-8')}")
            raise

    def install_requirements(self, conda_path, env_name, requirements_file):
        logging.info(f"Installing requirements for {env_name}...")
        env_path = os.path.join(conda_path, 'envs', env_name)
        
        # Log requirements file contents
        with open(requirements_file, 'r') as f:
            reqs = f.read()
            logging.info(f"Requirements file contents:\n{reqs}")
        
        self.run_command([os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-p', env_path, 'python', '-m', 'pip', 'install', '-r', requirements_file])
        
        # Only check for dulwich in pandrator_installer environment
        if env_name == 'pandrator_installer':
            logging.info("Checking if dulwich is installed in pandrator_installer environment...")
            try:
                self.run_command([
                    os.path.join(conda_path, 'Scripts', 'conda.exe'),
                    'run', '-p', env_path,
                    'python', '-c', 'import dulwich; print(f"Dulwich version {dulwich.__version__} is installed")'
                ])
                logging.info("Dulwich check completed successfully")
            except subprocess.CalledProcessError:
                logging.warning("Dulwich not found in pandrator_installer environment, installing separately...")
                try:
                    self.run_command([
                        os.path.join(conda_path, 'Scripts', 'conda.exe'),
                        'run', '-p', env_path, 'python', '-m',
                        'pip', 'install', 'dulwich'
                    ])
                    logging.info("Dulwich installed successfully in pandrator_installer environment")
                except subprocess.CalledProcessError as e:
                    logging.error(f"Failed to install dulwich in pandrator_installer environment: {str(e)}")
                    raise

    def install_package(self, conda_path, env_name, package):
        logging.info(f"Installing {package} in {env_name}...")
        env_path = os.path.join(conda_path, 'envs', env_name)
        self.run_command([os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-p', env_path, 'pip', 'install', package])

    def install_pytorch_and_xtts_api_server(self, conda_path, env_name):
        logging.info(f"Installing xtts-api-server, PyTorch, and FFmpeg in {env_name}...")
        env_path = os.path.join(conda_path, 'envs', env_name)
        
        try:
            # Install xtts-api-server package
            xtts_cmd = [os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-p', env_path, 'python', '-m', 'pip', 'install', 'xtts-api-server']
            self.run_command(xtts_cmd)
            
            # Install PyTorch
            if self.xtts_cpu_var.get():
                pytorch_cmd = [os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-p', env_path, 'python', '-m', 'pip', 'install', 'torch==2.1.1', 'torchaudio==2.1.1']
            else:
                pytorch_cmd = [os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-p', env_path, 'python', '-m', 'pip', 'install', 'torch==2.1.1+cu118', 'torchaudio==2.1.1+cu118', '--extra-index-url', 'https://download.pytorch.org/whl/cu118']
            self.run_command(pytorch_cmd)
            
            # Install FFmpeg
            ffmpeg_cmd = [os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-p', env_path, 'conda', 'install', '-c', 'conda-forge', 'ffmpeg', '-y']
            self.run_command(ffmpeg_cmd)
            
            logging.info("xtts-api-server, PyTorch, and FFmpeg installed successfully.")
        except subprocess.CalledProcessError as e:
            logging.error("Error installing xtts-api-server, PyTorch, or FFmpeg.")
            logging.error(f"Error output: {e.stderr.decode('utf-8')}")
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
        env_path = os.path.join(conda_path, 'envs', env_name)
        try:
            # Check current NumPy version
            numpy_version = subprocess.check_output([os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-p', env_path, 'python', '-c', 'import numpy; print(numpy.__version__)'], universal_newlines=True).strip()
            logging.info(f"Current NumPy version: {numpy_version}")
            
            # If NumPy version is 2.x, downgrade to 1.24.3
            if numpy_version.startswith('2.'):
                logging.info("Downgrading NumPy to version 1.24.3...")
                self.run_command([os.path.join(conda_path, 'Scripts', 'conda.exe'), 'run', '-p', env_path, 'python', '-m', 'pip', 'install', 'numpy==1.24.3'])
                logging.info("NumPy downgraded successfully.")
            else:
                logging.info("NumPy version is compatible. No changes needed.")
        except subprocess.CalledProcessError as e:
            logging.error("Error checking or updating NumPy version.")
            logging.error(f"Error message: {str(e)}")
            raise
        
    def clone_repo(self, repo_url, target_dir):
        logging.info(f"Cloning repository {repo_url} to {target_dir}...")
        try:
            porcelain.clone(repo_url, target_dir)
            logging.info("Repository cloned successfully.")
            logging.info("Pulling latest changes...")
            self.pull_repo(target_dir)  # Add pull after clone
        except Exception as e:
            logging.error(f"Failed to clone repository: {str(e)}")
            raise

    def pull_repo(self, repo_path):
        logging.info(f"Pulling updates for repository at {repo_path}...")
        try:
            repo = porcelain.open_repo(repo_path)
            porcelain.pull(repo)
            logging.info("Repository updated successfully.")
        except Exception as e:
            logging.error(f"Failed to update repository: {str(e)}")
            raise

    def install_pycroppdf_requirements(self, conda_path, env_name, pycroppdf_repo_path):
        logging.info(f"Installing PyCropPDF requirements in {env_name}...")
        try:
            requirements_file = os.path.join(pycroppdf_repo_path, 'requirements.txt')
            self.install_requirements(conda_path, env_name, requirements_file)
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to install PyCropPDF requirements in {env_name}")
            logging.error(f"Error message: {str(e)}")
            raise

    def install_rvc_python(self, conda_path, env_name):
        logging.info("Starting RVC Python installation")
        env_path = os.path.join(conda_path, 'envs', env_name)
        try:
            # Install specific pip version
            logging.info("Installing specific pip version...")
            self.run_command([
                os.path.join(conda_path, 'Scripts', 'conda.exe'),
                'run', '-p', env_path,
                'python', '-m', 'pip', 'install', 'pip==24'
            ])

            # Install RVC Python
            logging.info("Installing RVC Python...")
            self.run_command([
                os.path.join(conda_path, 'Scripts', 'conda.exe'),
                'run', '-p', env_path, 'python', '-m',
                'pip', 'install', 'rvc-python'
            ])

            # Install specific PyTorch version
            logging.info("Installing specific PyTorch version...")
            self.run_command([
                os.path.join(conda_path, 'Scripts', 'conda.exe'),
                'run', '-p', env_path, 'python', '-m',
                'pip', 'install', 'torch==2.1.1+cu121', 'torchaudio==2.1.1+cu121',
                '--index-url', 'https://download.pytorch.org/whl/cu121'
            ])

            logging.info("RVC Python installation completed successfully.")

        except Exception as e:
            error_msg = f"An error occurred during RVC Python installation: {str(e)}"
            logging.error(error_msg)
            logging.error(traceback.format_exc())
            raise

    def set_permissive_permissions(self, path):
        """Set permissive file permissions on installation directories"""
        if not self.is_admin():
            logging.info(f"Skipping permission setting on {path} (not running as admin)")
            return False
            
        try:
            self.update_status(f"Setting permissions on {os.path.basename(path)}...")
            logging.info(f"Setting permissive permissions on: {path}")
            
            # Use icacls to give Users full control (F) with inheritance flags (OI)(CI)
            # OI = Object Inherit, CI = Container Inherit, F = Full Control
            command = f'icacls "{path}" /grant:r Users:(OI)(CI)F /T /Q'
            subprocess.run(command, shell=True, check=True)
            
            logging.info(f"Successfully set permissions on: {path}")
            return True
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to set permissions on {path}: {str(e)}")
            logging.error(traceback.format_exc())
            return False

    def install_process(self):
        self.disable_buttons()
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        conda_path = os.path.join(pandrator_path, 'conda')
        pandrator_already_installed = os.path.exists(pandrator_path)
        
        # Check admin status
        is_admin = self.is_admin()
        if not is_admin:
            logging.warning("Running installer without admin privileges - some features may not work correctly")
            user_choice = messagebox.askyesno(
                "Non-Admin Installation", 
                "You're running the installer without administrator privileges.\n\n"
                "This will work for basic functionality but may cause issues with:\n"
                "- Visual Studio Build Tools installation\n"
                "- System-wide permissions\n\n"
                "Would you like to continue with limited installation?\n"
                "(Select 'No' to exit and restart as administrator)"
            )
            if not user_choice:
                self.update_status("Installation cancelled - please restart as administrator")
                self.enable_buttons()
                return
        
        try:
            # Create Pandrator directory if it doesn't exist
            if not pandrator_already_installed:
                os.makedirs(pandrator_path, exist_ok=True)
                if is_admin:
                    self.set_permissive_permissions(pandrator_path)
            
            self.update_progress(0.1)
            self.update_status("Installing Chocolatey...")
            if is_admin:
                self.install_chocolatey()
            else:
                self.update_status("Skipping Chocolatey installation (requires admin)")
                logging.warning("Skipping Chocolatey installation (requires admin)")

            self.update_progress(0.2)
            self.update_status("Installing dependencies...")
            try:
                if is_admin:
                    self.install_dependencies()
                else:
                    self.update_status("Checking for Calibre...")
                    if not self.check_program_installed('calibre'):
                        self.show_calibre_installation_message()
                    else:
                        logging.info("Calibre is already installed.")
            except Exception as e:
                logging.error(f"Error during dependency installation: {str(e)}")
                if is_admin:
                    self.show_calibre_installation_message()
            
            self.update_progress(0.4)
            self.update_status("Cloning repositories...")
            
            if self.pandrator_var.get() or not pandrator_already_installed:
                self.clone_repo('https://github.com/lukaszliniewicz/Pandrator.git', os.path.join(pandrator_path, 'Pandrator'))
                self.clone_repo('https://github.com/lukaszliniewicz/Subdub.git', os.path.join(pandrator_path, 'Subdub'))

            if self.xtts_var.get() or self.xtts_cpu_var.get():
                self.clone_repo('https://github.com/daswer123/xtts-api-server.git', os.path.join(pandrator_path, 'xtts-api-server'))

            self.update_progress(0.5)
            self.update_status("Installing Miniconda...")
            if not self.check_conda(conda_path):
                self.install_conda(conda_path)
                if is_admin:
                    self.set_permissive_permissions(conda_path)

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
                self.update_status("Installing Pandrator, Subdub, and PyCropPDF requirements...")
                pandrator_repo_path = os.path.join(pandrator_path, 'Pandrator')
                self.install_requirements(conda_path, 'pandrator_installer', os.path.join(pandrator_repo_path, 'requirements.txt'))
                
                # Clone and install PyCropPDF
                pycroppdf_repo_path = os.path.join(pandrator_repo_path, 'PyCropPDF')
                self.clone_repo('https://github.com/lukaszliniewicz/PyCropPDF.git', pycroppdf_repo_path)
                self.install_pycroppdf_requirements(conda_path, 'pandrator_installer', pycroppdf_repo_path)
                
                subdub_repo_path = os.path.join(pandrator_path, 'Subdub')
                self.install_subdub_requirements(conda_path, 'pandrator_installer', subdub_repo_path)

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

            if self.rvc_var.get():
                self.update_progress(0.8)
                self.update_status("Installing RVC Python...")
                self.install_rvc_python(conda_path, 'pandrator_installer')

            if self.whisperx_var.get():
                self.update_progress(0.85)
                self.update_status("Creating WhisperX Conda environment...")
                self.create_conda_env(conda_path, 'whisperx_installer', '3.10')
                self.update_progress(0.90)
                self.update_status("Installing WhisperX...")
                self.install_whisperx(conda_path, 'whisperx_installer')

            if self.xtts_finetuning_var.get():
                self.update_progress(0.85)
                self.update_status("Cloning XTTS Fine-tuning repository...")
                self.clone_repo('https://github.com/lukaszliniewicz/easy_xtts_trainer.git', os.path.join(pandrator_path, 'easy_xtts_trainer'))

                self.update_progress(0.90)
                self.update_status("Creating XTTS Fine-tuning Conda environment...")
                self.create_conda_env(conda_path, 'easy_xtts_trainer', '3.10')

                self.update_progress(0.95)
                self.update_status("Installing XTTS Fine-tuning requirements...")
                easy_xtts_trainer_path = os.path.join(pandrator_path, 'easy_xtts_trainer')
                self.install_requirements(conda_path, 'easy_xtts_trainer', os.path.join(easy_xtts_trainer_path, 'requirements.txt'))

                self.update_status("Installing PyTorch for XTTS Fine-tuning...")
                self.install_pytorch_for_xtts_finetuning(conda_path, 'easy_xtts_trainer')

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
            config['whisperx_support'] = config.get('whisperx_support', False) or self.whisperx_var.get()
            config['xtts_finetuning_support'] = config.get('xtts_finetuning_support', False) or self.xtts_finetuning_var.get()
            config['rvc_support'] = config.get('rvc_support', False) or self.rvc_var.get()

            with open(config_path, 'w') as f:
                json.dump(config, f)

            # Set final permissions if admin
            if is_admin:
                self.update_progress(0.98)
                self.update_status("Finalizing permissions...")
                self.set_permissive_permissions(pandrator_path)

            self.update_progress(1.0)
            self.update_status("Installation complete!")
            logging.info("Installation completed successfully.")

        except Exception as e:
            logging.error(f"Installation failed: {str(e)}")
            logging.error(traceback.format_exc())
            self.update_status("Installation failed. Check the log for details.")
        finally:
            self.refresh_ui_state()

    def update_pandrator(self):
        pandrator_base_path = os.path.join(self.initial_working_dir, 'Pandrator')
        pandrator_repo_path = os.path.join(pandrator_base_path, 'Pandrator')
        subdub_repo_path = os.path.join(pandrator_base_path, 'Subdub')
        easy_xtts_trainer_path = os.path.join(pandrator_base_path, 'easy_xtts_trainer')
        conda_path = os.path.join(pandrator_base_path, 'conda')
        
        # Check admin status - we'll proceed either way but handle differently
        is_admin = self.is_admin()
        if not is_admin:
            logging.info("Running update without admin privileges - file permission changes won't be applied")
        
        logging.info(f"Checking for Pandrator at: {pandrator_repo_path}")
        
        if not os.path.exists(pandrator_repo_path):
            error_msg = f"Pandrator directory not found at: {pandrator_repo_path}"
            logging.error(error_msg)
            self.update_status(error_msg)
            return

        self.disable_buttons()
        self.initialize_logging()
        
        self.update_status("Updating Pandrator and components...")
        logging.info("Starting update process")
        
        try:
            # Update Pandrator
            self.update_status("Updating Pandrator repository...")
            logging.info(f"Updating Pandrator in: {pandrator_repo_path}")
            self.pull_repo(pandrator_repo_path)
            
            # Update Pandrator requirements
            self.update_status("Updating Pandrator dependencies...")
            requirements_file = os.path.join(pandrator_repo_path, 'requirements.txt')
            logging.info(f"Updating requirements from: {requirements_file}")
            
            if not os.path.exists(requirements_file):
                logging.error(f"Requirements file not found at: {requirements_file}")
                raise FileNotFoundError(f"Requirements file not found: {requirements_file}")
            
            update_cmd = [
                os.path.join(conda_path, 'Scripts', 'conda.exe'),
                'run',
                '-p', 
                os.path.join(conda_path, 'envs', 'pandrator_installer'),
                '--no-capture-output', 'python', '-m',
                'pip', 'install', '-r', requirements_file
            ]
            logging.info(f"Executing update command: {' '.join(update_cmd)}")
            self.run_command(update_cmd, cwd=pandrator_repo_path)
            
            # Update Subdub
            if os.path.exists(subdub_repo_path):
                self.update_status("Updating Subdub...")
                logging.info(f"Updating Subdub in: {subdub_repo_path}")
                self.pull_repo(subdub_repo_path)
            else:
                logging.warning(f"Subdub directory not found at: {subdub_repo_path}")
            
            # Update easy XTTS trainer (repo and requirements)
            if os.path.exists(easy_xtts_trainer_path):
                self.update_status("Updating easy XTTS trainer...")
                logging.info(f"Updating easy XTTS trainer in: {easy_xtts_trainer_path}")
                self.pull_repo(easy_xtts_trainer_path)
                
                # Update requirements
                self.update_status("Updating easy XTTS trainer dependencies...")
                xtts_requirements_file = os.path.join(easy_xtts_trainer_path, 'requirements.txt')
                if os.path.exists(xtts_requirements_file):
                    logging.info("Installing updated requirements for easy XTTS trainer...")
                    self.install_requirements(conda_path, 'easy_xtts_trainer', xtts_requirements_file)
                else:
                    logging.warning(f"XTTS trainer requirements file not found at: {xtts_requirements_file}")
            else:
                logging.info("easy XTTS trainer not installed, skipping update.")

            # Set permissions if running as admin
            if is_admin:
                self.update_status("Setting permissions after update...")
                self.set_permissive_permissions(pandrator_base_path)
            
            self.update_status("Update completed successfully!")
            logging.info("Update process completed successfully")
        
        except Exception as e:
            error_msg = f"Failed to update: {str(e)}"
            logging.error(error_msg)
            logging.error(traceback.format_exc())
            self.update_status(f"Update failed: {error_msg}")
        
        finally:
            self.enable_buttons()
            self.refresh_ui_state()

    def update_whisperx_checkbox(self):
        xtts_finetuning_support = self.get_installed_components().get('xtts_finetuning', False)
        if self.xtts_finetuning_var.get() and not xtts_finetuning_support:
            self.whisperx_var.set(True)
            self.whisperx_checkbox.configure(state="disabled")
        elif xtts_finetuning_support:
            self.whisperx_var.set(False)
            self.whisperx_checkbox.configure(state="disabled")
        else:
            self.whisperx_checkbox.configure(state="normal")

    def install_subdub_requirements(self, conda_path, env_name, subdub_repo_path):
        logging.info(f"Installing Subdub requirements in {env_name}...")
        try:
            # Clone the Subdub repository if it doesn't exist
            if not os.path.exists(subdub_repo_path):
                self.clone_repo('https://github.com/lukaszliniewicz/Subdub.git', subdub_repo_path)
            
            requirements_file = os.path.join(subdub_repo_path, 'requirements.txt')
            self.install_requirements(conda_path, env_name, requirements_file)
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to install Subdub requirements in {env_name}")
            logging.error(f"Error message: {str(e)}")
            raise

    def launch_apps(self):
        base_path = os.path.abspath(self.initial_working_dir)
        pandrator_path = os.path.join(base_path, 'Pandrator')
        conda_path = os.path.join(pandrator_path, 'conda')

        self.update_progress(0.3)
        self.update_status("Preparing to launch...")
        logging.info(f"Launch process started. Base directory: {base_path}")
        logging.info(f"Pandrator path: {pandrator_path}")
        logging.info(f"Conda path: {conda_path}")

        pandrator_args = []
        tts_engine_launched = False

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
            
            pandrator_args = ['-connect', '-xtts']
            tts_engine_launched = True

        if self.launch_silero_var.get() and not tts_engine_launched:
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
            
            pandrator_args = ['-connect', '-silero']
            tts_engine_launched = True

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
                self.pandrator_process = self.run_script(conda_path, 'pandrator_installer', pandrator_script_path, pandrator_args)
            except Exception as e:
                error_msg = f"Failed to start Pandrator: {str(e)}"
                self.update_status(error_msg)
                logging.error(error_msg)
                logging.exception("Exception details:")
                return

        self.update_progress(1.0)
        self.update_status("Apps are running!")
        self.refresh_ui_state()
        self.after(5000, self.check_processes_status)

    def is_port_in_use(self, port):
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(('localhost', port)) == 0

    def run_script(self, conda_path, env_name, script_path, additional_args=[]):
        logging.info(f"Running script {script_path} in {env_name} with args: {additional_args}")
        
        script_dir = os.path.dirname(script_path)
        env_path = os.path.join(conda_path, 'envs', env_name)
        
        command = [
            os.path.join(conda_path, 'Scripts', 'conda.exe'),
            'run',
            '-p', env_path,
            '--no-capture-output',
            'python',
            script_path
        ] + additional_args
        
        process = subprocess.Popen(command, cwd=script_dir, creationflags=subprocess.DETACHED_PROCESS)
        return process


    def run_xtts_api_server(self, conda_path, env_name, xtts_server_path, use_cpu=False):
        logging.info(f"Attempting to run XTTS API server...")
        logging.info(f"Conda path: {conda_path}")
        logging.info(f"Environment name: {env_name}")
        logging.info(f"XTTS server path: {xtts_server_path}")
        logging.info(f"Use CPU: {use_cpu}")

        if not os.path.exists(xtts_server_path):
            raise FileNotFoundError(f"XTTS server path not found: {xtts_server_path}")

        # Check if port 8020 is already in use
        if self.is_port_in_use(8020):
            error_msg = "XTTS server cannot be started because port 8020 is already in use."
            logging.error(error_msg)
            CTkMessagebox(title="Error", message=error_msg, icon="cancel")
            return None

        xtts_log_file = os.path.join(xtts_server_path, 'xtts_server.log')
        env_path = os.path.join(conda_path, 'envs', env_name)

        xtts_server_command = [
            os.path.join(conda_path, 'Scripts', 'conda.exe'),
            'run',
            '-p', env_path,
            '--no-capture-output',
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

        def log_output(pipe, logfile, prefix):
            with open(logfile, 'a', encoding='utf-8') as f:
                for line in iter(pipe.readline, b''):
                    try:
                        decoded_line = line.decode('utf-8').strip()
                    except UnicodeDecodeError:
                        decoded_line = line.decode('utf-8', errors='replace').strip()
                    log_message = f"{prefix}: {decoded_line}"
                    f.write(log_message + '\n')
                    f.flush()
                    logging.info(log_message)
                    print(log_message, flush=True)

        try:
            process = subprocess.Popen(xtts_server_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=xtts_server_path)
            self.xtts_process = process
            
            stdout_thread = threading.Thread(target=log_output, args=(process.stdout, xtts_log_file, "XTTS stdout"), daemon=True)
            stderr_thread = threading.Thread(target=log_output, args=(process.stderr, xtts_log_file, "XTTS stderr"), daemon=True)
            
            stdout_thread.start()
            stderr_thread.start()

            logging.info(f"XTTS API server process started with PID: {process.pid}")
            return process
        except Exception as e:
            logging.error(f"Failed to start XTTS API server: {str(e)}")
            logging.exception("Exception details:")
            raise

    def check_xtts_server_online(self, url, max_attempts=200, wait_interval=5):
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
        env_path = os.path.join(conda_path, 'envs', env_name)

        # Create log file for silero server output
        silero_log_file = os.path.join(os.getcwd(), 'silero_server.log')

        # Run silero server command with output redirection
        silero_server_command = f'"{os.path.join(conda_path, "Scripts", "conda.exe")}" run -p "{env_path}" python -m silero_api_server > "{silero_log_file}" 2>&1'
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
        
        if not self.pandrator_process and not self.xtts_process and not self.silero_process:
            self.update_status("All processes have exited.")
            self.refresh_ui_state()
        else:
            self.after(5000, self.check_processes_status)  # Schedule next check

    def shutdown_apps(self):
        self.shutdown_xtts()
        self.shutdown_silero()

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

    def destroy(self):
        self.shutdown_apps()
        super().destroy()

if __name__ == "__main__":
    app = PandratorInstaller()
    app.mainloop()
