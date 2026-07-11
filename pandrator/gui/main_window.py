import os

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout,
    QTabWidget, QMessageBox, QPushButton
)
from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices, QKeyEvent

from .widgets.session_tab import SessionTab
from .widgets.sessions_manager_tab import SessionsManagerTab
from .widgets.text_processing_tab import TextProcessingTab
from .widgets.audio_processing_tab import AudioProcessingTab
from .widgets.providers_tab import ProvidersTab
from .widgets.logs_tab import LogsTab
from .widgets.train_xtts_tab import TrainXttsTab
from .widgets.generated_sentences_widget import GeneratedSentencesWidget
from .widgets.session_workspace import SessionWorkspace
from .dialogs.startup_wizard_dialog import StartupWizardDialog, wizard_icon
from .dialogs.voice_library_dialog import VoiceLibraryDialog

class MainWindow(QMainWindow):
    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic
        self._startup_wizard: StartupWizardDialog | None = None

        self.setWindowTitle("Pandrator")
        self.resize(1600, 900)

        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        main_layout = QHBoxLayout(self.central_widget)

        self.tab_widget = QTabWidget()
        self.tab_widget.setObjectName("leftPaneTabWidget")
        main_layout.addWidget(self.tab_widget)

        self._create_tabs()

        self.return_to_wizard_button = QPushButton("Return to Wizard", self.central_widget)
        self.return_to_wizard_button.setObjectName("returnToWizardButton")
        self.return_to_wizard_button.setIcon(wizard_icon("setup"))
        self.return_to_wizard_button.setToolTip(
            "Return to the same wizard page without losing your selections."
        )
        self.return_to_wizard_button.clicked.connect(self.open_startup_wizard)
        self.return_to_wizard_button.hide()

        self.logic.state_changed.connect(self.update_window_title)
        self.logic.app_notification.connect(self._on_app_notification)
        self.logic.show_error.connect(self._on_show_error)
        self.logic.dubbing_video_saved.connect(self._on_dubbing_video_saved)
        self.statusBar().showMessage("Ready", 3000)
        if bool(getattr(self.logic.state.wizard, "show_on_startup", True)):
            QTimer.singleShot(250, self.open_startup_wizard)

    def _create_tabs(self):
        self.session_tab = SessionTab(self.logic)
        self.right_widget = GeneratedSentencesWidget(self.logic)
        self.session_workspace = SessionWorkspace(
            self.session_tab,
            self.right_widget,
        )
        self.tab_widget.addTab(self.session_workspace, "Session")

        self.sessions_manager_tab = SessionsManagerTab(self.logic)
        self.tab_widget.addTab(self.sessions_manager_tab, "Sessions")

        self.text_processing_tab = TextProcessingTab(self.logic)
        self.tab_widget.addTab(self.text_processing_tab, "Text Processing")

        self.audio_processing_tab = AudioProcessingTab(self.logic)
        self.tab_widget.addTab(self.audio_processing_tab, "Audio Processing")

        self.providers_tab = ProvidersTab(self.logic)
        self.tab_widget.addTab(self.providers_tab, "Providers")
        
        self.train_xtts_tab = TrainXttsTab(self.logic)
        self.tab_widget.addTab(self.train_xtts_tab, "Train XTTS")

        self.logs_tab = LogsTab(self.logic)
        self.tab_widget.addTab(self.logs_tab, "Logs")

    def update_window_title(self):
        self.setWindowTitle(f"Pandrator - {self.logic.state.session_name}")
        self.session_tab.session_name_label.setText(self.logic.state.session_name)

    def open_startup_wizard(self):
        if self._startup_wizard is None:
            dialog = StartupWizardDialog(self.logic, self)
            dialog.open_providers_requested.connect(
                lambda: self._handoff_wizard_to_tab(self.providers_tab)
            )
            dialog.open_voices_requested.connect(self._handoff_wizard_to_voice_library)
            dialog.open_rvc_requested.connect(
                lambda: self._handoff_wizard_to_tab(self.audio_processing_tab)
            )
            dialog.finished.connect(lambda _result: self._on_startup_wizard_finished(dialog))
            self._startup_wizard = dialog
        self.return_to_wizard_button.hide()
        self._startup_wizard.refresh_sessions()
        self._startup_wizard._refresh_readiness()
        self._startup_wizard.show()
        self._startup_wizard.raise_()
        self._startup_wizard.activateWindow()

    def _show_return_to_wizard(self):
        self.return_to_wizard_button.adjustSize()
        self._position_return_to_wizard_button()
        self.return_to_wizard_button.show()
        self.return_to_wizard_button.raise_()

    def _handoff_wizard_to_tab(self, tab: QWidget):
        self.tab_widget.setCurrentWidget(tab)
        self._show_return_to_wizard()

    def _handoff_wizard_to_voice_library(self):
        self._show_return_to_wizard()
        VoiceLibraryDialog(self.logic, self).exec()

    def _on_startup_wizard_finished(self, dialog: StartupWizardDialog):
        if self._startup_wizard is dialog:
            self._startup_wizard = None
        self.return_to_wizard_button.hide()
        dialog.deleteLater()

    def _position_return_to_wizard_button(self):
        button = getattr(self, "return_to_wizard_button", None)
        if button is None:
            return
        margin = 22
        rect = self.central_widget.rect()
        button.move(
            max(margin, rect.right() - button.width() - margin),
            max(margin, rect.bottom() - button.height() - margin),
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_return_to_wizard_button()

    def _on_app_notification(self, message: str, timeout_ms: int = 5000):
        if message:
            self.statusBar().showMessage(message, max(0, int(timeout_ms or 0)))

    def _on_show_error(self, title: str, message: str):
        self.statusBar().showMessage(f"{title}: {message}", 12000)

        title_lower = (title or "").lower()
        if "information" in title_lower:
            QMessageBox.information(self, title, message)
        elif "warning" in title_lower or "no session" in title_lower:
            QMessageBox.warning(self, title, message)
        else:
            QMessageBox.critical(self, title, message)

    def _on_dubbing_video_saved(self, output_paths):
        paths = [str(path).strip() for path in (output_paths or []) if str(path).strip()]
        if not paths:
            return

        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Information)
        dialog.setWindowTitle("Export Saved")

        button_to_path = {}
        if len(paths) == 1:
            saved_path = paths[0]
            dialog.setText("The requested export was saved.")
            dialog.setInformativeText(os.path.basename(saved_path))
            open_file_button = dialog.addButton("Open File", QMessageBox.ButtonRole.AcceptRole)
            button_to_path[open_file_button] = saved_path
        else:
            dialog.setText(f"The requested export produced {len(paths)} files.")
            dialog.setInformativeText("Choose a file to open.")
            for saved_path in paths:
                button_label = f"Open {os.path.basename(saved_path)}"
                open_file_button = dialog.addButton(button_label, QMessageBox.ButtonRole.ActionRole)
                button_to_path[open_file_button] = saved_path

        dialog.addButton(QMessageBox.StandardButton.Close)
        dialog.exec()

        selected_path = button_to_path.get(dialog.clickedButton())
        if not selected_path:
            return

        if not os.path.exists(selected_path):
            QMessageBox.warning(
                self,
                "Open File Failed",
                f"The saved file could not be found:\n{selected_path}",
            )
            return

        if not QDesktopServices.openUrl(QUrl.fromLocalFile(selected_path)):
            QMessageBox.warning(
                self,
                "Open File Failed",
                f"Could not open the saved file:\n{selected_path}",
            )

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_M:
            focused_widget = self.focusWidget()
            if focused_widget == self.right_widget.sentences_list:
                self.right_widget.toggle_mark_for_selected()
        else:
            super().keyPressEvent(event)
