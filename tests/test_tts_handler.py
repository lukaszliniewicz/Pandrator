import unittest

from pandrator.logic import tts_handler


class TTSHandlerTests(unittest.TestCase):
    def test_kokoro_voice_language_inference(self):
        self.assertEqual(tts_handler.infer_kokoro_voice_language_code("af_heart"), "en")
        self.assertEqual(tts_handler.infer_kokoro_voice_language_code("bf_alice"), "en-gb")
        self.assertEqual(tts_handler.infer_kokoro_voice_language_code("jf_alpha"), "ja")
        self.assertEqual(tts_handler.infer_kokoro_voice_language_code("martin"), "de")
        self.assertEqual(tts_handler.infer_kokoro_voice_language_code("alloy"), "en")
        self.assertEqual(
            tts_handler.infer_kokoro_voice_language_code("af_heart(0.7)+am_echo(0.3)"),
            "en",
        )
        self.assertEqual(
            tts_handler.infer_kokoro_voice_language_code("af_heart+jf_alpha"),
            "",
        )

    def test_fishs2_payload_includes_advanced_settings(self):
        payload = tts_handler._build_fishs2_payload(
            "Hello",
            {
                "xtts_model": "fishs2",
                "speaker": "demo-voice",
                "speed": 1.25,
                "fishs2_temperature": 0.8,
                "fishs2_top_p": 0.65,
                "fishs2_chunk_length": 240,
                "fishs2_latency": "normal",
                "fishs2_normalize": False,
                "fishs2_prosody_volume": 3.5,
                "fishs2_normalize_loudness": False,
            },
        )

        self.assertEqual(payload["model"], "fishaudio/s2-pro")
        self.assertEqual(payload["voice"], "demo-voice")
        self.assertEqual(payload["temperature"], 0.8)
        self.assertEqual(payload["top_p"], 0.65)
        self.assertEqual(payload["chunk_length"], 240)
        self.assertEqual(payload["latency"], "normal")
        self.assertFalse(payload["normalize"])
        self.assertEqual(payload["speed"], 1.25)
        self.assertEqual(payload["prosody"]["speed"], 1.25)
        self.assertEqual(payload["prosody"]["volume"], 3.5)
        self.assertFalse(payload["prosody"]["normalize_loudness"])

    def test_fishs2_payload_clamps_advanced_settings(self):
        payload = tts_handler._build_fishs2_payload(
            "Hello",
            {
                "speed": 5.0,
                "fishs2_temperature": 2.0,
                "fishs2_top_p": -1.0,
                "fishs2_chunk_length": 999,
                "fishs2_latency": "fastest",
                "fishs2_prosody_volume": -100.0,
            },
        )

        self.assertEqual(payload["temperature"], 1.0)
        self.assertEqual(payload["top_p"], 0.0)
        self.assertEqual(payload["chunk_length"], 300)
        self.assertEqual(payload["latency"], "balanced")
        self.assertEqual(payload["speed"], 2.0)
        self.assertEqual(payload["prosody"]["volume"], -20.0)

    def test_chatterbox_payload_construction(self):
        from unittest.mock import patch
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.content = b"fake audio content"
            
            tts_handler._request_chatterbox_audio(
                "Hello world",
                {
                    "xtts_model": "turbo",
                    "speaker": "test-voice",
                    "speed": 1.2,
                    "language": "en",
                    "temperature": 0.7,
                    "exaggeration": 0.5,
                    "cfg_weight": 1.5,
                },
                "http://localhost:8040"
            )
            
            mock_post.assert_called_once()
            called_args, called_kwargs = mock_post.call_args
            payload = called_kwargs["json"]
            self.assertEqual(payload["model"], "chatterbox-turbo")
            self.assertEqual(payload["input"], "Hello world")
            self.assertEqual(payload["voice"], "test-voice")
            self.assertEqual(payload["speed"], 1.2)
            self.assertEqual(payload["language"], "en")
            self.assertEqual(payload["temperature"], 0.7)
            self.assertEqual(payload["exaggeration"], 0.5)
            self.assertEqual(payload["cfg_weight"], 1.5)

        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            
            tts_handler._request_chatterbox_audio(
                "Bonjour",
                {
                    "xtts_model": "chatterbox-multilingual",
                    "language": "fr",
                },
                "http://localhost:8040"
            )
            
            mock_post.assert_called_once()
            called_args, called_kwargs = mock_post.call_args
            payload = called_kwargs["json"]
            self.assertEqual(payload["model"], "chatterbox-multilingual")
            self.assertEqual(payload["input"], "Bonjour")
            self.assertIsNone(payload["voice"])
            self.assertEqual(payload["language"], "fr")


if __name__ == "__main__":
    unittest.main()
