from __future__ import annotations

import tempfile
import threading
import unittest
from unittest import mock

from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import Artifact, Job, Voice, VoiceSample
from pandrator.web.voice_reference_reuse import REFERENCE_PREPARATION_PROFILE
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.test_web_voice_library import silent_wav
from tests.web_test_support import prepare_web_test_data_root


class VoiceReferenceSetupTests(unittest.TestCase):
    """Disposable database/API and mocked-provider acceptance for exact setup."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap)
        self.client = self.app.test_client()
        self.csrf = self.client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]

    def tearDown(self):
        self.app.extensions["pandrator"]["database"].dispose()
        self.temporary.cleanup()

    def _headers(self, key=None):
        return {"X-CSRF-Token": self.csrf, **({"Idempotency-Key": key} if key else {})}

    def _designed(self):
        extension = self.app.extensions["pandrator"]
        voice = self.client.post(
            "/api/v1/voices", json={"name": "Exact designed", "language": "en"},
            headers=self._headers(),
        ).get_json()
        source = extension["paths"].artifacts / "tts-previews" / "exact.wav"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(silent_wav())
        preview = extension["artifacts"].register(
            source, kind="audio", role="tts_voice_preview", metadata={
                "service_id": "audio_cpp", "service_adapter": "audio_cpp",
                "model": "breeze_tts_2_q8_0", "preview_text": "Exact reviewed words.",
                "language": "en", "generation_prompt": "Calm narrator.",
            },
        )
        body = {
            "artifact_id": preview.id, "transcript": "Exact reviewed words.",
            "language": "en", "expected_voice_revision": voice["revision"],
        }
        response = self.client.post(
            f"/api/v1/voices/{voice['id']}/samples/from-preview", json=body,
            headers=self._headers("initial-prepare-key"),
        )
        self.assertEqual(202, response.status_code, response.get_json())
        handler = WorkflowHandlers(extension["database"], extension["paths"])
        receipt = handler.normalize_voice_recording(
            response.get_json()["payload_json"], lambda *_: None, threading.Event(),
        )
        body["expected_voice_revision"] = receipt["voice_revision"]
        return extension, voice, preview, source, body, receipt, handler

    def test_ready_reference_reuses_exact_output_without_job_or_revision_and_replays(self):
        extension, voice, preview, _, body, receipt, _ = self._designed()
        with extension["database"].session() as session:
            before = session.scalar(select(func.count()).select_from(Job))
            artifact = session.get(Artifact, receipt["artifact_id"])
            self.assertEqual(REFERENCE_PREPARATION_PROFILE, artifact.metadata_json["reference_preparation_profile"])
            provenance = artifact.metadata_json["sample_provenance"]
            self.assertEqual(preview.content_hash, provenance["source_preview_sha256"])
        path = f"/api/v1/voices/{voice['id']}/samples/from-preview"
        ready = self.client.post(path, json=body, headers=self._headers("reuse-reference-key"))
        self.assertEqual(200, ready.status_code, ready.get_json())
        self.assertTrue(ready.get_json()["reused_reference"])
        self.assertEqual(receipt["sample_id"], ready.get_json()["sample_id"])
        self.assertEqual(receipt["sample_sha256"], ready.get_json()["sample_sha256"])
        with extension["database"].session() as session:
            self.assertEqual(before, session.scalar(select(func.count()).select_from(Job)))
            stored = session.get(Voice, voice["id"])
            self.assertEqual(receipt["voice_revision"], stored.revision)
            stored.revision += 1
        replay = self.client.post(path, json=body, headers=self._headers("reuse-reference-key"))
        self.assertEqual(ready.get_json(), replay.get_json())
        samples = self.client.get(f"/api/v1/voices/{voice['id']}/samples").get_json()
        self.assertEqual(receipt["sample_sha256"], samples["items"][0]["sample_sha256"])

    def test_mismatched_or_unavailable_preparation_does_not_reuse(self):
        extension, voice, _, _, body, receipt, _ = self._designed()
        path = f"/api/v1/voices/{voice['id']}/samples/from-preview"
        for field, value in (("language", "de"), ("transcript", "Changed words.")):
            response = self.client.post(path, json={**body, field: value}, headers=self._headers())
            self.assertEqual(422 if field == "transcript" else 202, response.status_code)
        with extension["database"].session() as session:
            artifact = session.get(Artifact, receipt["artifact_id"])
            metadata = dict(artifact.metadata_json)
            metadata["reference_preparation_profile"] = "different/v1"
            artifact.metadata_json = metadata
        response = self.client.post(path, json=body, headers=self._headers())
        self.assertEqual(202, response.status_code)
        with extension["database"].session() as session:
            artifact = session.get(Artifact, receipt["artifact_id"])
            metadata = dict(artifact.metadata_json)
            metadata["reference_preparation_profile"] = REFERENCE_PREPARATION_PROFILE
            artifact.metadata_json = metadata
            output = extension["paths"].managed_path(artifact.relative_path)
        output.write_bytes(b"changed normalized bytes")
        self.assertEqual(202, self.client.post(path, json=body, headers=self._headers()).status_code)
        output.unlink()
        self.assertEqual(202, self.client.post(path, json=body, headers=self._headers()).status_code)

    def test_source_and_provenance_hash_changes_prevent_reuse(self):
        extension, voice, _, source, body, receipt, _ = self._designed()
        path = f"/api/v1/voices/{voice['id']}/samples/from-preview"
        with extension["database"].session() as session:
            artifact = session.get(Artifact, receipt["artifact_id"])
            metadata = dict(artifact.metadata_json)
            metadata["sample_provenance"] = {**metadata["sample_provenance"], "source_preview_sha256": "0" * 64}
            artifact.metadata_json = metadata
        self.assertEqual(202, self.client.post(path, json=body, headers=self._headers()).status_code)
        source.write_bytes(b"changed design bytes")
        self.assertEqual(409, self.client.post(path, json=body, headers=self._headers()).status_code)

    def test_publication_pins_exact_sample_and_rejects_changed_hash_and_payload(self):
        extension, voice, _, _, _, receipt, _ = self._designed()
        path = f"/api/v1/voices/{voice['id']}/providers/audio_cpp"
        headers = {**self._headers("exact-publication-key"), "If-Match": str(receipt["voice_revision"])}
        pins = {"sample_id": receipt["sample_id"], "sample_sha256": receipt["sample_sha256"]}
        published = self.client.post(path, json=pins, headers=headers)
        self.assertEqual(202, published.status_code, published.get_json())
        payload = published.get_json()["payload_json"]
        self.assertEqual(receipt["artifact_id"], payload["sample_artifact_id"])
        self.assertEqual(pins, {key: payload[key] for key in pins})
        conflict = self.client.post(path, json={**pins, "sample_sha256": "0" * 64}, headers=headers)
        self.assertEqual("idempotency_conflict", conflict.get_json()["error"]["code"])
        invalid = self.client.post(path, json={"sample_id": receipt["sample_id"]}, headers=headers)
        self.assertEqual(422, invalid.status_code)
        with extension["database"].session() as session:
            artifact = session.get(Artifact, receipt["artifact_id"])
            output = extension["paths"].managed_path(artifact.relative_path)
        output.write_bytes(b"changed normalized bytes")
        rejected = self.client.post(path, json=pins, headers={**headers, "Idempotency-Key": "changed-publication-key"})
        self.assertEqual(409, rejected.status_code)

    def _pinned(self, voice, receipt):
        return {"voice_id": voice["id"], "service_id": "audio_cpp",
                "expected_voice_revision": receipt["voice_revision"],
                "sample_id": receipt["sample_id"], "sample_artifact_id": receipt["artifact_id"],
                "sample_sha256": receipt["sample_sha256"]}

    def test_native_registration_reuse_and_endpoint_text_invalidation(self):
        extension, voice, _, _, _, prepared, handler = self._designed()
        settings = {"id": "audio_cpp", "name": "audio.cpp", "adapter": "audio_cpp", "api_base": "http://local/a"}
        with mock.patch("pandrator.web.workflow_voice.hydrate_tts_settings", return_value={}), mock.patch(
            "pandrator.logic.tts_handler.get_service_config", side_effect=lambda *_: settings
        ), mock.patch.object(handler.tts_providers, "upload_voice") as upload:
            first = handler.publish_voice(self._pinned(voice, prepared), lambda *_: None, threading.Event())
            second = handler.publish_voice(self._pinned(voice, {**prepared, "voice_revision": first["voice_revision"]}), lambda *_: None, threading.Event())
            self.assertTrue(second["reused_registration"])
            self.assertEqual(first["voice_revision"], second["voice_revision"])
            settings["api_base"] = "http://local/b"
            third = handler.publish_voice(self._pinned(voice, {**prepared, "voice_revision": second["voice_revision"]}), lambda *_: None, threading.Event())
            self.assertFalse(third["reused_registration"])
            with extension["database"].session() as session:
                sample = session.get(VoiceSample, prepared["sample_id"])
                sample.transcript = "Updated reviewed words."
                stored = session.get(Voice, voice["id"])
                stored.revision += 1
                revision = stored.revision
            fourth = handler.publish_voice(self._pinned(voice, {**prepared, "voice_revision": revision}), lambda *_: None, threading.Event())
            self.assertFalse(fourth["reused_registration"])
            replacement_path = extension["paths"].voices / voice["id"] / "replacement.wav"
            replacement_path.write_bytes(silent_wav())
            replacement = extension["artifacts"].register(replacement_path, kind="audio", role="voice_sample")
            with extension["database"].session() as session:
                sample = VoiceSample(voice_id=voice["id"], artifact_id=replacement.id, transcript="Updated reviewed words.", transcript_reviewed=True)
                session.add(sample)
                session.flush()
                stored = session.get(Voice, voice["id"])
                stored.revision += 1
                replacement_receipt = {"sample_id": sample.id, "artifact_id": replacement.id, "sample_sha256": replacement.content_hash, "voice_revision": stored.revision}
            fifth = handler.publish_voice(self._pinned(voice, replacement_receipt), lambda *_: None, threading.Event())
            self.assertFalse(fifth["reused_registration"])
            upload.assert_not_called()

    def test_worker_pins_prevent_provider_side_effects_on_hash_artifact_and_revision_drift(self):
        extension, voice, _, _, _, prepared, handler = self._designed()
        payload = self._pinned(voice, prepared)
        for change in ({"sample_sha256": "0" * 64}, {"sample_artifact_id": "another-artifact"}, {"expected_voice_revision": 999}):
            with mock.patch.object(handler.tts_providers, "upload_voice") as upload, self.assertRaises(ValueError):
                handler.publish_voice({**payload, **change, "service_id": "kobold_qwen"}, lambda *_: None, threading.Event())
            upload.assert_not_called()
        def change_revision(*_):
            with extension["database"].session() as session:
                session.get(Voice, voice["id"]).revision += 1
        with self.assertRaisesRegex(ValueError, "changed before it could be linked"):
            handler.publish_voice(payload, change_revision, threading.Event())

    def test_legacy_default_route_also_pins_normalized_sample(self):
        _, voice, _, _, _, prepared, _ = self._designed()
        response = self.client.post(
            f"/api/v1/voices/{voice['id']}/providers/audio_cpp", json={},
            headers={**self._headers(), "If-Match": str(prepared["voice_revision"])},
        )
        self.assertEqual(202, response.status_code)
        payload = response.get_json()["payload_json"]
        self.assertEqual(prepared["sample_id"], payload["sample_id"])
        self.assertEqual(prepared["sample_sha256"], payload["sample_sha256"])

    def test_recipe_signature_conflicts_before_reuse_on_same_key(self):
        _, voice, _, _, body, _, _ = self._designed()
        path = f"/api/v1/voices/{voice['id']}/samples/from-preview"
        request = {**body, "recipe_signature": "a" * 64}
        response = self.client.post(path, json=request, headers=self._headers("recipe-signature-key"))
        self.assertEqual(200, response.status_code)
        conflict = self.client.post(path, json={**request, "recipe_signature": "b" * 64}, headers=self._headers("recipe-signature-key"))
        self.assertEqual("idempotency_conflict", conflict.get_json()["error"]["code"])

    def test_explicit_pins_select_older_sample_for_remote_publication(self):
        extension, voice, _, _, body, prepared, handler = self._designed()
        newer_path = extension["paths"].voices / voice["id"] / "newer.wav"
        newer_path.write_bytes(silent_wav())
        newer = extension["artifacts"].register(newer_path, kind="audio", role="voice_sample")
        with extension["database"].session() as session:
            session.add(VoiceSample(voice_id=voice["id"], artifact_id=newer.id))
            stored = session.get(Voice, voice["id"])
            stored.revision += 1
            revision = stored.revision
            original = session.get(Artifact, prepared["artifact_id"])
            original_path = str(extension["paths"].managed_path(original.relative_path))
        response = self.client.post(
            f"/api/v1/voices/{voice['id']}/providers/kobold_qwen",
            json={"sample_id": prepared["sample_id"], "sample_sha256": prepared["sample_sha256"]},
            headers={**self._headers("older-exact-sample-key"), "If-Match": str(revision)},
        )
        self.assertEqual(202, response.status_code, response.get_json())
        with mock.patch("pandrator.web.workflow_voice.hydrate_tts_settings", return_value={}), mock.patch(
            "pandrator.logic.tts_handler.get_service_config", return_value={"id": "kobold_qwen", "adapter": "kobold_qwen", "voice_reference_text": "optional"}
        ), mock.patch.object(handler.tts_providers, "upload_voice", return_value="owned-old") as upload:
            result = handler.publish_voice(response.get_json()["payload_json"], lambda *_: None, threading.Event())
        self.assertEqual(prepared["sample_id"], result["sample_id"])
        self.assertEqual(original_path, upload.call_args.args[1])
        # Preparation must create a current reference rather than reuse this
        # historical match, which native synthesis would not select.
        response = self.client.post(
            f"/api/v1/voices/{voice['id']}/samples/from-preview",
            json={**body, "expected_voice_revision": result["voice_revision"]},
            headers=self._headers("prepare-current-reference-key"),
        )
        self.assertEqual(202, response.status_code, response.get_json())

    def test_legacy_marker_wrong_source_or_unreviewed_sample_never_reuses(self):
        extension, voice, _, _, body, prepared, _ = self._designed()
        path = f"/api/v1/voices/{voice['id']}/samples/from-preview"
        for field in ("marker", "source", "reviewed"):
            with extension["database"].session() as session:
                artifact = session.get(Artifact, prepared["artifact_id"])
                metadata = dict(artifact.metadata_json)
                if field == "marker":
                    metadata.pop("reference_preparation_profile", None)
                elif field == "source":
                    metadata["reference_preparation_profile"] = REFERENCE_PREPARATION_PROFILE
                    metadata["sample_provenance"] = {**metadata["sample_provenance"], "source_preview_artifact_id": "another-preview"}
                else:
                    metadata["sample_provenance"] = {**metadata["sample_provenance"], "source_preview_artifact_id": body["artifact_id"]}
                    session.get(VoiceSample, prepared["sample_id"]).transcript_reviewed = False
                artifact.metadata_json = metadata
            self.assertEqual(202, self.client.post(path, json=body, headers=self._headers()).status_code)

    def test_hash_change_during_provider_configuration_is_checked_before_upload(self):
        extension, voice, _, _, _, prepared, handler = self._designed()
        with extension["database"].session() as session:
            artifact = session.get(Artifact, prepared["artifact_id"])
            output = extension["paths"].managed_path(artifact.relative_path)
        def configure(*_):
            output.write_bytes(b"changed after initial pin check")
            return {"id": "kobold_qwen", "adapter": "kobold_qwen"}
        with mock.patch("pandrator.web.workflow_voice.hydrate_tts_settings", return_value={}), mock.patch(
            "pandrator.logic.tts_handler.get_service_config", side_effect=configure
        ), mock.patch.object(handler.tts_providers, "upload_voice") as upload, self.assertRaises(ValueError):
            handler.publish_voice({**self._pinned(voice, prepared), "service_id": "kobold_qwen"}, lambda *_: None, threading.Event())
        upload.assert_not_called()

    def test_native_historical_pin_is_rejected_without_registration_or_revision_change(self):
        extension, voice, _, _, _, prepared, handler = self._designed()
        newer_path = extension["paths"].voices / voice["id"] / "current-native.wav"
        newer_path.write_bytes(silent_wav())
        newer = extension["artifacts"].register(newer_path, kind="audio", role="voice_sample")
        with extension["database"].session() as session:
            session.add(VoiceSample(voice_id=voice["id"], artifact_id=newer.id))
            stored = session.get(Voice, voice["id"])
            stored.revision += 1
            revision = stored.revision
        request = {"sample_id": prepared["sample_id"], "sample_sha256": prepared["sample_sha256"]}
        response = self.client.post(
            f"/api/v1/voices/{voice['id']}/providers/audio_cpp", json=request,
            headers={**self._headers("native-historical-pin-key"), "If-Match": str(revision)},
        )
        self.assertEqual(409, response.status_code)
        self.assertEqual("current_reference_required", response.get_json()["error"]["code"])
        with mock.patch("pandrator.web.workflow_voice.hydrate_tts_settings", return_value={}), mock.patch(
            "pandrator.logic.tts_handler.get_service_config", return_value={"id": "audio_cpp", "adapter": "audio_cpp"}
        ), mock.patch.object(handler.tts_providers, "upload_voice") as upload, self.assertRaisesRegex(ValueError, "newest ready local reference"):
            handler.publish_voice(self._pinned(voice, {**prepared, "voice_revision": revision}), lambda *_: None, threading.Event())
        upload.assert_not_called()
        with extension["database"].session() as session:
            stored = session.get(Voice, voice["id"])
            self.assertEqual(revision, stored.revision)
            self.assertFalse((stored.metadata_json or {}).get("providers"))
