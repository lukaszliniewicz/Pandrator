"""Install and update orchestration workflows."""

import concurrent.futures
import logging
import os
import traceback


try:
    from packaging.specifiers import SpecifierSet as PackagingSpecifierSet
except ImportError:
    PackagingSpecifierSet = None

from .catalog import (
    COMPONENTS,
    LINUX_DEFERRED_INSTALL_COMPONENT_KEYS,
    LINUX_DEFERRED_REASON_BY_COMPONENT,
)
from .constants import (
    CHATTERBOX_API_REPO_DIRNAME,
    CHATTERBOX_API_REPO_URL,
    CHATTERBOX_GPU_SUPPORT_CONFIG_FLAG,
    KOBOLD_QWEN_API_REPO_DIRNAME,
    KOBOLD_QWEN_API_REPO_URL,
    KOBOLD_QWEN_GPU_SUPPORT_CONFIG_FLAG,
    MAGPIE_API_REPO_DIRNAME,
    MAGPIE_API_REPO_URL,
    MAGPIE_GPU_SUPPORT_CONFIG_FLAG,
    EASY_XTTS_TRAINER_REPO_URL,
    FISHS2_API_REPO_DIRNAME,
    FISHS2_API_REPO_URL,
    KOKORO_API_REPO_DIRNAME,
    KOKORO_API_REPO_URL,
    KOKORO_ENV_NAME,
    KOKORO_GPU_SUPPORT_CONFIG_FLAG,
    KOKORO_PYTHON_VERSION,
    NEMO_PYNINI_CONDA_SPEC,
    PANDRATOR_URL_DOWNLOADER_CONDA_SPEC,
    PANDRATOR_PYTHON_VERSION,
    PANDRATOR_REPO_BRANCH,
    PANDRATOR_REPO_URL,
    RVC_API_REPO_DIRNAME,
    RVC_API_REPO_URL,
    RVC_GPU_SUPPORT_CONFIG_FLAG,
    SILERO_API_REPO_DIRNAME,
    SILERO_API_REPO_URL,
    VOXCPM_API_REPO_DIRNAME,
    VOXCPM_API_REPO_URL,
    VOXTRAL_API_REPO_DIRNAME,
    VOXTRAL_API_REPO_URL,
    WHISPERX_PYTHON_VERSION,
    WHISPERX_REQUIRED_PACKAGE_SPECS,
    XTTS_API_REPO_DIRNAME,
    XTTS_API_REPO_URL,
    XTTS_FINETUNING_PYTHON_VERSION,
    XTTS_FINETUNING_TORCH_PACKAGE_SPECS,
)
from .models import InstallSelection, qwen_effective_model_size, qwen_model_variants
from .platforms import is_windows
from .reporting import HeadlessReporter, NullReporter


