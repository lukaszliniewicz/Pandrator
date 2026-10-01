import tempfile
import threading
import unittest
from unittest import mock

from sqlalchemy import select

from pandrator.runtime import DataPaths
from pandrator.web.artifact_selection import STAGE_OUTPUT_ROLES
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database, upgrade_database
from pandrator.web.export_inputs import resolve_export_inputs, select_media_export
from pandrator.web.jobs import JobQueue
from pandrator.web.media_edit import MediaEditService
from pandrator.web.models import (
    Artifact,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    MediaEditPlan,
    OutcomePlan,
    OutputAssembly,
    SessionSource,
    SourceAsset,
)
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workflows import MEDIA_EDIT_STAGES, WorkflowService
from pandrator.web.workspace import (
    OutcomePlanService,
    expected_output_assembly_snapshot,
    output_assembly_settings_hash,
)


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

    def _generation_run(self, *, status="completed", session_id=None):
        session_id = session_id or self.record.id
        with self.database.session() as session:
            plan = GenerationPlan(session_id=session_id)
            session.add(plan)
            session.flush()
            revision = GenerationPlanRevision(
                plan_id=plan.id,
                revision_number=1,
                settings_json={},
                operation_json={},
                content_hash=f"{session_id}-generation-plan",
            )
            session.add(revision)
            session.flush()
            plan.active_revision_id = revision.id
            run = GenerationRun(
                session_id=session_id,
                plan_revision_id=revision.id,
                sequence_number=1,
                status=status,
                settings_snapshot_json={},
            )
            session.add(run)
            session.flush()
            return run.id

    def _store_run_assembly(self, run_id, snapshot, *, name="selected.wav"):
        assembly_path = self.session_dir / name
        assembly_path.write_bytes(b"selected run assembly")
        artifact = self.artifacts.register(
            assembly_path,
            kind="audio",
            role="assembled_audio",
            session_id=self.record.id,
        )
        with self.database.session() as session:
            run = session.get(GenerationRun, run_id)
            expected_snapshot = expected_output_assembly_snapshot(
                session,
                run,
                snapshot,
            )
            expected_hash = output_assembly_settings_hash(snapshot)
            assembly = OutputAssembly(
                session_id=self.record.id,
                generation_run_id=run_id,
                status="completed",
                artifact_id=artifact.id,
                settings_json={
                    "resolved": expected_snapshot,
                    "plan_revision_id": run.plan_revision_id,
                },
                settings_hash=expected_hash,
            )
            session.add(assembly)
            session.flush()
        return artifact

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

    def test_pinned_run_mixed_or_dubbed_exports_route_through_variant(self):
        edited_media = self._rendered_media()
        run_id = self._generation_run()

        for export_mode in ("media", "audio"):
            for audio_mode in ("mixed", "dubbing_only"):
                with self.subTest(export_mode=export_mode, audio_mode=audio_mode):
                    resolved = self.workflow.resolve_stage(
                        self.record.id,
                        "export",
                        {
                            "export_mode": export_mode,
                            "audio_mode": audio_mode,
                            "generation_run_id": run_id,
                        },
                    )

                    self.assertEqual("export.variant", resolved.job_kind)
                    self.assertEqual(run_id, resolved.payload["settings"]["generation_run_id"])
                    contract = resolved.payload["export_contract"]
                    self.assertEqual(audio_mode, contract["audio_mode"])
                    self.assertEqual(edited_media.id, contract["source_artifact_id"])
                    self.assertEqual(
                        edited_media.content_hash,
                        contract["source_content_hash"],
                    )
                    self.assertEqual("derived_media_edit", contract["source_resolution"])

    def test_pinned_preserve_and_unpinned_media_edit_exports_stay_source_only(self):
        edited_media = self._rendered_media()
        run_id = self._generation_run()

        pinned_preserve = self.workflow.resolve_stage(
            self.record.id,
            "export",
            {
                "export_mode": "media",
                "audio_mode": "preserve",
                "generation_run_id": run_id,
            },
        )
        self.assertEqual("export.create", pinned_preserve.job_kind)
        self.assertEqual("preserve", pinned_preserve.payload["export_contract"]["audio_mode"])

        legacy_mixed_without_run = self.workflow.resolve_stage(
            self.record.id,
            "export",
            {"export_mode": "media", "audio_mode": "mixed"},
        )
        self.assertEqual("export.create", legacy_mixed_without_run.job_kind)
        self.assertIsNone(legacy_mixed_without_run.payload["export_contract"]["audio_mode"])
        self.assertEqual(
            edited_media.id,
            legacy_mixed_without_run.payload["export_contract"]["source_artifact_id"],
        )
        for resolved in (pinned_preserve, legacy_mixed_without_run):
            inputs = resolve_export_inputs(
                self.handlers._output_workflow_context(),
                resolved.payload,
            )
            selection = select_media_export(inputs)
            self.assertEqual("source", selection.audio_mode)
            self.assertIsNone(selection.dubbing_audio)
        with self.database.session() as session:
            self.assertIsNone(
                session.scalar(
                    select(OutputAssembly).where(
                        OutputAssembly.session_id == self.record.id,
                    )
                )
            )

    def test_subtitle_and_text_exports_do_not_assemble_selected_run(self):
        self._rendered_media()
        run_id = self._generation_run()
        for export_mode in ("subtitles", "text"):
            with self.subTest(export_mode=export_mode):
                resolved = self.workflow.resolve_stage(
                    self.record.id,
                    "export",
                    {
                        "export_mode": export_mode,
                        "audio_mode": "invalid-audio-mode",
                        "generation_run_id": run_id,
                    },
                )

                self.assertEqual("export.create", resolved.job_kind)
                self.assertEqual(run_id, resolved.payload["settings"]["generation_run_id"])
                self.assertIsNone(resolved.payload["export_contract"]["audio_mode"])
        with self.database.session() as session:
            self.assertIsNone(
                session.scalar(
                    select(OutputAssembly).where(
                        OutputAssembly.session_id == self.record.id,
                        OutputAssembly.generation_run_id == run_id,
                    )
                )
            )

    def test_pinned_media_edit_export_rejects_foreign_or_unfinished_runs(self):
        self._rendered_media()
        unfinished_run_id = self._generation_run(status="running")
        foreign_session = SessionService(self.database).create(
            "Another media edit", workflow_kind="media_edit"
        )
        foreign_run_id = self._generation_run(session_id=foreign_session.id)

        for run_id, message in (
            (unfinished_run_id, "Only a completed generation run"),
            (foreign_run_id, "does not belong to this session"),
        ):
            with self.subTest(run_id=run_id):
                with self.assertRaisesRegex(ValueError, message):
                    self.workflow.resolve_stage(
                        self.record.id,
                        "export",
                        {
                            "export_mode": "media",
                            "audio_mode": "mixed",
                            "generation_run_id": run_id,
                        },
                    )

    def test_variant_export_uses_exact_run_assembly_and_mixed_or_dubbed_audio(self):
        edited_media = self._rendered_media()
        run_id = self._generation_run()
        settings = {
            "export_mode": "media",
            "audio_mode": "mixed",
            "generation_run_id": run_id,
        }
        initial = self.workflow.resolve_stage(self.record.id, "export", settings)
        assembly = self._store_run_assembly(
            run_id,
            initial.payload["resolved_settings_snapshot"],
        )
        chosen_audio = []

        def render_stub(context, inputs, selection, *, output_dir, export_name, **_kwargs):
            chosen_audio.append(
                (selection.audio_mode, selection.upload_media.id, selection.dubbing_audio.id)
            )
            destination = output_dir / f"{export_name}_{selection.audio_mode}.mp4"
            destination.write_bytes(b"stubbed rendered video")
            return context.artifacts.register(
                destination,
                kind="export",
                role=f"export_{selection.audio_mode}",
                session_id=inputs.session_id,
                parent_ids=[selection.upload_media.id, selection.dubbing_audio.id],
                settings=inputs.settings,
            )

        for audio_mode, expected_worker_mode in (
            ("mixed", "mixed"),
            ("dubbing_only", "dubbed"),
        ):
            with self.subTest(audio_mode=audio_mode):
                queued = self.workflow.resolve_stage(
                    self.record.id,
                    "export",
                    {**settings, "audio_mode": audio_mode},
                )
                self.assertEqual("export.variant", queued.job_kind)
                self.assertEqual(audio_mode, queued.payload["export_contract"]["audio_mode"])
                with (
                    mock.patch(
                        "pandrator.logic.dubbing.audio_sync.media_has_audio_stream",
                        return_value=True,
                    ),
                    mock.patch(
                        "pandrator.web.workflow_export.render_video_export",
                        side_effect=render_stub,
                    ) as render,
                ):
                    result = self.handlers.export_variant(
                        queued.payload,
                        self._progress,
                        threading.Event(),
                    )

                self.assertEqual(1, len(result["artifact_ids"]))
                self.assertEqual(edited_media.id, chosen_audio[-1][1])
                self.assertEqual(assembly.id, chosen_audio[-1][2])
                self.assertEqual(expected_worker_mode, chosen_audio[-1][0])
                render.assert_called_once()

    def test_selected_run_without_matching_assembly_does_not_fall_back_to_history(self):
        self._rendered_media()
        self._artifact(
            "historical-assembly.wav",
            role="assembled_audio",
            kind="audio",
            content=b"unrelated historical audio",
        )
        run_id = self._generation_run()
        queued = self.workflow.resolve_stage(
            self.record.id,
            "export",
            {
                "export_mode": "media",
                "audio_mode": "mixed",
                "generation_run_id": run_id,
            },
        )

        with self.assertRaisesRegex(ValueError, "Assemble the selected generation run"):
            self.handlers.export(queued.payload, self._progress, threading.Event())

        self.assertFalse((self.session_dir / "exports").exists())

    def test_worker_rejects_audio_mode_that_disagrees_with_media_edit_contract(self):
        self._rendered_media()
        run_id = self._generation_run()
        settings = {
            "export_mode": "media",
            "audio_mode": "mixed",
            "generation_run_id": run_id,
        }
        initial = self.workflow.resolve_stage(self.record.id, "export", settings)
        assembly = self._store_run_assembly(
            run_id,
            initial.payload["resolved_settings_snapshot"],
        )
        queued = self.workflow.resolve_stage(self.record.id, "export", settings)
        payload = {
            **queued.payload,
            "pinned_assembly_artifact_id": assembly.id,
            "export_contract": {
                **queued.payload["export_contract"],
                "audio_mode": "dubbing_only",
            },
        }

        with self.assertRaisesRegex(ValueError, "queued audio mode"):
            self.handlers.export(payload, self._progress, threading.Event())

        self.assertFalse((self.session_dir / "exports").exists())

    def test_audio_only_pinned_generated_export_rejects_changed_edit_revision(self):
        edited_media = self._rendered_media()
        run_id = self._generation_run()
        settings = {
            "export_mode": "audio",
            "audio_mode": "mixed",
            "generation_run_id": run_id,
        }
        initial = self.workflow.resolve_stage(self.record.id, "export", settings)
        self._store_run_assembly(run_id, initial.payload["resolved_settings_snapshot"])
        queued = self.workflow.resolve_stage(self.record.id, "export", settings)
        self._media_edit_service().update(
            self.record.id,
            self.plan["revision"],
            keep_ranges=self.plan["keep_ranges"],
            instructions="A newer edit after queueing the audio export.",
        )
        with self.database.session() as session:
            session.get(Artifact, edited_media.id).state = "current"

        with (
            mock.patch(
                "pandrator.logic.dubbing.audio_sync.media_has_audio_stream"
            ) as media_has_audio_stream,
            mock.patch("pandrator.web.workflow_export.render_video_export") as render,
        ):
            with self.assertRaisesRegex(ValueError, "media-edit revision changed"):
                self.handlers.export_variant(
                    queued.payload,
                    self._progress,
                    threading.Event(),
                )

        media_has_audio_stream.assert_not_called()
        render.assert_not_called()

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

        with (
            mock.patch(
                "pandrator.logic.dubbing.audio_sync.media_has_audio_stream"
            ) as media_has_audio_stream,
            mock.patch.object(self.handlers, "_resolve_input") as resolve_input,
            mock.patch.object(self.handlers.artifacts, "register") as register,
        ):
            with self.assertRaisesRegex(ValueError, "selected media edit changed"):
                self.handlers.export(payload, self._progress, mock.sentinel.cancel)

        media_has_audio_stream.assert_not_called()
        resolve_input.assert_not_called()
        register.assert_not_called()

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

        with (
            mock.patch(
                "pandrator.logic.dubbing.audio_sync.media_has_audio_stream"
            ) as media_has_audio_stream,
            mock.patch.object(self.handlers, "_resolve_input") as resolve_input,
            mock.patch.object(self.handlers.artifacts, "register") as register,
        ):
            with self.assertRaisesRegex(ValueError, "media-edit revision changed"):
                self.handlers.export(
                    queued.payload,
                    self._progress,
                    mock.sentinel.cancel,
                )

        media_has_audio_stream.assert_not_called()
        resolve_input.assert_not_called()
        register.assert_not_called()

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

    def test_continuation_accepts_materialized_subtitles_with_document_revision_id(self):
        edited_subtitles = self._artifact(
            "materialized-edited.srt",
            role="media_edit_subtitles",
            kind="srt",
            content=b"1\n00:00:00,000 --> 00:00:01,000\nHello\n",
            metadata={
                "media_edit_revision_id": self.plan["revision_id"],
                "revision_id": "document-revision-id",
                "plan_id": self.plan["plan_id"],
                "revision": self.plan["revision"],
                "content_hash": self.plan["content_hash"],
            },
        )
        corrected_sources = []

        def correct(payload, _progress, _cancel_event):
            corrected_sources.append(payload["source_artifact_id"])
            return {"artifact_id": "fixture-correction"}

        with mock.patch.object(
            self.handlers,
            "handler_registry",
            {
                "dubbing.transcribe": lambda *_args: {},
                "dubbing.correct": correct,
            },
        ):
            result = self.handlers.continue_workflow(
                {"session_id": self.record.id, "target_stage": "correct"},
                self._progress,
                threading.Event(),
            )

        self.assertEqual([edited_subtitles.id], corrected_sources)
        self.assertEqual("correct", result["target_stage"])

    def test_active_media_edit_subtitle_revision_metadata_compatibility(self):
        cases = (
            (
                "dedicated revision id takes precedence",
                "media_edit_subtitles",
                {
                    "media_edit_revision_id": self.plan["revision_id"],
                    "revision_id": "document-revision-id",
                    "content_hash": self.plan["content_hash"],
                },
                True,
            ),
            (
                "legacy generic revision id",
                "media_edit_subtitles",
                self._edit_metadata(),
                True,
            ),
            (
                "legacy plan and revision metadata",
                "media_edit_subtitles",
                {
                    "revision_id": "document-revision-id",
                    "plan_id": self.plan["plan_id"],
                    "revision": self.plan["revision"],
                    "content_hash": self.plan["content_hash"],
                },
                True,
            ),
            (
                "wrong dedicated revision id does not fall back",
                "media_edit_subtitles",
                {
                    "media_edit_revision_id": "another-edit-revision",
                    "revision_id": self.plan["revision_id"],
                    "plan_id": self.plan["plan_id"],
                    "revision": self.plan["revision"],
                    "content_hash": self.plan["content_hash"],
                },
                False,
            ),
            (
                "stale content hash",
                "media_edit_subtitles",
                {
                    "media_edit_revision_id": self.plan["revision_id"],
                    "content_hash": "stale-content-hash",
                },
                False,
            ),
            (
                "stale plan id",
                "media_edit_subtitles",
                {
                    "revision_id": "document-revision-id",
                    "plan_id": "another-plan",
                    "revision": self.plan["revision"],
                    "content_hash": self.plan["content_hash"],
                },
                False,
            ),
            (
                "unrelated artifact role",
                "transcription",
                self._edit_metadata(),
                False,
            ),
        )

        for index, (label, role, metadata, expected) in enumerate(cases):
            with self.subTest(label=label):
                artifact = self._artifact(
                    f"revision-case-{index}.srt",
                    role=role,
                    kind="srt",
                    content=b"1\n00:00:00,000 --> 00:00:01,000\nHello\n",
                    metadata=metadata,
                )

                self.assertEqual(
                    expected,
                    self.handlers._matches_active_media_edit_revision(
                        self.record.id,
                        artifact,
                    ),
                )


if __name__ == "__main__":
    unittest.main()
