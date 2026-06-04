from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pandrator.app_logic import AppLogic


class _DummySignal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class _LogicHarness:
    def __init__(self):
        self.notifications = []
        self.state_changed = _DummySignal()
        self.show_error = _DummySignal()
        self.regeneration_thread = None
        self.rvc_processing_thread = None
        self.stop_playback_calls = 0
        self.playlist_active = False
        self.playlist_sentences = []
        self.current_playlist_index = 0
        self.timer_started = False
        self.playlist_timer = SimpleNamespace(start=self._start_playlist_timer)

    def _notify_user(self, message, timeout_ms=5000, level="info"):
        self.notifications.append((message, timeout_ms, level))

    def stop_playback(self):
        self.stop_playback_calls += 1

    def _start_playlist_timer(self):
        self.timer_started = True


class AppLogicNotificationTests(unittest.TestCase):
    def test_build_source_loaded_notification_specializes_dubbing_sources(self):
        srt_message = AppLogic._build_source_loaded_notification("captions.srt", ".srt")
        video_message = AppLogic._build_source_loaded_notification("clip.mp4", ".mp4")
        text_message = AppLogic._build_source_loaded_notification("book.txt", ".txt")

        self.assertIn("SRT source loaded: captions.srt.", srt_message)
        self.assertIn("Fine-Tune Timings", srt_message)
        self.assertEqual("Dubbing source loaded: clip.mp4", video_message)
        self.assertEqual("Source loaded: book.txt", text_message)

    def test_regenerate_sentences_normalizes_and_dedupes_selection(self):
        harness = _LogicHarness()
        harness._is_generation_running = lambda: False
        harness._is_regeneration_running = lambda: False
        harness._is_rvc_processing_running = lambda: False
        harness._regenerate_sentences_thread = object()

        def run_threaded_task(target, *args):
            harness.thread_target = target
            harness.thread_args = args
            return "thread"

        harness._run_threaded_task = run_threaded_task

        AppLogic.regenerate_sentences(harness, [" 1 ", "", "1", "2 ", "2"])

        self.assertEqual((["1", "2"],), harness.thread_args)
        self.assertEqual("thread", harness.regeneration_thread)
        self.assertEqual([], harness.notifications)
        self.assertEqual(1, harness.stop_playback_calls)

    def test_regenerate_sentences_warns_when_selection_is_empty(self):
        harness = _LogicHarness()
        harness._is_generation_running = lambda: False
        harness._is_regeneration_running = lambda: False
        harness._is_rvc_processing_running = lambda: False

        AppLogic.regenerate_sentences(harness, ["", "  "])

        self.assertEqual(
            [("Select at least one sentence to regenerate.", 5000, "warning")],
            harness.notifications,
        )

    def test_process_sentences_with_rvc_warns_when_selection_is_empty(self):
        harness = _LogicHarness()
        harness._is_generation_running = lambda: False
        harness._is_regeneration_running = lambda: False
        harness._is_rvc_processing_running = lambda: False
        harness.is_rvc_available = lambda: True
        harness.state = SimpleNamespace(rvc=SimpleNamespace(rvc_model="demo-model"))

        AppLogic.process_sentences_with_rvc(harness, ["", "   "])

        self.assertEqual(
            [("Select at least one valid sentence for RVC processing.", 5000, "warning")],
            harness.notifications,
        )

    def test_play_playlist_warns_when_no_audio_file_can_be_played(self):
        harness = _LogicHarness()
        harness.get_processed_sentences_snapshot = lambda: [
            {"sentence_number": "1", "tts_generated": "yes"},
        ]
        harness._play_current_playlist_item = lambda: False

        AppLogic.play_playlist(harness)

        self.assertEqual(
            [("No playable audio found in playlist.", 5000, "warning")],
            harness.notifications,
        )
        self.assertFalse(harness.timer_started)
        self.assertEqual(2, harness.stop_playback_calls)

    def test_save_metadata_notifies_after_successful_save(self):
        harness = _LogicHarness()
        harness.state = SimpleNamespace(session_name="Demo Session", metadata=None)

        with patch("pandrator.app_logic.session_handler.save_metadata") as save_metadata:
            AppLogic.save_metadata(harness, {"title": "Demo"})

        save_metadata.assert_called_once_with("Demo Session", {"title": "Demo"})
        self.assertEqual(
            [("Metadata saved.", 5000, "info")],
            harness.notifications,
        )

    def test_start_xtts_training_warns_when_training_is_already_running(self):
        harness = _LogicHarness()
        harness._is_xtts_training_running = lambda: True

        AppLogic.start_xtts_training(harness, {"epochs": 1})

        self.assertEqual(
            [("XTTS training is already running.", 5000, "warning")],
            harness.notifications,
        )


if __name__ == "__main__":
    unittest.main()
