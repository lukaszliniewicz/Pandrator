from __future__ import annotations

import hashlib
import tempfile
import unittest

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import Artifact, Voice
from tests.web_test_support import prepare_web_test_data_root


class VoiceReferenceImportTests(unittest.TestCase):
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

    def _headers(self, key: str | None = None) -> dict[str, str]:
        headers = {"X-CSRF-Token": self.csrf}
        if key is not None:
            headers["Idempotency-Key"] = key
        return headers

    def test_import_queues_managed_audio_with_hash_and_reviewed_transcript(self):
        voice = self.client.post(
            "/api/v1/voices",
            json={"name": "Imported reference voice"},
            headers=self._headers(),
        ).get_json()
        extension = self.app.extensions["pandrator"]
        source = extension["paths"].artifacts / "references" / "reference.wav"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"managed reference audio")
        artifact = extension["artifacts"].register(
            source, kind="audio", role="recording_upload"
        )
        response = self.client.post(
            f"/api/v1/voices/{voice['id']}/samples/from-artifact",
            json={
                "artifact_id": artifact.id,
                "transcript": "Reviewed imported words.",
                "transcript_reviewed": True,
                "language": "en",
                "expected_voice_revision": voice["revision"],
            },
            headers=self._headers("import-reference-key"),
        )
        self.assertEqual(202, response.status_code, response.get_json())
        payload = response.get_json()["payload_json"]
        self.assertEqual(artifact.id, payload["source_artifact_id"])
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).hexdigest(),
            payload["source_artifact_sha256"],
        )
        self.assertEqual("recording_upload", payload["source_artifact_role"])
        self.assertEqual(
            "imported_voice_reference", payload["sample_provenance"]["source_kind"]
        )
        self.assertEqual("Reviewed imported words.", payload["reviewed_transcript"])

    def test_import_rejects_hash_changes_and_bundled_voices(self):
        voice = self.client.post(
            "/api/v1/voices",
            json={"name": "Hash guarded reference voice"},
            headers=self._headers(),
        ).get_json()
        extension = self.app.extensions["pandrator"]
        source = extension["paths"].artifacts / "references" / "changed.wav"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"original")
        artifact = extension["artifacts"].register(
            source, kind="audio", role="recording_upload"
        )
        source.write_bytes(b"changed")
        response = self.client.post(
            f"/api/v1/voices/{voice['id']}/samples/from-artifact",
            json={
                "artifact_id": artifact.id,
                "expected_voice_revision": voice["revision"],
            },
            headers=self._headers(),
        )
        self.assertEqual(409, response.status_code, response.get_json())
        self.assertEqual(
            "reference_artifact_changed", response.get_json()["error"]["code"]
        )

        bundled = self.client.get("/api/v1/voices").get_json()["items"]
        bundled_voice = next(item for item in bundled if item["bundled"])
        protected = self.client.post(
            f"/api/v1/voices/{bundled_voice['id']}/samples/from-artifact",
            json={
                "artifact_id": artifact.id,
                "expected_voice_revision": bundled_voice["revision"],
            },
            headers=self._headers(),
        )
        self.assertEqual(409, protected.status_code, protected.get_json())
        self.assertEqual(
            "bundled_voice_protected", protected.get_json()["error"]["code"]
        )

    def test_import_replays_after_source_artifact_is_retired(self):
        voice = self.client.post(
            "/api/v1/voices",
            json={"name": "Retired imported reference"},
            headers=self._headers(),
        ).get_json()
        extension = self.app.extensions["pandrator"]
        source = extension["paths"].artifacts / "references" / "retired.wav"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"retired audio")
        artifact = extension["artifacts"].register(
            source, kind="audio", role="recording_upload"
        )
        body = {
            "artifact_id": artifact.id,
            "expected_voice_revision": voice["revision"],
        }
        path = f"/api/v1/voices/{voice['id']}/samples/from-artifact"
        first = self.client.post(
            path, json=body, headers=self._headers("retired-import-key")
        )
        self.assertEqual(202, first.status_code, first.get_json())
        with extension["database"].session() as session:
            session.get(Artifact, artifact.id).state = "deleted"
            session.get(Voice, voice["id"]).revision += 1
        replay = self.client.post(
            path, json=body, headers=self._headers("retired-import-key")
        )
        self.assertEqual(202, replay.status_code, replay.get_json())
        self.assertEqual(first.get_json()["id"], replay.get_json()["id"])
        self.assertEqual("true", replay.headers["Idempotency-Replayed"])

    def test_import_preserves_draft_without_marking_it_reviewed(self):
        voice = self.client.post(
            "/api/v1/voices",
            json={"name": "Unreviewed imported reference"},
            headers=self._headers(),
        ).get_json()
        extension = self.app.extensions["pandrator"]
        source = extension["paths"].artifacts / "references" / "unreviewed.wav"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"audio")
        artifact = extension["artifacts"].register(
            source, kind="audio", role="recording_upload"
        )
        response = self.client.post(
            f"/api/v1/voices/{voice['id']}/samples/from-artifact",
            json={
                "artifact_id": artifact.id,
                "transcript": "Do not mark this reviewed.",
                "transcript_reviewed": False,
                "expected_voice_revision": voice["revision"],
            },
            headers=self._headers(),
        )
        self.assertEqual(202, response.status_code, response.get_json())
        self.assertNotIn("reviewed_transcript", response.get_json()["payload_json"])
        self.assertEqual(
            "Do not mark this reviewed.",
            response.get_json()["payload_json"]["unreviewed_transcript"],
        )