class WorkflowMixin:
    def ensure_pandrator_environment_conda_packages(self, pandrator_path):
        """Install application command-line tools inside the portable Pixi environment."""
        for package_spec in (
            'ffmpeg',
            PANDRATOR_URL_DOWNLOADER_CONDA_SPEC,
            NEMO_PYNINI_CONDA_SPEC,
        ):
            self.add_pixi_conda_package(
                pandrator_path,
                'pandrator_installer',
                package_spec,
            )

    def validate_platform_install_selection(self, selection):
        if is_windows():
            return

        deferred_components = [
            key
            for key in selection.selected_components()
            if key in LINUX_DEFERRED_INSTALL_COMPONENT_KEYS
        ]
        if not deferred_components:
            return

        labels = '; '.join(
            f"{COMPONENTS[key].label}: "
            f"{LINUX_DEFERRED_REASON_BY_COMPONENT.get(COMPONENTS[key].packaging_key, 'pending qualification')}"
            for key in deferred_components
            if key in COMPONENTS
        )
        raise RuntimeError(
            "The selected component is not currently available on Linux. "
            f"{labels}."
        )

    def validate_platform_update_config(self, config):
        if is_windows():
            return

        deferred_labels = []
        seen_config_flags = set()
        for key in LINUX_DEFERRED_INSTALL_COMPONENT_KEYS:
            component = COMPONENTS.get(key)
            if component is None or component.config_flag in seen_config_flags:
                continue
            seen_config_flags.add(component.config_flag)
            if config.get(component.config_flag, False):
                reason = LINUX_DEFERRED_REASON_BY_COMPONENT.get(
                    component.packaging_key,
                    'pending qualification',
                )
                deferred_labels.append(f"{component.label}: {reason}")

        if not deferred_labels:
            return

        raise RuntimeError(
            "The installed component cannot currently be updated on Linux. "
            f"{'; '.join(deferred_labels)}."
        )



    def run_headless_install(
        self,
        components,
        install_pandrator=True,
        crispasr_backend="auto",
        crispasr_engine="whisper-large-v3",
        crispasr_model_quantization="f16",
        kobold_qwen_backend="auto",
        kobold_qwen_model_size="0.6b",
        kobold_qwen_quantization="f16",
        kobold_qwen_initial_model="base",
    ):
        selection = InstallSelection.from_components(
            components,
            install_pandrator=install_pandrator,
            crispasr_backend=crispasr_backend,
            crispasr_engine=crispasr_engine,
            crispasr_model_quantization=crispasr_model_quantization,
            kobold_qwen_backend=kobold_qwen_backend,
            kobold_qwen_model_size=kobold_qwen_model_size,
            kobold_qwen_quantization=kobold_qwen_quantization,
            kobold_qwen_initial_model=kobold_qwen_initial_model,
        )
        selected_components = set(selection.selected_components())

        selected_label = ', '.join(sorted(selected_components)) if selected_components else 'none'
        logging.info(
            "Starting headless installation in %s with components: %s",
            self.initial_working_dir,
            selected_label,
        )

        self.initialize_logging()
        self.reporter = HeadlessReporter()

        try:
            self.install_process(selection)
        finally:
            self.reporter = NullReporter()
            self.shutdown_logging()
            if hasattr(self, "refresh_ui_state"):
                self.refresh_ui_state()

        logging.info("Headless installation completed successfully.")


    def install_process(self, selection=None):
        """Main installation process - runs in a worker thread"""
        selection = selection or self.snapshot_install_selection()
        selection.validate()
        self.validate_platform_install_selection(selection)
        pandrator_var = selection.pandrator
        xtts_var = selection.xtts
        xtts_cpu_var = selection.xtts_cpu
        voxcpm_var = selection.voxcpm
        fishs2_var = selection.fishs2
        fishs2_cpu_var = selection.fishs2_cpu
        silero_var = selection.silero
        voxtral_var = selection.voxtral
        kokoro_var = selection.kokoro
        kokoro_cpu_var = selection.kokoro_cpu
        rvc_var = selection.rvc
        rvc_cpu_var = selection.rvc_cpu
        crispasr_var = selection.crispasr
        xtts_finetuning_var = selection.xtts_finetuning
        chatterbox_var = selection.chatterbox
        chatterbox_cpu_var = selection.chatterbox_cpu
        kobold_qwen_var = selection.kobold_qwen
        kobold_qwen_cpu_var = selection.kobold_qwen_cpu
        magpie_var = selection.magpie
        magpie_cpu_var = selection.magpie_cpu

        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        pandrator_already_installed = os.path.exists(pandrator_path)
        pandrator_repo_path = os.path.join(pandrator_path, 'Pandrator')
        xtts_repo_path = os.path.join(pandrator_path, XTTS_API_REPO_DIRNAME)
        voxcpm_repo_path = os.path.join(pandrator_path, VOXCPM_API_REPO_DIRNAME)
        fishs2_repo_path = os.path.join(pandrator_path, FISHS2_API_REPO_DIRNAME)
        voxtral_repo_path = os.path.join(pandrator_path, VOXTRAL_API_REPO_DIRNAME)
        kokoro_repo_path = os.path.join(pandrator_path, KOKORO_API_REPO_DIRNAME)
        easy_xtts_trainer_path = os.path.join(pandrator_path, 'easy_xtts_trainer')
        chatterbox_repo_path = os.path.join(pandrator_path, CHATTERBOX_API_REPO_DIRNAME)
        kobold_qwen_repo_path = os.path.join(pandrator_path, KOBOLD_QWEN_API_REPO_DIRNAME)
        magpie_repo_path = os.path.join(pandrator_path, MAGPIE_API_REPO_DIRNAME)
        silero_repo_path = os.path.join(pandrator_path, SILERO_API_REPO_DIRNAME)
        rvc_repo_path = os.path.join(pandrator_path, RVC_API_REPO_DIRNAME)

        pandrator_repo_missing = not os.path.exists(pandrator_repo_path)
        pandrator_env_missing = not os.path.exists(self.get_pixi_manifest_path(pandrator_path, 'pandrator_installer'))
        needs_pandrator_environment = pandrator_var or not pandrator_already_installed or pandrator_env_missing

        # Check admin status
        is_admin = self.is_admin()
        if is_windows() and not is_admin:
            logging.warning("Running installer without admin privileges - some features may not work correctly")

        try:
            self.configure_tls_certificates()

            # Create Pandrator directory if it doesn't exist
            if not pandrator_already_installed:
                os.makedirs(pandrator_path, exist_ok=True)
                if is_admin:
                    self.set_permissive_permissions(pandrator_path)
                  # Phase 1: Concurrently download dependencies and clone top-level repos
            concurrent_tasks = {}

            # Calibre installation task
            def install_calibre_task():
                try:
                    if not is_admin:
                        logging.info("[Calibre Setup] Checking for Calibre...")
                    dependencies_ok = self.install_dependencies(
                        pandrator_path,
                        allow_system_install=is_admin,
                    )
                    if not dependencies_ok:
                        logging.warning(
                            "[Calibre Setup] Calibre is unavailable. MOBI import will require manual setup."
                        )
                except Exception as e:
                    logging.error(f"[Calibre Setup] Error during dependency installation: {str(e)}")
                    self.show_calibre_installation_message()

            concurrent_tasks["Calibre Setup"] = install_calibre_task

            # Pixi installation task
            def install_pixi_task():
                if not self.check_pixi(pandrator_path):
                    self.install_pixi(pandrator_path)
                    if is_admin:
                        self.set_permissive_permissions(os.path.join(pandrator_path, 'bin'))

            concurrent_tasks["Pixi Setup"] = install_pixi_task

            # FFmpeg installation task
            def install_ffmpeg_task():
                if not self.ensure_bundled_ffmpeg_with_subtitles(pandrator_path):
                    logging.warning(
                        "[FFmpeg Setup] Bundled FFmpeg with subtitle burning support could not be prepared. "
                        "Soft subtitles will still work."
                    )

            concurrent_tasks["FFmpeg Setup"] = install_ffmpeg_task

            # Git repositories tasks
            if pandrator_var or not pandrator_already_installed or pandrator_repo_missing:
                concurrent_tasks["Clone Pandrator"] = (
                    self.clone_repo,
                    (PANDRATOR_REPO_URL, pandrator_repo_path),
                    {'branch': PANDRATOR_REPO_BRANCH},
                )
            if (xtts_var or xtts_cpu_var) and not os.path.exists(xtts_repo_path):
                concurrent_tasks["Clone XTTS"] = (self.clone_repo, (XTTS_API_REPO_URL, xtts_repo_path), {})
            if voxcpm_var and not os.path.exists(voxcpm_repo_path):
                concurrent_tasks["Clone VoxCPM"] = (self.clone_repo, (VOXCPM_API_REPO_URL, voxcpm_repo_path), {})
            if fishs2_var and not os.path.exists(fishs2_repo_path):
                concurrent_tasks["Clone FishS2"] = (self.clone_repo, (FISHS2_API_REPO_URL, fishs2_repo_path), {})
            if voxtral_var and not os.path.exists(voxtral_repo_path):
                concurrent_tasks["Clone Voxtral"] = (self.clone_repo, (VOXTRAL_API_REPO_URL, voxtral_repo_path), {})
            if (kokoro_var or kokoro_cpu_var) and not os.path.exists(kokoro_repo_path):
                concurrent_tasks["Clone Kokoro"] = (self.clone_repo, (KOKORO_API_REPO_URL, kokoro_repo_path), {})
            if xtts_finetuning_var and not os.path.exists(easy_xtts_trainer_path):
                concurrent_tasks["Clone XTTS Trainer"] = (self.clone_repo, (EASY_XTTS_TRAINER_REPO_URL, easy_xtts_trainer_path), {})
            if (chatterbox_var or chatterbox_cpu_var) and not os.path.exists(chatterbox_repo_path):
                concurrent_tasks["Clone Chatterbox"] = (self.clone_repo, (CHATTERBOX_API_REPO_URL, chatterbox_repo_path), {})
            if (kobold_qwen_var or kobold_qwen_cpu_var) and not os.path.exists(kobold_qwen_repo_path):
                concurrent_tasks["Clone Qwen3 TTS"] = (self.clone_repo, (KOBOLD_QWEN_API_REPO_URL, kobold_qwen_repo_path), {})
            if (magpie_var or magpie_cpu_var) and not os.path.exists(magpie_repo_path):
                concurrent_tasks["Clone Magpie"] = (self.clone_repo, (MAGPIE_API_REPO_URL, magpie_repo_path), {})
            if silero_var and not os.path.exists(silero_repo_path):
                concurrent_tasks["Clone Silero"] = (self.clone_repo, (SILERO_API_REPO_URL, silero_repo_path), {})
            if (rvc_var or rvc_cpu_var) and not os.path.exists(rvc_repo_path):
                concurrent_tasks["Clone RVC"] = (self.clone_repo, (RVC_API_REPO_URL, rvc_repo_path), {})

            self.execute_concurrently(concurrent_tasks, max_workers=8)

            if not self.check_pixi(pandrator_path):
                self.reporter.status("Pixi installation failed")
                logging.error("Pixi installation failed")
                return

            shared_pixi_path = self.get_pixi_executable(pandrator_path)

            if needs_pandrator_environment:
                self.reporter.progress(0.6)
                self.reporter.status("Creating Pandrator Pixi environment...")
                self.create_pixi_env(pandrator_path, 'pandrator_installer', PANDRATOR_PYTHON_VERSION)
                self.ensure_pandrator_environment_conda_packages(pandrator_path)

                self.reporter.progress(0.7)
                self.reporter.status("Installing Pandrator dependencies...")
                self.install_requirements(pandrator_path, 'pandrator_installer', os.path.join(pandrator_repo_path, 'requirements.txt'))
                self.ensure_nemo_text_processing_runtime(pandrator_path)
                self.ensure_wtpsplit_runtime(pandrator_path)
                self.ensure_pdf_ocr_runtime(pandrator_path)

            # Bootstrapping
            kokoro_bootstrap_future = None
            bootstrap_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

            if kokoro_var or kokoro_cpu_var:
                self.reporter.progress(0.85)
                self.reporter.status("Creating Kokoro Pixi environment...")
                self.create_pixi_env(pandrator_path, KOKORO_ENV_NAME, KOKORO_PYTHON_VERSION)

                def kokoro_bootstrap_task():
                    logging.info("[Bootstrap: Kokoro] Starting Kokoro API server bootstrap in background...")
                    self.install_kokoro_api_server(
                        pandrator_path,
                        kokoro_repo_path,
                        env_name=KOKORO_ENV_NAME,
                        use_gpu=kokoro_var,
                    )
                    logging.info("[Bootstrap: Kokoro] Kokoro API server bootstrap completed.")

                kokoro_bootstrap_future = bootstrap_executor.submit(kokoro_bootstrap_task)

            if xtts_var or xtts_cpu_var:
                self.reporter.progress(0.8)
                self.reporter.status("Bootstrapping XTTS2 API server (temporary startup)...")
                self.install_xtts_api_server(
                    xtts_repo_path,
                    use_cpu=xtts_cpu_var,
                    pixi_path=shared_pixi_path,
                )

            if voxcpm_var:
                self.reporter.progress(0.85)
                self.reporter.status("Bootstrapping VoxCPM API server (temporary startup)...")
                self.install_voxcpm_api_server(
                    voxcpm_repo_path,
                    pixi_path=shared_pixi_path,
                )

            if fishs2_var or fishs2_cpu_var:
                self.reporter.progress(0.88)
                self.reporter.status("Bootstrapping FishS2 API server (temporary startup)...")
                
                backend = selection.fishs2_backend if fishs2_var else 'cpu'
                model_quant = selection.fishs2_model_quant
                
                self.install_fishs2_api_server(
                    fishs2_repo_path,
                    backend=backend,
                    model_quant=model_quant,
                    pixi_path=shared_pixi_path,
                )

            if chatterbox_var or chatterbox_cpu_var:
                self.reporter.progress(0.89)
                self.reporter.status("Bootstrapping Chatterbox API server (temporary startup)...")
                self.install_chatterbox_api_server(
                    chatterbox_repo_path,
                    use_cpu=chatterbox_cpu_var,
                    pixi_path=shared_pixi_path,
                )

            if kobold_qwen_var or kobold_qwen_cpu_var:
                self.reporter.progress(0.89)
                qwen_model_size = qwen_effective_model_size(
                    selection.kobold_qwen_initial_model,
                    selection.kobold_qwen_model_size,
                )
                requested_qwen_variants = qwen_model_variants(
                    selection.kobold_qwen_initial_model
                )
                logging.info(
                    "Qwen3 TTS install selection: models=%s, size=%s, quantization=%s, backend=%s",
                    ",".join(requested_qwen_variants),
                    qwen_model_size,
                    selection.kobold_qwen_quantization,
                    "cpu" if kobold_qwen_cpu_var else selection.kobold_qwen_backend,
                )
                for model_variant in requested_qwen_variants:
                    self.reporter.status(
                        f"Bootstrapping Qwen3 TTS {model_variant} model (temporary startup)..."
                    )
                    self.install_kobold_qwen_api_server(
                        kobold_qwen_repo_path,
                        backend="cpu" if kobold_qwen_cpu_var else selection.kobold_qwen_backend,
                        model_size=qwen_model_size,
                        quantization=selection.kobold_qwen_quantization,
                        initial_model=model_variant,
                        pixi_path=shared_pixi_path,
                    )

            if magpie_var or magpie_cpu_var:
                self.reporter.progress(0.89)
                self.reporter.status("Bootstrapping Magpie API server (temporary startup)...")
                self.install_magpie_api_server(
                    magpie_repo_path,
                    use_cpu=magpie_cpu_var,
                    pixi_path=shared_pixi_path,
                )

            if silero_var:
                self.reporter.progress(0.8)
                self.reporter.status("Installing Silero and its modern CIS voice pack...")
                self.install_silero_api_server(
                    silero_repo_path,
                    pandrator_path=pandrator_path,
                    pixi_path=shared_pixi_path,
                )

            if voxtral_var:
                self.reporter.progress(0.9)
                self.reporter.status("Bootstrapping Voxtral API server...")
                self.install_voxtral_api_server(voxtral_repo_path)

            if rvc_var or rvc_cpu_var:
                self.reporter.progress(0.8)
                self.reporter.status("Preparing RVC service...")
                self.install_rvc_api_server(
                    rvc_repo_path,
                    use_cpu=rvc_cpu_var,
                    pixi_path=shared_pixi_path,
                )
                self.remove_legacy_rvc_from_pandrator_env(pandrator_path)

            crispasr_install = None
            if crispasr_var:
                self.reporter.progress(0.88)
                self.reporter.status("Installing CrispASR transcription runtime...")
                crispasr_install = self.install_crispasr(
                    pandrator_path,
                    requested_backend=selection.crispasr_backend,
                )

            if xtts_finetuning_var:
                # easy_xtts_trainer still invokes WhisperX internally. This is
                # a private trainer dependency, not a Pandrator STT backend.
                self.reporter.status("Preparing XTTS trainer transcription dependency...")
                self.create_pixi_env(pandrator_path, 'whisperx_installer', WHISPERX_PYTHON_VERSION)
                self.install_whisperx(pandrator_path, 'whisperx_installer')
                self.reporter.progress(0.85)
                self.reporter.status("Cloning XTTS Fine-tuning repository...")
                self.clone_repo(EASY_XTTS_TRAINER_REPO_URL, easy_xtts_trainer_path)

                self.reporter.progress(0.90)
                self.reporter.status("Creating XTTS Fine-tuning Pixi environment...")
                self.create_pixi_env(pandrator_path, 'easy_xtts_trainer', XTTS_FINETUNING_PYTHON_VERSION)
                self.add_pixi_conda_package(pandrator_path, 'easy_xtts_trainer', 'ffmpeg')

                self.reporter.progress(0.95)
                self.reporter.status("Installing XTTS Fine-tuning requirements...")
                self.install_requirements(pandrator_path, 'easy_xtts_trainer', os.path.join(easy_xtts_trainer_path, 'requirements.txt'))

                self.reporter.status("Installing XTTS fine-tuning bundled wheel...")
                self.install_xtts_finetuning_bundled_wheel(
                    pandrator_path,
                    'easy_xtts_trainer',
                    easy_xtts_trainer_path,
                )

                self.reporter.status("Installing PyTorch for XTTS Fine-tuning...")
                self.install_pytorch_for_xtts_finetuning(pandrator_path, 'easy_xtts_trainer')

            if kokoro_bootstrap_future is not None:
                self.reporter.status("Waiting for Kokoro API server bootstrap to complete...")
                kokoro_bootstrap_future.result()
                bootstrap_executor.shutdown()

            # Create or update config file
            config = self.load_install_config(pandrator_path)

            # Update config based on what was installed or already exists
            config['cuda_support'] = config.get('cuda_support', False) or xtts_var
            config['xtts_support'] = config.get('xtts_support', False) or xtts_var or xtts_cpu_var
            config['voxcpm_support'] = config.get('voxcpm_support', False) or voxcpm_var
            config['fishs2_support'] = config.get('fishs2_support', False) or fishs2_var or fishs2_cpu_var
            config['fishs2_gpu_support'] = config.get('fishs2_gpu_support', False) or fishs2_var
            if fishs2_var or fishs2_cpu_var:
                config['fishs2_backend'] = getattr(selection, 'fishs2_backend', 'auto')
                config['fishs2_model_quant'] = getattr(selection, 'fishs2_model_quant', 'q6_k')
            config['silero_support'] = config.get('silero_support', False) or silero_var
            config['voxtral_support'] = config.get('voxtral_support', False) or voxtral_var
            config['kokoro_support'] = config.get('kokoro_support', False) or kokoro_var or kokoro_cpu_var
            config[KOKORO_GPU_SUPPORT_CONFIG_FLAG] = (
                config.get(KOKORO_GPU_SUPPORT_CONFIG_FLAG, False) or kokoro_var
            )
            config['crispasr_support'] = config.get('crispasr_support', False) or crispasr_var
            if crispasr_install:
                config['crispasr_backend'] = crispasr_install['requested_backend']
                config['crispasr_runtime_variant'] = crispasr_install['runtime_variant']
                config['crispasr_compiled_backends'] = crispasr_install['compiled_backends']
            # Retire legacy user-facing STT flags after successful migration.
            if config['crispasr_support']:
                config['whisperx_support'] = False
                config['parakeet_onnx_support'] = False
            config['xtts_finetuning_support'] = config.get('xtts_finetuning_support', False) or xtts_finetuning_var
            config['rvc_support'] = config.get('rvc_support', False) or rvc_var or rvc_cpu_var
            config[RVC_GPU_SUPPORT_CONFIG_FLAG] = (
                config.get(RVC_GPU_SUPPORT_CONFIG_FLAG, False) or rvc_var
            )
            config['chatterbox_support'] = config.get('chatterbox_support', False) or chatterbox_var or chatterbox_cpu_var
            config[CHATTERBOX_GPU_SUPPORT_CONFIG_FLAG] = (
                config.get(CHATTERBOX_GPU_SUPPORT_CONFIG_FLAG, False) or chatterbox_var
            )
            config['kobold_qwen_support'] = config.get('kobold_qwen_support', False) or kobold_qwen_var or kobold_qwen_cpu_var
            config[KOBOLD_QWEN_GPU_SUPPORT_CONFIG_FLAG] = (
                config.get(KOBOLD_QWEN_GPU_SUPPORT_CONFIG_FLAG, False) or kobold_qwen_var
            )
            if kobold_qwen_var or kobold_qwen_cpu_var:
                config['kobold_qwen_backend'] = "cpu" if kobold_qwen_cpu_var else selection.kobold_qwen_backend
                config['kobold_qwen_model_size'] = qwen_effective_model_size(
                    selection.kobold_qwen_initial_model,
                    selection.kobold_qwen_model_size,
                )
                config['kobold_qwen_quantization'] = selection.kobold_qwen_quantization
                requested_variants = list(qwen_model_variants(selection.kobold_qwen_initial_model))
                installed_variants = list(config.get('kobold_qwen_installed_models') or [])
                for model_variant in requested_variants:
                    if model_variant not in installed_variants:
                        installed_variants.append(model_variant)
                config['kobold_qwen_installed_models'] = installed_variants
                config['kobold_qwen_model_selection'] = selection.kobold_qwen_initial_model
                config['kobold_qwen_initial_model'] = requested_variants[0]
            if crispasr_var:
                config['crispasr_backend'] = selection.crispasr_backend
                config['crispasr_engine'] = selection.crispasr_engine
                config['crispasr_model_quantization'] = selection.crispasr_model_quantization
            config['magpie_support'] = config.get('magpie_support', False) or magpie_var or magpie_cpu_var
            config[MAGPIE_GPU_SUPPORT_CONFIG_FLAG] = (
                config.get(MAGPIE_GPU_SUPPORT_CONFIG_FLAG, False) or magpie_var
            )

            self.save_install_config(pandrator_path, config)

            self.write_packaging_layout(pandrator_path)

            self.reporter.status("Cleaning installer package caches...")
            self.cleanup_installer_package_caches(pandrator_path)

            # Set final permissions if admin
            if is_admin:
                self.reporter.progress(0.98)
                self.reporter.status("Finalizing permissions...")
                self.set_permissive_permissions(pandrator_path)

            self.reporter.progress(1.0)
            self.reporter.status("Installation complete!")
            logging.info("Installation completed successfully.")

        except Exception as e:
            logging.error(f"Installation failed: {str(e)}")
            logging.error(traceback.format_exc())
            self.reporter.status("Installation failed. Check the log for details.")
            raise


    def update_process(self, stop_running_processes=False):
        """Main update process - runs in a worker thread"""
        pandrator_base_path = os.path.join(self.initial_working_dir, 'Pandrator')
        pandrator_repo_path = os.path.join(pandrator_base_path, 'Pandrator')
        xtts_repo_path = os.path.join(pandrator_base_path, XTTS_API_REPO_DIRNAME)
        voxcpm_repo_path = os.path.join(pandrator_base_path, VOXCPM_API_REPO_DIRNAME)
        fishs2_repo_path = os.path.join(pandrator_base_path, FISHS2_API_REPO_DIRNAME)
        voxtral_repo_path = os.path.join(pandrator_base_path, VOXTRAL_API_REPO_DIRNAME)
        kokoro_repo_path = os.path.join(pandrator_base_path, KOKORO_API_REPO_DIRNAME)
        easy_xtts_trainer_path = os.path.join(pandrator_base_path, 'easy_xtts_trainer')
        chatterbox_repo_path = os.path.join(pandrator_base_path, CHATTERBOX_API_REPO_DIRNAME)
        kobold_qwen_repo_path = os.path.join(pandrator_base_path, KOBOLD_QWEN_API_REPO_DIRNAME)
        magpie_repo_path = os.path.join(pandrator_base_path, MAGPIE_API_REPO_DIRNAME)
        silero_repo_path = os.path.join(pandrator_base_path, SILERO_API_REPO_DIRNAME)
        rvc_repo_path = os.path.join(pandrator_base_path, RVC_API_REPO_DIRNAME)
        if stop_running_processes:
            self.reporter.status("Stopping Pandrator and running services before the update...")
            stopped = self.stop_running_installation_processes(pandrator_base_path)
            logging.info("Stopped %d installation process(es) before update.", len(stopped))
        self.ensure_update_runtime_stopped(pandrator_base_path)
        config = self.load_install_config(pandrator_base_path, detect_rvc=True)
        if KOKORO_GPU_SUPPORT_CONFIG_FLAG not in config:
            config[KOKORO_GPU_SUPPORT_CONFIG_FLAG] = False
            self.save_install_config(pandrator_base_path, config)
        if KOBOLD_QWEN_GPU_SUPPORT_CONFIG_FLAG not in config:
            config[KOBOLD_QWEN_GPU_SUPPORT_CONFIG_FLAG] = False
            self.save_install_config(pandrator_base_path, config)
        self.validate_platform_update_config(config)

        pandrator_env_missing = not os.path.exists(self.get_pixi_manifest_path(pandrator_base_path, 'pandrator_installer'))
        kokoro_env_missing = not os.path.exists(self.get_pixi_manifest_path(pandrator_base_path, KOKORO_ENV_NAME))

        # Check admin status
        is_admin = self.is_admin()

        try:
            self.configure_tls_certificates()

            # Phase 1: Concurrently pull/clone all top-level repos and check calibre/ffmpeg/pixi
            update_tasks = {}

            # Calibre task
            def update_calibre_task():
                self.install_dependencies(pandrator_base_path, allow_system_install=is_admin)

            update_tasks["Calibre Check"] = update_calibre_task

            # Pixi task
            def update_pixi_task():
                if not self.check_pixi(pandrator_base_path):
                    self.install_pixi(pandrator_base_path)
                    if is_admin:
                        self.set_permissive_permissions(os.path.join(pandrator_base_path, 'bin'))

            update_tasks["Pixi Check"] = update_pixi_task

            # FFmpeg task
            def update_ffmpeg_task():
                if not self.ensure_bundled_ffmpeg_with_subtitles(pandrator_base_path):
                    logging.warning(
                        "[FFmpeg Check] Bundled FFmpeg with subtitle burning support could not be prepared during update. "
                        "Soft subtitles will still work."
                    )

            update_tasks["FFmpeg Check"] = update_ffmpeg_task

            # Git repos tasks
            # Pandrator
            update_tasks["Update Pandrator"] = (self.pull_repo, (pandrator_repo_path,), {})

            # XTTS
            if config.get('xtts_support', False):
                if os.path.exists(xtts_repo_path):
                    update_tasks["Update XTTS"] = (self.pull_repo, (xtts_repo_path,), {})
                else:
                    update_tasks["Clone XTTS"] = (self.clone_repo, (XTTS_API_REPO_URL, xtts_repo_path), {})

            # VoxCPM
            if config.get('voxcpm_support', False):
                if os.path.exists(voxcpm_repo_path):
                    update_tasks["Update VoxCPM"] = (self.pull_repo, (voxcpm_repo_path,), {})
                else:
                    update_tasks["Clone VoxCPM"] = (self.clone_repo, (VOXCPM_API_REPO_URL, voxcpm_repo_path), {})

            # FishS2
            if config.get('fishs2_support', False):
                if os.path.exists(fishs2_repo_path):
                    update_tasks["Update FishS2"] = (self.pull_repo, (fishs2_repo_path,), {})
                else:
                    update_tasks["Clone FishS2"] = (self.clone_repo, (FISHS2_API_REPO_URL, fishs2_repo_path), {})

            # Voxtral
            if config.get('voxtral_support', False):
                if os.path.exists(voxtral_repo_path):
                    update_tasks["Update Voxtral"] = (self.pull_repo, (voxtral_repo_path,), {})
                else:
                    update_tasks["Clone Voxtral"] = (self.clone_repo, (VOXTRAL_API_REPO_URL, voxtral_repo_path), {})

            # Kokoro
            if config.get('kokoro_support', False):
                if os.path.exists(kokoro_repo_path):
                    update_tasks["Update Kokoro"] = (self.pull_repo, (kokoro_repo_path,), {})
                else:
                    update_tasks["Clone Kokoro"] = (self.clone_repo, (KOKORO_API_REPO_URL, kokoro_repo_path), {})

            # Chatterbox
            if config.get('chatterbox_support', False):
                if os.path.exists(chatterbox_repo_path):
                    update_tasks["Update Chatterbox"] = (self.pull_repo, (chatterbox_repo_path,), {})
                else:
                    update_tasks["Clone Chatterbox"] = (self.clone_repo, (CHATTERBOX_API_REPO_URL, chatterbox_repo_path), {})

            # Qwen3 TTS
            if config.get('kobold_qwen_support', False):
                if os.path.exists(kobold_qwen_repo_path):
                    update_tasks["Update Qwen3 TTS"] = (self.pull_repo, (kobold_qwen_repo_path,), {})
                else:
                    update_tasks["Clone Qwen3 TTS"] = (self.clone_repo, (KOBOLD_QWEN_API_REPO_URL, kobold_qwen_repo_path), {})

            # Magpie
            if config.get('magpie_support', False):
                if os.path.exists(magpie_repo_path):
                    update_tasks["Update Magpie"] = (self.pull_repo, (magpie_repo_path,), {})
                else:
                    update_tasks["Clone Magpie"] = (self.clone_repo, (MAGPIE_API_REPO_URL, magpie_repo_path), {})

            if config.get('silero_support', False):
                if os.path.exists(silero_repo_path):
                    update_tasks["Update Silero"] = (self.pull_repo, (silero_repo_path,), {})
                else:
                    update_tasks["Clone Silero"] = (self.clone_repo, (SILERO_API_REPO_URL, silero_repo_path), {})

            if config.get('rvc_support', False):
                if os.path.exists(rvc_repo_path):
                    update_tasks["Update RVC"] = (self.pull_repo, (rvc_repo_path,), {})
                else:
                    update_tasks["Clone RVC"] = (self.clone_repo, (RVC_API_REPO_URL, rvc_repo_path), {})

            # easy_xtts_trainer
            if os.path.exists(easy_xtts_trainer_path):
                update_tasks["Update XTTS Trainer"] = (self.pull_repo, (easy_xtts_trainer_path,), {})
            elif config.get('xtts_finetuning_support', False):
                update_tasks["Clone XTTS Trainer"] = (self.clone_repo, (EASY_XTTS_TRAINER_REPO_URL, easy_xtts_trainer_path), {})

            self.execute_concurrently(update_tasks, max_workers=8)

            if not self.check_pixi(pandrator_base_path):
                raise FileNotFoundError("Pixi installation failed during update.")

            shared_pixi_path = self.get_pixi_executable(pandrator_base_path)

            self.reporter.status("Backing up local state database...")
            backup_paths = self.backup_state_database(pandrator_repo_path)
            if backup_paths:
                logging.info(
                    "Backed up %d state database file(s) before update.",
                    len(backup_paths),
                )
            else:
                logging.info("No local state database files found to back up before update.")

            if config.get('rvc_support', False):
                self.reporter.status("Migrating RVC to the dedicated service...")
                rvc_use_cpu = not config.get(RVC_GPU_SUPPORT_CONFIG_FLAG, False)
                self.migrate_rvc_to_service(
                    pandrator_base_path,
                    pandrator_repo_path,
                    rvc_repo_path,
                    use_cpu=rvc_use_cpu,
                    pixi_path=shared_pixi_path,
                )

            # Setup environments
            self.reporter.status("Checking Pandrator environment...")
            self.create_pixi_env(pandrator_base_path, 'pandrator_installer', PANDRATOR_PYTHON_VERSION)
            self.ensure_pandrator_environment_conda_packages(pandrator_base_path)
            self.reporter.status("Checking Pandrator runtime...")
            self.ensure_pandrator_runtime(pandrator_base_path, 'pandrator_installer')

            requirements_file = os.path.join(pandrator_repo_path, 'requirements.txt')
            logging.info(f"Checking requirements from: {requirements_file}")

            if not os.path.exists(requirements_file):
                logging.error(f"Requirements file not found at: {requirements_file}")
                raise FileNotFoundError(f"Requirements file not found: {requirements_file}")

            self.reporter.status("Checking Pandrator dependencies...")
            needs_pandrator_requirements, pandrator_requirements_reason = self.should_install_requirements(
                pandrator_base_path,
                'pandrator_installer',
                requirements_file,
            )
            if needs_pandrator_requirements:
                self.reporter.status("Updating Pandrator dependencies...")
                logging.info(f"Installing Pandrator requirements because {pandrator_requirements_reason}")
                self.install_requirements(pandrator_base_path, 'pandrator_installer', requirements_file)
            else:
                logging.info(f"Skipping Pandrator requirements install: {pandrator_requirements_reason}")
            self.reporter.status("Checking NeMo text normalization...")
            self.ensure_nemo_text_processing_runtime(pandrator_base_path)
            self.reporter.status("Checking sentence segmentation model...")
            self.ensure_wtpsplit_runtime(pandrator_base_path)
            self.reporter.status("Checking PDF OCR models...")
            self.ensure_pdf_ocr_runtime(pandrator_base_path)

            # Concurrently bootstrap Kokoro in background if it needs bootstrapping
            kokoro_bootstrap_future = None
            bootstrap_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

            if config.get('kokoro_support', False):
                kokoro_gpu_support = config.get(KOKORO_GPU_SUPPORT_CONFIG_FLAG, False)
                if kokoro_env_missing:
                    self.reporter.status("Creating Kokoro Pixi environment...")
                    self.create_pixi_env(pandrator_base_path, KOKORO_ENV_NAME, KOKORO_PYTHON_VERSION)

                if kokoro_env_missing or not self.is_kokoro_runtime_ready(
                    pandrator_base_path,
                    kokoro_repo_path,
                    use_gpu=kokoro_gpu_support,
                ):
                    def kokoro_update_bootstrap_task():
                        logging.info("[Bootstrap: Kokoro] Starting Kokoro API server bootstrap in background (update)...")
                        self.install_kokoro_api_server(
                            pandrator_base_path,
                            kokoro_repo_path,
                            env_name=KOKORO_ENV_NAME,
                            use_gpu=kokoro_gpu_support,
                        )
                        logging.info("[Bootstrap: Kokoro] Kokoro API server bootstrap completed.")

                    kokoro_bootstrap_future = bootstrap_executor.submit(kokoro_update_bootstrap_task)

            # Main thread bootstraps port 8020 API servers
            if config.get('xtts_support', False):
                if not self.is_xtts_runtime_ready(xtts_repo_path):
                    self.reporter.status("Bootstrapping XTTS2 API server (temporary startup)...")
                    self.install_xtts_api_server(
                        xtts_repo_path,
                        use_cpu=not config.get('cuda_support', False),
                        pixi_path=shared_pixi_path,
                    )

            if config.get('voxcpm_support', False):
                if not self.is_voxcpm_runtime_ready(voxcpm_repo_path):
                    self.reporter.status("Bootstrapping VoxCPM API server...")
                    self.install_voxcpm_api_server(
                        voxcpm_repo_path,
                        pixi_path=shared_pixi_path,
                    )

            if config.get('fishs2_support', False):
                if not self.is_fishs2_runtime_ready(fishs2_repo_path):
                    self.reporter.status("Bootstrapping FishS2 API server...")
                    self.install_fishs2_api_server(
                        fishs2_repo_path,
                        pixi_path=shared_pixi_path,
                    )

            if config.get('silero_support', False):
                self.reporter.status("Installing/upgrading Silero and verifying its default model...")
                self.install_silero_api_server(
                    silero_repo_path,
                    pandrator_path=pandrator_base_path,
                    pixi_path=shared_pixi_path,
                )

            if config.get('voxtral_support', False):
                if not self.is_voxtral_runtime_ready(voxtral_repo_path):
                    self.reporter.status("Bootstrapping Voxtral API server...")
                    self.install_voxtral_api_server(voxtral_repo_path)

            if config.get('chatterbox_support', False):
                if not self.is_chatterbox_runtime_ready(chatterbox_repo_path):
                    self.reporter.status("Bootstrapping Chatterbox API server...")
                    chatterbox_gpu_support = config.get('chatterbox_gpu_support', False)
                    self.install_chatterbox_api_server(
                        chatterbox_repo_path,
                        use_cpu=not chatterbox_gpu_support,
                        pixi_path=shared_pixi_path,
                    )

            if config.get('kobold_qwen_support', False):
                if not self.is_kobold_qwen_runtime_ready(kobold_qwen_repo_path):
                    self.reporter.status("Bootstrapping Qwen3 TTS API server...")
                    kobold_qwen_gpu_support = config.get(KOBOLD_QWEN_GPU_SUPPORT_CONFIG_FLAG, False)
                    installed_models = config.get('kobold_qwen_installed_models') or [
                        config.get('kobold_qwen_initial_model', 'base')
                    ]
                    for model_variant in installed_models:
                        self.reporter.status(f"Bootstrapping Qwen3 TTS {model_variant} model...")
                        self.install_kobold_qwen_api_server(
                            kobold_qwen_repo_path,
                            backend=config.get('kobold_qwen_backend', 'auto') if kobold_qwen_gpu_support else 'cpu',
                            model_size=config.get('kobold_qwen_model_size', '0.6b'),
                            quantization=config.get('kobold_qwen_quantization', 'f16'),
                            initial_model=model_variant,
                            pixi_path=shared_pixi_path,
                        )

            if config.get('magpie_support', False):
                if not self.is_magpie_runtime_ready(magpie_repo_path):
                    self.reporter.status("Bootstrapping Magpie API server...")
                    magpie_gpu_support = config.get('magpie_gpu_support', False)
                    self.install_magpie_api_server(
                        magpie_repo_path,
                        use_cpu=not magpie_gpu_support,
                        pixi_path=shared_pixi_path,
                    )

            crispasr_required = bool(
                config.get('crispasr_support', False)
                or config.get('whisperx_support', False)
                or config.get('parakeet_onnx_support', False)
            )
            if crispasr_required:
                self.reporter.status("Installing/updating CrispASR transcription runtime...")
                crispasr_install = self.install_crispasr(
                    pandrator_base_path,
                    requested_backend=str(config.get('crispasr_backend') or 'auto'),
                )
                config['crispasr_support'] = True
                config['crispasr_backend'] = crispasr_install['requested_backend']
                config['crispasr_runtime_variant'] = crispasr_install['runtime_variant']
                config['crispasr_compiled_backends'] = crispasr_install['compiled_backends']
                config['whisperx_support'] = False
                config['parakeet_onnx_support'] = False

            if config.get('xtts_finetuning_support', False):
                self.reporter.status("Checking XTTS trainer's private WhisperX dependency...")
                self.create_pixi_env(pandrator_base_path, 'whisperx_installer', WHISPERX_PYTHON_VERSION)
                self.add_pixi_conda_package(pandrator_base_path, 'whisperx_installer', 'cudnn=8.9.7.29')
                self.add_pixi_conda_package(pandrator_base_path, 'whisperx_installer', 'ffmpeg')

                whisperx_needs_install, whisperx_reason = self.component_needs_package_sync(
                    pandrator_base_path,
                    'whisperx_installer',
                    WHISPERX_REQUIRED_PACKAGE_SPECS,
                )

                if whisperx_needs_install:
                    self.reporter.status("Installing/upgrading WhisperX dependencies...")
                    logging.info(f"Installing WhisperX packages because {whisperx_reason}")
                    self.install_whisperx(pandrator_base_path, 'whisperx_installer')
                else:
                    logging.info(f"Skipping WhisperX reinstall: {whisperx_reason}")

            # Update easy XTTS trainer (repo and requirements)
            if os.path.exists(easy_xtts_trainer_path):
                self.reporter.status("Updating easy XTTS trainer...")
                logging.info(f"Updating easy XTTS trainer in: {easy_xtts_trainer_path}")
                self.pull_repo(easy_xtts_trainer_path)

                xtts_requirements_file = os.path.join(easy_xtts_trainer_path, 'requirements.txt')
                if os.path.exists(xtts_requirements_file):
                    self.create_pixi_env(pandrator_base_path, 'easy_xtts_trainer', XTTS_FINETUNING_PYTHON_VERSION)
                    self.add_pixi_conda_package(pandrator_base_path, 'easy_xtts_trainer', 'ffmpeg')

                    self.reporter.status("Checking easy XTTS trainer dependencies...")
                    needs_easy_xtts_requirements, easy_xtts_requirements_reason = self.should_install_requirements(
                        pandrator_base_path,
                        'easy_xtts_trainer',
                        xtts_requirements_file,
                    )
                    if needs_easy_xtts_requirements:
                        self.reporter.status("Updating easy XTTS trainer dependencies...")
                        logging.info(
                            "Installing easy XTTS trainer requirements because %s",
                            easy_xtts_requirements_reason,
                        )
                        self.install_requirements(
                            pandrator_base_path,
                            'easy_xtts_trainer',
                            xtts_requirements_file,
                        )
                    else:
                        logging.info(
                            "Skipping easy XTTS trainer requirements install: %s",
                            easy_xtts_requirements_reason,
                        )

                    self.reporter.status("Checking easy XTTS trainer bundled wheel...")
                    self.install_xtts_finetuning_bundled_wheel(
                        pandrator_base_path,
                        'easy_xtts_trainer',
                        easy_xtts_trainer_path,
                    )

                    needs_xtts_torch, xtts_torch_reason = self.component_needs_package_sync(
                        pandrator_base_path,
                        'easy_xtts_trainer',
                        XTTS_FINETUNING_TORCH_PACKAGE_SPECS,
                    )
                    if needs_xtts_torch:
                        self.reporter.status("Updating XTTS fine-tuning PyTorch packages...")
                        logging.info(f"Installing XTTS fine-tuning torch packages because {xtts_torch_reason}")
                        self.install_pytorch_for_xtts_finetuning(pandrator_base_path, 'easy_xtts_trainer')
                    else:
                        logging.info(f"Skipping XTTS fine-tuning torch reinstall: {xtts_torch_reason}")
                else:
                    logging.warning(f"XTTS trainer requirements file not found at: {xtts_requirements_file}")
            elif config.get('xtts_finetuning_support', False):
                self.reporter.status("Migrating easy XTTS trainer to Pixi...")
                self.clone_repo(EASY_XTTS_TRAINER_REPO_URL, easy_xtts_trainer_path)
                self.create_pixi_env(pandrator_base_path, 'easy_xtts_trainer', XTTS_FINETUNING_PYTHON_VERSION)
                self.add_pixi_conda_package(pandrator_base_path, 'easy_xtts_trainer', 'ffmpeg')
                xtts_requirements_file = os.path.join(easy_xtts_trainer_path, 'requirements.txt')
                self.install_requirements(pandrator_base_path, 'easy_xtts_trainer', xtts_requirements_file)
                self.install_xtts_finetuning_bundled_wheel(
                    pandrator_base_path,
                    'easy_xtts_trainer',
                    easy_xtts_trainer_path,
                )
                self.install_pytorch_for_xtts_finetuning(pandrator_base_path, 'easy_xtts_trainer')
            else:
                logging.info("easy XTTS trainer not installed, skipping update.")

            if kokoro_bootstrap_future is not None:
                self.reporter.status("Waiting for Kokoro API server bootstrap to complete...")
                kokoro_bootstrap_future.result()
                bootstrap_executor.shutdown()

            self.write_packaging_layout(pandrator_base_path)

            self.reporter.status("Cleaning installer package caches...")
            self.cleanup_installer_package_caches(pandrator_base_path)

            # Set permissions if running as admin
            if is_admin:
                self.reporter.status("Setting permissions after update...")
                self.set_permissive_permissions(pandrator_base_path)

            self.reporter.status("Update completed successfully!")
            logging.info("Update process completed successfully")

        except Exception as e:
            error_msg = f"Failed to update: {str(e)}"
            logging.error(error_msg)
            logging.error(traceback.format_exc())
            self.reporter.status(f"Update failed: {error_msg}")
            raise
