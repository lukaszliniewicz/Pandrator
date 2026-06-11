"""Component-specific installation and bootstrap operations."""

import logging
import os
import platform
import shutil
import subprocess
import time
import traceback

import psutil
import requests

try:
    from packaging.specifiers import SpecifierSet as PackagingSpecifierSet
except ImportError:
    PackagingSpecifierSet = None

from .constants import (
    BUNDLED_WHEELS_RELATIVE_PATH,
    KOKORO_ENV_NAME,
    KOKORO_GPU_TORCH_INDEX_URL_ARM64,
    KOKORO_GPU_TORCH_INDEX_URL_X86_64,
    KOKORO_GPU_TORCH_VERSION_ARM64,
    KOKORO_GPU_TORCH_VERSION_X86_64,
    KOKORO_TORCH_BASE_VERSION,
    PYOPENJTALK_WHEEL_PREFIX,
    RVC_NUMPY_SPEC,
    RVC_PYTHON_FORK_INSTALL_SPEC,
    RVC_TORCHAUDIO_VERSION,
    RVC_TORCHVISION_VERSION,
    RVC_TORCH_INDEX_URL,
    RVC_TORCH_VERSION,
    SUBDUB_EDITABLE_INSTALL_SPEC,
    SUBDUB_REPO_URL,
    WHISPERX_CTRANSLATE2_VERSION,
    WHISPERX_TORCHAUDIO_VERSION,
    WHISPERX_TORCHVISION_VERSION,
    WHISPERX_TORCH_INDEX_URL,
    WHISPERX_TORCH_VERSION,
    WHISPERX_VERSION,
    XTTS_FINETUNING_BUNDLED_WHEEL_PREFIX,
)


