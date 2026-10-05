"""Non-window installer service used by headless automation."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .components import ComponentOperationsMixin
from .lifecycle_guard import LifecycleBusy, installation_lifecycle_guard
from .models import DEFAULT_QWEN_MODEL_SIZE, InstallSelection
from .operations import OperationsMixin
from .pixi import PixiEnvironmentMixin
from .reporting import HeadlessReporter, Reporter
from .runtime import RuntimeMixin
from .runtime_metadata import uninstall_runtime_may_be_active
from .storage import StorageMixin
from .workflows import WorkflowMixin


class HeadlessInstaller(
    StorageMixin,
    OperationsMixin,
    PixiEnvironmentMixin,
    ComponentOperationsMixin,
    WorkflowMixin,
    RuntimeMixin,
):
    """Installer workflow host for headless automation."""

    def __init__(self, working_dir):
        self.headless = True
        self.initial_working_dir = os.path.abspath(working_dir or os.getcwd())
        self._installation_depth: int = 0
        self.reporter: Reporter = HeadlessReporter()
        self.worker = None
        self.log_filename = None
        self.tls_configured = False
        self.ca_bundle_path = None
        self.backend_stop_targets: list[str] = []

        for process_attr in (
            "xtts_process",
            "voxcpm_process",
            "fishs2_process",
            "pandrator_process",
            "silero_process",
            "voxtral_process",
            "kokoro_process",
            "chatterbox_process",
            "magpie_process",
            "kobold_qwen_process",
            "rvc_process",
        ):
            setattr(self, process_attr, None)

        self.disable_deepspeed_var = False

    @contextmanager
    def _installation_operation(self) -> Iterator[None]:
        root = Path(self.initial_working_dir) / "Pandrator"
        with installation_lifecycle_guard(root, shared=False):
            if self._installation_depth == 0:
                if uninstall_runtime_may_be_active(root):
                    raise LifecycleBusy(
                        "Pandrator may still be running. Stop it and check its runtime metadata before installing."
                    )
                if self.get_running_installation_processes(str(root)):
                    raise LifecycleBusy("Pandrator is still running. Stop it before installing.")
            self._installation_depth += 1
            try:
                yield
            finally:
                self._installation_depth -= 1

    def run_headless_install(
        self,
        components: Iterable[str],
        install_pandrator: bool = True,
        crispasr_backend: str = "auto",
        crispasr_engine: str = "whisper-large-v3",
        crispasr_model_quantization: str | None = None,
        kobold_qwen_backend: str = "auto",
        kobold_qwen_model_size: str = DEFAULT_QWEN_MODEL_SIZE,
        kobold_qwen_quantization: str = "f16",
        kobold_qwen_initial_model: str = "base",
    ) -> None:
        with self._installation_operation():
            return super().run_headless_install(
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

    def install_process(self, selection: InstallSelection | None = None) -> None:
        with self._installation_operation():
            return super().install_process(selection)

    def update_status(self, text):
        self.reporter.status(text)

    def notify_error(self, title: str, message: str) -> None:
        logging.error("%s: %s", title, message)

    def notify_warning(self, title, message):
        logging.warning("%s: %s", title, message)
