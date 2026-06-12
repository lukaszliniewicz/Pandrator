"""Non-window installer service used by headless automation."""

from __future__ import annotations

import logging
import os

from .components import ComponentOperationsMixin
from .operations import OperationsMixin
from .pixi import PixiEnvironmentMixin
from .reporting import HeadlessReporter
from .runtime import RuntimeMixin
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
    """Installer workflow host without Qt widgets or a window."""

    def __init__(self, working_dir):
        self.headless = True
        self.initial_working_dir = os.path.abspath(working_dir or os.getcwd())
        self.reporter = HeadlessReporter()
        self.worker = None
        self.log_filename = None
        self.tls_configured = False
        self.ca_bundle_path = None
        self.backend_stop_targets = []

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
            "rvc_process",
        ):
            setattr(self, process_attr, None)

        self.disable_deepspeed_var = False

    def update_status(self, text):
        self.reporter.status(text)

    def notify_error(self, title, message):
        logging.error("%s: %s", title, message)

    def notify_warning(self, title, message):
        logging.warning("%s: %s", title, message)
