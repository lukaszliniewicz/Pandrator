import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QMimeData
from PyQt6.QtWidgets import QApplication, QPlainTextEdit

from pandrator.gui.dialogs.paste_text_dialog import PasteTextDialog
from pandrator.gui.dialogs.source_picker_dialog import SourcePickerDialog


class SourceDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def _write_file(self, name: str, content: bytes) -> str:
        path = os.path.join(self.temp_dir.name, name)
        with open(path, "wb") as file_handle:
            file_handle.write(content)
        return path

    def test_paste_dialog_accepts_plain_text_only(self):
        dialog = PasteTextDialog()
        self.addCleanup(dialog.close)

        mime_data = QMimeData()
        mime_data.setHtml("<p><b>Styled</b> text</p>")
        mime_data.setText("Styled text")
        dialog.text_edit.insertFromMimeData(mime_data)

        self.assertIsInstance(dialog.text_edit, QPlainTextEdit)
        self.assertEqual(dialog.get_data()["text"], "Styled text")

    def test_source_picker_previews_text_and_opens_selected_source(self):
        source_path = self._write_file("sample.srt", b"1\n00:00:00,000 --> 00:00:01,000\nHello\n")
        dialog = SourcePickerDialog(
            [{"name": "sample.srt", "source_type": "srt", "source_path": source_path}]
        )
        self.addCleanup(dialog.close)

        preview = dialog.preview_box.toPlainText()
        self.assertIn("sample.srt", preview)
        self.assertIn("Hello", preview)
        self.assertTrue(dialog.open_preview_button.isEnabled())

        with patch(
            "pandrator.gui.dialogs.source_picker_dialog.QDesktopServices.openUrl",
            return_value=True,
        ) as open_url:
            dialog._open_selected_preview()

        open_url.assert_called_once()
        self.assertEqual(
            os.path.normcase(os.path.abspath(open_url.call_args.args[0].toLocalFile())),
            os.path.normcase(os.path.abspath(source_path)),
        )

    def test_source_picker_guides_binary_preview_and_limits_long_text(self):
        pdf_path = self._write_file("sample.pdf", b"%PDF-1.4")
        binary_preview = SourcePickerDialog.build_source_preview(pdf_path)
        self.assertIn("Use Open Preview", binary_preview)

        long_text_path = self._write_file(
            "long.txt",
            ("x" * (SourcePickerDialog.PREVIEW_CHARACTER_LIMIT + 10)).encode("utf-8"),
        )
        text_preview = SourcePickerDialog.build_source_preview(long_text_path)
        self.assertIn("[Preview truncated]", text_preview)
        self.assertNotIn("x" * (SourcePickerDialog.PREVIEW_CHARACTER_LIMIT + 1), text_preview)

        missing_preview = SourcePickerDialog.build_source_preview(
            os.path.join(self.temp_dir.name, "missing.txt")
        )
        self.assertIn("missing", missing_preview.lower())


if __name__ == "__main__":
    unittest.main()