class ComponentOperationsMixin:
    def get_kokoro_torch_install_options(self, use_gpu=False):
        if not use_gpu:
            return f'torch=={KOKORO_TORCH_BASE_VERSION}', None

        machine = platform.machine().lower()
        if machine in {'arm64', 'aarch64'}:
            return f'torch=={KOKORO_GPU_TORCH_VERSION_ARM64}', KOKORO_GPU_TORCH_INDEX_URL_ARM64

        if machine not in {'amd64', 'x86_64'}:
            logging.warning(
                "Unknown architecture '%s' for Kokoro GPU install; defaulting to x86_64 CUDA wheels.",
                machine,
            )

        return f'torch=={KOKORO_GPU_TORCH_VERSION_X86_64}', KOKORO_GPU_TORCH_INDEX_URL_X86_64

    def get_kokoro_required_package_specs(self, use_gpu=False):
        torch_spec, _ = self.get_kokoro_torch_install_options(use_gpu=use_gpu)
        return (torch_spec,)

    def install_pytorch_for_kokoro(self, pandrator_path, env_name, use_gpu=False):
        torch_spec, index_url = self.get_kokoro_torch_install_options(use_gpu=use_gpu)
        command = ['python', '-m', 'pip', 'install', '--upgrade', '--force-reinstall', torch_spec]
        if index_url:
            command.extend(['--index-url', index_url])

        logging.info(
            "Installing PyTorch for Kokoro in %s (%s mode)...",
            env_name,
            'GPU' if use_gpu else 'CPU',
        )
        self.run_pixi_in_env(
            pandrator_path,
            env_name,
            command,
        )

    def get_kokoro_runtime_env(self, pandrator_path, kokoro_repo_path, use_gpu=False):
        env = self.get_pixi_subprocess_env(pandrator_path)
        env['PYTHONUTF8'] = '1'
        env['USE_GPU'] = 'true' if use_gpu else 'false'
        env['USE_ONNX'] = 'false'
        env['MODEL_DIR'] = 'src/models'
        env['VOICES_DIR'] = 'src/voices/v1_0'
        env['WEB_PLAYER_PATH'] = os.path.join(kokoro_repo_path, 'web')
        env['PYTHONPATH'] = f"{kokoro_repo_path};{os.path.join(kokoro_repo_path, 'api')}"

        dll_path, data_path = self.resolve_espeak_paths()
        if dll_path:
            env['PHONEMIZER_ESPEAK_LIBRARY'] = dll_path
        if data_path:
            env['PHONEMIZER_ESPEAK_DATA'] = data_path
            env['ESPEAK_DATA_PATH'] = data_path

        return env

    def is_kokoro_runtime_ready(self, pandrator_path, kokoro_repo_path, use_gpu=False):
        manifest_path = self.get_pixi_manifest_path(pandrator_path, KOKORO_ENV_NAME)
        model_path = os.path.join(
            kokoro_repo_path,
            'api',
            'src',
            'models',
            'v1_0',
            'kokoro-v1_0.pth',
        )
        if not os.path.exists(manifest_path) or not os.path.exists(model_path):
            return False

        kokoro_needs_sync, kokoro_reason = self.component_needs_package_sync(
            pandrator_path,
            KOKORO_ENV_NAME,
            self.get_kokoro_required_package_specs(use_gpu=use_gpu),
        )
        if kokoro_needs_sync:
            logging.info("Kokoro runtime requires package sync because %s", kokoro_reason)
            return False

        return True

    def get_bundled_wheels_directories(self, pandrator_path):
        candidate_directories = [
            os.path.join(pandrator_path, 'Pandrator', BUNDLED_WHEELS_RELATIVE_PATH),
            os.path.join(self.initial_working_dir, BUNDLED_WHEELS_RELATIVE_PATH),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), BUNDLED_WHEELS_RELATIVE_PATH),
        ]

        bundled_directories = []
        seen = set()
        for candidate in candidate_directories:
            normalized_candidate = os.path.normcase(os.path.normpath(candidate))
            if normalized_candidate in seen:
                continue

            seen.add(normalized_candidate)
            if os.path.isdir(candidate):
                bundled_directories.append(candidate)

        return bundled_directories

    def find_bundled_pyopenjtalk_wheel(self, pandrator_path):
        for wheels_directory in self.get_bundled_wheels_directories(pandrator_path):
            try:
                wheel_names = sorted(os.listdir(wheels_directory), reverse=True)
            except OSError:
                continue

            for wheel_name in wheel_names:
                normalized_name = wheel_name.lower()
                if not normalized_name.endswith('.whl'):
                    continue
                if not normalized_name.startswith(PYOPENJTALK_WHEEL_PREFIX):
                    continue

                wheel_path = os.path.join(wheels_directory, wheel_name)
                if os.path.isfile(wheel_path):
                    return wheel_path, wheels_directory

        return '', ''

    def find_bundled_xtts_finetuning_wheel(self, pandrator_path, easy_xtts_trainer_path):
        candidate_directories = [
            os.path.join(easy_xtts_trainer_path, 'vendor'),
            *self.get_bundled_wheels_directories(pandrator_path),
        ]

        seen = set()
        for wheels_directory in candidate_directories:
            normalized_directory = os.path.normcase(os.path.normpath(wheels_directory))
            if normalized_directory in seen:
                continue

            seen.add(normalized_directory)
            if not os.path.isdir(wheels_directory):
                continue

            try:
                wheel_names = sorted(os.listdir(wheels_directory), reverse=True)
            except OSError:
                continue

            for wheel_name in wheel_names:
                normalized_name = wheel_name.lower()
                if not normalized_name.endswith('.whl'):
                    continue
                if not normalized_name.startswith(XTTS_FINETUNING_BUNDLED_WHEEL_PREFIX):
                    continue

                wheel_path = os.path.join(wheels_directory, wheel_name)
                if os.path.isfile(wheel_path):
                    return wheel_path, wheels_directory

        return '', ''

    def install_xtts_finetuning_bundled_wheel(self, pandrator_path, env_name, easy_xtts_trainer_path):
        bundled_wheel_path, bundled_wheel_directory = self.find_bundled_xtts_finetuning_wheel(
            pandrator_path,
            easy_xtts_trainer_path,
        )
        if not bundled_wheel_path:
            logging.warning(
                "Bundled XTTS fine-tuning wheel was not found in %s. "
                "Skipping optional source-text alignment dependency installation.",
                easy_xtts_trainer_path,
            )
            return False

        logging.info(f"Installing bundled XTTS fine-tuning wheel: {bundled_wheel_path}")
        self.run_pixi_in_env(
            pandrator_path,
            env_name,
            ['python', '-m', 'pip', 'install', '--upgrade', bundled_wheel_path],
            cwd=easy_xtts_trainer_path,
        )
        logging.info(
            "Installed bundled XTTS fine-tuning wheel from: %s",
            bundled_wheel_directory,
        )
        return True

    def check_kokoro_server_online(self, url, max_attempts=90, wait_interval=5, process=None):
        """Check if the Kokoro server is online and responding."""
        for attempt in range(1, max_attempts + 1):
            if process is not None and process.poll() is not None:
                logging.error("Kokoro server process exited before coming online.")
                return False

            try:
                response = requests.get(url, timeout=5)
                if response.status_code == 200:
                    logging.info("Kokoro server is online.")
                    return True
            except requests.exceptions.RequestException:
                pass

            logging.info("Kokoro server is not online. Waiting... (Attempt %s/%s)", attempt, max_attempts)
            time.sleep(wait_interval)

        logging.error("Kokoro server failed to come online within the specified attempts.")
        return False

    def install_kokoro_api_server(
        self,
        pandrator_path,
        kokoro_repo_path,
        env_name=KOKORO_ENV_NAME,
        use_gpu=False,
        runtime_use_gpu=None,
    ):
        if runtime_use_gpu is None:
            runtime_use_gpu = use_gpu

        install_extra = 'gpu' if use_gpu else 'cpu'
        logging.info(
            "Bootstrapping Kokoro API server in %s (%s install, %s launch)...",
            kokoro_repo_path,
            install_extra.upper(),
            'GPU' if runtime_use_gpu else 'CPU',
        )
        main_path = os.path.join(kokoro_repo_path, 'api', 'src', 'main.py')
        if not os.path.exists(main_path):
            raise FileNotFoundError(f"Kokoro API entrypoint not found at: {main_path}")

        espeak_ok = self.install_espeak_ng_direct(pandrator_path=pandrator_path)
        if not espeak_ok:
            logging.warning(
                "Automatic eSpeak NG installation was not fully verified. "
                "Proceeding; Kokoro may still work via espeakng-loader."
            )

        self.run_pixi_in_env(
            pandrator_path,
            env_name,
            ['python', '-m', 'pip', 'install', '--upgrade', 'pip'],
            cwd=kokoro_repo_path,
        )

        bundled_pyopenjtalk_wheel, bundled_wheel_directory = self.find_bundled_pyopenjtalk_wheel(pandrator_path)
        if bundled_pyopenjtalk_wheel:
            logging.info(f"Installing bundled pyopenjtalk wheel: {bundled_pyopenjtalk_wheel}")
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                ['python', '-m', 'pip', 'install', '--upgrade', bundled_pyopenjtalk_wheel],
                cwd=kokoro_repo_path,
            )

        _, kokoro_torch_index_url = self.get_kokoro_torch_install_options(use_gpu=use_gpu)
        editable_install_command = ['python', '-m', 'pip', 'install', '--upgrade', '-e', f'.[{install_extra}]']
        wheels_directories = self.get_bundled_wheels_directories(pandrator_path)
        if wheels_directories:
            for wheels_directory in wheels_directories:
                editable_install_command.extend(['--find-links', wheels_directory])
            editable_install_command.append('--prefer-binary')

            if bundled_wheel_directory:
                logging.info(
                    "Using bundled wheel directory for Kokoro dependency resolution: %s",
                    bundled_wheel_directory,
                )

        if kokoro_torch_index_url:
            editable_install_command.extend(['--extra-index-url', kokoro_torch_index_url])

        self.run_pixi_in_env(
            pandrator_path,
            env_name,
            editable_install_command,
            cwd=kokoro_repo_path,
        )
        self.install_pytorch_for_kokoro(
            pandrator_path,
            env_name,
            use_gpu=use_gpu,
        )

        self.run_pixi_in_env(
            pandrator_path,
            env_name,
            # Force UTF-8 so Kokoro's Unicode-rich config.json validates on Windows.
            ['python', '-X', 'utf8', 'docker/scripts/download_model.py', '--output', 'api/src/models/v1_0'],
            cwd=kokoro_repo_path,
        )

        if self.is_port_in_use(8880):
            raise RuntimeError("Kokoro server cannot be bootstrapped because port 8880 is already in use.")

        process = None
        try:
            process = self.run_kokoro_api_server(
                pandrator_path,
                env_name,
                kokoro_repo_path,
                use_gpu=runtime_use_gpu,
            )
            if not self.check_kokoro_server_online(
                'http://127.0.0.1:8880/health',
                max_attempts=180,
                wait_interval=5,
                process=process,
            ):
                return_code = process.poll()
                if return_code is not None:
                    raise RuntimeError(
                        f"Kokoro bootstrap process exited before server was ready (exit code {return_code}). "
                        f"See log: {getattr(process, 'log_file_path', '')}"
                    )
                raise RuntimeError(
                    "Kokoro bootstrap did not bring the server online in time. "
                    f"See log: {getattr(process, 'log_file_path', '')}"
                )
        finally:
            if process is not None:
                logging.info("Stopping temporary Kokoro bootstrap process.")
                self.terminate_process_tree(process)
                if hasattr(process, 'log_handle') and process.log_handle:
                    process.log_handle.close()

    def run_kokoro_api_server(self, pandrator_path, env_name, kokoro_server_path, use_gpu=False):
        """Run the Kokoro API server in a dedicated Pixi environment."""
        logging.info(
            "Running Kokoro API server from %s (%s mode)...",
            kokoro_server_path,
            'GPU' if use_gpu else 'CPU',
        )

        if self.is_port_in_use(8880):
            error_msg = "Kokoro server cannot be started because port 8880 is already in use."
            logging.error(error_msg)
            self.notify_error("Error", error_msg)
            return None

        main_path = os.path.join(kokoro_server_path, 'api', 'src', 'main.py')
        if not os.path.exists(main_path):
            raise FileNotFoundError(f"Kokoro API entrypoint not found at: {main_path}")

        kokoro_log_file = os.path.join(kokoro_server_path, 'kokoro_server.log')
        command = self.build_pixi_run_command(
            pandrator_path,
            env_name,
            [
                'python',
                '-m',
                'uvicorn',
                'api.src.main:app',
                '--host',
                '127.0.0.1',
                '--port',
                '8880',
            ],
        )

        kokoro_env = self.get_kokoro_runtime_env(
            pandrator_path,
            kokoro_server_path,
            use_gpu=use_gpu,
        )
        log_handle = open(kokoro_log_file, 'a', encoding='utf-8')
        try:
            process = subprocess.Popen(
                command,
                cwd=kokoro_server_path,
                env=kokoro_env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **self.get_hidden_subprocess_kwargs(),
            )
        except Exception:
            log_handle.close()
            raise

        process.log_handle = log_handle
        process.log_file_path = kokoro_log_file
        self.kokoro_process = process
        return process

    def build_xtts_launcher_command(self, use_cpu=False, pixi_path=None):
        command = ['cmd', '/c', 'run.bat']
        if use_cpu:
            command.append('--cpu')
        else:
            command.extend(['--backend', 'cuda'])

        if pixi_path:
            command.extend(['--pixi-path', pixi_path])

        return command

    def build_voxcpm_launcher_command(self, pixi_path=None):
        command = ['cmd', '/c', 'run.bat']
        if pixi_path:
            command.extend(['--pixi-path', pixi_path])
        return command

    def build_fishs2_launcher_command(self, pixi_path=None):
        command = ['cmd', '/c', 'run.bat']
        if pixi_path:
            command.extend(['--pixi-path', pixi_path])
        return command

    def build_chatterbox_launcher_command(self, pixi_path=None):
        command = ['cmd', '/c', 'run.bat']
        if pixi_path:
            command.extend(['--pixi-path', pixi_path])
        return command

    def _read_text_if_exists(self, file_path):
        if not os.path.exists(file_path):
            return ""
        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as handle:
                return handle.read()
        except OSError:
            return ""

    def _read_log_tail_if_exists(self, file_path, max_lines=40):
        content = self._read_text_if_exists(file_path)
        if not content:
            return ""

        lines = content.splitlines()
        if len(lines) <= max_lines:
            return "\n".join(lines)

        return "\n".join(lines[-max_lines:])

    def get_xtts_pixi_argument(self, xtts_repo_path, pixi_path):
        if not pixi_path:
            return None

        run_bat_contents = self._read_text_if_exists(os.path.join(xtts_repo_path, 'run.bat')).lower()
        run_py_contents = self._read_text_if_exists(os.path.join(xtts_repo_path, 'run.py')).lower()
        if '--pixi-path' in run_bat_contents or '--pixi-path' in run_py_contents:
            return pixi_path

        logging.info("XTTS launcher does not advertise --pixi-path, skipping shared Pixi argument.")
        return None

    def get_voxcpm_pixi_argument(self, voxcpm_repo_path, pixi_path):
        if not pixi_path:
            return None

        run_bat_contents = self._read_text_if_exists(os.path.join(voxcpm_repo_path, 'run.bat')).lower()
        run_py_contents = self._read_text_if_exists(os.path.join(voxcpm_repo_path, 'run.py')).lower()
        if '--pixi-path' in run_bat_contents or '--pixi-path' in run_py_contents:
            return pixi_path

        logging.info("VoxCPM launcher does not advertise --pixi-path, skipping shared Pixi argument.")
        return None

    def get_fishs2_pixi_argument(self, fishs2_repo_path, pixi_path):
        if not pixi_path:
            return None

        run_bat_contents = self._read_text_if_exists(os.path.join(fishs2_repo_path, 'run.bat')).lower()
        run_py_contents = self._read_text_if_exists(os.path.join(fishs2_repo_path, 'run.py')).lower()
        if '--pixi-path' in run_bat_contents or '--pixi-path' in run_py_contents:
            return pixi_path

        logging.info("FishS2 launcher does not advertise --pixi-path, skipping shared Pixi argument.")
        return None

    def get_chatterbox_pixi_argument(self, chatterbox_repo_path, pixi_path):
        if not pixi_path:
            return None

        run_bat_contents = self._read_text_if_exists(os.path.join(chatterbox_repo_path, 'run.bat')).lower()
        run_py_contents = self._read_text_if_exists(os.path.join(chatterbox_repo_path, 'run.py')).lower()
        if '--pixi-path' in run_bat_contents or '--pixi-path' in run_py_contents:
            return pixi_path

        logging.info("Chatterbox launcher does not advertise --pixi-path, skipping shared Pixi argument.")
        return None

    def terminate_process_tree(self, process, timeout=10):
        if process is None:
            return

        try:
            parent = psutil.Process(process.pid)
        except psutil.NoSuchProcess:
            return

        try:
            for child in parent.children(recursive=True):
                try:
                    child.terminate()
                except psutil.AccessDenied:
                    logging.warning(f"Access denied when terminating child process with PID: {child.pid}")
            parent.terminate()
            process.wait(timeout=timeout)
            return
        except (psutil.TimeoutExpired, subprocess.TimeoutExpired):
            logging.warning(f"Process tree did not terminate in {timeout}s, forcing kill")
        except psutil.NoSuchProcess:
            return

        try:
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                except psutil.AccessDenied:
                    logging.warning(f"Access denied when killing child process with PID: {child.pid}")
            parent.kill()
        except psutil.NoSuchProcess:
            return

    def is_xtts_runtime_ready(self, xtts_repo_path):
        run_bat_path = os.path.join(xtts_repo_path, 'run.bat')
        env_python_path = os.path.join(xtts_repo_path, '.pixi', 'envs', 'default', 'python.exe')
        return all(os.path.exists(path) for path in (run_bat_path, env_python_path))

    def install_xtts_api_server(self, xtts_repo_path, use_cpu=False, pixi_path=None):
        logging.info(f"Bootstrapping XTTS2 API server in {xtts_repo_path}...")
        logging.info(
            "XTTS bootstrap starts the server temporarily to validate runtime and will stop it after health checks."
        )

        run_script_path = os.path.join(xtts_repo_path, 'run.bat')
        if not os.path.exists(run_script_path):
            raise FileNotFoundError(f"XTTS2 run script not found at: {run_script_path}")

        if self.is_port_in_use(8020):
            raise RuntimeError("XTTS server cannot be bootstrapped because port 8020 is already in use.")

        xtts_install_log_file = os.path.join(xtts_repo_path, 'xtts_install.log')
        command = self.build_xtts_launcher_command(
            use_cpu=use_cpu,
            pixi_path=self.get_xtts_pixi_argument(xtts_repo_path, pixi_path),
        )

        log_handle = open(xtts_install_log_file, 'a', encoding='utf-8')
        process = None
        try:
            process = subprocess.Popen(
                command,
                cwd=xtts_repo_path,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **self.get_hidden_subprocess_kwargs(),
            )

            if not self.check_xtts_server_online(
                'http://127.0.0.1:8020',
                max_attempts=360,
                wait_interval=5,
                process=process,
            ):
                return_code = process.poll()
                if return_code is not None:
                    raise RuntimeError(
                        f"XTTS2 bootstrap process exited before server was ready (exit code {return_code}). "
                        f"See log: {xtts_install_log_file}"
                    )
                raise RuntimeError(
                    "XTTS2 bootstrap did not bring the server online in time. "
                    f"See log: {xtts_install_log_file}"
                )
        finally:
            if process is not None:
                logging.info("Stopping temporary XTTS2 bootstrap process.")
                self.terminate_process_tree(process)
            log_handle.close()

    def is_voxcpm_runtime_ready(self, voxcpm_repo_path):
        run_bat_path = os.path.join(voxcpm_repo_path, 'run.bat')
        env_python_path = os.path.join(voxcpm_repo_path, '.pixi', 'envs', 'default', 'python.exe')
        return all(os.path.exists(path) for path in (run_bat_path, env_python_path))

    def install_voxcpm_api_server(self, voxcpm_repo_path, pixi_path=None):
        logging.info(f"Bootstrapping VoxCPM API server in {voxcpm_repo_path}...")
        logging.info(
            "VoxCPM bootstrap starts the server temporarily to validate runtime and will stop it after health checks."
        )

        run_script_path = os.path.join(voxcpm_repo_path, 'run.bat')
        if not os.path.exists(run_script_path):
            raise FileNotFoundError(f"VoxCPM run script not found at: {run_script_path}")

        if self.is_port_in_use(8020):
            raise RuntimeError("VoxCPM server cannot be bootstrapped because port 8020 is already in use.")

        voxcpm_install_log_file = os.path.join(voxcpm_repo_path, 'voxcpm_install.log')
        command = self.build_voxcpm_launcher_command(
            pixi_path=self.get_voxcpm_pixi_argument(voxcpm_repo_path, pixi_path),
        )

        log_handle = open(voxcpm_install_log_file, 'a', encoding='utf-8')
        process = None
        try:
            process = subprocess.Popen(
                command,
                cwd=voxcpm_repo_path,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **self.get_hidden_subprocess_kwargs(),
            )

            if not self.check_voxcpm_server_online(
                'http://127.0.0.1:8020',
                max_attempts=360,
                wait_interval=5,
                process=process,
            ):
                return_code = process.poll()
                if return_code is not None:
                    raise RuntimeError(
                        f"VoxCPM bootstrap process exited before server was ready (exit code {return_code}). "
                        f"See log: {voxcpm_install_log_file}"
                    )
                raise RuntimeError(
                    "VoxCPM bootstrap did not bring the server online in time. "
                    f"See log: {voxcpm_install_log_file}"
                )
        finally:
            if process is not None:
                logging.info("Stopping temporary VoxCPM bootstrap process.")
                self.terminate_process_tree(process)
            log_handle.close()

    def is_fishs2_runtime_ready(self, fishs2_repo_path):
        run_bat_path = os.path.join(fishs2_repo_path, 'run.bat')
        env_python_path = os.path.join(fishs2_repo_path, '.pixi', 'envs', 'default', 'python.exe')
        return all(os.path.exists(path) for path in (run_bat_path, env_python_path))

    def install_fishs2_api_server(self, fishs2_repo_path, pixi_path=None):
        logging.info(f"Bootstrapping FishS2 API server in {fishs2_repo_path}...")
        logging.info(
            "FishS2 bootstrap starts the server temporarily to validate runtime and will stop it after health checks."
        )

        run_script_path = os.path.join(fishs2_repo_path, 'run.bat')
        if not os.path.exists(run_script_path):
            raise FileNotFoundError(f"FishS2 run script not found at: {run_script_path}")

        if self.is_port_in_use(8020):
            raise RuntimeError("FishS2 server cannot be bootstrapped because port 8020 is already in use.")

        fishs2_install_log_file = os.path.join(fishs2_repo_path, 'fishs2_install.log')
        command = self.build_fishs2_launcher_command(
            pixi_path=self.get_fishs2_pixi_argument(fishs2_repo_path, pixi_path),
        )

        log_handle = open(fishs2_install_log_file, 'a', encoding='utf-8')
        process = None
        try:
            process = subprocess.Popen(
                command,
                cwd=fishs2_repo_path,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **self.get_hidden_subprocess_kwargs(),
            )

            if not self.check_fishs2_server_online(
                'http://127.0.0.1:8020',
                max_attempts=360,
                wait_interval=5,
                process=process,
            ):
                return_code = process.poll()
                if return_code is not None:
                    raise RuntimeError(
                        f"FishS2 bootstrap process exited before server was ready (exit code {return_code}). "
                        f"See log: {fishs2_install_log_file}"
                    )
                raise RuntimeError(
                    "FishS2 bootstrap did not bring the server online in time. "
                    f"See log: {fishs2_install_log_file}"
                )
        finally:
            if process is not None:
                logging.info("Stopping temporary FishS2 bootstrap process.")
                self.terminate_process_tree(process)
            log_handle.close()

    def is_chatterbox_runtime_ready(self, chatterbox_repo_path):
        run_bat_path = os.path.join(chatterbox_repo_path, 'run.bat')
        env_python_path = os.path.join(chatterbox_repo_path, '.pixi', 'envs', 'default', 'python.exe')
        return all(os.path.exists(path) for path in (run_bat_path, env_python_path))

    def install_chatterbox_api_server(self, chatterbox_repo_path, use_cpu=False, pixi_path=None):
        logging.info(f"Bootstrapping Chatterbox API server in {chatterbox_repo_path}...")
        logging.info(
            "Chatterbox bootstrap starts the server temporarily to validate runtime and will stop it after health checks."
        )

        run_script_path = os.path.join(chatterbox_repo_path, 'run.bat')
        if not os.path.exists(run_script_path):
            raise FileNotFoundError(f"Chatterbox run script not found at: {run_script_path}")

        if self.is_port_in_use(8040):
            raise RuntimeError("Chatterbox server cannot be bootstrapped because port 8040 is already in use.")

        chatterbox_install_log_file = os.path.join(chatterbox_repo_path, 'chatterbox_install.log')
        command = [run_script_path]
        if use_cpu:
            command.append('cpu')
        if pixi_path:
            command.extend(['--pixi-path', pixi_path])

        log_handle = open(chatterbox_install_log_file, 'a', encoding='utf-8')
        process = None
        try:
            process = subprocess.Popen(
                command,
                cwd=chatterbox_repo_path,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **self.get_hidden_subprocess_kwargs(),
            )

            if not self.check_chatterbox_server_online(
                'http://127.0.0.1:8040',
                max_attempts=360,
                wait_interval=5,
                process=process,
            ):
                return_code = process.poll()
                if return_code is not None:
                    raise RuntimeError(
                        f"Chatterbox bootstrap process exited before server was ready (exit code {return_code}). "
                        f"See log: {chatterbox_install_log_file}"
                    )
                raise RuntimeError(
                    "Chatterbox bootstrap did not bring the server online in time. "
                    f"See log: {chatterbox_install_log_file}"
                )
        finally:
            if process is not None:
                logging.info("Stopping temporary Chatterbox bootstrap process.")
                self.terminate_process_tree(process)
            log_handle.close()

    def is_voxtral_runtime_ready(self, voxtral_repo_path):
        venv_python_path = os.path.join(voxtral_repo_path, '.runtime', 'venv', 'Scripts', 'python.exe')
        return os.path.exists(venv_python_path)

    def install_voxtral_api_server(self, voxtral_repo_path):
        logging.info(f"Bootstrapping Voxtral API server in {voxtral_repo_path}...")
        run_script_path = os.path.join(voxtral_repo_path, 'run.ps1')
        if not os.path.exists(run_script_path):
            raise FileNotFoundError(f"Voxtral run script not found at: {run_script_path}")

        command = [
            'powershell',
            '-ExecutionPolicy',
            'Bypass',
            '-File',
            run_script_path,
            '-ProjectRoot',
            voxtral_repo_path,
            '-NoStart',
            '-Model',
            'gguf',
        ]

        self.run_command(
            command,
            cwd=voxtral_repo_path,
            env=self.get_pixi_subprocess_env(os.path.dirname(voxtral_repo_path)),
        )

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

    def install_silero_api_server(self, pandrator_path, env_name):
        logging.info(f"Installing Silero API server in {env_name}...")
        try:
            self.install_package(pandrator_path, env_name, 'requests')
            self.install_package(pandrator_path, env_name, 'silero-api-server')
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to install Silero API server in {env_name}")
            logging.error(f"Error message: {str(e)}")
            raise

    def run_git_command(self, arguments, cwd=None):
        self.configure_tls_certificates()
        git_executable = shutil.which('git')
        if not git_executable:
            raise FileNotFoundError("git.exe was not found on PATH")

        git_command = [git_executable]
        if os.name == 'nt':
            git_command.extend(['-c', 'http.sslBackend=schannel'])

        return self.run_command(
            git_command + arguments,
            cwd=cwd,
            env=self.get_network_subprocess_env()
        )

    def get_dulwich_porcelain(self):
        try:
            from dulwich import porcelain
            return porcelain
        except ImportError as e:
            raise RuntimeError(
                "Git failed and Dulwich is not available in the launcher runtime."
            ) from e

    def is_existing_git_repo(self, repo_path):
        git_metadata_path = os.path.join(repo_path, '.git')
        return os.path.isdir(git_metadata_path) or os.path.isfile(git_metadata_path)

    def clone_repo(self, repo_url, target_dir):
        logging.info(f"Cloning repository {repo_url} to {target_dir}...")
        self.configure_tls_certificates()

        if os.path.exists(target_dir):
            if not os.path.isdir(target_dir):
                raise RuntimeError(
                    f"Cannot clone repository because target path exists and is not a directory: {target_dir}"
                )

            if self.is_existing_git_repo(target_dir):
                logging.info(
                    f"Repository already exists at {target_dir}; skipping clone and pulling latest changes instead."
                )
                self.pull_repo(target_dir)
                return

            if any(os.scandir(target_dir)):
                raise RuntimeError(
                    f"Cannot clone repository because target directory already exists and is not empty: {target_dir}"
                )

        try:
            self.run_git_command(['clone', repo_url, target_dir])
            logging.info("Repository cloned successfully with git.")
        except Exception as git_error:
            logging.warning(f"git clone failed, falling back to Dulwich: {str(git_error)}")
            try:
                porcelain = self.get_dulwich_porcelain()
                porcelain.clone(repo_url, target_dir)
                logging.info("Repository cloned successfully with Dulwich.")
            except Exception as dulwich_error:
                if self.is_certificate_error(dulwich_error):
                    logging.warning(
                        "TLS certificate verification failed during Dulwich clone. "
                        "Retrying after reloading certificate bundle..."
                    )
                    self.configure_tls_certificates(force=True)
                    try:
                        porcelain = self.get_dulwich_porcelain()
                        porcelain.clone(repo_url, target_dir)
                        logging.info("Repository cloned successfully with Dulwich after certificate refresh.")
                        logging.info("Pulling latest changes...")
                        self.pull_repo(target_dir)
                        return
                    except Exception as retry_error:
                        logging.error(f"Failed to clone repository after certificate refresh: {str(retry_error)}")
                        raise RuntimeError(
                            "TLS certificate verification failed while downloading from GitHub. "
                            "Check Windows certificates and proxy TLS settings."
                        ) from retry_error
                logging.error(f"Failed to clone repository: {str(dulwich_error)}")
                raise

        logging.info("Pulling latest changes...")
        self.pull_repo(target_dir)

    def pull_repo(self, repo_path):
        logging.info(f"Pulling updates for repository at {repo_path}...")
        self.configure_tls_certificates()
        try:
            self.run_git_command(['pull', '--ff-only'], cwd=repo_path)
            logging.info("Repository updated successfully with git.")
        except Exception as git_error:
            logging.warning(f"git pull failed, falling back to Dulwich: {str(git_error)}")
            try:
                porcelain = self.get_dulwich_porcelain()
                repo = porcelain.open_repo(repo_path)
                porcelain.pull(repo)
                logging.info("Repository updated successfully with Dulwich.")
            except Exception as dulwich_error:
                if self.is_certificate_error(dulwich_error):
                    logging.warning(
                        "TLS certificate verification failed during Dulwich pull. "
                        "Retrying after reloading certificate bundle..."
                    )
                    self.configure_tls_certificates(force=True)
                    try:
                        porcelain = self.get_dulwich_porcelain()
                        repo = porcelain.open_repo(repo_path)
                        porcelain.pull(repo)
                        logging.info("Repository updated successfully with Dulwich after certificate refresh.")
                        return
                    except Exception as retry_error:
                        logging.error(f"Failed to update repository after certificate refresh: {str(retry_error)}")
                        raise RuntimeError(
                            "TLS certificate verification failed while downloading from GitHub. "
                            "Check Windows certificates and proxy TLS settings."
                        ) from retry_error
                logging.error(f"Failed to update repository: {str(dulwich_error)}")
                raise

    def install_pycroppdf_requirements(self, pandrator_path, env_name, pycroppdf_repo_path):
        logging.info(f"Installing PyCropPDF requirements in {env_name}...")
        try:
            requirements_file = os.path.join(pycroppdf_repo_path, 'requirements.txt')
            self.install_requirements(pandrator_path, env_name, requirements_file)
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to install PyCropPDF requirements in {env_name}")
            logging.error(f"Error message: {str(e)}")
            raise

    def install_rvc_python(self, pandrator_path, env_name):
        logging.info("Starting RVC Python installation")
        try:
            logging.info("Installing specific pip version...")
            # Keep the older pip pin here because newer pip versions break this toolchain.
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                ['python', '-m', 'pip', 'install', 'pip==24']
            )

            python_version = self.get_env_python_version(pandrator_path, env_name)
            fairseq_wheel_url = self.get_rvc_fairseq_wheel_url(python_version)

            logging.info(f"Installing RVC Python fork for Python {python_version}...")
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                [
                    'python', '-m', 'pip', 'install',
                    '--upgrade', '--force-reinstall',
                    RVC_PYTHON_FORK_INSTALL_SPEC,
                ]
            )

            logging.info("Installing fairseq wheel required by the RVC fork...")
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                [
                    'python', '-m', 'pip', 'install',
                    '--upgrade', '--force-reinstall',
                    fairseq_wheel_url,
                ]
            )

            logging.info("Installing PyTorch stack for RVC...")
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                [
                    'python', '-m', 'pip', 'install', '--upgrade',
                    f'torch=={RVC_TORCH_VERSION}',
                    f'torchvision=={RVC_TORCHVISION_VERSION}',
                    f'torchaudio=={RVC_TORCHAUDIO_VERSION}',
                    '--index-url', RVC_TORCH_INDEX_URL,
                ]
            )

            logging.info("Pinning NumPy to <2 for faiss/RVC compatibility...")
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                [
                    'python', '-m', 'pip', 'install',
                    '--upgrade', '--force-reinstall',
                    RVC_NUMPY_SPEC,
                ]
            )

            logging.info("Verifying RVC runtime imports...")
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                [
                    'python', '-c',
                    (
                        'import numpy as np; '
                        'import fairseq, rvc_python, torch, torchvision, torchaudio; '
                        'assert int(np.__version__.split(".")[0]) < 2, '
                        'f"NumPy must be <2 for RVC compatibility, got {np.__version__}"'
                    ),
                ]
            )

            logging.info("RVC Python installation completed successfully.")

        except Exception as e:
            error_msg = f"An error occurred during RVC Python installation: {str(e)}"
            logging.error(error_msg)
            logging.error(traceback.format_exc())
            raise

    def install_whisperx(self, pandrator_path, env_name):
        logging.info(f"Installing WhisperX in {env_name}...")
        try:
            self.add_pixi_conda_package(pandrator_path, env_name, 'cudnn=8.9.7.29')
            self.add_pixi_conda_package(pandrator_path, env_name, 'ffmpeg')

            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                [
                    'python', '-m', 'pip', 'install',
                    f'torch=={WHISPERX_TORCH_VERSION}',
                    f'torchvision=={WHISPERX_TORCHVISION_VERSION}',
                    f'torchaudio=={WHISPERX_TORCHAUDIO_VERSION}',
                    '--index-url', WHISPERX_TORCH_INDEX_URL
                ]
            )

            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                [
                    'python', '-m', 'pip', 'install',
                    f'whisperx=={WHISPERX_VERSION}',
                    f'ctranslate2=={WHISPERX_CTRANSLATE2_VERSION}'
                ]
            )

            logging.info("WhisperX installation completed successfully.")
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to install WhisperX in {env_name}")
            logging.error(f"Error message: {str(e)}")
            raise

    def install_subdub_requirements(self, pandrator_path, env_name, subdub_repo_path):
        logging.info(f"Installing Subdub package in {env_name}...")
        try:
            if not os.path.exists(subdub_repo_path):
                self.clone_repo(SUBDUB_REPO_URL, subdub_repo_path)

            pyproject_file = os.path.join(subdub_repo_path, 'pyproject.toml')
            if os.path.exists(pyproject_file):
                logging.info(
                    "Detected refactored Subdub package layout (pyproject.toml). "
                    "Installing editable package with GUI extras..."
                )
                self.run_pixi_in_env(
                    pandrator_path,
                    env_name,
                    ['python', '-m', 'pip', 'install', '-e', SUBDUB_EDITABLE_INSTALL_SPEC],
                    cwd=subdub_repo_path,
                )
                self.ensure_subdub_runtime(pandrator_path, env_name, subdub_repo_path)
                return

            requirements_file = os.path.join(subdub_repo_path, 'requirements.txt')
            if not os.path.exists(requirements_file):
                raise FileNotFoundError(
                    f"Subdub dependency manifest not found. Expected either {pyproject_file} or {requirements_file}."
                )

            logging.info("Using legacy Subdub requirements.txt installation path.")
            self.install_requirements(pandrator_path, env_name, requirements_file)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logging.error(f"Failed to install Subdub package in {env_name}")
            logging.error(f"Error message: {str(e)}")
            raise

    def set_permissive_permissions(self, path):
        """Set permissive file permissions on installation directories"""
        if not self.is_admin():
            logging.info(f"Skipping permission setting on {path} (not running as admin)")
            return False

        try:
            self.update_status(f"Setting permissions on {os.path.basename(path)}...")
            logging.info(f"Setting permissive permissions on: {path}")

            icacls_executable = shutil.which('icacls')
            if not icacls_executable:
                system_root = os.environ.get('SystemRoot', r'C:\Windows')
                fallback_icacls = os.path.join(system_root, 'System32', 'icacls.exe')
                if os.path.exists(fallback_icacls):
                    icacls_executable = fallback_icacls

            if not icacls_executable:
                logging.error(f"Could not locate icacls.exe. Skipping permission update for {path}")
                return False

            # Use icacls to give Users full control (F) with inheritance flags (OI)(CI)
            # OI = Object Inherit, CI = Container Inherit, F = Full Control
            command = [
                icacls_executable,
                path,
                '/grant:r',
                'Users:(OI)(CI)F',
                '/T',
                '/Q',
            ]
            completed_process = subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **self.get_hidden_subprocess_kwargs(),
            )

            if completed_process.stdout:
                logging.debug(f"icacls output for {path}: {completed_process.stdout}")
            if completed_process.stderr:
                logging.debug(f"icacls stderr for {path}: {completed_process.stderr}")

            logging.info(f"Successfully set permissions on: {path}")
            return True
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to set permissions on {path}: {str(e)}")
            if e.stdout:
                logging.error(f"icacls stdout: {e.stdout}")
            if e.stderr:
                logging.error(f"icacls stderr: {e.stderr}")
            logging.error(traceback.format_exc())
            return False
        except FileNotFoundError as e:
            logging.error(f"Permission tool missing while updating {path}: {str(e)}")
            logging.error(traceback.format_exc())
            return False
        except Exception as e:
            logging.error(f"Unexpected error while setting permissions on {path}: {str(e)}")
            logging.error(traceback.format_exc())
            return False
