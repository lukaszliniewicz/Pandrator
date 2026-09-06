import tempfile
import unittest

from pandrator.runtime import DataPaths
from pandrator.web.artifact_selection import STAGE_OUTPUT_ROLES
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database, upgrade_database
from pandrator.web.jobs import JobQueue
from pandrator.web.media_edit import MediaEditService
from pandrator.web.models import OutcomePlan, SessionSource, SourceAsset
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workflows import MEDIA_EDIT_STAGES, WorkflowService
from pandrator.web.workspace import OutcomePlanService


class MediaEditWorkflowTests(unittest.TestCase):
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
