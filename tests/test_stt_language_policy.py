import hashlib
import tempfile
import unittest
from unittest.mock import Mock

from pandrator.logic.dubbing import crispasr, stt_backends
from pandrator.logic.dubbing.stt_languages import (
    PARAKEET_V3_LANGUAGE_CODES,
    WHISPER_LARGE_V3_LANGUAGE_CODES,
    normalize_stt_language,
    validate_stt_language,
)
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import Job, QuickTranscription
from tests.web_test_support import prepare_web_test_data_root


class STTLanguagePolicyTests(unittest.TestCase):
    def test_model_language_tables_are_authoritative_and_distinct(self):
        self.assertEqual(len(PARAKEET_V3_LANGUAGE_CODES), 25)
        self.assertEqual(len(WHISPER_LARGE_V3_LANGUAGE_CODES), 100)
        self.assertEqual(len(set(PARAKEET_V3_LANGUAGE_CODES)), 25)
        self.assertEqual(len(set(WHISPER_LARGE_V3_LANGUAGE_CODES)), 100)
        self.assertIn("de", WHISPER_LARGE_V3_LANGUAGE_CODES)
        self.assertIn("nn", WHISPER_LARGE_V3_LANGUAGE_CODES)
        self.assertIn("no", WHISPER_LARGE_V3_LANGUAGE_CODES)
        self.assertNotIn("ja", PARAKEET_V3_LANGUAGE_CODES)

    def test_normalization_accepts_regions_and_documented_aliases(self):
        expected = {
            "ptBR": "pt",
            "pt-BR": "pt",
            "pt_BR": "pt",
            "zhCN": "zh",
            "zh-CN": "zh",
            "nb": "no",
            "iw": "he",
            "jv": "jw",
            "nn": "nn",
            "": "auto",
            "auto": "auto",
            None: "auto",
        }
        for requested, normalized in expected.items():
            with self.subTest(requested=requested):
                self.assertEqual(normalize_stt_language(requested), normalized)

    def test_validation_rejects_only_known_local_coverage(self):
        self.assertEqual(validate_stt_language("parakeet", "pt-BR"), "pt")
        self.assertEqual(validate_stt_language("whisper", "nn"), "nn")
        self.assertEqual(validate_stt_language("moss", "language-without-a-table"), "language-without-a-table")
        self.assertEqual(
            validate_stt_language("azure_mai_transcribe_2", "ja"),
            "ja",
        )
        with self.assertRaisesRegex(ValueError, "Unsupported language 'ja'"):
            validate_stt_language("parakeet", "ja")
        with self.assertRaisesRegex(ValueError, "Unsupported language 'tlh'"):
            validate_stt_language("whisper", "tlh")

    def test_backend_language_options_use_the_same_exact_tables(self):
        self.assertEqual(
            tuple(option.code for option in stt_backends.language_options_for_backend("parakeet")),
            PARAKEET_V3_LANGUAGE_CODES,
        )
        self.assertEqual(
            tuple(option.code for option in stt_backends.language_options_for_backend("whisper")),
            WHISPER_LARGE_V3_LANGUAGE_CODES,
        )
        self.assertEqual(
            stt_backends.normalize_stt_language_for_backend("whisper", "ptBR").code,
            "pt",
        )
        self.assertEqual(
            stt_backends.normalize_stt_language_for_backend("whisper", "nn").code,
            "nn",
        )
        cloud_options = stt_backends.language_options_for_backend(
            "azure_mai_transcribe_2"
        )
        self.assertEqual(cloud_options[0].code, "auto")
        self.assertIn("yue", {option.code for option in cloud_options})
        self.assertNotIn("haw", {option.code for option in cloud_options})

    def test_moss_command_keeps_explicit_unknown_language_and_no_whisper_flag(self):
        command = crispasr.build_command(
            "audio.wav",
            "output",
            {"stt_engine": "moss", "stt_language": "language-without-a-table"},
            executable="crispasr-test",
        )
        self.assertNotIn("-l", command)
        self.assertEqual(
            validate_stt_language("moss", "language-without-a-table"),
            "language-without-a-table",
        )

    def test_unsupported_prefetch_is_rejected_before_opening_download(self):
        opener = Mock(side_effect=AssertionError("unsupported model must not download"))
        with self.assertRaisesRegex(ValueError, "Unsupported language 'ja'"):
            crispasr._prefetch_windows_model(
                {"stt_engine": "parakeet", "stt_language": "ja"},
                opener=opener,
            )
        opener.assert_not_called()


class QuickTranscriptionLanguageRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap_tokens = BootstrapTokenStore()
        token = bootstrap_tokens.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap_tokens,
            background_maintenance=False,
        )
        self.client = self.app.test_client()
        response = self.client.post("/api/v1/auth/bootstrap", json={"token": token})
        self.assertEqual(200, response.status_code, response.get_json())
        self.headers = {"X-CSRF-Token": response.get_json()["csrf_token"]}
        self.extension = self.app.extensions["pandrator"]

    def tearDown(self):
        self.extension["quick_transcriptions"].stop_maintenance()
        self.extension["database"].dispose()
        self.temporary.cleanup()

    @staticmethod
    def _payload(content=b"fake audio", **overrides):
        payload = {
            "filename": "clip.wav",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "format": "txt",
        }
        payload.update(overrides)
        return payload

    def test_local_unsupported_language_is_rejected_without_a_record_or_job(self):
        response = self.client.post(
            "/api/v1/transcriptions",
            json=self._payload(language="ja", engine="parakeet"),
            headers={**self.headers, "Idempotency-Key": "quick-language-ja"},
        )
        self.assertEqual(400, response.status_code, response.get_json())
        self.assertEqual("unsupported_language", response.get_json()["error"]["code"])
        with self.extension["database"].session() as session:
            self.assertIsNone(session.query(QuickTranscription).first())
            self.assertIsNone(session.query(Job).first())

    def test_cloud_engine_bypasses_local_language_table(self):
        response = self.client.post(
            "/api/v1/transcriptions",
            json=self._payload(
                language="language-without-a-table",
                engine="azure_mai_transcribe_2",
            ),
            headers={**self.headers, "Idempotency-Key": "quick-language-cloud"},
        )
        self.assertEqual(201, response.status_code, response.get_json())
        with self.extension["database"].session() as session:
            record = session.get(QuickTranscription, response.get_json()["id"])
            self.assertIsNotNone(record)
            self.assertEqual("language-without-a-table", record.settings_json["stt_language"])


if __name__ == "__main__":
    unittest.main()
