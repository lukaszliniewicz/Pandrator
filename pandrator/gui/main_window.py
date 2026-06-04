import os

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout,
    QTabWidget, QSplitter, QScrollArea, QMessageBox
)
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QKeyEvent

from .widgets.session_tab import SessionTab
from .widgets.sessions_manager_tab import SessionsManagerTab
from .widgets.text_processing_tab import TextProcessingTab
from .widgets.audio_processing_tab import AudioProcessingTab
from .widgets.providers_tab import ProvidersTab
from .widgets.logs_tab import LogsTab
from .widgets.train_xtts_tab import TrainXttsTab
from .widgets.generated_sentences_widget import GeneratedSentencesWidget

class MainWindow(QMainWindow):
    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic

        self.setWindowTitle("Pandrator")
        self.resize(1600, 900)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)

        left_scroll_area = QScrollArea()
        left_scroll_area.setObjectName("leftPaneScrollArea")
        left_scroll_area.setWidgetResizable(True)

        self.tab_widget = QTabWidget()
        self.tab_widget.setObjectName("leftPaneTabWidget")
        left_scroll_area.setWidget(self.tab_widget)

        self.right_widget = GeneratedSentencesWidget(self.logic)

        splitter.addWidget(left_scroll_area)
        splitter.addWidget(self.right_widget)
        splitter.setSizes([800, 800])

        self._create_tabs()

        self.logic.state_changed.connect(self.update_window_title)
        self.logic.app_notification.connect(self._on_app_notification)
        self.logic.show_error.connect(self._on_show_error)
        self.logic.dubbing_video_saved.connect(self._on_dubbing_video_saved)
        self.statusBar().showMessage("Ready", 3000)

    def _create_tabs(self):
        self.session_tab = SessionTab(self.logic)
        self.tab_widget.addTab(self.session_tab, "Session")

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
        dialog.setWindowTitle("Dubbing Saved")

        button_to_path = {}
        if len(paths) == 1:
            saved_path = paths[0]
            dialog.setText("Dubbing was added to the video and saved.")
            dialog.setInformativeText(os.path.basename(saved_path))
            open_file_button = dialog.addButton("Open File", QMessageBox.ButtonRole.AcceptRole)
            button_to_path[open_file_button] = saved_path
        else:
            dialog.setText(f"Dubbing was added and {len(paths)} video files were saved.")
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
                for item in self.right_widget.sentences_list.selectedItems():
                    sentence_number = item.data(Qt.ItemDataRole.UserRole)
                    self.logic.mark_sentence(sentence_number, True)
            elif focused_widget == self.right_widget.marked_list:
                for item in self.right_widget.marked_list.selectedItems():
                    sentence_number = item.data(Qt.ItemDataRole.UserRole)
                    self.logic.mark_sentence(sentence_number, False)
        else:
            super().keyPressEvent(event)
