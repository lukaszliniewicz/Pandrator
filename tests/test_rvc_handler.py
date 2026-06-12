import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pydub import AudioSegment

from pandrator.logic import rvc_handler


def wav_bytes() -> bytes:
    output = io.BytesIO()
    AudioSegment.silent(duration=100).export(output, format="wav")
    return output.getvalue()


class RVCHandlerTests(unittest.TestCase):
    @patch("pandrator.logic.rvc_handler.requests.get")
    def test_is_available_uses_health_endpoint(self, get):
        get.return_value = Mock(
            json=lambda: {"ready": True},
            raise_for_status=lambda: None,
        )

        self.assertTrue(rvc_handler.is_rvc_available())
        get.assert_called_once_with(
            "http://127.0.0.1:8050/health",
            timeout=rvc_handler.RVC_HEALTH_TIMEOUT_SECONDS,
        )

    @patch("pandrator.logic.rvc_handler.requests.post")
    def test_process_with_rvc_posts_audio_and_parameters(self, post):
        post.return_value = Mock(ok=True, content=wav_bytes())
        source = AudioSegment.silent(duration=100)

        converted = rvc_handler.process_with_rvc(
            source,
            {"rvc_model": "alpha", "pitch": 2, "volume_envelope": 0.75},
        )

        self.assertEqual(len(converted), len(source))
        request = post.call_args
        self.assertEqual(request.kwargs["data"]["model"], "alpha")
        self.assertEqual(request.kwargs["data"]["pitch"], 2)
        self.assertEqual(request.kwargs["data"]["volume_envelope"], 0.75)

    @patch("pandrator.logic.rvc_handler.requests.post")
    def test_upload_copies_model_and_refreshes_service(self, post):
        post.return_value = Mock(raise_for_status=lambda: None)
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir) / "source"
            models_dir = Path(temp_dir) / "models"
            source_dir.mkdir()
            pth = source_dir / "alpha.pth"
            index = source_dir / "alpha.index"
            pth.write_bytes(b"pth")
            index.write_bytes(b"index")

            model_name = rvc_handler.upload_rvc_model(str(pth), str(index), str(models_dir))

            self.assertEqual(model_name, "alpha")
            self.assertEqual((models_dir / "alpha" / "alpha.pth").read_bytes(), b"pth")
            self.assertEqual((models_dir / "alpha" / "alpha.index").read_bytes(), b"index")
            post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
