from PyQt6.QtWidgets import QDialog, QFormLayout, QLineEdit, QDialogButtonBox

class MetadataDialog(QDialog):
    def __init__(self, metadata: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Metadata")

        layout = QFormLayout(self)

        self.title_edit = QLineEdit(metadata.get("title", ""))
        self.album_edit = QLineEdit(metadata.get("album", ""))
        self.artist_edit = QLineEdit(metadata.get("artist", ""))
        self.genre_edit = QLineEdit(metadata.get("genre", ""))
        self.language_edit = QLineEdit(metadata.get("language", ""))

        layout.addRow("Title:", self.title_edit)
        layout.addRow("Album:", self.album_edit)
        layout.addRow("Artist:", self.artist_edit)
        layout.addRow("Genre:", self.genre_edit)
        layout.addRow("Language:", self.language_edit)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def get_metadata(self) -> dict:
        """Returns the updated metadata from the dialog's fields."""
        return {
            "title": self.title_edit.text(),
            "album": self.album_edit.text(),
            "artist": self.artist_edit.text(),
            "genre": self.genre_edit.text(),
            "language": self.language_edit.text()
        }
