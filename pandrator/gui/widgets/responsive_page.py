from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLayout,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


DEFAULT_PAGE_MAX_WIDTH = 1400


def configure_form_grid(
    layout: QGridLayout,
    *,
    label_width: int = 180,
    field_column: int = 1,
    trailing_column: int | None = None,
) -> QGridLayout:
    """Give form grids compact labels and a predictable responsive field area."""

    layout.setHorizontalSpacing(12)
    layout.setVerticalSpacing(8)
    layout.setColumnMinimumWidth(0, label_width)
    layout.setColumnStretch(0, 0)
    layout.setColumnStretch(field_column, 0)
    if trailing_column is None:
        trailing_column = field_column + 1
    layout.setColumnStretch(trailing_column, 1)
    return layout


class ScrollableSettingsPage(QWidget):
    """Responsive, independently scrollable shell for form-heavy application pages."""

    def __init__(
        self,
        parent=None,
        *,
        max_content_width: int = DEFAULT_PAGE_MAX_WIDTH,
        page_object_name: str = "scrollableSettingsPage",
    ):
        super().__init__(parent)
        self.setObjectName(page_object_name)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("settingsPageScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.scroll_area.verticalScrollBar().setSingleStep(24)
        outer_layout.addWidget(self.scroll_area)

        self.page_shell = QWidget()
        self.page_shell.setObjectName("settingsPageShell")
        shell_layout = QHBoxLayout(self.page_shell)
        shell_layout.setContentsMargins(24, 16, 24, 24)
        shell_layout.setSpacing(0)
        shell_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        self.content_widget = QWidget()
        self.content_widget.setObjectName("settingsPageContent")
        self.content_widget.setMaximumWidth(max_content_width)
        self.content_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )

        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(8)
        self.content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.content_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        # The content receives almost all available width until it reaches its
        # maximum; equal side stretches then center it on wider displays.
        shell_layout.addStretch(1)
        shell_layout.addWidget(
            self.content_widget,
            100,
            alignment=Qt.AlignmentFlag.AlignTop,
        )
        shell_layout.addStretch(1)
        self.scroll_area.setWidget(self.page_shell)

    def showEvent(self, event):
        self._configure_form_controls()
        super().showEvent(event)

    def _configure_form_controls(self):
        for spinbox in self.content_widget.findChildren(QAbstractSpinBox):
            spinbox.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
            if spinbox.maximumWidth() > 180:
                spinbox.setMaximumWidth(180)
            if not spinbox.property("settingsWheelGuardInstalled"):
                spinbox.installEventFilter(self)
                spinbox.setProperty("settingsWheelGuardInstalled", True)

        for combo in self.content_widget.findChildren(QComboBox):
            if combo.maximumWidth() > 560:
                combo.setMaximumWidth(560)
            if not combo.property("settingsWheelGuardInstalled"):
                combo.installEventFilter(self)
                combo.setProperty("settingsWheelGuardInstalled", True)

        for line_edit in self.content_widget.findChildren(QLineEdit):
            if line_edit.maximumWidth() > 800:
                line_edit.setMaximumWidth(800)

    def eventFilter(self, watched, event):
        if (
            event.type() == QEvent.Type.Wheel
            and isinstance(event, QWheelEvent)
            and isinstance(watched, (QAbstractSpinBox, QComboBox))
            and not watched.hasFocus()
        ):
            self._scroll_page(event)
            return True
        return super().eventFilter(watched, event)

    def _scroll_page(self, event: QWheelEvent):
        scroll_bar = self.scroll_area.verticalScrollBar()
        pixel_delta = event.pixelDelta().y()
        if pixel_delta:
            distance = pixel_delta
        else:
            wheel_steps = event.angleDelta().y() / 120
            distance = int(
                wheel_steps
                * QApplication.wheelScrollLines()
                * max(16, scroll_bar.singleStep())
            )
        if distance:
            scroll_bar.setValue(scroll_bar.value() - distance)
        event.accept()
