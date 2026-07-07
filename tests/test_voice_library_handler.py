import json
import os
import shutil
import tempfile
import unittest

from pandrator.logic import voice_library_handler


class VoiceLibraryHandlerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_attrs = {
            "APP_ROOT_DIR": voice_library_handler.APP_ROOT_DIR,
            "VOICE_LIBRARY_DIR": voice_library_handler.VOICE_LIBRARY_DIR,
            "VOICE_LIBRARY_STORAGE_DIR": voice_library_handler.VOICE_LIBRARY_STORAGE_DIR,
            "VOICE_LIBRARY_INDEX_FILE": voice_library_handler.VOICE_LIBRARY_INDEX_FILE,
            "VOICE_LIBRARY_SEED_FILE": voice_library_handler.VOICE_LIBRARY_SEED_FILE,
        }

        self.seed_dir = os.path.join(self.temp_dir.name, "tts_voices")
        os.makedirs(self.seed_dir, exist_ok=True)
        self.sample_source = os.path.join(self.seed_dir, "sample_male_new.wav")

        repo_sample = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "tts_voices", "sample_male_new.wav")
        )
        shutil.copy2(repo_sample, self.sample_source)

        self.seed_path = os.path.join(self.seed_dir, "voice_library_seed.json")
        with open(self.seed_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "version": 1,
                    "voices": [
                        {
                            "id": "sample_male_new",
                            "name": "sample_male_new",
                            "notes": "Seeded test voice.",
                            "prompt_text": "",
                            "language_code": "en",
                            "samples": [
                                {
                                    "id": "sample_male_new",
                                    "file_name": "sample_male_new.wav",
                                    "relative_path": "sample_male_new.wav",
                                    "transcript": "seeded transcript",
                                }
                            ],
                        }
                    ],
                },
                f,
            )

        voice_library_handler.APP_ROOT_DIR = self.temp_dir.name
        voice_library_handler.VOICE_LIBRARY_DIR = self.seed_dir
        voice_library_handler.VOICE_LIBRARY_STORAGE_DIR = os.path.join(self.seed_dir, "library")
        voice_library_handler.VOICE_LIBRARY_INDEX_FILE = os.path.join(self.seed_dir, "voice_library.json")
        voice_library_handler.VOICE_LIBRARY_SEED_FILE = self.seed_path

    def tearDown(self):
        for key, value in self.original_attrs.items():
            setattr(voice_library_handler, key, value)
        self.temp_dir.cleanup()

    def test_ensure_bundled_voice_library_adds_seed_voice(self):
        voice_library_handler.ensure_bundled_voice_library()

        with open(voice_library_handler.VOICE_LIBRARY_INDEX_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)

        self.assertEqual(payload.get("version"), 1)
        voices = payload.get("voices")
        self.assertEqual(len(voices), 1)
        voice = voices[0]
        self.assertEqual(voice.get("id"), "sample_male_new")
        self.assertEqual(len(voice.get("samples")), 1)

        sample_record = voice.get("samples")[0]
        sample_path = os.path.join(
            voice_library_handler.VOICE_LIBRARY_DIR,
            sample_record.get("relative_path"),
        )
        self.assertTrue(os.path.isfile(sample_path))

    def test_ensure_bundled_voice_library_is_idempotent(self):
        voice_library_handler.ensure_bundled_voice_library()
        first_payload = voice_library_handler.list_voices()
        self.assertEqual(len(first_payload), 1)

        voice_library_handler.ensure_bundled_voice_library()
        second_payload = voice_library_handler.list_voices()
        self.assertEqual(len(second_payload), 1)
