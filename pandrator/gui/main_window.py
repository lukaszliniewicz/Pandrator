from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout,
    QTabWidget, QSplitter, QScrollArea, QMessageBox, QLabel
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent

from .widgets.session_tab import SessionTab
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
        self._last_training_status = ""
        self._last_training_progress = 0

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
        self.logic.log_message.connect(self._on_log_message)
        self.logic.show_error.connect(self._on_show_error)
        self.logic.progress_updated.connect(self._on_generation_progress_updated)
        self.logic.xtts_training_running_changed.connect(self._on_training_running_changed)
        self.logic.xtts_training_status_updated.connect(self._on_training_status_updated)
        self.logic.xtts_training_progress_updated.connect(self._on_training_progress_updated)
        self.logic.tts_connection_running_changed.connect(self._on_tts_connection_running_changed)

        self.activity_status_label = QLabel("Idle")
        self.statusBar().addPermanentWidget(self.activity_status_label)
        self.statusBar().showMessage("Ready", 3000)
        self._refresh_activity_status()

    def _create_tabs(self):
        self.session_tab = SessionTab(self.logic)
        self.tab_widget.addTab(self.session_tab, "Session")

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
        self._refresh_activity_status()

    def _on_log_message(self, message: str):
        if message:
            self.statusBar().showMessage(message, 5000)

    def _on_show_error(self, title: str, message: str):
        self.statusBar().showMessage(f"{title}: {message}", 12000)

        title_lower = (title or "").lower()
        if "information" in title_lower:
            QMessageBox.information(self, title, message)
        elif "warning" in title_lower or "no session" in title_lower:
            QMessageBox.warning(self, title, message)
        else:
            QMessageBox.critical(self, title, message)

    def _on_generation_progress_updated(self, current: int, total: int, _elapsed: float):
        if total <= 0:
            return

        progress_percent = int((current / total) * 100)
        self.activity_status_label.setText(
            f"Generation {current}/{total} ({progress_percent}%)"
        )

    def _on_training_running_changed(self, running: bool):
        if running:
            self._last_training_progress = 0
        else:
            self._last_training_status = ""
            self._last_training_progress = 0

        self._refresh_activity_status()

    def _on_training_status_updated(self, message: str):
        self._last_training_status = message or ""
        self._refresh_activity_status()

    def _on_training_progress_updated(self, progress: int):
        self._last_training_progress = max(0, min(100, int(progress)))
        self._refresh_activity_status()

    def _on_tts_connection_running_changed(self, running: bool):
        if running:
            self.activity_status_label.setText("Connecting TTS...")
        else:
            self._refresh_activity_status()

    def _refresh_activity_status(self):
        if self.logic.is_tts_connection_running():
            self.activity_status_label.setText("Connecting TTS...")
            return

        if self.logic.is_xtts_training_running():
            status = self._last_training_status or "Training in progress..."
            if self._last_training_progress > 0:
                self.activity_status_label.setText(f"XTTS {self._last_training_progress}% - {status}")
            else:
                self.activity_status_label.setText(f"XTTS - {status}")
            return

        lifecycle_status = self.logic.get_lifecycle_status()
        if lifecycle_status == "Generating" and self.activity_status_label.text().startswith("Generation "):
            return

        self.activity_status_label.setText(lifecycle_status)

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
