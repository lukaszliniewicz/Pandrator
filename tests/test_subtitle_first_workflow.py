"""Subtitle-first sessions must have real, reusable timed input, not just a filename."""

import io
import tempfile
import unittest
import uuid
import threading
from unittest.mock import patch

from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import Artifact, Document, DocumentRevision, Segment, Job
from pandrator.web.subtitle_sources import parse_subtitle_source

SRT = "1\n00:00:01,000 --> 00:00:03,000\nThese meetings, these interfaith circles.\n\n2\n00:00:04,000 --> 00:00:06,000\nSomething that can help.\n"


class SubtitleFirstWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap)
        self.client = self.app.test_client()
        self.headers = {"X-CSRF-Token": self.client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]}
        self.services = self.app.extensions["pandrator"]
        self.database = self.services["database"]

    def create_session(self, kind="voiceover"):
        response = self.client.post("/api/v1/sessions", json={"name": f"Subtitle-first regression {uuid.uuid4().hex[:8]}", "workflow_kind": kind}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["id"]

    def upload(self, session_id, content=SRT, filename="source.srt"):
        response = self.client.post("/api/v1/uploads", data={"session_id": session_id, "file": (io.BytesIO(content.encode()), filename)}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()

    def test_srt_upload_materializes_timed_revision_in_both_workflows(self):
        for kind in ("voiceover", "subtitles"):
            with self.subTest(kind=kind):
                session_id = self.create_session(kind)
                uploaded = self.upload(session_id)
                revision = uploaded["attachment"]["subtitle_revision"]
                self.assertFalse(revision["reused"])
                with self.database.session() as session:
                    document = session.get(Document, revision["document_id"])
                    self.assertEqual(document.stage, "transcription")
                    self.assertEqual(document.active_revision_id, revision["revision_id"])
                    rows = list(session.scalars(select(Segment).where(Segment.revision_id == revision["revision_id"]).order_by(Segment.ordinal)))
                    self.assertEqual([row.start_ms for row in rows], [1000, 4000])
                    self.assertEqual(rows[0].text, "These meetings, these interfaith circles.")
                    self.assertEqual(session.get(Artifact, uploaded["artifact_id"]).role, "upload")
                workflow = self.client.get(f"/api/v1/sessions/{session_id}/workflow").get_json()
                correction = next(item for item in workflow["stages"] if item["key"] == "correct")
                self.assertIn(correction["status"], {"ready", "completed"})

    def test_identical_upload_reuses_document_revision_and_preserves_source_asset(self):
        session_id = self.create_session()
        first = self.upload(session_id)
        second = self.upload(session_id)
        self.assertEqual(first["attachment"]["subtitle_revision"]["revision_id"], second["attachment"]["subtitle_revision"]["revision_id"])
        self.assertTrue(second["attachment"]["subtitle_revision"]["reused"])
        with self.database.session() as session:
            count = session.scalar(select(func.count()).select_from(DocumentRevision).join(Document).where(Document.session_id == session_id))
            self.assertEqual(count, 1)
        replay = self.client.post(f"/api/v1/sessions/{session_id}/sources/adopt-subtitles", json={"source_asset_id": second["source_asset_id"]}, headers=self.headers)
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertTrue(replay.get_json()["reused"])

    def test_adoption_cannot_read_another_sessions_unattached_source(self):
        owner = self.create_session()
        other = self.create_session()
        uploaded = self.upload(owner)
        response = self.client.post(f"/api/v1/sessions/{other}/sources/adopt-subtitles", json={"source_asset_id": uploaded["source_asset_id"]}, headers=self.headers)
        self.assertEqual(response.status_code, 404)

    def test_vtt_short_timestamps_and_cue_settings_are_supported(self):
        cues = parse_subtitle_source("WEBVTT\n\ncue-one\n00:01.000 --> 00:03.000 align:start\nHello, world!\n", "vtt")
        self.assertEqual([(cue.start_ms, cue.end_ms, cue.text) for cue in cues], [(1000, 3000, "Hello, world!")])

    def test_partial_invalid_subtitle_import_is_refused(self):
        with self.assertRaises(ValueError):
            parse_subtitle_source(SRT + "\n3\ninvalid --> invalid\nDo not silently drop me.\n", "srt")


    def attach_media(self, session_id, filename="original.mp4"):
        response = self.client.post("/api/v1/uploads", data={"file": (io.BytesIO(b"test-media-payload"), filename)}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.get_json())
        uploaded = response.get_json()
        revision = self.client.get(f"/api/v1/sessions/{session_id}").get_json()["revision"]
        attached = self.client.post(f"/api/v1/sessions/{session_id}/sources", json={"source_asset_id": uploaded["source_asset_id"], "role": "media"}, headers={**self.headers, "If-Match": f'"{revision}"'})
        self.assertEqual(attached.status_code, 201, attached.get_json())
        return uploaded

    def test_independent_media_target_preserves_subtitle_source_and_revision(self):
        from pandrator.web.source_resolution import resolve_primary_source, resolve_media_source
        session_id = self.create_session()
        uploaded = self.upload(session_id)
        media = self.attach_media(session_id)
        status = self.client.get(f"/api/v1/sessions/{session_id}/sources/subtitle-status").get_json()
        self.assertTrue(status["can_align"], status)
        self.assertTrue(status["has_video"], status)
        self.assertFalse(status["adoption_required"], status)
        self.assertEqual(status["subtitle_revision_id"], uploaded["attachment"]["subtitle_revision"]["revision_id"])
        with self.database.session() as session:
            self.assertEqual(resolve_primary_source(session, session_id).artifact.id, uploaded["artifact_id"])
            self.assertEqual(resolve_media_source(session, session_id).artifact.id, media["artifact_id"])
        replacement = self.attach_media(session_id, "replacement.wav")
        with self.database.session() as session:
            self.assertEqual(resolve_media_source(session, session_id).artifact.id, replacement["artifact_id"])
            self.assertEqual(resolve_primary_source(session, session_id).artifact.id, uploaded["artifact_id"])

    def test_alignment_requires_media_and_pins_authoritative_text(self):
        session_id = self.create_session()
        uploaded = self.upload(session_id)
        endpoint = f"/api/v1/sessions/{session_id}/sources/align-subtitles"
        status = self.client.get(f"/api/v1/sessions/{session_id}/sources/subtitle-status").get_json()
        missing = self.client.post(endpoint, json={"expected_revision": status["session_revision"]}, headers=self.headers)
        self.assertEqual(missing.status_code, 422, missing.get_json())
        self.attach_media(session_id)
        status = self.client.get(f"/api/v1/sessions/{session_id}/sources/subtitle-status").get_json()
        request = {"method": "ctc", "expected_revision": status["session_revision"]}
        response = self.client.post(endpoint, json=request, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.get_json())
        repeated = self.client.post(endpoint, json=request, headers=self.headers)
        self.assertEqual(repeated.status_code, 409, repeated.get_json())
        with self.database.session() as session:
            job = session.get(Job, response.get_json()["job_id"])
            payload = dict(job.payload_json)
        self.assertEqual(payload["caption_artifact_id"], uploaded["attachment"]["subtitle_revision"]["artifact_id"])
        handlers = self.services["workflow_handlers"]
        with patch.object(handlers, "_transcribe_media_edit_with_ctc", return_value={"word_count": 42}) as aligner, patch("pandrator.logic.dubbing.transcription.transcribe_source_file_with_metadata") as asr:
            result = handlers.transcribe(payload, lambda *args: None, threading.Event())
        self.assertEqual(result["word_count"], 42)
        self.assertEqual(aligner.call_args.kwargs["caption_artifact"].id, payload["caption_artifact_id"])
        asr.assert_not_called()

    def test_legacy_subtitle_requires_adoption_instead_of_false_ready_stage(self):
        session_id = self.create_session()
        library = self.services["source_library"]
        saved = library.artifacts
        library.artifacts = None
        try:
            uploaded = self.upload(session_id)
        finally:
            library.artifacts = saved
        workflow = self.client.get(f"/api/v1/sessions/{session_id}/workflow").get_json()
        self.assertTrue(workflow["subtitle_source"]["adoption_required"])
        correction = next(stage for stage in workflow["stages"] if stage["key"] == "correct")
        self.assertEqual(correction["status"], "unavailable")
        adopted = self.client.post(f"/api/v1/sessions/{session_id}/sources/adopt-subtitles", json={"source_asset_id": uploaded["source_asset_id"]}, headers=self.headers)
        self.assertEqual(adopted.status_code, 201, adopted.get_json())
        self.assertFalse(self.client.get(f"/api/v1/sessions/{session_id}/workflow").get_json()["subtitle_source"]["adoption_required"])

    def test_vtt_session_uses_imported_revision_without_transcription(self):
        session_id = self.create_session("subtitles")
        self.upload(session_id, "WEBVTT\n\n00:01.000 --> 00:03.000\nHello, world!\n", "source.vtt")
        workflow = self.client.get(f"/api/v1/sessions/{session_id}/workflow").get_json()
        self.assertFalse(any(stage["key"] == "transcribe" for stage in workflow["stages"]))
        self.assertFalse(workflow["subtitle_source"]["adoption_required"])

    def test_export_contract_pins_separate_video_target_and_survives_replacement(self):
        session_id = self.create_session()
        subtitle = self.upload(session_id)
        media = self.attach_media(session_id)
        resolved = self.services["workflows"].resolve_stage(
            session_id, "export", {"export_mode": "media", "audio_mode": "preserve"}
        )
        contract = resolved.payload["export_contract"]
        self.assertEqual(contract["source_artifact_id"], media["artifact_id"])
        self.assertNotEqual(contract["source_artifact_id"], subtitle["artifact_id"])
        self.assertEqual(contract["source_profile"], "video")
        replacement = self.attach_media(session_id, "replacement.mp4")
        updated = self.services["workflows"].resolve_stage(
            session_id, "export", {"export_mode": "media", "audio_mode": "preserve"}
        )
        self.assertEqual(updated.payload["export_contract"]["source_artifact_id"], replacement["artifact_id"])
        self.assertEqual(contract["source_artifact_id"], media["artifact_id"])

    def test_detached_media_is_not_recovered_from_old_uploads(self):
        from pandrator.web.models import SessionSource
        from pandrator.web.source_resolution import resolve_media_source

        session_id = self.create_session()
        self.upload(session_id)
        self.attach_media(session_id)
        with self.database.session() as session:
            attachment = session.scalar(select(SessionSource).where(
                SessionSource.session_id == session_id,
                SessionSource.role == "media", SessionSource.is_current.is_(True),
            ))
            attachment_id, revision = attachment.id, attachment.revision
        self.services["source_library"].detach(session_id, attachment_id, revision)
        with self.database.session() as session:
            self.assertFalse(resolve_media_source(session, session_id).has_audio)
        status = self.client.get(f"/api/v1/sessions/{session_id}/sources/subtitle-status").get_json()
        self.assertFalse(status["can_align"])
        self.assertIsNone(status["media_artifact_id"])
        with self.assertRaisesRegex(ValueError, "requires an attached"):
            self.services["workflows"].resolve_stage(
                session_id, "export", {"export_mode": "media", "audio_mode": "preserve"}
            )
