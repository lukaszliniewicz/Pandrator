"""Expandable, uniform option card used by the installer UI."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class CapabilityBadge(QLabel):
    def __init__(self, text: str, supported: bool, parent=None):
        super().__init__(text, parent)
        self.setObjectName("voiceCapabilityBadge")
        self.setProperty("supported", supported)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedWidth(118)


class BackendOptionCard(QFrame):
    """A fixed-height summary that expands via its surface or chevron."""

    expanded_changed = pyqtSignal(bool)
    COLLAPSED_HEIGHT = 112

    def __init__(
        self,
        control: QWidget,
        description: str,
        *,
        extra_controls=(),
        details: str = "",
        languages: str = "",
        voice_cloning: bool | None = None,
        prebuilt_voices: bool | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("optionCard")
        self.setProperty("expanded", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._expanded = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        summary = QFrame(self)
        summary.setObjectName("optionCardSummary")
        summary.setFixedHeight(self.COLLAPSED_HEIGHT)
        summary_layout = QHBoxLayout(summary)
        summary_layout.setContentsMargins(16, 13, 12, 13)
        summary_layout.setSpacing(14)

        copy_layout = QVBoxLayout()
        copy_layout.setSpacing(5)
        copy_layout.addWidget(control)
        description_label = QLabel(description)
        description_label.setObjectName("mutedLabel")
        description_label.setWordWrap(True)
        description_label.setMinimumWidth(0)
        description_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        copy_layout.addWidget(description_label)
        copy_layout.addStretch()
        summary_layout.addLayout(copy_layout, 1)

        paired_capabilities = (
            voice_cloning is not None or prebuilt_voices is not None
        )
        if paired_capabilities:
            capability_layout = QVBoxLayout()
            capability_layout.setSpacing(5)
            capability_layout.addWidget(
                CapabilityBadge("Voice cloning", bool(voice_cloning))
            )
            capability_layout.addWidget(
                CapabilityBadge("Pre-built voices", bool(prebuilt_voices))
            )
            capability_layout.addStretch()
            summary_layout.addLayout(capability_layout)

        self.details_panel = QFrame(self)
        self.details_panel.setObjectName("optionCardDetails")
        details_layout = QVBoxLayout(self.details_panel)
        right_margin = 150 if paired_capabilities else 14
        details_layout.setContentsMargins(16, 14, right_margin, 16)
        details_layout.setSpacing(7)

        if languages:
            languages_heading = QLabel("SUPPORTED LANGUAGES")
            languages_heading.setObjectName("optionCardEyebrow")
            details_layout.addWidget(languages_heading)
            languages_label = QLabel(languages)
            languages_label.setObjectName("optionCardLanguages")
            languages_label.setWordWrap(True)
            details_layout.addWidget(languages_label)

        if details:
            note_label = QLabel(details)
            note_label.setObjectName("mutedLabel")
            note_label.setWordWrap(True)
            details_layout.addWidget(note_label)

        extra_controls = tuple(extra_controls or ())
        if extra_controls:
            settings_row = QHBoxLayout()
            settings_row.setContentsMargins(0, 4, 0, 0)
            settings_row.setSpacing(9)
            for extra_control in extra_controls:
                settings_row.addWidget(extra_control)
            settings_row.addStretch()
            details_layout.addLayout(settings_row)

        self._expandable = bool(languages or details or extra_controls)
        self.details_panel.setVisible(False)

        self.chevron = QToolButton(summary)
        self.chevron.setObjectName("optionCardChevron")
        self.chevron.setArrowType(Qt.ArrowType.DownArrow)
        self.chevron.setToolTip("Show details")
        self.chevron.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chevron.setVisible(self._expandable)
        self.chevron.clicked.connect(self.toggle_expanded)
        summary_layout.addWidget(
            self.chevron,
            0,
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
        )

        root.addWidget(summary)
        root.addWidget(self.details_panel)
        self._apply_expansion_geometry()

    @property
    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool):
        expanded = bool(expanded and self._expandable)
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self.details_panel.setVisible(expanded)
        self.chevron.setArrowType(
            Qt.ArrowType.UpArrow if expanded else Qt.ArrowType.DownArrow
        )
        self.chevron.setToolTip("Hide details" if expanded else "Show details")
        self.setProperty("expanded", expanded)
        self.style().unpolish(self)
        self.style().polish(self)
        self._apply_expansion_geometry()
        self.expanded_changed.emit(expanded)

    def toggle_expanded(self):
        self.set_expanded(not self._expanded)

    def _apply_expansion_geometry(self):
        if self._expanded:
            self.setMinimumHeight(self.COLLAPSED_HEIGHT)
            self.setMaximumHeight(16777215)
            self.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Preferred,
            )
        else:
            self.setMinimumHeight(self.COLLAPSED_HEIGHT)
            self.setMaximumHeight(self.COLLAPSED_HEIGHT)
            self.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
        self.updateGeometry()

    def mousePressEvent(self, event):
        if self._expandable and not self._is_interactive_child(
            self.childAt(event.position().toPoint())
        ):
            self.toggle_expanded()
        super().mousePressEvent(event)

    def _is_interactive_child(self, child: QWidget | None) -> bool:
        while child is not None and child is not self:
            if isinstance(
                child,
                (QAbstractButton, QComboBox, QLineEdit, QSpinBox),
            ):
                return True
            child = child.parentWidget()
        return False
