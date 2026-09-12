import io
import tempfile
import unittest
import wave
from uuid import uuid4

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import Voice, VoiceSample
from pandrator.web.voice_library import resolve_voice_sample_transcription_settings
from tests.web_test_support import prepare_web_test_data_root


def silent_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0\0" * 160)
    return output.getvalue()


class VoiceTranscriptionDefaultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
        )
        self.client = self.app.test_client()
        self.csrf = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]

    def tearDown(self):
        self.app.extensions["pandrator"]["database"].dispose()
        self.temporary.cleanup()

    def _sample(self, language: str | None = "de") -> tuple[Voice, VoiceSample]:
        extension = self.app.extensions["pandrator"]
        with extension["database"].session() as session:
            voice = Voice(
                name=f"Voice {language or 'auto'} {uuid4().hex}",
                language=language,
            )
            session.add(voice)
            session.flush()
            voice_id = voice.id
        sample_path = extension["paths"].voices / voice_id / "sample.wav"
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        sample_path.write_bytes(silent_wav())
        artifact = extension["artifacts"].register(
            sample_path,
            kind="audio",
            role="voice_sample",
        )
        with extension["database"].session() as session:
            sample = VoiceSample(
                voice_id=voice_id,
                artifact_id=artifact.id,
                transcript="Keep this transcript.",
            )
            session.add(sample)
            session.flush()
            sample_id = sample.id
        return (
            Voice(id=voice_id, name="", language=language),
            VoiceSample(id=sample_id, voice_id=voice_id, artifact_id=artifact.id),
        )

    def _queue(self, language: str | None, settings=None):
        voice, sample = self._sample(language)
        return self._queue_sample(voice, sample, settings)

    def _queue_sample(self, voice: Voice, sample: VoiceSample, settings=None):
        response = self.client.post(
            f"/api/v1/voices/{voice.id}/samples/{sample.id}/transcribe",
            json={} if settings is None else settings,
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(202, response.status_code, response.get_json())
        return response.get_json()

    def test_supported_voice_language_defaults_to_parakeet_and_preserves_request(self):
        queued = self._queue("de", {"stt_threads": 2})
        settings = queued["payload_json"]["settings"]
        self.assertEqual("parakeet", settings["stt_engine"])
        self.assertEqual("parakeet", settings["stt_backend"])
        self.assertEqual("de", settings["stt_language"])
        self.assertEqual(2, settings["stt_threads"])

    def test_japanese_voice_language_defaults_to_whisper(self):
        settings = self._queue("ja")["payload_json"]["settings"]
        self.assertEqual("whisper", settings["stt_engine"])
        self.assertEqual("whisper", settings["stt_backend"])
        self.assertEqual("ja", settings["stt_language"])

    def test_regional_voice_language_uses_base_language_without_rewriting_payload(self):
        settings = self._queue("de-DE")["payload_json"]["settings"]
        self.assertEqual("parakeet", settings["stt_engine"])
        self.assertEqual("de-DE", settings["stt_language"])

    def test_missing_or_null_json_settings_keep_voice_language_defaults(self):
        for body in (None, b"null"):
            with self.subTest(body=body):
                voice, sample = self._sample("de")
                request_kwargs = {
                    "headers": {"X-CSRF-Token": self.csrf},
                    "content_type": "application/json",
                }
                if body is not None:
                    request_kwargs["data"] = body
                response = self.client.post(
                    f"/api/v1/voices/{voice.id}/samples/{sample.id}/transcribe",
                    **request_kwargs,
                )
                self.assertEqual(202, response.status_code, response.get_json())
                settings = response.get_json()["payload_json"]["settings"]
                self.assertEqual("parakeet", settings["stt_engine"])
                self.assertEqual("de", settings["stt_language"])

    def test_explicit_engine_or_backend_and_language_are_preserved(self):
        settings = resolve_voice_sample_transcription_settings(
            {"stt_engine": "whisper", "stt_language": "auto", "marker": "kept"},
            "de",
        )
        self.assertEqual("whisper", settings["stt_engine"])
        self.assertEqual("auto", settings["stt_language"])
        self.assertEqual("kept", settings["marker"])

        settings = resolve_voice_sample_transcription_settings(
            {"stt_backend": "moss"},
            "de",
        )
        self.assertEqual("moss", settings["stt_backend"])
        self.assertNotIn("stt_engine", settings)

        automatic = resolve_voice_sample_transcription_settings(
            {"stt_language": "auto"},
            "ja",
        )
        self.assertEqual("parakeet", automatic["stt_engine"])
        self.assertEqual("parakeet", automatic["stt_backend"])

    def test_input_settings_and_voice_sample_metadata_are_not_mutated(self):
        input_settings = {"stt_language": "", "nested": {"value": 1}}
        resolved = resolve_voice_sample_transcription_settings(input_settings, "de")
        self.assertEqual({"stt_language": "", "nested": {"value": 1}}, input_settings)
        resolved["nested"]["value"] = 2
        self.assertEqual(1, input_settings["nested"]["value"])

        voice, sample = self._sample("de")
        extension = self.app.extensions["pandrator"]
        with extension["database"].session() as session:
            before_voice = session.get(Voice, voice.id)
            before_sample = session.get(VoiceSample, sample.id)
            before_metadata = dict(before_voice.metadata_json or {})
            before_transcript = before_sample.transcript
        self._queue_sample(voice, sample)
        with extension["database"].session() as session:
            self.assertEqual(before_metadata, session.get(Voice, voice.id).metadata_json)
            self.assertEqual(before_transcript, session.get(VoiceSample, sample.id).transcript)

    def test_non_object_settings_are_rejected(self):
        voice, sample = self._sample("de")
        response = self.client.post(
            f"/api/v1/voices/{voice.id}/samples/{sample.id}/transcribe",
            json=["invalid"],
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(422, response.status_code)
        self.assertEqual("validation_error", response.get_json()["error"]["code"])


if __name__ == "__main__":
    unittest.main()
