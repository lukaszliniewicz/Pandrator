"""Subtitle-first sessions must have real, reusable timed input, not just a filename."""

import io
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    Artifact,
    AudioTake,
    Document,
    DocumentRevision,
    GenerationPlan,
    GenerationSegment,
    Job,
    OutputAssembly,
    Segment,
    SessionSetting,
    SessionSource,
)
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


    def upload_media(self, filename="original.mp4"):
        response = self.client.post("/api/v1/uploads", data={"file": (io.BytesIO(b"test-media-payload"), filename)}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()

    def attach_media_asset(self, session_id, source_asset_id, *, idempotency_key=None):
        revision = self.client.get(f"/api/v1/sessions/{session_id}").get_json()["revision"]
        headers = {**self.headers, "If-Match": f'"{revision}"'}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return self.client.post(
            f"/api/v1/sessions/{session_id}/sources",
            json={"source_asset_id": source_asset_id, "role": "media"},
            headers=headers,
        )

    def attach_media(self, session_id, filename="original.mp4"):
        uploaded = self.upload_media(filename)
        attached = self.attach_media_asset(session_id, uploaded["source_asset_id"])
        self.assertEqual(attached.status_code, 201, attached.get_json())
        return uploaded

    def seed_recording_history(self, session_id, subtitle_artifact_id, media_artifact_id):
        plan = self.services["generation"].create_plan(
            session_id,
            source_revision_id=None,
            segments=[{"text": "A preserved speech block.", "source_segment_ids": ["cue-1"]}],
        )
        with self.database.session() as session:
            segment = session.scalar(
                select(GenerationSegment).where(
                    GenerationSegment.plan_revision_id == plan["active_revision_id"]
                )
            )
            correction = Artifact(
                session_id=session_id,
                kind="txt",
                role="correction",
                relative_path=f"sessions/{session_id}/history/correction.txt",
                metadata_json={"source_artifact_id": subtitle_artifact_id},
            )
            raw_take_artifact = Artifact(
                session_id=session_id,
                kind="audio",
                role="generation_take",
                relative_path=f"sessions/{session_id}/history/raw-take.wav",
            )
            word_timing = Artifact(
                session_id=session_id,
                kind="json",
                role="media_edit_word_timestamps",
                relative_path=f"sessions/{session_id}/history/word-timestamps.json",
                metadata_json={"source_media_artifact_id": media_artifact_id},
            )
            session.add_all([correction, raw_take_artifact, word_timing])
            session.flush()
            aligned = Artifact(
                session_id=session_id,
                kind="srt",
                role="transcription",
                relative_path=f"sessions/{session_id}/history/aligned.srt",
                metadata_json={
                    "authoritative_transcript_artifact_id": subtitle_artifact_id,
                    "source_artifact_id": media_artifact_id,
                    "source_media_artifact_id": media_artifact_id,
                    "aligned_word_timestamps_artifact_id": word_timing.id,
                },
            )
            take = AudioTake(
                generation_segment_id=segment.id,
                artifact_id=raw_take_artifact.id,
                kind="tts",
                status="completed",
                is_active=True,
                duration_ms=900,
            )
            assembly = OutputAssembly(
                session_id=session_id,
                status="completed",
                settings_json={"source_artifact_id": media_artifact_id},
            )
            session.add_all([aligned, take, assembly])
            session.flush()
            return {
                "plan_id": plan["id"],
                "plan_revision_id": plan["active_revision_id"],
                "segment_id": segment.id,
                "correction_id": correction.id,
                "raw_take_artifact_id": raw_take_artifact.id,
                "raw_take_id": take.id,
                "word_timing_id": word_timing.id,
                "aligned_id": aligned.id,
                "assembly_id": assembly.id,
            }

    def switch_media_attachment_in_db(self, session_id, source_asset_id):
        with self.database.session() as session:
            current = session.scalar(
                select(SessionSource).where(
                    SessionSource.session_id == session_id,
                    SessionSource.role == "media",
                    SessionSource.is_current.is_(True),
                )
            )
            current.is_current = False
            current.revision += 1
            session.add(
                SessionSource(
                    session_id=session_id,
                    source_asset_id=source_asset_id,
                    role="media",
                    is_current=True,
                )
            )
            session.flush()

    def test_independent_media_target_preserves_subtitle_source_and_revision(self):
        from pandrator.web.source_resolution import (
            resolve_media_source,
            resolve_primary_source,
        )
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

    def test_media_replacement_stales_recording_outputs_and_preserves_text_and_takes(self):
        session_id = self.create_session()
        subtitle = self.upload(session_id)
        media_a = self.attach_media(session_id)
        history = self.seed_recording_history(
            session_id,
            subtitle["attachment"]["subtitle_revision"]["artifact_id"],
            media_a["artifact_id"],
        )
        media_b = self.upload_media("replacement.wav")

        replaced = self.attach_media_asset(
            session_id,
            media_b["source_asset_id"],
            idempotency_key="subtitle-media-replacement",
        )
        self.assertEqual(replaced.status_code, 201, replaced.get_json())

        with self.database.session() as session:
            word_timing = session.get(Artifact, history["word_timing_id"])
            aligned = session.get(Artifact, history["aligned_id"])
            assembly = session.get(OutputAssembly, history["assembly_id"])
            primary_upload = session.get(Artifact, subtitle["artifact_id"])
            imported_srt = session.get(
                Artifact, subtitle["attachment"]["subtitle_revision"]["artifact_id"]
            )
            correction = session.get(Artifact, history["correction_id"])
            raw_take_artifact = session.get(Artifact, history["raw_take_artifact_id"])
            plan = session.get(GenerationPlan, history["plan_id"])
            take = session.get(AudioTake, history["raw_take_id"])
            timing = session.get(SessionSetting, (session_id, "_recording_timing_review"))

            self.assertEqual(word_timing.state, "stale")
            self.assertEqual(assembly.status, "stale")
            self.assertTrue((aligned.metadata_json or {}).get("timing_requires_review"))
            self.assertEqual(primary_upload.state, "current")
            self.assertEqual(imported_srt.state, "current")
            self.assertEqual(correction.state, "current")
            self.assertEqual(raw_take_artifact.state, "current")
            self.assertIsNotNone(plan)
            self.assertEqual(take.status, "completed")
            self.assertTrue(take.is_active)
            self.assertTrue((timing.value_json or {}).get("required"))
            self.assertEqual(
                (timing.value_json or {}).get("media_artifact_id"),
                media_b["artifact_id"],
            )

        subtitle_status = self.client.get(
            f"/api/v1/sessions/{session_id}/sources/subtitle-status"
        ).get_json()
        self.assertEqual(subtitle_status["media_artifact_id"], media_b["artifact_id"])
        self.assertIsNone(subtitle_status["word_timing_artifact_id"])

    def test_media_replacement_is_blocked_by_active_job_without_invalidation(self):
        for job_status in ("queued", "running"):
            with self.subTest(job_status=job_status):
                session_id = self.create_session()
                subtitle = self.upload(session_id)
                media_a = self.attach_media(session_id)
                history = self.seed_recording_history(
                    session_id,
                    subtitle["attachment"]["subtitle_revision"]["artifact_id"],
                    media_a["artifact_id"],
                )
                with self.database.session() as session:
                    session.add(Job(kind="dubbing.transcribe", session_id=session_id, status=job_status))
                    session.flush()
                    timing = session.get(SessionSetting, (session_id, "_recording_timing_review"))
                    timing_before = (timing.revision, dict(timing.value_json or {}))
                media_b = self.upload_media(f"blocked-replacement-{job_status}.wav")

                blocked = self.attach_media_asset(
                    session_id,
                    media_b["source_asset_id"],
                    idempotency_key=f"subtitle-media-replacement-blocked-{job_status}",
                )
                self.assertEqual(blocked.status_code, 409, blocked.get_json())

                with self.database.session() as session:
                    from pandrator.web.source_resolution import resolve_media_source

                    self.assertEqual(resolve_media_source(session, session_id).artifact.id, media_a["artifact_id"])
                    self.assertEqual(session.get(Artifact, history["word_timing_id"]).state, "current")
                    self.assertEqual(session.get(OutputAssembly, history["assembly_id"]).status, "completed")
                    timing = session.get(SessionSetting, (session_id, "_recording_timing_review"))
                    self.assertEqual((timing.revision, dict(timing.value_json or {})), timing_before)
                    self.assertIsNone(
                        session.scalar(
                            select(SessionSource).where(
                                SessionSource.session_id == session_id,
                                SessionSource.source_asset_id == media_b["source_asset_id"],
                                SessionSource.role == "media",
                                SessionSource.is_current.is_(True),
                            )
                        )
                    )

    def test_reattaching_same_media_does_not_invalidate_recording_outputs(self):
        session_id = self.create_session()
        subtitle = self.upload(session_id)
        media = self.attach_media(session_id)
        history = self.seed_recording_history(
            session_id,
            subtitle["attachment"]["subtitle_revision"]["artifact_id"],
            media["artifact_id"],
        )
        with self.database.session() as session:
            timing = session.get(SessionSetting, (session_id, "_recording_timing_review"))
            timing_before = (timing.revision, dict(timing.value_json or {}))

        reattached = self.attach_media_asset(
            session_id,
            media["source_asset_id"],
            idempotency_key="subtitle-media-reattach-same",
        )
        self.assertEqual(reattached.status_code, 201, reattached.get_json())

        with self.database.session() as session:
            self.assertEqual(session.get(Artifact, history["word_timing_id"]).state, "current")
            self.assertEqual(session.get(OutputAssembly, history["assembly_id"]).status, "completed")
            timing = session.get(SessionSetting, (session_id, "_recording_timing_review"))
            self.assertEqual((timing.revision, dict(timing.value_json or {})), timing_before)

    def test_media_replacement_without_idempotency_advances_and_checks_revision(self):
        session_id = self.create_session()
        self.upload(session_id)
        self.attach_media(session_id)
        revision = self.client.get(f"/api/v1/sessions/{session_id}").get_json()["revision"]
        media_b = self.upload_media("media-b.wav")
        media_c = self.upload_media("media-c.wav")
        endpoint = f"/api/v1/sessions/{session_id}/sources"
        headers = {**self.headers, "If-Match": f'"{revision}"'}
        first = self.client.post(endpoint, headers=headers, json={"role": "media", "source_asset_id": media_b["source_asset_id"]})
        self.assertEqual(201, first.status_code, first.get_json())
        self.assertEqual(f'"{revision + 1}"', first.headers["ETag"])
        second = self.client.post(endpoint, headers=headers, json={"role": "media", "source_asset_id": media_c["source_asset_id"]})
        self.assertEqual(409, second.status_code, second.get_json())
        status = self.client.get(f"/api/v1/sessions/{session_id}/sources/subtitle-status").get_json()
        self.assertEqual(media_b["artifact_id"], status["media_artifact_id"])

    def test_queued_alignment_for_old_media_is_rejected_before_ctc(self):
        session_id = self.create_session()
        self.upload(session_id)
        self.attach_media(session_id)
        endpoint = f"/api/v1/sessions/{session_id}/sources/align-subtitles"
        status = self.client.get(
            f"/api/v1/sessions/{session_id}/sources/subtitle-status"
        ).get_json()
        queued = self.client.post(
            endpoint,
            json={"method": "ctc", "expected_revision": status["session_revision"]},
            headers=self.headers,
        )
        self.assertEqual(queued.status_code, 202, queued.get_json())
        with self.database.session() as session:
            payload = dict(session.get(Job, queued.get_json()["job_id"]).payload_json)

        media_b = self.upload_media("legacy-switch.wav")
        self.switch_media_attachment_in_db(session_id, media_b["source_asset_id"])
        handlers = self.services["workflow_handlers"]
        with (
            patch.object(handlers, "_transcribe_media_edit_with_ctc") as aligner,
            self.assertRaisesRegex(ValueError, "attached recording changed"),
        ):
            handlers.transcribe(payload, lambda *args: None, threading.Event())
        aligner.assert_not_called()

    def test_subtitle_status_exposes_word_timing_only_for_current_matching_media(self):
        session_id = self.create_session()
        subtitle = self.upload(session_id)
        media = self.attach_media(session_id)
        history = self.seed_recording_history(
            session_id,
            subtitle["attachment"]["subtitle_revision"]["artifact_id"],
            media["artifact_id"],
        )
        endpoint = f"/api/v1/sessions/{session_id}/sources/subtitle-status"
        status = self.client.get(endpoint).get_json()
        self.assertEqual(status["word_timing_artifact_id"], history["word_timing_id"])

        with self.database.session() as session:
            aligned = session.get(Artifact, history["aligned_id"])
            aligned.metadata_json = {
                **dict(aligned.metadata_json or {}),
                "source_artifact_id": "a-different-media-artifact",
            }
        self.assertIsNone(self.client.get(endpoint).get_json()["word_timing_artifact_id"])

        with self.database.session() as session:
            aligned = session.get(Artifact, history["aligned_id"])
            aligned.metadata_json = {
                **dict(aligned.metadata_json or {}),
                "source_artifact_id": media["artifact_id"],
            }
            session.get(Artifact, history["word_timing_id"]).state = "stale"
        self.assertIsNone(self.client.get(endpoint).get_json()["word_timing_artifact_id"])

        with self.database.session() as session:
            session.get(Artifact, history["word_timing_id"]).state = "current"
            aligned = session.get(Artifact, history["aligned_id"])
            aligned.metadata_json = {
                key: value
                for key, value in dict(aligned.metadata_json or {}).items()
                if key != "aligned_word_timestamps_artifact_id"
            }
        self.assertIsNone(self.client.get(endpoint).get_json()["word_timing_artifact_id"])

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
