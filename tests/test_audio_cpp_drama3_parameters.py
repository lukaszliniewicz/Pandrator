import unittest

from pandrator.logic.audio_cpp_parameters import (
    request_parameters_for_family,
    request_parameters_for_model,
    validate_model_options,
)


class AudioCppDrama3ParameterTests(unittest.TestCase):
    drama_model_id = "fish_drama3_q4_experts_q8_backbone"
    plapre_model_id = "plapre_3_bf16"

    def _assert_drama_parameters(self, parameters):
        self.assertEqual(4097, parameters["top_k"]["default"])
        self.assertEqual(1, parameters["top_k"]["minimum"])
        self.assertEqual(1024, parameters["max_tokens"]["default"])
        self.assertEqual(128, parameters["text_chunk_size"]["default"])
        self.assertTrue({"temperature", "top_p", "text_chunk_size"}.issubset(parameters))

        unsupported = {
            "encode_reference_only",
            "reference_text",
            "multi_reference_cond",
            "references",
            "previous_takes",
            "previous_takes_file",
        }
        session_only = {
            "backbone_prefill_cache_mb",
            "tower_prefill_cache_mb",
            "profile_first_audio",
            "context_length",
            "cpu_expert_layers",
            "disk_experts",
            "pin_experts",
            "expert_cache_size",
            "codec_encoder_f32_accumulation",
            "codec_encoder_bf16_compute",
            "codec_encoder_chunk_samples",
            "codec_backend",
        }
        self.assertTrue(unsupported.isdisjoint(parameters))
        self.assertTrue(session_only.isdisjoint(parameters))

    def test_drama_family_and_model_exclude_reference_and_session_options(self):
        self._assert_drama_parameters(request_parameters_for_family("fish_drama3"))
        self._assert_drama_parameters(
            request_parameters_for_model("fish_drama3", self.drama_model_id)
        )

    def test_plapre_keeps_sampling_xvector_and_chunk_controls(self):
        parameters = request_parameters_for_family("plapre")

        self.assertTrue(
            {
                "temperature",
                "top_p",
                "top_k",
                "seed",
                "x_vector_only_mode",
                "text_chunk_size",
                "text_chunk_mode",
                "silence_duration_sec",
            }.issubset(parameters)
        )
        self.assertEqual(False, parameters["x_vector_only_mode"]["default"])
        unsupported = {
            "word_duration_frames",
            "pronunciations",
            "edit_start_frame",
            "edit_end_frame",
            "context_text",
            "context_audio_file",
            "same_speaker",
            "voice_id",
            "reference_text",
        }
        self.assertTrue(unsupported.isdisjoint(parameters))

    def test_unintegrated_drama_and_plapre_options_are_rejected(self):
        excluded_options = (
            ("fish_drama3", "encode_reference_only", True, self.drama_model_id),
            ("fish_drama3", "multi_reference_cond", "[]", self.drama_model_id),
            ("fish_drama3", "references", "[]", self.drama_model_id),
            ("fish_drama3", "previous_takes", "[]", self.drama_model_id),
            (
                "fish_drama3",
                "previous_takes_file",
                "path-that-must-not-be-read.json",
                self.drama_model_id,
            ),
            ("plapre", "word_duration_frames", "[1, 2]", self.plapre_model_id),
            ("plapre", "pronunciations", "[]", self.plapre_model_id),
            ("plapre", "edit_start_frame", 0, self.plapre_model_id),
            ("plapre", "edit_end_frame", 1, self.plapre_model_id),
            ("plapre", "context_text", "Earlier utterance", self.plapre_model_id),
            (
                "plapre",
                "context_audio_file",
                "path-that-must-not-be-read.wav",
                self.plapre_model_id,
            ),
            ("plapre", "same_speaker", True, self.plapre_model_id),
            ("plapre", "voice_id", "tor", self.plapre_model_id),
            ("plapre", "reference_text", "Transcript", self.plapre_model_id),
        )
        for family, option, value, model_id in excluded_options:
            with self.subTest(family=family, option=option):
                with self.assertRaisesRegex(ValueError, "Unknown"):
                    validate_model_options(
                        family, {option: value}, model_id=model_id
                    )

    def test_drama_model_sampling_roundtrip_and_top_k_lower_bound(self):
        sampling = {
            "temperature": 0.8,
            "top_p": 0.9,
            "top_k": 64,
            "seed": 17,
            "text_chunk_size": 128,
        }
        self.assertEqual(
            sampling,
            validate_model_options(
                "fish_drama3", sampling, model_id=self.drama_model_id
            ),
        )
        with self.assertRaisesRegex(ValueError, "at least 1"):
            validate_model_options(
                "fish_drama3", {"top_k": 0}, model_id=self.drama_model_id
            )


if __name__ == "__main__":
    unittest.main()
