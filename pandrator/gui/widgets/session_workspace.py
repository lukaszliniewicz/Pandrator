from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
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
        self._mode = "create"
        self._review_count = 0

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

        self.create_scroll_area = QScrollArea()
        self.create_scroll_area.setObjectName("workspaceCreateScrollArea")
        self.create_scroll_area.setWidgetResizable(True)
        self.create_scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.create_scroll_area.setWidget(self.create_widget)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setObjectName("sessionWorkspaceSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.create_scroll_area)
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

        self.set_mode("create")

    def _create_mode_button(self, text: str, mode: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("workspaceModeButton")
        button.setCheckable(True)
        button.setProperty("workspaceMode", mode)
        return button

    def mode(self) -> str:
        return self._mode

    def set_review_count(self, count: int):
        self._review_count = max(0, int(count or 0))
        self.review_button.setText(f"Review {self._review_count}")

    def set_mode(self, mode: str):
        normalized_mode = str(mode or "create").strip().lower()
        if normalized_mode not in {"create", "review", "split"}:
            normalized_mode = "create"

        show_create = normalized_mode in {"create", "split"}
        show_review = normalized_mode in {"review", "split"}
        self.create_scroll_area.setVisible(show_create)
        self.review_widget.setVisible(show_review)

        target_button = {
            "create": self.create_button,
            "review": self.review_button,
            "split": self.split_button,
        }[normalized_mode]
        target_button.setChecked(True)

        if normalized_mode == "split":
            available_width = max(2, self.splitter.width())
            self.splitter.setSizes([available_width // 2, available_width // 2])

        mode_changed = normalized_mode != self._mode
        self._mode = normalized_mode
        if mode_changed:
            self.mode_changed.emit(normalized_mode)
