import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QApplication, QWidget

from pandrator.gui.widgets.generated_sentences_widget import GeneratedSentencesWidget
from pandrator.gui.widgets.session_workspace import SessionWorkspace


class FakeReviewLogic(QWidget):
    state_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.sentences = []
        self.playing_sentence_number = None
        self.state = SimpleNamespace(
            session_name="Test Session",
            audio_processing=SimpleNamespace(output_format="wav"),
        )

    def list_audio_variants(self):
        return [{"id": "source", "label": "Original (0)"}]

    def get_active_audio_variant_id(self):
        return "source"

    def set_active_audio_variant(self, _variant_id):
        pass

    def get_audio_variant_sentences_snapshot(self):
        return [dict(sentence) for sentence in self.sentences]

    def get_processed_sentences_snapshot(self):
        return [dict(sentence) for sentence in self.sentences]

    def get_source_audio_sentence_numbers(self):
        return [
            str(sentence["sentence_number"])
            for sentence in self.sentences
            if sentence.get("tts_generated") == "yes"
        ]

    def get_current_playing_sentence_number(self):
        return self.playing_sentence_number

    def is_generation_running(self):
        return False

    def is_regeneration_running(self):
        return False

    def is_rvc_processing_running(self):
        return False

    def is_rvc_available(self):
        return False

    def is_generation_or_regeneration_running(self):
        return False

    def stop_playback(self):
        self.playing_sentence_number = None
        self.state_changed.emit()

    def mark_sentence(self, sentence_number, marked):
        for sentence in self.sentences:
            if str(sentence.get("sentence_number")) == str(sentence_number):
                sentence["marked"] = bool(marked)
        self.state_changed.emit()

    def update_sentence_text(self, sentence_number, text):
        for sentence in self.sentences:
            if str(sentence.get("sentence_number")) == str(sentence_number):
                sentence["processed_sentence"] = text


class DummyReview(QWidget):
    create_requested = pyqtSignal()
    review_count_changed = pyqtSignal(int)

    def review_count(self):
        return 0


class GeneratedSentencesUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_empty_review_shows_guidance_and_disables_actions(self):
        logic = FakeReviewLogic()
        widget = GeneratedSentencesWidget(logic)

        self.assertIs(widget.content_stack.currentWidget(), widget.empty_state)
        self.assertEqual(
            widget.empty_state_title.text(),
            "Nothing has been generated yet",
        )
        self.assertFalse(widget.play_button.isEnabled())
        self.assertFalse(widget.play_as_playlist_button.isEnabled())
        self.assertFalse(widget.stop_button.isEnabled())
        self.assertFalse(widget.save_output_button.isEnabled())
        self.assertFalse(widget.edit_button.isEnabled())

    def test_single_table_filters_marked_rows_and_enables_contextual_actions(self):
        logic = FakeReviewLogic()
        logic.sentences = [
            {
                "sentence_number": "1",
                "processed_sentence": "First sentence",
                "tts_generated": "yes",
                "marked": False,
            },
            {
                "sentence_number": "2",
                "processed_sentence": "Second sentence",
                "tts_generated": "yes",
                "marked": True,
            },
        ]
        widget = GeneratedSentencesWidget(logic)

        self.assertEqual(widget.sentences_list.columnCount(), 3)
        self.assertEqual(widget.sentences_list.rowCount(), 2)
        self.assertEqual(widget.all_filter_button.text(), "All 2")
        self.assertEqual(widget.marked_filter_button.text(), "Marked 1")
        self.assertTrue(widget.play_as_playlist_button.isEnabled())
        self.assertTrue(widget.save_output_button.isEnabled())
        self.assertFalse(widget.play_button.isEnabled())

        widget.sentences_list.selectRow(0)
        self.assertTrue(widget.play_button.isEnabled())
        self.assertTrue(widget.edit_button.isEnabled())
        self.assertEqual(widget.selection_label.text(), "1 selected")

        widget.marked_filter_button.click()
        self.assertEqual(widget.sentences_list.rowCount(), 1)
        number_item = widget.sentences_list.item(0, widget.NUMBER_COLUMN)
        self.assertEqual(number_item.text(), "2")

    def test_workspace_switches_between_shared_create_review_and_split_views(self):
        create_widget = QWidget()
        review_widget = DummyReview()
        workspace = SessionWorkspace(create_widget, review_widget)

        self.assertEqual(workspace.mode(), "create")
        self.assertFalse(workspace.create_scroll_area.isHidden())
        self.assertTrue(review_widget.isHidden())

        workspace.set_mode("review")
        self.assertEqual(workspace.mode(), "review")
        self.assertTrue(workspace.create_scroll_area.isHidden())
        self.assertFalse(review_widget.isHidden())

        workspace.set_mode("split")
        self.assertEqual(workspace.mode(), "split")
        self.assertFalse(workspace.create_scroll_area.isHidden())
        self.assertFalse(review_widget.isHidden())

        review_widget.create_requested.emit()
        self.assertEqual(workspace.mode(), "create")


if __name__ == "__main__":
    unittest.main()
