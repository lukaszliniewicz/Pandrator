import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pydub import AudioSegment

from pandrator.app_logic import AppLogic
from pandrator.logic import audio_variant_handler


class _DummySignal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class AppLogicAudioVariantTests(unittest.TestCase):
    def test_rvc_variant_creation_preserves_source_wav(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = os.path.join(temp_dir, "Sentence_wavs")
            os.makedirs(source_dir, exist_ok=True)
            source_path = os.path.join(source_dir, "Demo Session_sentence_1.wav")
            exported_file = AudioSegment.silent(duration=50).export(source_path, format="wav")
            exported_file.close()
            source_mtime = os.path.getmtime(source_path)

            harness = SimpleNamespace(
                state=SimpleNamespace(session_name="Demo Session", active_audio_variant_id="source"),
                log_message=_DummySignal(),
            )
            harness._get_audio_variant_base_dir = lambda ensure_exists=False: temp_dir
            harness._find_source_sentence_wav_path = lambda sentence_number: source_path

            settings = {
                "rvc_model": "Demo Voice",
                "pitch": 0,
                "filter_radius": 3,
                "index_rate": 0.3,
                "volume_envelope": 1.0,
                "protect": 0.3,
                "f0_method": "rmvpe",
            }

            with patch("pandrator.app_logic.rvc_handler.process_with_rvc") as process_with_rvc:
                process_with_rvc.side_effect = lambda audio, _settings: audio
                variant_id = AppLogic._process_source_wav_to_rvc_variant(
                    harness,
                    "1",
                    settings,
                    source_wav_path=source_path,
                )

            variant_path = audio_variant_handler.variant_sentence_path(
                temp_dir,
                variant_id,
                "Demo Session",
                "1",
            )

            self.assertTrue(os.path.isfile(source_path))
            self.assertEqual(source_mtime, os.path.getmtime(source_path))
            self.assertTrue(os.path.isfile(variant_path))
            self.assertNotEqual(os.path.abspath(source_path), os.path.abspath(variant_path))


if __name__ == "__main__":
    unittest.main()
