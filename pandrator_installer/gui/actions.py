"""GUI-only actions that adapt installer workflows to Qt widgets."""

import logging
import os

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QMessageBox

from ..reporting import SignalReporter
from .support import Worker


class GuiActionsMixin:
    def install_pandrator(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        pandrator_already_installed = os.path.exists(pandrator_path)

        installed_components = self.get_installed_components()

        selection = self.snapshot_install_selection()
        selection.validate()

        new_components_selected = (
            ((selection.xtts or selection.xtts_cpu) and not installed_components['xtts']) or
            (selection.voxcpm and not installed_components['voxcpm']) or
            (selection.fishs2 and not installed_components['fishs2']) or
            (selection.silero and not installed_components['silero']) or
            (selection.voxtral and not installed_components['voxtral']) or
            ((selection.kokoro or selection.kokoro_cpu) and not installed_components['kokoro']) or
            ((selection.rvc or selection.rvc_cpu) and not installed_components['rvc']) or
            (selection.crispasr and not installed_components['crispasr']) or
            (selection.xtts_finetuning and not installed_components['xtts_finetuning']) or
            ((selection.chatterbox or selection.chatterbox_cpu) and not installed_components['chatterbox']) or
            ((selection.kobold_qwen or selection.kobold_qwen_cpu) and not installed_components['kobold_qwen']) or
            ((selection.magpie or selection.magpie_cpu) and not installed_components['magpie'])
        )

        if pandrator_already_installed and not selection.pandrator:
            if not new_components_selected:
                QMessageBox.information(self, "Info", "No new components selected for installation.")
                return
        elif not pandrator_already_installed and not selection.pandrator:
            QMessageBox.critical(self, "Error", "Pandrator must be installed first before adding new components.")
            return

        self.disable_buttons()
        self.progress_bar.setValue(0)
        self.status_label.setText("Installing...")

        self.initialize_logging()

        logging.info("Installation process started.")

        # Create worker thread to run the installation
        self.worker = Worker(self.install_process, selection)
        self.reporter = SignalReporter(self.worker.update_progress, self.worker.update_status)
        self.worker.update_progress.connect(self.update_progress)
        self.worker.update_status.connect(self.update_status)
        self.worker.finished.connect(self.on_installation_finished)
        self.worker.error.connect(self.on_installation_error)
        self.worker.start()

    def on_installation_finished(self):
        """Handle completion of installation process"""
        self.update_status("Installation complete!")
        self.tabs.setCurrentWidget(self.launch_tab)
        self.enable_buttons()
        QMessageBox.information(self, "Success", "Installation completed successfully!")

    def on_installation_error(self, error_message):
        """Handle installation errors"""
        self.update_status(f"Installation failed: {error_message}")
        self.enable_buttons()
        QMessageBox.critical(self, "Installation Error", f"Installation failed:\n\n{error_message}\n\nCheck the log for more details.")

    def open_log_file(self):
        """Open the log file with the default system application"""
        if hasattr(self, 'log_filename') and self.log_filename and os.path.exists(self.log_filename):
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(self.log_filename)))
        else:
            QMessageBox.warning(self, "Log Not Available", "No log file is available yet.")

    def update_pandrator(self):
        """Update Pandrator and components"""
        pandrator_base_path = os.path.join(self.initial_working_dir, 'Pandrator')
        pandrator_repo_path = os.path.join(pandrator_base_path, 'Pandrator')

        # Check admin status
        is_admin = self.is_admin()
        if os.name == 'nt' and not is_admin:
            logging.info("Running update without admin privileges - file permission changes won't be applied")

        logging.info(f"Checking for Pandrator at: {pandrator_repo_path}")

        if not os.path.exists(pandrator_repo_path):
            error_msg = f"Pandrator directory not found at: {pandrator_repo_path}"
            logging.error(error_msg)
            self.update_status(error_msg)
            QMessageBox.critical(self, "Update Error", error_msg)
            return

        running_processes = self.get_running_installation_processes(pandrator_base_path)
        stop_running_processes = False
        if running_processes:
            dialog = QMessageBox(self)
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setWindowTitle("Pandrator Is Running")
            dialog.setText("Pandrator and its services must stop before the update can begin.")
            dialog.setInformativeText(
                "The installer window will remain open. Any active job will be interrupted, "
                "but completed artifacts will be preserved.\n\nRunning: "
                + self.describe_running_installation_processes(running_processes)
            )
            stop_button = dialog.addButton("Stop and Update", QMessageBox.ButtonRole.AcceptRole)
            cancel_button = dialog.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            dialog.setDefaultButton(stop_button)
            dialog.setEscapeButton(cancel_button)
            dialog.exec()
            if dialog.clickedButton() is not stop_button:
                return
            stop_running_processes = True

        self.disable_buttons()
        self.initialize_logging()

        self.update_status("Updating Pandrator and components...")
        logging.info("Starting update process")

        # Create worker thread to run the update
        self.worker = Worker(self.update_process, stop_running_processes)
        self.reporter = SignalReporter(self.worker.update_progress, self.worker.update_status)
        self.worker.update_progress.connect(self.update_progress)
        self.worker.update_status.connect(self.update_status)
        self.worker.finished.connect(self.on_update_finished)
        self.worker.error.connect(self.on_update_error)
        self.worker.start()

    def on_update_finished(self):
        """Handle completion of update process"""
        self.update_status("Update complete!")
        self.enable_buttons()
        QMessageBox.information(self, "Success", "Update completed successfully!")

    def on_update_error(self, error_message):
        """Handle update errors"""
        self.update_status(f"Update failed: {error_message}")
        self.enable_buttons()
        QMessageBox.critical(self, "Update Error", f"Update failed:\n\n{error_message}\n\nCheck the log for more details.")

    def _known_running_backends(self):
        running_backends = self._collect_running_backends()
        supervised_backends = self._collect_supervised_backends()
        known = []
        seen = set()
        supervised_identities = {
            (backend_key, process.pid)
            for backend_key, _label, process in supervised_backends
        }
        for backend_key, label, process in [*running_backends, *supervised_backends]:
            identity = (backend_key, process.pid)
            if identity in seen:
                continue
            seen.add(identity)
            known.append(
                (backend_key, label, process, identity in supervised_identities)
            )
        return known

    def update_backend_runtime_controls(self):
        if not hasattr(self, 'active_backend_value_label'):
            return

        known = self._known_running_backends()
        if known:
            running_details = ", ".join(
                f"{label} (PID {process.pid}{', managed with Pandrator' if supervised else ''})"
                for _, label, process, supervised in known
            )
            self.active_backend_value_label.setText(f"Running: {running_details}")
        else:
            self.active_backend_value_label.setText("No backend running")

        worker_busy = bool(self.worker and self.worker.isRunning())
        running_keys = {backend_key for backend_key, _label, _process, _supervised in known}
        installed_components = self.get_installed_components()
        for backend_key, card in getattr(self, 'launch_backend_cards', {}).items():
            is_running = backend_key in running_keys
            card.set_runtime_state(is_running, stoppable=not worker_busy)
            control = self.launch_backend_controls[backend_key]
            if is_running:
                control.blockSignals(True)
                control.setChecked(False)
                control.blockSignals(False)
                control.setEnabled(False)
                control.setToolTip("Already running. Use Stop to shut down this backend.")
            else:
                control.setEnabled(bool(installed_components.get(backend_key)) and not worker_busy)
                control.setToolTip("")

        if hasattr(self, 'stop_backend_button'):
            self.stop_backend_button.setEnabled(bool(known) and not worker_busy)
            self.stop_backend_button.setText("Stop All Running Backends")

        if hasattr(self, 'refresh_backend_status_button'):
            self.refresh_backend_status_button.setEnabled(not worker_busy)

    def stop_running_backends(self):
        self._stop_running_backend_records(self._known_running_backends())

    def stop_running_backend(self, backend_key):
        records = [
            record
            for record in self._known_running_backends()
            if record[0] == backend_key
        ]
        self._stop_running_backend_records(records, requested_key=backend_key)

    def _stop_running_backend_records(self, records, requested_key=None):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(
                self,
                "Please Wait",
                "Another operation is still in progress.",
            )
            return

        if not records:
            self.update_backend_runtime_controls()
            label = self._backend_label_from_key(requested_key) if requested_key else "backend"
            QMessageBox.information(
                self,
                "No Backend Running",
                f"{label} is not running.",
            )
            return

        running_backend_names = ", ".join(dict.fromkeys(label for _, label, _, _ in records))
        confirm_stop = QMessageBox.question(
            self,
            "Stop Backend",
            "Stop the selected backend(s)?\n\n"
            f"{running_backend_names}\n\n"
            "Pandrator will stay open and your active session will remain available.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if confirm_stop != QMessageBox.StandardButton.Yes:
            return

        self.backend_stop_targets = list(dict.fromkeys(backend_key for backend_key, _, _, _ in records))
        self.backend_stop_labels = list(dict.fromkeys(label for _, label, _, _ in records))
        self.initialize_logging()
        self.progress_bar.setValue(0)
        self.update_status("Stopping running backend(s)...")
        self.install_button.setEnabled(False)
        self.update_button.setEnabled(False)
        self.launch_button.setEnabled(False)
        self.stop_backend_button.setEnabled(False)
        self.refresh_backend_status_button.setEnabled(False)

        self.worker = Worker(self.stop_running_backends_process)
        self.reporter = SignalReporter(self.worker.update_progress, self.worker.update_status)
        self.worker.update_progress.connect(self.update_progress)
        self.worker.update_status.connect(self.update_status)
        self.worker.finished.connect(self.on_stop_backends_finished)
        self.worker.error.connect(self.on_stop_backends_error)
        self.worker.start()

    def on_stop_backends_finished(self):
        stopped_names = ", ".join(getattr(self, 'backend_stop_labels', [])) or "Backend"
        self.backend_stop_targets = []
        self.backend_stop_labels = []
        self.enable_buttons()
        self.update_status(f"Stopped {stopped_names}. You can launch another backend without closing Pandrator.")

    def on_stop_backends_error(self, error_message):
        self.backend_stop_targets = []
        self.backend_stop_labels = []
        self.enable_buttons()

        self.update_status(f"Stopping backend failed: {error_message}")
        QMessageBox.critical(
            self,
            "Stop Backend Error",
            f"Failed to stop backend(s):\n\n{error_message}\n\nCheck the log for details.",
        )

    def launch_apps(self):
        """Launch the selected applications"""
        if self.worker and self.worker.isRunning():
            QMessageBox.information(
                self,
                "Please Wait",
                "Another operation is still in progress.",
            )
            return

        selection = self.snapshot_launch_selection()
        if not self.prepare_launch_security(selection):
            return
        try:
            self.persist_launch_preferences(selection)
        except RuntimeError as error:
            self.notify_error("Configuration Error", str(error))
            return
        self.initialize_logging()
        self._apply_launch_selection_state(selection)

        # Create worker thread to run the launch process
        self.worker = Worker(self.launch_process, selection)
        self.reporter = SignalReporter(self.worker.update_progress, self.worker.update_status)
        self.worker.update_progress.connect(self.update_progress)
        self.worker.update_status.connect(self.update_status)
        self.worker.finished.connect(self.on_launch_finished)
        self.worker.error.connect(self.on_launch_error)
        self.worker.start()

    def on_launch_finished(self):
        """Handle successful launch"""
        if getattr(self, "launch_pandrator_var", False):
            self.update_status("Pandrator is ready in your browser")
        else:
            self.update_status("Selected services are ready")
        self.enable_buttons()
        self.update_backend_runtime_controls()
        # Start process monitoring
        QTimer.singleShot(5000, self.check_processes_status)

    def on_launch_error(self, error_message):
        """Handle launch errors"""
        self.update_status(f"Launch failed: {error_message}")
        self.enable_buttons()
        self.update_backend_runtime_controls()
        QMessageBox.critical(self, "Launch Error", f"Failed to launch applications:\n\n{error_message}\n\nCheck the log for more details.")

    def check_processes_status(self):
        """Check the status of running processes and update UI accordingly"""
        any_process_running = False
        pandrator_exited = False

        # Check Pandrator
        if self.pandrator_process and self.pandrator_process.poll() is not None:
            # Pandrator has exited
            return_code = self.pandrator_process.poll()
            startup_log_file = getattr(self.pandrator_process, 'log_file_path', '')
            self._close_process_log_handle(self.pandrator_process)

            if return_code not in (None, 0):
                details = f"Pandrator exited with code {return_code}."
                if startup_log_file:
                    details += f" See log: {startup_log_file}"
                logging.error(details)

            self.pandrator_process = None
            pandrator_exited = True
        elif self.pandrator_process:
            any_process_running = True

        if pandrator_exited:
            self.shutdown_apps()  # Shut down other apps when Pandrator exits

        running_backends = self._collect_running_backends()
        if running_backends:
            any_process_running = True
        if self._get_running_rvc_process():
            any_process_running = True

        if not any_process_running:
            self.update_status("All processes have exited.")
            self.refresh_ui_state()
        else:
            QTimer.singleShot(5000, self.check_processes_status)  # Schedule next check

        self.update_backend_runtime_controls()

    def closeEvent(self, event):
        """Handle window close event"""
        running = bool(
            self.pandrator_process
            or self._collect_running_backends()
            or self._get_running_rvc_process()
        )
        if running and not getattr(self, "_quit_requested", False):
            dialog = QMessageBox(self)
            dialog.setWindowTitle("Pandrator is still running")
            dialog.setText("The browser can close independently. What should the launcher do with the running services?")
            minimize = dialog.addButton("Minimize to tray", QMessageBox.ButtonRole.AcceptRole)
            stop = dialog.addButton("Stop everything", QMessageBox.ButtonRole.DestructiveRole)
            cancel = dialog.addButton(QMessageBox.StandardButton.Cancel)
            dialog.exec()
            if dialog.clickedButton() is minimize:
                event.ignore()
                self.hide()
                self.tray_icon.showMessage("Pandrator", "Services are still running. Use the tray menu to return or stop them.")
                return
            if dialog.clickedButton() is cancel:
                event.ignore()
                return
            if dialog.clickedButton() is not stop:
                event.ignore()
                return
        self.shutdown_apps()
        self.shutdown_logging()
        event.accept()
