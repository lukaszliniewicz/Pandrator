"""Subtitle-first sessions must have real, reusable timed input, not just a filename."""

import hashlib
import io
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, select

from pandrator.logic.dubbing.llm_correction import CorrectionResult
from pandrator.logic.dubbing.llm_translation import TranslationResult
from pandrator.web.api import create_app
from pandrator.web.artifact_selection import selected_artifacts
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
    SessionStageSelection,
)
from pandrator.web.subtitle_sources import parse_subtitle_source

SRT = "1\n00:00:01,000 --> 00:00:03,000\nThese meetings, these interfaith circles.\n\n2\n00:00:04,000 --> 00:00:06,000\nSomething that can help.\n"
VTT = "WEBVTT\n\n00:01.000 --> 00:03.000\nFirst source cue.\n\n00:04.000 --> 00:06.000\nSecond source cue.\n"
VTT_SRT = "1\n00:00:01,000 --> 00:00:03,000\nFirst source cue.\n\n2\n00:00:04,000 --> 00:00:06,000\nSecond source cue.\n"
CORRECTED_SRT = "1\n00:00:01,000 --> 00:00:03,000\nFirst corrected cue.\n\n2\n00:00:04,000 --> 00:00:06,000\nSecond corrected cue.\n"
TRANSLATED_SRT = "1\n00:00:01,000 --> 00:00:03,000\nErster korrigierter Satz.\n\n2\n00:00:04,000 --> 00:00:06,000\nZweiter korrigierter Satz.\n"


class SubtitleFirstWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        bootstrap = BootstrapTokenStore()
        self.app = create_app(data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap)
        self.addCleanup(self.app.extensions["pandrator"]["database"].dispose)
        self.client = self.app.test_client()
        # Start token lifetime after potentially slow app setup.
        token = bootstrap.issue()
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

    def upload_legacy_subtitles(self, session_id, content, filename):
        library = self.services["source_library"]
        saved = library.artifacts
        library.artifacts = None
        try:
            return self.upload(session_id, content, filename)
        finally:
            library.artifacts = saved

    def assert_vtt_revision(self, session_id, uploaded, adopted, *, reused):
        self.assertEqual(adopted["reused"], reused)
        self.assertEqual(adopted["source_artifact_id"], uploaded["artifact_id"])
        source, source_path = self.services["artifacts"].resolve(uploaded["artifact_id"])
        original_bytes = VTT.encode()
        source_digest = hashlib.sha256(original_bytes).hexdigest()
        self.assertEqual(source_path.read_bytes(), original_bytes)
        self.assertEqual(source.content_hash, source_digest)

        canonical, canonical_path = self.services["artifacts"].resolve(adopted["artifact_id"])
        self.assertEqual(canonical.role, "transcription")
        self.assertEqual(canonical.kind, "srt")
        self.assertTrue(
            canonical.relative_path.endswith(f"imported-subtitles/{source_digest}.srt")
        )
        self.assertEqual(canonical_path.read_text(encoding="utf-8"), VTT_SRT)

        with self.database.session() as session:
            document = session.get(Document, adopted["document_id"])
            revision = session.get(DocumentRevision, adopted["revision_id"])
            rows = list(
                session.scalars(
                    select(Segment)
                    .where(Segment.revision_id == adopted["revision_id"])
                    .order_by(Segment.ordinal)
                )
            )
            self.assertEqual(document.stage, "transcription")
            self.assertEqual(document.active_revision_id, revision.id)
            self.assertEqual(revision.document_id, document.id)
            self.assertEqual(revision.revision_number, 1)
            self.assertEqual(
                [(row.start_ms, row.end_ms, row.text) for row in rows],
                [
                    (1000, 3000, "First source cue."),
                    (4000, 6000, "Second source cue."),
                ],
            )
            self.assertEqual(
                (canonical.metadata_json or {}).get("imported_source_hash"),
                source_digest,
            )

        return source_path, original_bytes, canonical

    def run_vtt_transforms(self, session_id, canonical_artifact_id):
        handlers = self.services["workflow_handlers"]
        workflows = self.services["workflows"]
        observed = {}

        def write_result(operation_dir, filename, content):
            path = Path(operation_dir) / filename
            path.write_text(content, encoding="utf-8")
            return path

        def fake_correction(operation_dir, input_path, settings, **_kwargs):
            observed["correction_input"] = Path(input_path).read_text(encoding="utf-8")
            self.assertEqual(settings["original_language"], "en")
            self.assertEqual(
                [(cue.start_ms, cue.end_ms, cue.text) for cue in parse_subtitle_source(observed["correction_input"], "srt")],
                [(1000, 3000, "First source cue."), (4000, 6000, "Second source cue.")],
            )
            path = write_result(operation_dir, "fixture-corrected.srt", CORRECTED_SRT)
            return CorrectionResult(srt_content=CORRECTED_SRT, output_path=str(path))

        def fake_translation(operation_dir, input_path, settings, **_kwargs):
            observed["translation_input"] = Path(input_path).read_text(encoding="utf-8")
            observed["translation_languages"] = (
                settings["original_language"],
                settings["target_language"],
            )
            self.assertEqual(
                [(cue.start_ms, cue.end_ms, cue.text) for cue in parse_subtitle_source(observed["translation_input"], "srt")],
                [(1000, 3000, "First corrected cue."), (4000, 6000, "Second corrected cue.")],
            )
            path = write_result(operation_dir, "fixture-translated.srt", TRANSLATED_SRT)
            return TranslationResult(
                srt_content=TRANSLATED_SRT,
                block_responses=[],
                glossary={},
                output_path=str(path),
            )

        def fixture_llm_settings(settings, stage):
            model_key = "correction_model" if stage == "correction" else "translation_model"
            return {
                **settings,
                "llm_default_model": "fixture/model",
                model_key: "fixture/model",
                "request_timeout_seconds": 30,
                "max_segments_per_batch": 1,
            }

        correction_settings = {
            "original_language": "en",
            "source_language": "en",
            "target_language": "de",
            "correction_style": "publishable",
            "web_research_enabled": False,
        }
        translation_settings = {
            "original_language": "en",
            "source_language": "en",
            "target_language": "de",
            "translation_backend": "llm",
            "web_research_enabled": False,
        }

        correction_stage = workflows.resolve_stage(
            session_id, "correct", correction_settings
        )
        self.assertEqual(
            correction_stage.payload["source_artifact_id"], canonical_artifact_id
        )
        with (
            patch.object(handlers, "_with_database_llm_settings", side_effect=fixture_llm_settings),
            patch.object(handlers, "_run_stage_web_research", return_value=None),
            patch(
                "pandrator.logic.dubbing.llm_correction.correct_srt_file_with_result",
                side_effect=fake_correction,
            ) as correction_engine,
            patch(
                "pandrator.logic.dubbing.llm_translation.translate_srt_file_with_result",
                side_effect=fake_translation,
            ) as translation_engine,
        ):
            corrected = handlers.correct(
                correction_stage.payload, lambda *_args: None, threading.Event()
            )
            with self.database.session() as session:
                selected_correction = selected_artifacts(session, session_id)["correct"]
                self.assertEqual(selected_correction.id, corrected["artifact_id"])
                correction_selection = session.get(
                    SessionStageSelection, (session_id, "correct")
                )
                correction_selection_revision = correction_selection.revision
                correction_revision = session.get(
                    DocumentRevision,
                    (selected_correction.metadata_json or {})["revision_id"],
                )
                correction_document = session.get(Document, correction_revision.document_id)
                correction_rows = list(
                    session.scalars(
                        select(Segment)
                        .where(Segment.revision_id == correction_revision.id)
                        .order_by(Segment.ordinal)
                    )
                )
                self.assertEqual(correction_document.stage, "correction")
                self.assertEqual(correction_document.active_revision_id, correction_revision.id)
                self.assertEqual(
                    [(row.start_ms, row.end_ms, row.text) for row in correction_rows],
                    [
                        (1000, 3000, "First corrected cue."),
                        (4000, 6000, "Second corrected cue."),
                    ],
                )

            input_manifest = self.client.get(
                f"/api/v1/sessions/{session_id}/workflow-inputs"
            ).get_json()
            selected_input = self.client.put(
                f"/api/v1/sessions/{session_id}/workflow-inputs",
                json={
                    "consumer": "translation",
                    "role": "correction",
                    "artifact_id": corrected["artifact_id"],
                    "expected_outcome_revision": input_manifest["outcome_revision"],
                    "expected_selection_revision": correction_selection_revision,
                    "expected_translation_settings_revision": input_manifest[
                        "translation_settings_revision"
                    ],
                },
                headers={
                    **self.headers,
                    "Idempotency-Key": f"vtt-translation-input-{session_id}",
                },
            )
            self.assertEqual(selected_input.status_code, 200, selected_input.get_json())
            self.assertEqual(
                selected_input.get_json()["selected"]["artifact_id"],
                corrected["artifact_id"],
            )
            translation_stage = workflows.resolve_stage(
                session_id, "translate", translation_settings
            )
            self.assertEqual(
                translation_stage.payload["source_artifact_id"],
                corrected["artifact_id"],
            )
            translated = handlers.translate(
                translation_stage.payload, lambda *_args: None, threading.Event()
            )

        correction_engine.assert_called_once()
        translation_engine.assert_called_once()
        self.assertEqual(observed["translation_languages"], ("en", "de"))
        self.assertEqual(
            [(cue.start_ms, cue.end_ms, cue.text) for cue in parse_subtitle_source(observed["correction_input"], "srt")],
            [(1000, 3000, "First source cue."), (4000, 6000, "Second source cue.")],
        )
        self.assertEqual(
            [(cue.start_ms, cue.end_ms, cue.text) for cue in parse_subtitle_source(observed["translation_input"], "srt")],
            [(1000, 3000, "First corrected cue."), (4000, 6000, "Second corrected cue.")],
        )
        with self.database.session() as session:
            selected_translation = selected_artifacts(session, session_id)["translate"]
            self.assertEqual(selected_translation.id, translated["artifact_id"])
            self.assertFalse(
                any(
                    "transcrib" in job.kind.casefold()
                    for job in session.scalars(
                        select(Job).where(Job.session_id == session_id)
                    )
                )
            )

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

    def test_two_cue_vtt_upload_is_idempotent_and_flows_through_correction_and_translation(self):
        session_id = self.create_session("subtitles")
        uploaded = self.upload(session_id, VTT, "source.vtt")

        adoption = self.client.post(
            f"/api/v1/sessions/{session_id}/sources/adopt-subtitles",
            json={"source_asset_id": uploaded["source_asset_id"]},
            headers=self.headers,
        )
        self.assertEqual(adoption.status_code, 200, adoption.get_json())
        adoption_body = adoption.get_json()
        self.assertEqual(
            adoption_body["artifact_id"],
            uploaded["attachment"]["subtitle_revision"]["artifact_id"],
        )
        source_path, original_bytes, canonical = self.assert_vtt_revision(
            session_id, uploaded, adoption_body, reused=True
        )

        replay = self.client.post(
            f"/api/v1/sessions/{session_id}/sources/adopt-subtitles",
            json={"source_asset_id": uploaded["source_asset_id"]},
            headers=self.headers,
        )
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertTrue(replay.get_json()["reused"])
        self.assertEqual(replay.get_json()["artifact_id"], canonical.id)

        self.run_vtt_transforms(session_id, canonical.id)
        self.assertEqual(source_path.read_bytes(), original_bytes)

    def test_legacy_two_cue_vtt_adoption_is_idempotent_and_transformable(self):
        session_id = self.create_session("subtitles")
        uploaded = self.upload_legacy_subtitles(session_id, VTT, "legacy-source.vtt")
        self.assertIsNone(uploaded["attachment"]["subtitle_revision"])

        adoption = self.client.post(
            f"/api/v1/sessions/{session_id}/sources/adopt-subtitles",
            json={"source_asset_id": uploaded["source_asset_id"]},
            headers=self.headers,
        )
        self.assertEqual(adoption.status_code, 201, adoption.get_json())
        adoption_body = adoption.get_json()
        source_path, original_bytes, canonical = self.assert_vtt_revision(
            session_id, uploaded, adoption_body, reused=False
        )

        replay = self.client.post(
            f"/api/v1/sessions/{session_id}/sources/adopt-subtitles",
            json={"source_asset_id": uploaded["source_asset_id"]},
            headers=self.headers,
        )
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertTrue(replay.get_json()["reused"])
        self.assertEqual(replay.get_json()["artifact_id"], canonical.id)

        self.run_vtt_transforms(session_id, canonical.id)
        self.assertEqual(source_path.read_bytes(), original_bytes)

    def test_malformed_legacy_vtt_adoption_has_no_partial_publication(self):
        session_id = self.create_session("subtitles")
        malformed = (
            "WEBVTT\n\n00:01.000 --> 00:03.000\nValid cue.\n\n"
            "00:04.000 --> not-a-time\nMalformed cue.\n"
        )
        uploaded = self.upload_legacy_subtitles(
            session_id, malformed, "legacy-malformed.vtt"
        )
        _, source_path = self.services["artifacts"].resolve(uploaded["artifact_id"])
        source_bytes = malformed.encode()
        source_digest = hashlib.sha256(source_bytes).hexdigest()
        self.assertEqual(source_path.read_bytes(), source_bytes)

        adoption = self.client.post(
            f"/api/v1/sessions/{session_id}/sources/adopt-subtitles",
            json={"source_asset_id": uploaded["source_asset_id"]},
            headers=self.headers,
        )
        self.assertEqual(adoption.status_code, 422, adoption.get_json())
        self.assertIn("invalid cue timestamp", adoption.get_json()["error"]["message"])
        self.assertEqual(source_path.read_bytes(), source_bytes)
        self.assertEqual(
            list(self.services["paths"].sessions.rglob(f"{source_digest}.srt")), []
        )
        with self.database.session() as session:
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(Artifact).where(
                        Artifact.session_id == session_id,
                        Artifact.role == "transcription",
                    )
                ),
                0,
            )
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(Document).where(
                        Document.session_id == session_id,
                        Document.stage == "transcription",
                    )
                ),
                0,
            )
            self.assertFalse(
                any(
                    "transcrib" in job.kind.casefold()
                    for job in session.scalars(
                        select(Job).where(Job.session_id == session_id)
                    )
                )
            )

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
