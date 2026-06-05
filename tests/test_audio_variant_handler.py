import os
import tempfile
import unittest

from pandrator.logic import audio_variant_handler


class AudioVariantHandlerTests(unittest.TestCase):
    def test_rvc_variant_registration_tracks_existing_sentence_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = {
                "rvc_model": "Demo Voice",
                "pitch": 0,
                "filter_radius": 3,
                "index_rate": 0.3,
                "volume_envelope": 1.0,
                "protect": 0.3,
                "f0_method": "rmvpe",
            }
            sentence_path = audio_variant_handler.rvc_variant_sentence_path(
                temp_dir,
                settings,
                "Demo Session",
                "7",
                ensure_dir=True,
            )
            with open(sentence_path, "wb") as file_handle:
                file_handle.write(b"placeholder")

            record = audio_variant_handler.register_rvc_variant_sentence(temp_dir, settings, "7")
            variants = audio_variant_handler.list_rvc_variants(temp_dir, "Demo Session")

            self.assertEqual(record["id"], variants[0]["id"])
            self.assertEqual(["7"], variants[0]["sentence_numbers"])
            self.assertIn("demo-voice", variants[0]["id"])

    def test_rvc_variant_settings_get_distinct_ids(self):
        base_settings = {
            "rvc_model": "Demo Voice",
            "pitch": 0,
            "filter_radius": 3,
            "index_rate": 0.3,
            "volume_envelope": 1.0,
            "protect": 0.3,
            "f0_method": "rmvpe",
        }
        shifted_settings = {**base_settings, "pitch": 2}

        first_id = audio_variant_handler.rvc_variant_id_for_settings(base_settings)
        second_id = audio_variant_handler.rvc_variant_id_for_settings(shifted_settings)

        self.assertNotEqual(first_id, second_id)

    def test_removing_variant_sentence_deletes_file_and_prunes_empty_variant(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = {
                "rvc_model": "Demo Voice",
                "pitch": 0,
                "filter_radius": 3,
                "index_rate": 0.3,
                "volume_envelope": 1.0,
                "protect": 0.3,
                "f0_method": "rmvpe",
            }
            sentence_path = audio_variant_handler.rvc_variant_sentence_path(
                temp_dir,
                settings,
                "Demo Session",
                "1",
                ensure_dir=True,
            )
            with open(sentence_path, "wb") as file_handle:
                file_handle.write(b"placeholder")
            audio_variant_handler.register_rvc_variant_sentence(temp_dir, settings, "1")

            removed_count = audio_variant_handler.remove_variant_sentences(
                temp_dir,
                "Demo Session",
                ["1"],
            )

            self.assertEqual(1, removed_count)
            self.assertFalse(os.path.exists(sentence_path))
            self.assertEqual([], audio_variant_handler.list_rvc_variants(temp_dir, "Demo Session"))


if __name__ == "__main__":
    unittest.main()
