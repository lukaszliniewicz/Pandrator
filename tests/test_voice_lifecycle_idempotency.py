from __future__ import annotations

import io
import tempfile
import unittest
import wave

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import Artifact, Voice, VoiceSample
from tests.web_test_support import prepare_web_test_data_root


def silent_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0\0" * 160)
    return output.getvalue()


class VoiceLifecycleIdempotencyTests(unittest.TestCase):
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

    def _headers(self, key: str | None = None, **extra: str) -> dict[str, str]:
        headers = {"X-CSRF-Token": self.csrf, **extra}
        if key is not None:
            headers["Idempotency-Key"] = key
        return headers

    def _voice(self, name: str = "Lifecycle voice") -> dict:
        response = self.client.post(
            "/api/v1/voices",
            json={"name": name},
            headers=self._headers(),
        )
        self.assertEqual(201, response.status_code, response.get_json())
        return response.get_json()

    def test_create_replays_same_voice_and_conflicts_on_different_payload(self):
        first = self.client.post(
            "/api/v1/voices",
            json={"name": "Idempotent voice"},
            headers=self._headers("voice-create-key"),
        )
        replay = self.client.post(
            "/api/v1/voices",
            json={"name": "Idempotent voice"},
            headers=self._headers("voice-create-key"),
        )
        conflict = self.client.post(
            "/api/v1/voices",
            json={"name": "Another voice"},
            headers=self._headers("voice-create-key"),
        )

        self.assertEqual(201, first.status_code)
        self.assertEqual(201, replay.status_code)
        self.assertEqual(first.get_json()["id"], replay.get_json()["id"])
        self.assertEqual("true", replay.headers["Idempotency-Replayed"])
        self.assertEqual(409, conflict.status_code)
        self.assertEqual("idempotency_conflict", conflict.get_json()["error"]["code"])

    def test_promote_replays_after_voice_revision_changes(self):
        voice = self._voice("Promoted lifecycle voice")
        extension = self.app.extensions["pandrator"]
        preview_path = extension["paths"].artifacts / "tts-previews" / "promote.wav"
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_bytes(silent_wav())
        preview = extension["artifacts"].register(
            preview_path,
            kind="audio",
            role="tts_voice_preview",
            metadata={
                "service_id": "audio_cpp",
                "model": "breeze_tts_2_q8_0",
                "preview_text": "Promote this reference.",
            },
        )
        body = {
            "artifact_id": preview.id,
            "transcript": "Promote this reference.",
            "expected_voice_revision": voice["revision"],
        }
        path = f"/api/v1/voices/{voice['id']}/samples/from-preview"
        first = self.client.post(path, json=body, headers=self._headers("promote-key"))
        self.assertEqual(202, first.status_code, first.get_json())
        with extension["database"].session() as session:
            record = session.get(Voice, voice["id"])
            record.revision += 1
        replay = self.client.post(path, json=body, headers=self._headers("promote-key"))
        self.assertEqual(202, replay.status_code, replay.get_json())
        self.assertEqual(first.get_json()["id"], replay.get_json()["id"])
        self.assertEqual("true", replay.headers["Idempotency-Replayed"])

    def test_promote_replays_after_preview_artifact_is_retired(self):
        voice = self._voice("Retired preview lifecycle voice")
        extension = self.app.extensions["pandrator"]
        preview_path = extension["paths"].artifacts / "tts-previews" / "retired.wav"
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_bytes(silent_wav())
        preview = extension["artifacts"].register(
            preview_path,
            kind="audio",
            role="tts_voice_preview",
            metadata={
                "service_id": "audio_cpp",
                "model": "breeze_tts_2_q8_0",
                "preview_text": "Retire this reference.",
            },
        )
        body = {
            "artifact_id": preview.id,
            "transcript": "Retire this reference.",
            "expected_voice_revision": voice["revision"],
        }
        path = f"/api/v1/voices/{voice['id']}/samples/from-preview"
        first = self.client.post(
            path, json=body, headers=self._headers("retired-promote-key")
        )
        self.assertEqual(202, first.status_code, first.get_json())
        with extension["database"].session() as session:
            session.get(Artifact, preview.id).state = "deleted"
        replay = self.client.post(
            path, json=body, headers=self._headers("retired-promote-key")
        )
        self.assertEqual(202, replay.status_code, replay.get_json())
        self.assertEqual(first.get_json()["id"], replay.get_json()["id"])
        self.assertEqual("true", replay.headers["Idempotency-Replayed"])

    def test_publish_and_transcript_replay_after_revision_changes(self):
        voice = self._voice("Published lifecycle voice")
        extension = self.app.extensions["pandrator"]
        sample_path = extension["paths"].voices / voice["id"] / "sample.wav"
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        sample_path.write_bytes(silent_wav())
        artifact = extension["artifacts"].register(
            sample_path, kind="audio", role="voice_sample"
        )
        with extension["database"].session() as session:
            sample = VoiceSample(voice_id=voice["id"], artifact_id=artifact.id)
            session.add(sample)
            session.flush()
            sample_id = sample.id

        publish_path = f"/api/v1/voices/{voice['id']}/providers/kobold_qwen"
        publish_headers = self._headers(
            "publish-key", **{"If-Match": f'"{voice["revision"]}"'}
        )
        first_publish = self.client.post(publish_path, headers=publish_headers)
        self.assertEqual(202, first_publish.status_code, first_publish.get_json())
        with extension["database"].session() as session:
            session.get(Voice, voice["id"]).revision += 1
        replay_publish = self.client.post(publish_path, headers=publish_headers)
        self.assertEqual(202, replay_publish.status_code, replay_publish.get_json())
        self.assertEqual(
            first_publish.get_json()["id"], replay_publish.get_json()["id"]
        )

        with extension["database"].session() as session:
            current_revision = session.get(Voice, voice["id"]).revision
        transcript_path = f"/api/v1/voices/{voice['id']}/samples/{sample_id}/transcript"
        transcript_body = {
            "transcript": "Reviewed lifecycle words.",
            "expected_voice_revision": current_revision,
        }
        first_transcript = self.client.patch(
            transcript_path,
            json=transcript_body,
            headers=self._headers(
                "transcript-key", **{"If-Match": f'"{current_revision}"'}
            ),
        )
        self.assertEqual(200, first_transcript.status_code, first_transcript.get_json())
        replay_transcript = self.client.patch(
            transcript_path,
            json=transcript_body,
            headers=self._headers(
                "transcript-key", **{"If-Match": f'"{current_revision}"'}
            ),
        )
        self.assertEqual(
            200, replay_transcript.status_code, replay_transcript.get_json()
        )
        self.assertEqual("true", replay_transcript.headers["Idempotency-Replayed"])
        self.assertEqual(first_transcript.get_json(), replay_transcript.get_json())

    def test_publish_replays_after_sample_removal_and_conflicts_before_preflight(self):
        voice = self._voice("Removed sample lifecycle voice")
        extension = self.app.extensions["pandrator"]
        sample_path = extension["paths"].voices / voice["id"] / "sample.wav"
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        sample_path.write_bytes(silent_wav())
        artifact = extension["artifacts"].register(
            sample_path, kind="audio", role="voice_sample"
        )
        with extension["database"].session() as session:
            sample = VoiceSample(voice_id=voice["id"], artifact_id=artifact.id)
            session.add(sample)
            session.flush()
            sample_id = sample.id

        publish_path = f"/api/v1/voices/{voice['id']}/providers/kobold_qwen"
        headers = self._headers(
            "removed-sample-publish-key", **{"If-Match": f'"{voice["revision"]}"'}
        )
        first = self.client.post(publish_path, headers=headers)
        self.assertEqual(202, first.status_code, first.get_json())
        with extension["database"].session() as session:
            session.delete(session.get(VoiceSample, sample_id))
            session.get(Voice, voice["id"]).revision += 1
        replay = self.client.post(publish_path, headers=headers)
        self.assertEqual(202, replay.status_code, replay.get_json())
        self.assertEqual(first.get_json()["id"], replay.get_json()["id"])
        self.assertEqual("true", replay.headers["Idempotency-Replayed"])
        conflict = self.client.post(
            f"/api/v1/voices/{voice['id']}/providers/other-service",
            headers=headers,
        )
        self.assertEqual(409, conflict.status_code, conflict.get_json())
        self.assertEqual("idempotency_conflict", conflict.get_json()["error"]["code"])
