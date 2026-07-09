import copy
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pandrator.app_state import AppState
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
        self.log_message = _DummySignal()
        self.progress_updated = _DummySignal()
        self.regeneration_thread = None
        self.rvc_processing_thread = None
        self.cancel_regeneration_flag = threading.Event()
        self.stop_playback_calls = 0
        self.playlist_active = False
        self.playlist_sentences = []
        self.current_playlist_index = 0
        self.timer_started = False
        self.playlist_timer = SimpleNamespace(start=self._start_playlist_timer)
        self.session_activity_calls = []
        self.processed_sentences = []
        self.state = SimpleNamespace(session_name="Demo Session", active_audio_variant_id="source")

    def _notify_user(self, message, timeout_ms=5000, level="info"):
        self.notifications.append((message, timeout_ms, level))

    def stop_playback(self):
        self.stop_playback_calls += 1

    def _start_playlist_timer(self):
        self.timer_started = True

    def _set_session_activity(self, headline, detail, tone):
        self.session_activity_calls.append((headline, detail, tone))

    def get_processed_sentences_snapshot(self):
        return copy.deepcopy(self.processed_sentences)

    def get_audio_variant_sentences_snapshot(self):
        return self.get_processed_sentences_snapshot()

    def _set_processed_sentences_snapshot(self, sentences):
        self.processed_sentences = copy.deepcopy(sentences)

    def cancel_regeneration(self):
        return AppLogic.cancel_regeneration(self)


class _KokoroLogicHarness:
    def __init__(self):
        self.state = AppState()
        self.state.tts.service = "Kokoro"
        self.state_changed = _DummySignal()
        self.notifications = []
        self.persist_global_calls = 0
        self.persist_session_calls = 0

    def _sync_kokoro_language_from_voice(self, speaker_name):
        return AppLogic._sync_kokoro_language_from_voice(self, speaker_name)

    def _normalize_kokoro_default_voices(self, raw_defaults):
        return AppLogic._normalize_kokoro_default_voices(raw_defaults)

    def _persist_global_settings(self, force=False):
        self.persist_global_calls += 1

    def _persist_session_config(self, force=False):
        self.persist_session_calls += 1

    def _is_named_session_active(self):
        return True

    def _notify_user(self, message, timeout_ms=5000, level="info"):
        self.notifications.append((message, timeout_ms, level))


class AppLogicNotificationTests(unittest.TestCase):
    def test_build_source_loaded_notification_specializes_dubbing_sources(self):
        srt_message = AppLogic._build_source_loaded_notification("captions.srt", ".srt")
        video_message = AppLogic._build_source_loaded_notification("clip.mp4", ".mp4")
        audio_message = AppLogic._build_source_loaded_notification("meeting.mp3", ".mp3")
        text_message = AppLogic._build_source_loaded_notification("book.txt", ".txt")

        self.assertIn("SRT source loaded: captions.srt.", srt_message)
        self.assertIn("Fine-Tune Timings", srt_message)
        self.assertEqual("Dubbing source loaded: clip.mp4", video_message)
        self.assertEqual("Dubbing source loaded: meeting.mp3", audio_message)
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

    def test_cancel_regeneration_sets_flag_and_updates_activity(self):
        harness = _LogicHarness()
        harness._is_regeneration_running = lambda: True

        AppLogic.cancel_regeneration(harness)

        self.assertTrue(harness.cancel_regeneration_flag.is_set())
        self.assertEqual(
            [
                (
                    "Cancelling regeneration",
                    "The current sentence will finish before the remaining regeneration queue stops.",
                    "warning",
                )
            ],
            harness.session_activity_calls,
        )
        self.assertEqual(
            [("Cancelling regeneration after current sentence...",)],
            harness.log_message.calls,
        )
        self.assertEqual([()], harness.state_changed.calls)

    def test_cancel_generation_delegates_to_regeneration_when_needed(self):
        harness = _LogicHarness()
        harness._is_generation_running = lambda: False
        harness._is_regeneration_running = lambda: True

        AppLogic.cancel_generation(harness)

        self.assertTrue(harness.cancel_regeneration_flag.is_set())
        self.assertEqual([], harness.notifications)

    def test_kokoro_preferred_voice_uses_language_default(self):
        selected = AppLogic._preferred_kokoro_voice_for_language(
            "ja",
            ["af_heart", "jf_alpha"],
            current_speaker="af_heart",
            default_voices={"ja": "jf_alpha"},
        )

        self.assertEqual(selected, "jf_alpha")

    def test_kokoro_speaker_selection_syncs_language(self):
        harness = _KokoroLogicHarness()
        harness.state.tts.language = "en"

        AppLogic.set_tts_speaker_voice(harness, "jf_alpha")

        self.assertEqual(harness.state.tts.speaker, "jf_alpha")
        self.assertEqual(harness.state.tts.language, "ja")
        self.assertEqual([()], harness.state_changed.calls)

    def test_save_kokoro_default_voice_persists_language_mapping(self):
        harness = _KokoroLogicHarness()

        success, message = AppLogic.save_kokoro_default_voice(harness, "bf_alice")

        self.assertTrue(success)
        self.assertIn("bf_alice", message)
        self.assertEqual(harness.state.tts.kokoro_default_voices["en-gb"], "bf_alice")
        self.assertEqual(harness.state.tts.speaker, "bf_alice")
        self.assertEqual(harness.state.tts.language, "en-gb")
        self.assertEqual(1, harness.persist_global_calls)
        self.assertEqual(1, harness.persist_session_calls)
        self.assertEqual([()], harness.state_changed.calls)

    def test_regeneration_worker_stops_after_cancellation_request(self):
        harness = _LogicHarness()
        harness.processed_sentences = [
            {"sentence_number": "1", "text": "First"},
            {"sentence_number": "2", "text": "Second"},
        ]
        harness.regeneration_thread = threading.current_thread()
        executed_numbers = []

        def execute_generation(sentence_dict):
            executed_numbers.append(sentence_dict["sentence_number"])
            if sentence_dict["sentence_number"] == "1":
                harness.cancel_regeneration_flag.set()
            updated = dict(sentence_dict)
            updated["tts_generated"] = "yes"
            return True, updated

        harness._execute_generation_for_sentence = execute_generation

        AppLogic._regenerate_sentences_thread(harness, ["1", "2"])

        self.assertEqual(["1"], executed_numbers)
        self.assertEqual("yes", harness.processed_sentences[0]["tts_generated"])
        self.assertNotIn("tts_generated", harness.processed_sentences[1])
        self.assertEqual(None, harness.regeneration_thread)
        self.assertFalse(harness.cancel_regeneration_flag.is_set())
        self.assertIn(
            ("Regeneration cancelled", "Regenerated 1 sentence(s).", "warning"),
            harness.session_activity_calls,
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
