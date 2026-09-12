import tempfile
import unittest
from unittest import mock

from sqlalchemy import select

from pandrator.runtime import DataPaths
from pandrator.web.artifact_selection import STAGE_OUTPUT_ROLES
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database, upgrade_database
from pandrator.web.jobs import JobQueue
from pandrator.web.media_edit import MediaEditService
from pandrator.web.models import Artifact, MediaEditPlan, OutcomePlan, SessionSource, SourceAsset
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workflows import MEDIA_EDIT_STAGES, WorkflowService
from pandrator.web.workspace import OutcomePlanService


class MediaEditWorkflowTests(unittest.TestCase):
    @staticmethod
    def _progress(_value, _detail=None):
        return None

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = DataPaths.from_value(self.temporary.name).ensure()
        upgrade_database(self.paths.database)
        self.database = Database(self.paths.database)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.record = SessionService(self.database).create(
            "Edited webinar",
            workflow_kind="media_edit",
            included_stages=["transcribe", "edit_media", "correct", "export"],
        )
        self.session_dir = self.paths.sessions / self.record.storage_key
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.original = self._artifact(
            "original.mp4",
            role="upload",
            kind="video",
            content=b"original",
        )
        self.transcription = self._artifact(
            "transcription.srt",
            role="transcription",
            kind="srt",
            content=b"1\n00:00:00,000 --> 00:00:01,000\nHello\n",
            parent_ids=[self.original.id],
        )
        with self.database.session() as session:
            asset = SourceAsset(
                artifact_id=self.original.id,
                display_name="original.mp4",
                kind="mp4",
                mime_type="video/mp4",
            )
            session.add(asset)
            session.flush()
            session.add(
                SessionSource(
                    session_id=self.record.id,
                    source_asset_id=asset.id,
                    role="primary",
                    is_current=True,
                )
            )
            session.add(
                OutcomePlan(
                    session_id=self.record.id,
                    value_json={
                        "workflow_kind": "media_edit",
                        "deliverables": {"edited_media": True},
                        "transformations": {
                            "transcribe": True,
                            "media_edit": True,
                            "correct": True,
                            "generate_audio": False,
                        },
                        "inputs": {
                            "translation": "source",
                            "generation": "media_edit",
                        },
                    },
                )
            )
        self.plan = MediaEditService(
            self.database,
            self.artifacts,
            lambda _session_id: self.session_dir,
            duration_probe=lambda _path: 5000,
        ).prepare(self.record.id)["plan"]
        self.workflow = WorkflowService(self.database, JobQueue(self.database))
        self.handlers = WorkflowHandlers(self.database, self.paths)

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _artifact(
        self,
        name,
        *,
        role,
        kind,
        content,
        parent_ids=None,
        metadata=None,
    ):
        path = self.session_dir / name
        path.write_bytes(content)
        return self.artifacts.register(
            path,
            kind=kind,
            role=role,
            session_id=self.record.id,
            parent_ids=parent_ids,
            metadata=metadata,
        )

    def _edit_metadata(self):
        return {
            "revision_id": self.plan["revision_id"],
            "content_hash": self.plan["content_hash"],
        }

    def _rendered_media(self, name="edited.mp4"):
        return self._artifact(
            name,
            role="media_edit_media",
            kind="video",
            content=b"edited media",
            metadata=self._edit_metadata(),
        )

    def _convert_to_voiceover(self):
        self.record = SessionService(self.database).update(
            self.record.id,
            self.record.revision,
            {"workflow_kind": "voiceover"},
        )

    def _media_edit_service(self):
        return MediaEditService(
            self.database,
            self.artifacts,
            lambda _session_id: self.session_dir,
            duration_probe=lambda _path: 5000,
        )

    def test_definitions_make_edit_reviewable_and_downstream(self):
        self.assertEqual(
            [
                "transcribe",
                "edit_media",
                "correct",
                "translate",
                "optimize_document",
                "optimize_tts",
                "preview",
                "generate_audio",
                "export",
            ],
            [stage.key for stage in MEDIA_EDIT_STAGES],
        )
        edit = next(stage for stage in MEDIA_EDIT_STAGES if stage.key == "edit_media")
        self.assertFalse(edit.executable)
        self.assertEqual(("media_edit_subtitles",), STAGE_OUTPUT_ROLES["edit_media"])

    def test_outcome_plan_projection_retains_media_edit_stage(self):
        service = OutcomePlanService(self.database)
        current = service.get(self.record.id)

        updated = service.update(
            self.record.id,
            current["revision"],
            current["value"],
        )

        self.assertIn("edit_media", [item["key"] for item in updated["pipeline"]])
        with self.database.session() as session:
            record = session.get(type(self.record), self.record.id)
            self.assertIn("edit_media", record.included_stages_json)

    def test_edited_subtitles_feed_text_stages_and_generation(self):
        edited_subtitles = self._artifact(
            "edited.srt",
            role="media_edit_subtitles",
            kind="srt",
            content=b"1\n00:00:00,000 --> 00:00:01,000\nHello\n",
            metadata=self._edit_metadata(),
        )

        correction = self.workflow.resolve_stage(self.record.id, "correct")
        generation = self.workflow.resolve_stage(self.record.id, "generate_audio")

        self.assertEqual(edited_subtitles.id, correction.source_artifact_id)
        self.assertEqual(edited_subtitles.id, generation.source_artifact_id)
        self.assertEqual("workflow.continue", generation.job_kind)

    def test_inflight_materialized_subtitle_document_keeps_media_edit_identity(self):
        edited_subtitles = self._artifact(
            "materialized-edited.srt",
            role="media_edit_subtitles",
            kind="srt",
            content=b"1\n00:00:00,000 --> 00:00:01,000\nHello\n",
            metadata={
                "revision_id": "document-revision-id",
                "plan_id": self.plan["plan_id"],
                "revision": self.plan["revision"],
                "content_hash": self.plan["content_hash"],
            },
        )

        snapshot = self.workflow.snapshot(self.record.id)
        stages = {stage["key"]: stage for stage in snapshot["stages"]}

        self.assertEqual("completed", stages["edit_media"]["status"])
        self.assertEqual(edited_subtitles.id, stages["edit_media"]["artifact"]["id"])
        self.assertEqual("ready", stages["correct"]["status"])

    def test_export_contract_pins_rendered_media_not_original(self):
        self._artifact(
            "edited.srt",
            role="media_edit_subtitles",
            kind="srt",
            content=b"1\n00:00:00,000 --> 00:00:01,000\nHello\n",
            metadata=self._edit_metadata(),
        )
        edited_media = self._artifact(
            "edited.mp4",
            role="media_edit_media",
            kind="video",
            content=b"edited",
            metadata=self._edit_metadata(),
        )

        resolved = self.workflow.resolve_stage(
            self.record.id,
            "export",
            {"export_mode": "media", "audio_mode": "preserve"},
        )

        contract = resolved.payload["export_contract"]
        self.assertEqual(edited_media.id, contract["source_artifact_id"])
        self.assertEqual("derived_media_edit", contract["source_resolution"])
        self.assertEqual(edited_media.id, resolved.source_artifact_id)
        self.assertNotEqual(self.original.id, contract["source_artifact_id"])

    def test_export_rejects_render_from_an_obsolete_edit_revision(self):
        edited_media = self._artifact(
            "obsolete-edit.mp4",
            role="media_edit_media",
            kind="video",
            content=b"obsolete",
            metadata=self._edit_metadata(),
        )
        MediaEditService(
            self.database,
            self.artifacts,
            lambda _session_id: self.session_dir,
            duration_probe=lambda _path: 5000,
        ).update(
            self.record.id,
            self.plan["revision"],
            keep_ranges=self.plan["keep_ranges"],
            instructions="A newer edit.",
        )
        # Simulate a legacy/racing row that escaped invalidation. Resolution
        # still refuses an artifact from anything but the active revision.
        with self.database.session() as session:
            session.get(type(edited_media), edited_media.id).state = "current"

        with self.assertRaisesRegex(ValueError, "Render and review"):
            self.workflow.resolve_stage(
                self.record.id,
                "export",
                {"export_mode": "media", "audio_mode": "preserve"},
            )

    def test_converted_voiceover_exports_pin_the_active_edited_media(self):
        edited_media = self._rendered_media()
        self._convert_to_voiceover()

        for export_mode in ("media", "audio"):
            for audio_mode in ("preserve", "mixed", "dubbing_only"):
                with self.subTest(export_mode=export_mode, audio_mode=audio_mode):
                    resolved = self.workflow.resolve_stage(
                        self.record.id,
                        "export",
                        {
                            "export_mode": export_mode,
                            "audio_mode": audio_mode,
                        },
                    )

                    contract = resolved.payload["export_contract"]
                    self.assertEqual(edited_media.id, contract["source_artifact_id"])
                    self.assertEqual(
                        edited_media.content_hash,
                        contract["source_content_hash"],
                    )
                    self.assertEqual("derived_media_edit", contract["source_resolution"])

    def test_converted_voiceover_export_requires_a_current_render(self):
        self._convert_to_voiceover()

        for export_mode in ("media", "audio"):
            with self.subTest(export_mode=export_mode):
                with self.assertRaisesRegex(ValueError, "Render and review"):
                    self.workflow.resolve_stage(
                        self.record.id,
                        "export",
                        {
                            "export_mode": export_mode,
                            "audio_mode": "preserve",
                        },
                    )

    def test_converted_voiceover_export_rejects_an_obsolete_current_render(self):
        edited_media = self._rendered_media("obsolete-edited.mp4")
        newer = self._media_edit_service().update(
            self.record.id,
            self.plan["revision"],
            keep_ranges=self.plan["keep_ranges"],
            instructions="A newer edit.",
        )
        self.assertNotEqual(self.plan["revision_id"], newer["plan"]["revision_id"])
        with self.database.session() as session:
            session.get(Artifact, edited_media.id).state = "current"
        self._convert_to_voiceover()

        for export_mode in ("media", "audio"):
            with self.subTest(export_mode=export_mode):
                with self.assertRaisesRegex(ValueError, "Render and review"):
                    self.workflow.resolve_stage(
                        self.record.id,
                        "export",
                        {
                            "export_mode": export_mode,
                            "audio_mode": "preserve",
                        },
                    )

    def test_worker_rejects_an_original_source_contract_for_converted_voiceover(self):
        self._rendered_media()
        self._convert_to_voiceover()
        payload = {
            "session_id": self.record.id,
            "settings": {"export_mode": "media", "audio_mode": "preserve"},
            "export_contract": {
                "version": 1,
                "workflow_kind": "voiceover",
                "export_mode": "media",
                "audio_mode": "preserve",
                "source_artifact_id": self.original.id,
                "source_content_hash": self.original.content_hash,
                "source_profile": "video",
                "source_resolution": "attached",
            },
        }

        with mock.patch(
            "pandrator.logic.dubbing.audio_sync.media_has_audio_stream"
        ) as media_has_audio_stream:
            with self.assertRaisesRegex(ValueError, "selected media edit changed"):
                self.handlers.export(payload, self._progress, mock.sentinel.cancel)

        media_has_audio_stream.assert_not_called()

    def test_worker_rejects_a_queued_export_after_the_edit_revision_changes(self):
        edited_media = self._rendered_media()
        old_revision_id = self.plan["revision_id"]
        newer = self._media_edit_service().update(
            self.record.id,
            self.plan["revision"],
            keep_ranges=self.plan["keep_ranges"],
            instructions="A newer edit.",
        )
        newer_revision_id = newer["plan"]["revision_id"]
        with self.database.session() as session:
            plan = session.scalar(
                select(MediaEditPlan).where(MediaEditPlan.session_id == self.record.id)
            )
            plan.active_revision_id = old_revision_id
            session.get(Artifact, edited_media.id).state = "current"
        self._convert_to_voiceover()
        queued = self.workflow.resolve_stage(
            self.record.id,
            "export",
            {"export_mode": "media", "audio_mode": "preserve"},
        )
        with self.database.session() as session:
            plan = session.scalar(
                select(MediaEditPlan).where(MediaEditPlan.session_id == self.record.id)
            )
            plan.active_revision_id = newer_revision_id
            session.get(Artifact, edited_media.id).state = "current"

        with mock.patch(
            "pandrator.logic.dubbing.audio_sync.media_has_audio_stream"
        ) as media_has_audio_stream:
            with self.assertRaisesRegex(ValueError, "media-edit revision changed"):
                self.handlers.export(
                    queued.payload,
                    self._progress,
                    mock.sentinel.cancel,
                )

        media_has_audio_stream.assert_not_called()

    def test_subtitle_only_voiceover_export_does_not_require_a_render(self):
        self._convert_to_voiceover()
        for export_mode in ("subtitles", "text"):
            with self.subTest(export_mode=export_mode):
                resolved = self.workflow.resolve_stage(
                    self.record.id,
                    "export",
                    {
                        "export_mode": export_mode,
                        "subtitle_format": "srt",
                        "subtitle_selection": "source",
                    },
                )

                self.assertEqual(
                    export_mode,
                    resolved.payload["export_contract"]["export_mode"],
                )
                result = self.handlers.export(
                    resolved.payload,
                    self._progress,
                    mock.sentinel.cancel,
                )

                exported, _path = self.artifacts.resolve(result["artifact_ids"][0])
                self.assertEqual(
                    f"export_{'subtitle' if export_mode == 'subtitles' else 'text'}_source",
                    exported.role,
                )

    def test_voiceover_without_a_media_edit_plan_keeps_the_original_source(self):
        voiceover = SessionService(self.database).create(
            "Ordinary voiceover",
            workflow_kind="voiceover",
        )
        voiceover_dir = self.paths.sessions / voiceover.storage_key
        voiceover_dir.mkdir(parents=True, exist_ok=True)
        source_path = voiceover_dir / "ordinary.mp4"
        source_path.write_bytes(b"ordinary source")
        source = self.artifacts.register(
            source_path,
            kind="video",
            role="upload",
            session_id=voiceover.id,
        )

        resolved = self.workflow.resolve_stage(
            voiceover.id,
            "export",
            {"export_mode": "media", "audio_mode": "preserve"},
        )

        contract = resolved.payload["export_contract"]
        self.assertEqual(source.id, contract["source_artifact_id"])
        self.assertEqual(source.content_hash, contract["source_content_hash"])
        self.assertEqual("legacy", contract["source_resolution"])

    def test_continuation_uses_edited_subtitles_without_retranscribing_media(self):
        self.assertEqual(
            ("media_edit_subtitles",),
            WorkflowHandlers._continuation_input_roles(
                "generate_audio",
                ("translation", "correction", "transcription"),
                "media_edit",
                {"generation": "media_edit"},
                {},
            ),
        )
        required = WorkflowHandlers._continuation_required_stages(
            "media_edit",
            "generate_audio",
            False,
            {"generation": "source", "translation": "source"},
            {"correction": False, "translation": False},
        )
        self.assertEqual({"generate_audio"}, required)


if __name__ == "__main__":
    unittest.main()
