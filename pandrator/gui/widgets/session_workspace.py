from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)


class SessionWorkspace(QWidget):
    """Hosts the Create and Review surfaces without duplicating either widget."""

    mode_changed = pyqtSignal(str)

    def __init__(self, create_widget: QWidget, review_widget: QWidget, parent=None):
        super().__init__(parent)
        self.create_widget = create_widget
        self.review_widget = review_widget
        self._mode = "split"
        self._review_count = 0
        self._equal_split_pending = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("workspaceHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 8, 12, 8)
        header_layout.setSpacing(6)

        title = QLabel("Session Workspace")
        title.setObjectName("workspaceTitle")
        header_layout.addWidget(title)
        header_layout.addStretch(1)

        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)

        self.create_button = self._create_mode_button("Create", "create")
        self.review_button = self._create_mode_button("Review 0", "review")
        self.split_button = self._create_mode_button("Split View", "split")
        for button in (self.create_button, self.review_button, self.split_button):
            self.mode_group.addButton(button)
            header_layout.addWidget(button)

        layout.addWidget(header)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setObjectName("sessionWorkspaceSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.create_widget)
        self.splitter.addWidget(self.review_widget)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        layout.addWidget(self.splitter, 1)

        self.create_button.clicked.connect(lambda: self.set_mode("create"))
        self.review_button.clicked.connect(lambda: self.set_mode("review"))
        self.split_button.clicked.connect(lambda: self.set_mode("split"))

        create_requested = getattr(self.review_widget, "create_requested", None)
        if create_requested is not None:
            create_requested.connect(lambda: self.set_mode("create"))

        review_count_changed = getattr(self.review_widget, "review_count_changed", None)
        if review_count_changed is not None:
            review_count_changed.connect(self.set_review_count)

        review_count = getattr(self.review_widget, "review_count", None)
        if callable(review_count):
            self.set_review_count(review_count())

        self.set_mode("split")

    def _create_mode_button(self, text: str, mode: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("workspaceModeButton")
        button.setCheckable(True)
        button.setProperty("workspaceMode", mode)
        return button

    def mode(self) -> str:
        return self._mode

    def showEvent(self, event):
        super().showEvent(event)
        if self._mode == "split":
            self._schedule_equal_split()

    def set_review_count(self, count: int):
        self._review_count = max(0, int(count or 0))
        self.review_button.setText(f"Review {self._review_count}")

    def set_mode(self, mode: str):
        normalized_mode = str(mode or "create").strip().lower()
        if normalized_mode not in {"create", "review", "split"}:
            normalized_mode = "create"

        show_create = normalized_mode in {"create", "split"}
        show_review = normalized_mode in {"review", "split"}
        self.create_widget.setVisible(show_create)
        self.review_widget.setVisible(show_review)

        target_button = {
            "create": self.create_button,
            "review": self.review_button,
            "split": self.split_button,
        }[normalized_mode]
        target_button.setChecked(True)

        if normalized_mode == "split":
            self._schedule_equal_split()

        mode_changed = normalized_mode != self._mode
        self._mode = normalized_mode
        if mode_changed:
            self.mode_changed.emit(normalized_mode)

    def _schedule_equal_split(self):
        if self._equal_split_pending:
            return
        self._equal_split_pending = True
        QTimer.singleShot(0, self._apply_equal_split)

    def _apply_equal_split(self):
        self._equal_split_pending = False
        if self._mode != "split":
            return

        available_width = max(
            2,
            self.splitter.width()
            - self.splitter.handleWidth() * max(0, self.splitter.count() - 1),
        )
        left_width = available_width // 2
        self.splitter.setSizes([left_width, available_width - left_width])
