"""Backend launch, health checking, and process lifecycle management."""

import logging
import os
import subprocess
import time

import psutil
import requests

try:
    from packaging.specifiers import SpecifierSet as PackagingSpecifierSet
except ImportError:
    PackagingSpecifierSet = None

from .catalog import BACKEND_COMPONENT_KEYS, COMPONENTS
from .constants import (
    CHATTERBOX_API_REPO_DIRNAME,
    MAGPIE_API_REPO_DIRNAME,
    FISHS2_API_REPO_DIRNAME,
    KOKORO_API_REPO_DIRNAME,
    KOKORO_ENV_NAME,
    KOKORO_GPU_SUPPORT_CONFIG_FLAG,
    KOKORO_PYTHON_VERSION,
    RVC_API_REPO_DIRNAME,
    RVC_GPU_SUPPORT_CONFIG_FLAG,
    VOXCPM_API_REPO_DIRNAME,
    VOXTRAL_API_REPO_DIRNAME,
    XTTS_API_REPO_DIRNAME,
)
from .platforms import is_windows


class RuntimeMixin:
    def _backend_runtime_specs(self):
        return tuple(
            (
                key,
                COMPONENTS[key].label,
                COMPONENTS[key].process_attr,
                getattr(self, f"shutdown_{key}"),
            )
            for key in BACKEND_COMPONENT_KEYS
        )

    @staticmethod
    def _close_process_log_handle(process):
        if hasattr(process, 'log_handle') and process.log_handle:
            try:
                process.log_handle.close()
            except Exception:
                pass
            process.log_handle = None

    def _collect_running_backends(self):
        running = []
        for backend_key, backend_label, process_attr, _ in self._backend_runtime_specs():
            process = getattr(self, process_attr, None)
            if not process:
                continue

            try:
                return_code = process.poll()
            except Exception:
                return_code = 1

            if return_code is None:
                running.append((backend_key, backend_label, process))
            else:
                self._close_process_log_handle(process)
                setattr(self, process_attr, None)

        return running

    def _get_running_pandrator_process(self):
        process = self.pandrator_process
        if not process:
            return None

        try:
            return_code = process.poll()
        except Exception:
            return_code = 1

        if return_code is None:
            return process

        self._close_process_log_handle(process)
        self.pandrator_process = None
        return None

    def _get_running_rvc_process(self):
        process = self.rvc_process
        if not process:
            return None

        try:
            return_code = process.poll()
        except Exception:
            return_code = 1

        if return_code is None:
            return process

        self._close_process_log_handle(process)
        self.rvc_process = None
        return None

    def _selected_launch_backend_keys(self):
        selected = []
        if self.launch_xtts_var:
            selected.append('xtts')
        if self.launch_voxcpm_var:
            selected.append('voxcpm')
        if self.launch_fishs2_var:
            selected.append('fishs2')
        if self.launch_voxtral_var:
            selected.append('voxtral')
        if self.launch_silero_var:
            selected.append('silero')
        if self.launch_kokoro_var:
            selected.append('kokoro')
        if self.launch_chatterbox_var:
            selected.append('chatterbox')
        if self.launch_magpie_var:
            selected.append('magpie')
        return selected

    def _apply_launch_selection_state(self, selection):
        self.launch_pandrator_var = selection.pandrator
        self.launch_rvc_var = selection.rvc
        self.rvc_cpu_launch_var = selection.rvc_cpu
        self.launch_xtts_var = selection.xtts
        self.disable_deepspeed_var = selection.disable_deepspeed
        self.xtts_cpu_launch_var = selection.xtts_cpu
        self.launch_voxcpm_var = selection.voxcpm
        self.launch_fishs2_var = selection.fishs2
        self.launch_voxtral_var = selection.voxtral
        self.launch_kokoro_var = selection.kokoro
        self.kokoro_cpu_launch_var = selection.kokoro_cpu
        self.launch_silero_var = selection.silero
        self.launch_chatterbox_var = selection.chatterbox
        self.chatterbox_cpu_launch_var = selection.chatterbox_cpu
        self.launch_magpie_var = selection.magpie
        self.magpie_cpu_launch_var = selection.magpie_cpu

    def _backend_label_from_key(self, backend_key):
        for key, label, _, _ in self._backend_runtime_specs():
            if key == backend_key:
                return label
        return str(backend_key or '').strip() or 'Backend'

    def _stop_backends_by_keys(self, backend_keys, report_progress=False):
        requested_keys = [str(key).strip().lower() for key in backend_keys if str(key).strip()]
        if not requested_keys:
            return

        shutdown_by_key = {
            key: shutdown
            for key, _, _, shutdown in self._backend_runtime_specs()
        }

        total = len(requested_keys)
        for index, backend_key in enumerate(requested_keys, start=1):
            shutdown = shutdown_by_key.get(backend_key)
            if shutdown:
                shutdown()

            if report_progress:
                self.reporter.progress(index / total)



    def stop_running_backends_process(self):
        targets = list(self.backend_stop_targets)
        if not targets:
            self.reporter.status("No backend stop target found.")
            return

        labels = [self._backend_label_from_key(target) for target in targets]
        self.reporter.status(
            "Stopping backend(s): " + ", ".join(labels)
        )
        self._stop_backends_by_keys(targets, report_progress=True)
        self.reporter.status("Backend stop complete.")




    def launch_process(self, selection=None):
        """Main launch process - runs in a worker thread"""
        if selection is not None:
            self._apply_launch_selection_state(selection)

        base_path = os.path.abspath(self.initial_working_dir)
        pandrator_path = os.path.join(base_path, 'Pandrator')

        self.reporter.progress(0.3)
        self.reporter.status("Preparing to launch...")
        logging.info(f"Launch process started. Base directory: {base_path}")
        logging.info(f"Pandrator path: {pandrator_path}")
        logging.info(f"Pixi path: {self.get_pixi_executable(pandrator_path)}")

        if not self.check_pixi(pandrator_path):
            raise FileNotFoundError(
                "Pixi runtime not found. Run Install or Update to migrate this installation."
            )

        install_config = self.load_install_config(pandrator_path)
        shared_pixi_path = self.get_pixi_executable(pandrator_path)

        selected_backend_keys = self._selected_launch_backend_keys()
        selected_backend_key = selected_backend_keys[0] if selected_backend_keys else None
        running_backends = self._collect_running_backends()

        if selected_backend_key and running_backends:
            conflicting_backends = [
                backend_key
                for backend_key, _, _ in running_backends
                if backend_key != selected_backend_key
            ]
            if conflicting_backends:
                running_names = ", ".join(
                    self._backend_label_from_key(backend_key)
                    for backend_key in conflicting_backends
                )
                self.reporter.status(
                    "Stopping running backend(s) before switching: " + running_names
                )
                self._stop_backends_by_keys(conflicting_backends)
                running_backends = self._collect_running_backends()

        running_backend_keys = {
            backend_key
            for backend_key, _, _ in running_backends
        }
        running_pandrator_process = self._get_running_pandrator_process()
        running_rvc_process = self._get_running_rvc_process()

        pandrator_args = []
        tts_engine_launched = False

        if self.launch_xtts_var:
            self.reporter.progress(0.4)
            xtts_server_url = 'http://127.0.0.1:8020'
            if 'xtts' in running_backend_keys and self.xtts_process:
                self.reporter.status("XTTS server is already running. Reusing existing backend.")
            else:
                self.reporter.status("Starting XTTS server...")
                xtts_server_path = os.path.join(pandrator_path, XTTS_API_REPO_DIRNAME)
                logging.info(f"XTTS server path: {xtts_server_path}")

                if not os.path.exists(xtts_server_path):
                    error_msg = f"XTTS server path not found: {xtts_server_path}"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    raise FileNotFoundError(error_msg)

                try:
                    use_cpu = self.xtts_cpu_launch_var
                    xtts_process = self.run_xtts_api_server(
                        xtts_server_path,
                        use_cpu,
                        pixi_path=shared_pixi_path,
                    )
                except Exception as e:
                    error_msg = f"Failed to start XTTS server: {str(e)}"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    logging.exception("Exception details:")
                    raise

                self.xtts_process = xtts_process

                if not self.check_xtts_server_online(xtts_server_url, process=xtts_process):
                    error_msg = "XTTS server failed to come online"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    self.shutdown_xtts()
                    raise RuntimeError(error_msg)

            pandrator_args = ['-connect', '-xtts']
            tts_engine_launched = True

        if self.launch_voxcpm_var and not tts_engine_launched:
            self.reporter.progress(0.5)
            voxcpm_server_url = 'http://127.0.0.1:8020'
            if 'voxcpm' in running_backend_keys and self.voxcpm_process:
                self.reporter.status("VoxCPM server is already running. Reusing existing backend.")
            else:
                self.reporter.status("Starting VoxCPM server...")
                voxcpm_server_path = os.path.join(pandrator_path, VOXCPM_API_REPO_DIRNAME)
                logging.info(f"VoxCPM server path: {voxcpm_server_path}")

                if not os.path.exists(voxcpm_server_path):
                    error_msg = f"VoxCPM server path not found: {voxcpm_server_path}"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    raise FileNotFoundError(error_msg)

                try:
                    self.voxcpm_process = self.run_voxcpm_api_server(
                        voxcpm_server_path,
                        pixi_path=shared_pixi_path,
                    )
                except Exception as e:
                    error_msg = f"Failed to start VoxCPM server: {str(e)}"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    logging.exception("Exception details:")
                    raise

                if not self.check_voxcpm_server_online(voxcpm_server_url, process=self.voxcpm_process):
                    error_msg = "VoxCPM server failed to come online"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    self.shutdown_voxcpm()
                    raise RuntimeError(error_msg)

            pandrator_args = ['-connect', '-voxcpm']
            tts_engine_launched = True

        if self.launch_fishs2_var and not tts_engine_launched:
            self.reporter.progress(0.53)
            fishs2_server_url = 'http://127.0.0.1:8020'
            if 'fishs2' in running_backend_keys and self.fishs2_process:
                self.reporter.status("FishS2 server is already running. Reusing existing backend.")
            else:
                self.reporter.status("Starting FishS2 server...")
                fishs2_server_path = os.path.join(pandrator_path, FISHS2_API_REPO_DIRNAME)
                logging.info(f"FishS2 server path: {fishs2_server_path}")

                if not os.path.exists(fishs2_server_path):
                    error_msg = f"FishS2 server path not found: {fishs2_server_path}"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    raise FileNotFoundError(error_msg)

                try:
                    self.fishs2_process = self.run_fishs2_api_server(
                        fishs2_server_path,
                        pixi_path=shared_pixi_path,
                    )
                except Exception as e:
                    error_msg = f"Failed to start FishS2 server: {str(e)}"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    logging.exception("Exception details:")
                    raise

                if not self.check_fishs2_server_online(fishs2_server_url, process=self.fishs2_process):
                    error_msg = "FishS2 server failed to come online"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    self.shutdown_fishs2()
                    raise RuntimeError(error_msg)

            pandrator_args = ['-connect', '-fishs2']
            tts_engine_launched = True

        if self.launch_voxtral_var and not tts_engine_launched:
            self.reporter.progress(0.55)
            voxtral_server_url = 'http://127.0.0.1:8000/health'
            if 'voxtral' in running_backend_keys and self.voxtral_process:
                self.reporter.status("Voxtral server is already running. Reusing existing backend.")
            else:
                self.reporter.status("Starting Voxtral server...")
                voxtral_server_path = os.path.join(pandrator_path, VOXTRAL_API_REPO_DIRNAME)
                logging.info(f"Voxtral server path: {voxtral_server_path}")

                if not os.path.exists(voxtral_server_path):
                    error_msg = f"Voxtral server path not found: {voxtral_server_path}"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    raise FileNotFoundError(error_msg)

                try:
                    self.voxtral_process = self.run_voxtral_api_server(voxtral_server_path)
                except Exception as e:
                    error_msg = f"Failed to start Voxtral server: {str(e)}"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    logging.exception("Exception details:")
                    raise

                if not self.check_voxtral_server_online(voxtral_server_url):
                    error_msg = "Voxtral server failed to come online"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    self.shutdown_voxtral()
                    raise RuntimeError(error_msg)

            pandrator_args = ['-connect', '-voxtral']
            tts_engine_launched = True

        if self.launch_silero_var and not tts_engine_launched:
            self.reporter.progress(0.6)
            silero_server_url = 'http://127.0.0.1:8001/docs'
            if 'silero' in running_backend_keys and self.silero_process:
                self.reporter.status("Silero server is already running. Reusing existing backend.")
            else:
                self.reporter.status("Starting Silero server...")

                try:
                    self.silero_process = self.run_silero_api_server(pandrator_path, 'silero_api_server_installer')
                except Exception as e:
                    error_msg = f"Failed to start Silero server: {str(e)}"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    logging.exception("Exception details:")
                    raise

                if not self.check_silero_server_online(silero_server_url):
                    error_msg = "Silero server failed to come online"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    self.shutdown_silero()
                    raise RuntimeError(error_msg)

            pandrator_args = ['-connect', '-silero']
            tts_engine_launched = True

        if self.launch_kokoro_var and not tts_engine_launched:
            self.reporter.progress(0.65)
            kokoro_server_url = 'http://127.0.0.1:8880/health'
            if 'kokoro' in running_backend_keys and self.kokoro_process:
                self.reporter.status("Kokoro server is already running. Reusing existing backend.")
            else:
                self.reporter.status("Starting Kokoro server...")
                kokoro_server_path = os.path.join(pandrator_path, KOKORO_API_REPO_DIRNAME)
                kokoro_gpu_support = install_config.get(KOKORO_GPU_SUPPORT_CONFIG_FLAG, False)
                kokoro_launch_gpu = kokoro_gpu_support and not self.kokoro_cpu_launch_var
                logging.info(f"Kokoro server path: {kokoro_server_path}")

                if not os.path.exists(kokoro_server_path):
                    error_msg = f"Kokoro server path not found: {kokoro_server_path}"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    raise FileNotFoundError(error_msg)

                if not self.is_kokoro_runtime_ready(
                    pandrator_path,
                    kokoro_server_path,
                    use_gpu=kokoro_gpu_support,
                ):
                    self.reporter.status("Preparing Kokoro runtime...")
                    self.create_pixi_env(pandrator_path, KOKORO_ENV_NAME, KOKORO_PYTHON_VERSION)
                    self.install_kokoro_api_server(
                        pandrator_path,
                        kokoro_server_path,
                        env_name=KOKORO_ENV_NAME,
                        use_gpu=kokoro_gpu_support,
                        runtime_use_gpu=kokoro_launch_gpu,
                    )

                try:
                    self.kokoro_process = self.run_kokoro_api_server(
                        pandrator_path,
                        KOKORO_ENV_NAME,
                        kokoro_server_path,
                        use_gpu=kokoro_launch_gpu,
                    )
                except Exception as e:
                    error_msg = f"Failed to start Kokoro server: {str(e)}"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    logging.exception("Exception details:")
                    raise

                if not self.check_kokoro_server_online(kokoro_server_url, process=self.kokoro_process):
                    error_msg = "Kokoro server failed to come online"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    self.shutdown_kokoro()
                    raise RuntimeError(error_msg)

            pandrator_args = ['-connect', '-kokoro']
            tts_engine_launched = True

        if self.launch_chatterbox_var and not tts_engine_launched:
            self.reporter.progress(0.70)
            chatterbox_server_url = 'http://127.0.0.1:8040'
            if 'chatterbox' in running_backend_keys and self.chatterbox_process:
                self.reporter.status("Chatterbox server is already running. Reusing existing backend.")
            else:
                self.reporter.status("Starting Chatterbox server...")
                chatterbox_server_path = os.path.join(pandrator_path, CHATTERBOX_API_REPO_DIRNAME)
                logging.info(f"Chatterbox server path: {chatterbox_server_path}")

                if not os.path.exists(chatterbox_server_path):
                    error_msg = f"Chatterbox server path not found: {chatterbox_server_path}"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    raise FileNotFoundError(error_msg)

                chatterbox_gpu_support = install_config.get('chatterbox_gpu_support', False)
                chatterbox_launch_gpu = chatterbox_gpu_support and not self.chatterbox_cpu_launch_var

                try:
                    self.chatterbox_process = self.run_chatterbox_api_server(
                        chatterbox_server_path,
                        use_cpu=not chatterbox_launch_gpu,
                        pixi_path=shared_pixi_path,
                    )
                except Exception as e:
                    error_msg = f"Failed to start Chatterbox server: {str(e)}"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    logging.exception("Exception details:")
                    raise

                if not self.check_chatterbox_server_online(chatterbox_server_url, process=self.chatterbox_process):
                    error_msg = "Chatterbox server failed to come online"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    self.shutdown_chatterbox()
                    raise RuntimeError(error_msg)

            pandrator_args = ['-connect', '-chatterbox']
            tts_engine_launched = True

        if self.launch_magpie_var and not tts_engine_launched:
            self.reporter.progress(0.70)
            magpie_server_url = 'http://127.0.0.1:8030'
            if 'magpie' in running_backend_keys and self.magpie_process:
                self.reporter.status("Magpie server is already running. Reusing existing backend.")
            else:
                self.reporter.status("Starting Magpie server...")
                magpie_server_path = os.path.join(pandrator_path, MAGPIE_API_REPO_DIRNAME)
                logging.info(f"Magpie server path: {magpie_server_path}")

                if not os.path.exists(magpie_server_path):
                    error_msg = f"Magpie server path not found: {magpie_server_path}"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    raise FileNotFoundError(error_msg)

                magpie_gpu_support = install_config.get('magpie_gpu_support', False)
                magpie_launch_gpu = magpie_gpu_support and not self.magpie_cpu_launch_var

                try:
                    self.magpie_process = self.run_magpie_api_server(
                        magpie_server_path,
                        use_cpu=not magpie_launch_gpu,
                        pixi_path=shared_pixi_path,
                    )
                except Exception as e:
                    error_msg = f"Failed to start Magpie server: {str(e)}"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    logging.exception("Exception details:")
                    raise

                if not self.check_magpie_server_online(magpie_server_url, process=self.magpie_process):
                    error_msg = "Magpie server failed to come online"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    self.shutdown_magpie()
                    raise RuntimeError(error_msg)

            pandrator_args = ['-connect', '-magpie']
            tts_engine_launched = True

        if self.launch_rvc_var:
            self.reporter.progress(0.8)
            rvc_server_url = 'http://127.0.0.1:8050/health'
            if running_rvc_process:
                self.reporter.status("RVC service is already running. Reusing it.")
            elif self.is_port_in_use(8050):
                if not self.check_rvc_server_online(rvc_server_url, max_attempts=1, wait_interval=0):
                    raise RuntimeError("RVC service cannot start because port 8050 is already in use.")
                self.reporter.status("RVC service is already available on port 8050.")
            else:
                self.reporter.status("Starting RVC service...")
                rvc_server_path = os.path.join(pandrator_path, RVC_API_REPO_DIRNAME)
                rvc_models_dir = os.path.join(pandrator_path, 'Pandrator', 'rvc_models')
                os.makedirs(rvc_models_dir, exist_ok=True)
                rvc_gpu_support = install_config.get(RVC_GPU_SUPPORT_CONFIG_FLAG, False)
                rvc_use_cpu = self.rvc_cpu_launch_var or not rvc_gpu_support
                self.rvc_process = self.run_rvc_api_server(
                    rvc_server_path,
                    rvc_models_dir,
                    use_cpu=rvc_use_cpu,
                    pixi_path=shared_pixi_path,
                )
                if not self.check_rvc_server_online(rvc_server_url, process=self.rvc_process):
                    self.shutdown_rvc()
                    raise RuntimeError("RVC service failed to come online")

        if self.launch_pandrator_var:
            self.reporter.progress(0.85)
            if running_pandrator_process:
                self.reporter.progress(0.9)
                self.reporter.status(
                    "Pandrator is already running. Reusing the current instance."
                )
            else:
                self.reporter.status("Checking Pandrator runtime...")
                self.ensure_pandrator_runtime(pandrator_path, 'pandrator_installer')

                self.reporter.progress(0.9)
                self.reporter.status("Starting Pandrator...")
                pandrator_repo_path = os.path.join(pandrator_path, 'Pandrator')
                pandrator_script_candidates = [
                    os.path.join(pandrator_repo_path, 'main.py'),
                    os.path.join(pandrator_repo_path, 'pandrator.py'),
                ]
                pandrator_script_path = next(
                    (candidate for candidate in pandrator_script_candidates if os.path.exists(candidate)),
                    '',
                )

                if pandrator_script_path:
                    logging.info(f"Pandrator script path: {pandrator_script_path}")
                else:
                    logging.error(
                        "Pandrator script not found. Checked candidates: %s",
                        ", ".join(pandrator_script_candidates),
                    )
                    error_msg = (
                        "Pandrator script not found. Checked: "
                        + ", ".join(pandrator_script_candidates)
                    )
                    self.reporter.status(error_msg)
                    raise FileNotFoundError(error_msg)

                try:
                    self.pandrator_process = self.run_script(pandrator_path, 'pandrator_installer', pandrator_script_path, pandrator_args)
                    self.ensure_process_started(
                        self.pandrator_process,
                        'Pandrator',
                        getattr(self.pandrator_process, 'log_file_path', ''),
                    )
                except Exception as e:
                    error_msg = f"Failed to start Pandrator: {str(e)}"
                    self.reporter.status(error_msg)
                    logging.error(error_msg)
                    logging.exception("Exception details:")
                    raise

        self.reporter.progress(1.0)
        self.reporter.status("Apps are running!")



    def is_port_in_use(self, port):
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(('localhost', port)) == 0

    def run_script(self, pandrator_path, env_name, script_path, additional_args=None):
        if additional_args is None:
            additional_args = []

        logging.info(f"Running script {script_path} in {env_name} with args: {additional_args}")

        script_dir = os.path.dirname(script_path)
        command = self.build_pixi_run_command(
            pandrator_path,
            env_name,
            ['python', script_path] + additional_args
        )

        pandrator_log_file = os.path.join(script_dir, 'pandrator_startup.log')
        log_handle = open(pandrator_log_file, 'a', encoding='utf-8')

        try:
            process = subprocess.Popen(
                command,
                cwd=script_dir,
                env=self.get_pixi_subprocess_env(pandrator_path),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **self.get_hidden_subprocess_kwargs(),
            )
        except Exception:
            log_handle.close()
            raise

        process.log_handle = log_handle
        process.log_file_path = pandrator_log_file
        logging.info(f"Pandrator startup log: {pandrator_log_file}")
        return process

    def ensure_process_started(self, process, process_name, startup_log_file, grace_period_seconds=2):
        if process is None:
            raise RuntimeError(f"{process_name} process was not created.")

        time.sleep(grace_period_seconds)
        return_code = process.poll()
        if return_code is None:
            return

        if hasattr(process, 'log_handle') and process.log_handle:
            process.log_handle.flush()
            process.log_handle.close()
            process.log_handle = None

        details = f"{process_name} exited immediately with code {return_code}."
        if startup_log_file:
            details += f" See log: {startup_log_file}"

        log_tail = self._read_log_tail_if_exists(startup_log_file)
        if log_tail:
            details += f" Last output:\n{log_tail}"

        raise RuntimeError(details)

    def run_xtts_api_server(self, xtts_server_path, use_cpu=False, pixi_path=None):
        """Run the XTTS2 API server via its upstream launcher script."""
        logging.info("Attempting to run XTTS API server...")
        logging.info(f"XTTS server path: {xtts_server_path}")
        logging.info(f"Use CPU: {use_cpu}")

        if not os.path.exists(xtts_server_path):
            raise FileNotFoundError(f"XTTS server path not found: {xtts_server_path}")

        if self.is_port_in_use(8020):
            error_msg = "XTTS server cannot be started because port 8020 is already in use."
            logging.error(error_msg)
            self.notify_error("Error", error_msg)
            return None

        run_script_path = os.path.join(xtts_server_path, 'run.bat')
        if not os.path.exists(run_script_path):
            raise FileNotFoundError(f"XTTS run script not found at: {run_script_path}")

        xtts_log_file = os.path.join(xtts_server_path, 'xtts_server.log')
        command = self.build_xtts_launcher_command(
            use_cpu=use_cpu,
            pixi_path=self.get_xtts_pixi_argument(xtts_server_path, pixi_path),
        )
        xtts_env = self.get_pixi_subprocess_env(os.path.dirname(xtts_server_path))
        if use_cpu:
            xtts_env['XTTS_DEVICE'] = 'cpu'
            xtts_env['XTTS_USE_DEEPSPEED'] = 'false'
        elif self.disable_deepspeed_var:
            xtts_env['XTTS_USE_DEEPSPEED'] = 'false'
        else:
            xtts_env.pop('XTTS_USE_DEEPSPEED', None)

        log_handle = open(xtts_log_file, 'a', encoding='utf-8')
        try:
            process = subprocess.Popen(
                command,
                cwd=xtts_server_path,
                env=xtts_env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **self.get_hidden_subprocess_kwargs(),
            )
        except Exception:
            log_handle.close()
            raise

        process.log_handle = log_handle
        logging.info(f"XTTS API server process started with PID: {process.pid}")
        return process

    def run_rvc_api_server(self, rvc_server_path, models_dir, use_cpu=False, pixi_path=None):
        """Run the RVC auxiliary service via its repository launcher."""
        if not os.path.exists(rvc_server_path):
            raise FileNotFoundError(f"RVC service path not found: {rvc_server_path}")
        if self.is_port_in_use(8050):
            raise RuntimeError("RVC service cannot be started because port 8050 is already in use.")

        run_script_path = os.path.join(rvc_server_path, 'run.bat')
        if not os.path.exists(run_script_path):
            raise FileNotFoundError(f"RVC run script not found at: {run_script_path}")

        rvc_log_file = os.path.join(rvc_server_path, 'rvc_server.log')
        command = self.build_rvc_launcher_command(
            use_cpu=use_cpu,
            pixi_path=pixi_path,
            models_dir=models_dir,
        )
        log_handle = open(rvc_log_file, 'a', encoding='utf-8')
        try:
            process = subprocess.Popen(
                command,
                cwd=rvc_server_path,
                env=self.get_pixi_subprocess_env(os.path.dirname(rvc_server_path)),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **self.get_hidden_subprocess_kwargs(),
            )
        except Exception:
            log_handle.close()
            raise

        process.log_handle = log_handle
        process.log_file_path = rvc_log_file
        self.rvc_process = process
        return process

    def check_rvc_server_online(self, url, max_attempts=180, wait_interval=5, process=None):
        """Check whether the RVC service is ready."""
        for attempt in range(1, max_attempts + 1):
            if process is not None and process.poll() is not None:
                logging.error("RVC service process exited before coming online.")
                return False
            try:
                response = requests.get(url, timeout=5)
                if response.status_code == 200 and response.json().get('ready'):
                    logging.info("RVC service is online.")
                    return True
            except (requests.exceptions.RequestException, ValueError):
                pass

            logging.info("RVC service is not online yet. Waiting... (Attempt %s/%s)", attempt, max_attempts)
            time.sleep(wait_interval)

        return False

    def check_xtts_server_online(self, base_url, max_attempts=120, wait_interval=5, process=None):
        """Check if the XTTS server is online and responding."""
        probe_paths = ['/health', '/v1/models', '/docs']
        for attempt in range(1, max_attempts + 1):
            if process is not None and process.poll() is not None:
                logging.error("XTTS server process exited before coming online.")
                return False

            for probe_path in probe_paths:
                try:
                    response = requests.get(f"{base_url}{probe_path}", timeout=5)
                    if response.status_code == 404:
                        continue
                    if response.status_code < 400:
                        logging.info("XTTS server is online.")
                        return True
                except requests.exceptions.RequestException:
                    continue

            logging.info("XTTS server is not online yet. Waiting... (Attempt %s/%s)", attempt, max_attempts)
            time.sleep(wait_interval)

        logging.error("XTTS server failed to come online within the specified attempts.")
        return False

    def run_voxcpm_api_server(self, voxcpm_server_path, pixi_path=None):
        """Run the VoxCPM API server via its upstream launcher script."""
        logging.info(f"Running VoxCPM API server from {voxcpm_server_path}...")

        if self.is_port_in_use(8020):
            error_msg = "VoxCPM server cannot be started because port 8020 is already in use."
            logging.error(error_msg)
            self.notify_error("Error", error_msg)
            return None

        run_script_path = os.path.join(voxcpm_server_path, 'run.bat')
        if not os.path.exists(run_script_path):
            raise FileNotFoundError(f"VoxCPM run script not found at: {run_script_path}")

        voxcpm_log_file = os.path.join(voxcpm_server_path, 'voxcpm_server.log')
        command = self.build_voxcpm_launcher_command(
            pixi_path=self.get_voxcpm_pixi_argument(voxcpm_server_path, pixi_path),
        )

        log_handle = open(voxcpm_log_file, 'a', encoding='utf-8')
        try:
            process = subprocess.Popen(
                command,
                cwd=voxcpm_server_path,
                env=self.get_pixi_subprocess_env(os.path.dirname(voxcpm_server_path)),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **self.get_hidden_subprocess_kwargs(),
            )
        except Exception:
            log_handle.close()
            raise

        process.log_handle = log_handle
        self.voxcpm_process = process
        return process

    def check_voxcpm_server_online(self, base_url, max_attempts=120, wait_interval=5, process=None):
        """Check if the VoxCPM server is online and responding."""
        probe_paths = ['/health', '/v1/models', '/v1/audio/voices']
        for attempt in range(1, max_attempts + 1):
            if process is not None and process.poll() is not None:
                logging.error("VoxCPM server process exited before coming online.")
                return False

            for probe_path in probe_paths:
                try:
                    response = requests.get(f"{base_url}{probe_path}", timeout=5)
                    if response.status_code == 404:
                        continue
                    if response.status_code < 400:
                        logging.info("VoxCPM server is online.")
                        return True
                except requests.exceptions.RequestException:
                    continue

            logging.info(
                "VoxCPM server is not online yet. Waiting... (Attempt %s/%s)",
                attempt,
                max_attempts,
            )
            time.sleep(wait_interval)

        logging.error("VoxCPM server failed to come online within the specified attempts.")
        return False

    def run_fishs2_api_server(self, fishs2_server_path, pixi_path=None):
        """Run the FishS2 API server via its upstream launcher script."""
        logging.info(f"Running FishS2 API server from {fishs2_server_path}...")

        if self.is_port_in_use(8020):
            error_msg = "FishS2 server cannot be started because port 8020 is already in use."
            logging.error(error_msg)
            self.notify_error("Error", error_msg)
            return None

        run_script_path = os.path.join(fishs2_server_path, 'run.bat')
        if not os.path.exists(run_script_path):
            raise FileNotFoundError(f"FishS2 run script not found at: {run_script_path}")

        fishs2_log_file = os.path.join(fishs2_server_path, 'fishs2_server.log')
        command = self.build_fishs2_launcher_command(
            pixi_path=self.get_fishs2_pixi_argument(fishs2_server_path, pixi_path),
        )

        log_handle = open(fishs2_log_file, 'a', encoding='utf-8')
        try:
            process = subprocess.Popen(
                command,
                cwd=fishs2_server_path,
                env=self.get_pixi_subprocess_env(os.path.dirname(fishs2_server_path)),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **self.get_hidden_subprocess_kwargs(),
            )
        except Exception:
            log_handle.close()
            raise

        process.log_handle = log_handle
        self.fishs2_process = process
        return process

    def check_fishs2_server_online(self, base_url, max_attempts=120, wait_interval=5, process=None):
        """Check if the FishS2 server is online and responding."""
        probe_paths = ['/health', '/v1/models', '/v1/audio/voices']
        for attempt in range(1, max_attempts + 1):
            if process is not None and process.poll() is not None:
                logging.error("FishS2 server process exited before coming online.")
                return False

            for probe_path in probe_paths:
                try:
                    response = requests.get(f"{base_url}{probe_path}", timeout=5)
                    if response.status_code == 404:
                        continue
                    if response.status_code < 400:
                        logging.info("FishS2 server is online.")
                        return True
                except requests.exceptions.RequestException:
                    continue

            logging.info(
                "FishS2 server is not online yet. Waiting... (Attempt %s/%s)",
                attempt,
                max_attempts,
            )
            time.sleep(wait_interval)

        logging.error("FishS2 server failed to come online within the specified attempts.")
        return False

    def run_chatterbox_api_server(self, chatterbox_server_path, use_cpu=False, pixi_path=None):
        """Run the Chatterbox API server via its cross-platform launcher."""
        logging.info(f"Running Chatterbox API server from {chatterbox_server_path}...")

        if self.is_port_in_use(8040):
            error_msg = "Chatterbox server cannot be started because port 8040 is already in use."
            logging.error(error_msg)
            self.notify_error("Error", error_msg)
            return None

        run_script_name = 'run.bat' if is_windows() else 'run.py'
        run_script_path = os.path.join(chatterbox_server_path, run_script_name)
        if not os.path.exists(run_script_path):
            raise FileNotFoundError(f"Chatterbox run script not found at: {run_script_path}")

        chatterbox_log_file = os.path.join(chatterbox_server_path, 'chatterbox_server.log')
        command = self.build_chatterbox_launcher_command(use_cpu=use_cpu, pixi_path=pixi_path)

        log_handle = open(chatterbox_log_file, 'a', encoding='utf-8')
        try:
            process = subprocess.Popen(
                command,
                cwd=chatterbox_server_path,
                env=self.get_pixi_subprocess_env(os.path.dirname(chatterbox_server_path)),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **self.get_hidden_subprocess_kwargs(),
            )
        except Exception:
            log_handle.close()
            raise

        process.log_handle = log_handle
        self.chatterbox_process = process
        return process

    def run_magpie_api_server(self, magpie_server_path, use_cpu=False, pixi_path=None):
        """Run the Magpie API server via its run.bat script."""
        logging.info(f"Running Magpie API server from {magpie_server_path}...")

        if self.is_port_in_use(8030):
            error_msg = "Magpie server cannot be started because port 8030 is already in use."
            logging.error(error_msg)
            self.notify_error("Error", error_msg)
            return None

        run_script_path = os.path.join(magpie_server_path, 'run.bat')
        if not os.path.exists(run_script_path):
            raise FileNotFoundError(f"Magpie run script not found at: {run_script_path}")

        magpie_log_file = os.path.join(magpie_server_path, 'magpie_server.log')
        command = [run_script_path]
        if pixi_path:
            command.extend(['--pixi-path', pixi_path])

        env = self.get_pixi_subprocess_env(os.path.dirname(magpie_server_path))
        if use_cpu:
            env["MAGPIE_DEVICE"] = "cpu"
        else:
            env["MAGPIE_DEVICE"] = "cuda"

        if pixi_path:
            pixi_bin_dir = os.path.dirname(pixi_path)
            env["PATH"] = pixi_bin_dir + os.pathsep + env.get("PATH", "")

        log_handle = open(magpie_log_file, 'a', encoding='utf-8')
        try:
            process = subprocess.Popen(
                command,
                cwd=magpie_server_path,
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **self.get_hidden_subprocess_kwargs(),
            )
        except Exception:
            log_handle.close()
            raise

        process.log_handle = log_handle
        self.magpie_process = process
        return process

    def check_magpie_server_online(self, base_url, max_attempts=120, wait_interval=5, process=None):
        """Check if the Magpie server is online and responding."""
        probe_paths = ['/v1/models', '/v1/audio/voices']
        for attempt in range(1, max_attempts + 1):
            if process is not None and process.poll() is not None:
                logging.error("Magpie server process exited before coming online.")
                return False

            for probe_path in probe_paths:
                try:
                    response = requests.get(f"{base_url}{probe_path}", timeout=5)
                    if response.status_code == 404:
                        continue
                    if response.status_code < 400:
                        logging.info("Magpie server is online.")
                        return True
                except requests.exceptions.RequestException:
                    continue

            logging.info(
                "Magpie server is not online yet. Waiting... (Attempt %s/%s)",
                attempt,
                max_attempts,
            )
            time.sleep(wait_interval)

        logging.error("Magpie server failed to come online within the specified attempts.")
        return False

    def check_chatterbox_server_online(self, base_url, max_attempts=120, wait_interval=5, process=None):
        """Check if the Chatterbox server is online and responding."""
        probe_paths = ['/v1/models', '/v1/audio/voices']
        for attempt in range(1, max_attempts + 1):
            if process is not None and process.poll() is not None:
                logging.error("Chatterbox server process exited before coming online.")
                return False

            for probe_path in probe_paths:
                try:
                    response = requests.get(f"{base_url}{probe_path}", timeout=5)
                    if response.status_code == 404:
                        continue
                    if response.status_code < 400:
                        logging.info("Chatterbox server is online.")
                        return True
                except requests.exceptions.RequestException:
                    continue

            logging.info(
                "Chatterbox server is not online yet. Waiting... (Attempt %s/%s)",
                attempt,
                max_attempts,
            )
            time.sleep(wait_interval)

        logging.error("Chatterbox server failed to come online within the specified attempts.")
        return False

    def run_voxtral_api_server(self, voxtral_server_path):
        """Run the Voxtral API server via its upstream launcher script."""
        logging.info(f"Running Voxtral API server from {voxtral_server_path}...")

        if self.is_port_in_use(8000):
            error_msg = "Voxtral server cannot be started because port 8000 is already in use."
            logging.error(error_msg)
            self.notify_error("Error", error_msg)
            return None

        run_script_path = os.path.join(voxtral_server_path, 'run.ps1')
        if not os.path.exists(run_script_path):
            raise FileNotFoundError(f"Voxtral run script not found at: {run_script_path}")

        voxtral_log_file = os.path.join(voxtral_server_path, 'voxtral_server.log')
        log_handle = open(voxtral_log_file, 'a', encoding='utf-8')

        command = [
            'powershell',
            '-ExecutionPolicy',
            'Bypass',
            '-File',
            run_script_path,
            '-ProjectRoot',
            voxtral_server_path,
            '-BindHost',
            '127.0.0.1',
            '-Port',
            '8000',
            '-Model',
            'gguf',
        ]

        try:
            process = subprocess.Popen(
                command,
                cwd=voxtral_server_path,
                env=self.get_pixi_subprocess_env(os.path.dirname(voxtral_server_path)),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **self.get_hidden_subprocess_kwargs(),
            )
        except Exception:
            log_handle.close()
            raise

        process.log_handle = log_handle
        self.voxtral_process = process
        return process

    def check_voxtral_server_online(self, url, max_attempts=60, wait_interval=5):
        """Check if the Voxtral server is online and responding."""
        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.get(url, timeout=5)
                if response.status_code == 200:
                    logging.info("Voxtral server is online.")
                    return True
            except requests.exceptions.RequestException:
                pass

            logging.info("Voxtral server is not online. Waiting... (Attempt %s/%s)", attempt, max_attempts)
            time.sleep(wait_interval)

        logging.error("Voxtral server failed to come online within the specified attempts.")
        return False

    def run_silero_api_server(self, pandrator_path, env_name):
        """Run the Silero API server"""
        logging.info(f"Running Silero API server in {env_name}...")

        if self.is_port_in_use(8001):
            error_msg = "Silero server cannot be started because port 8001 is already in use."
            logging.error(error_msg)
            self.notify_error("Error", error_msg)
            return None

        silero_log_file = os.path.join(pandrator_path, 'silero_server.log')
        silero_server_command = self.build_pixi_run_command(
            pandrator_path,
            env_name,
            ['python', '-m', 'silero_api_server']
        )

        log_handle = open(silero_log_file, 'a', encoding='utf-8')
        try:
            process = subprocess.Popen(
                silero_server_command,
                cwd=pandrator_path,
                env=self.get_pixi_subprocess_env(pandrator_path),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **self.get_hidden_subprocess_kwargs(),
            )
        except Exception:
            log_handle.close()
            raise

        process.log_handle = log_handle
        self.silero_process = process
        return process

    def check_silero_server_online(self, url, max_attempts=30, wait_interval=10):
        """Check if the Silero server is online and responding"""
        attempt = 1
        while attempt <= max_attempts:
            try:
                response = requests.get(url)
                if response.status_code == 200:
                    logging.info("Silero server is online.")
                    return True
            except requests.exceptions.RequestException:
                logging.info(f"Silero server is not online. Waiting... (Attempt {attempt}/{max_attempts})")

            time.sleep(wait_interval)
            attempt += 1

        logging.error("Silero server failed to come online within the specified attempts.")
        return False


    def shutdown_apps(self):
        """Shut down all running applications"""
        self.shutdown_voxcpm()
        self.shutdown_fishs2()
        self.shutdown_xtts()
        self.shutdown_voxtral()
        self.shutdown_kokoro()
        self.shutdown_silero()
        self.shutdown_chatterbox()
        self.shutdown_magpie()
        self.shutdown_rvc()

    def shutdown_rvc(self):
        """Shut down the RVC auxiliary service."""
        if not self.rvc_process:
            return

        logging.info("Terminating RVC service process with PID: %s", self.rvc_process.pid)
        self.terminate_process_tree(self.rvc_process)
        self._close_process_log_handle(self.rvc_process)
        self.rvc_process = None

    def shutdown_xtts(self):
        """Shut down the XTTS server"""
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
            if hasattr(self.xtts_process, 'log_handle') and self.xtts_process.log_handle:
                self.xtts_process.log_handle.close()
            self.xtts_process = None

        # Check if any process is using port 8020 and kill it
        seen_pids = set()
        for conn in psutil.net_connections():
            if hasattr(conn, 'laddr') and hasattr(conn.laddr, 'port') and conn.laddr.port == 8020:
                if conn.pid in seen_pids:
                    continue
                seen_pids.add(conn.pid)
                try:
                    if conn.pid in (None, 0):
                        continue
                    process = psutil.Process(conn.pid)
                    if process.pid != 0:  # Skip System Idle Process
                        process.terminate()
                        logging.info(f"Terminated process using port 8020: PID {conn.pid}")
                except psutil.NoSuchProcess:
                    logging.info(f"Process using port 8020 (PID {conn.pid}) no longer exists")
                except psutil.AccessDenied:
                    logging.warning(f"Access denied when terminating process with PID: {conn.pid}")

    def shutdown_voxcpm(self):
        """Shut down the VoxCPM server."""
        if self.voxcpm_process:
            logging.info(f"Terminating VoxCPM process with PID: {self.voxcpm_process.pid}")
            try:
                parent = psutil.Process(self.voxcpm_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.terminate()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when terminating child process with PID: {child.pid}")
                parent.terminate()
                self.voxcpm_process.wait(timeout=10)
            except psutil.NoSuchProcess:
                logging.info("VoxCPM process already terminated.")
            except psutil.TimeoutExpired:
                logging.warning("VoxCPM process did not terminate, forcing kill")
                parent = psutil.Process(self.voxcpm_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when killing child process with PID: {child.pid}")
                parent.kill()

            if hasattr(self.voxcpm_process, 'log_handle') and self.voxcpm_process.log_handle:
                self.voxcpm_process.log_handle.close()
            self.voxcpm_process = None

    def shutdown_fishs2(self):
        """Shut down the FishS2 server."""
        if self.fishs2_process:
            logging.info(f"Terminating FishS2 process with PID: {self.fishs2_process.pid}")
            try:
                parent = psutil.Process(self.fishs2_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.terminate()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when terminating child process with PID: {child.pid}")
                parent.terminate()
                self.fishs2_process.wait(timeout=10)
            except psutil.NoSuchProcess:
                logging.info("FishS2 process already terminated.")
            except psutil.TimeoutExpired:
                logging.warning("FishS2 process did not terminate, forcing kill")
                parent = psutil.Process(self.fishs2_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when killing child process with PID: {child.pid}")
                parent.kill()

            if hasattr(self.fishs2_process, 'log_handle') and self.fishs2_process.log_handle:
                self.fishs2_process.log_handle.close()
            self.fishs2_process = None

    def shutdown_chatterbox(self):
        """Shut down the Chatterbox server."""
        if self.chatterbox_process:
            logging.info(f"Terminating Chatterbox process with PID: {self.chatterbox_process.pid}")
            try:
                parent = psutil.Process(self.chatterbox_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.terminate()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when terminating child process with PID: {child.pid}")
                parent.terminate()
                self.chatterbox_process.wait(timeout=10)
            except psutil.NoSuchProcess:
                logging.info("Chatterbox process already terminated.")
            except psutil.TimeoutExpired:
                logging.warning("Chatterbox process did not terminate, forcing kill")
                parent = psutil.Process(self.chatterbox_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when killing child process with PID: {child.pid}")
                parent.kill()

            if hasattr(self.chatterbox_process, 'log_handle') and self.chatterbox_process.log_handle:
                self.chatterbox_process.log_handle.close()
            self.chatterbox_process = None

    def shutdown_magpie(self):
        """Shut down the Magpie server."""
        if self.magpie_process:
            logging.info(f"Terminating Magpie process with PID: {self.magpie_process.pid}")
            try:
                parent = psutil.Process(self.magpie_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.terminate()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when terminating child process with PID: {child.pid}")
                parent.terminate()
                self.magpie_process.wait(timeout=10)
            except psutil.NoSuchProcess:
                logging.info("Magpie process already terminated.")
            except psutil.TimeoutExpired:
                logging.warning("Magpie process did not terminate, forcing kill")
                parent = psutil.Process(self.magpie_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when killing child process with PID: {child.pid}")
                parent.kill()

            if hasattr(self.magpie_process, 'log_handle') and self.magpie_process.log_handle:
                self.magpie_process.log_handle.close()
            self.magpie_process = None

    def shutdown_voxtral(self):
        """Shut down the Voxtral server"""
        if self.voxtral_process:
            logging.info(f"Terminating Voxtral process with PID: {self.voxtral_process.pid}")
            try:
                parent = psutil.Process(self.voxtral_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.terminate()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when terminating child process with PID: {child.pid}")
                parent.terminate()
                self.voxtral_process.wait(timeout=10)
            except psutil.NoSuchProcess:
                logging.info("Voxtral process already terminated.")
            except psutil.TimeoutExpired:
                logging.warning("Voxtral process did not terminate, forcing kill")
                parent = psutil.Process(self.voxtral_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when killing child process with PID: {child.pid}")
                parent.kill()
            if hasattr(self.voxtral_process, 'log_handle') and self.voxtral_process.log_handle:
                self.voxtral_process.log_handle.close()
            self.voxtral_process = None

        # Check if any process is using port 8000 and kill it
        for conn in psutil.net_connections():
            if hasattr(conn, 'laddr') and hasattr(conn.laddr, 'port') and conn.laddr.port == 8000:
                try:
                    process = psutil.Process(conn.pid)
                    if process.pid != 0:  # Skip System Idle Process
                        process.terminate()
                        logging.info(f"Terminated process using port 8000: PID {conn.pid}")
                except psutil.NoSuchProcess:
                    logging.info(f"Process using port 8000 (PID {conn.pid}) no longer exists")
                except psutil.AccessDenied:
                    logging.warning(f"Access denied when terminating process with PID: {conn.pid}")

    def shutdown_kokoro(self):
        """Shut down the Kokoro server"""
        if self.kokoro_process:
            logging.info(f"Terminating Kokoro process with PID: {self.kokoro_process.pid}")
            try:
                parent = psutil.Process(self.kokoro_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.terminate()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when terminating child process with PID: {child.pid}")
                parent.terminate()
                self.kokoro_process.wait(timeout=10)
            except psutil.NoSuchProcess:
                logging.info("Kokoro process already terminated.")
            except psutil.TimeoutExpired:
                logging.warning("Kokoro process did not terminate, forcing kill")
                parent = psutil.Process(self.kokoro_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when killing child process with PID: {child.pid}")
                parent.kill()
            if hasattr(self.kokoro_process, 'log_handle') and self.kokoro_process.log_handle:
                self.kokoro_process.log_handle.close()
            self.kokoro_process = None

        # Check if any process is using port 8880 and kill it
        for conn in psutil.net_connections():
            if hasattr(conn, 'laddr') and hasattr(conn.laddr, 'port') and conn.laddr.port == 8880:
                try:
                    process = psutil.Process(conn.pid)
                    if process.pid != 0:  # Skip System Idle Process
                        process.terminate()
                        logging.info(f"Terminated process using port 8880: PID {conn.pid}")
                except psutil.NoSuchProcess:
                    logging.info(f"Process using port 8880 (PID {conn.pid}) no longer exists")
                except psutil.AccessDenied:
                    logging.warning(f"Access denied when terminating process with PID: {conn.pid}")

    def shutdown_silero(self):
        """Shut down the Silero server"""
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
            if hasattr(self.silero_process, 'log_handle') and self.silero_process.log_handle:
                self.silero_process.log_handle.close()
            self.silero_process = None

        # Check if any process is using port 8001 and kill it
        for conn in psutil.net_connections():
            if hasattr(conn, 'laddr') and hasattr(conn.laddr, 'port') and conn.laddr.port == 8001:
                try:
                    process = psutil.Process(conn.pid)
                    if process.pid != 0:  # Skip System Idle Process
                        process.terminate()
                        logging.info(f"Terminated process using port 8001: PID {conn.pid}")
                except psutil.NoSuchProcess:
                    logging.info(f"Process using port 8001 (PID {conn.pid}) no longer exists")
                except psutil.AccessDenied:
                    logging.warning(f"Access denied when terminating process with PID: {conn.pid}")
