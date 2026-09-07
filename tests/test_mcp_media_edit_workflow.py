import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.schemas import (
    AttachExistingSourceInput,
    BrowseLocalSourcesInput,
    CreateMediaEditDispatchRunInput,
    CreateSessionInput,
    CreateTextSourceInput,
    DownloadArtifactInput,
    GetMediaEditArguments,
    GetMediaEditDispatchRunInput,
    GetWorkInput,
    ImportLocalSourceInput,
    ListArtifactsInput,
    PlanMediaEditWorkflowInput,
    PlanWorkflowInput,
    PrepareMediaEditArguments,
    RenderMediaEditArguments,
    UpdateMediaEditArguments,
    UpdateSessionInput,
    UpdateSessionSettingsInput,
)
from pandrator_mcp.tools.media_edit_workflow import plan_media_edit_workflow


def _ready_settings(**override):
    defaults = {
        "caption_alignment_method": "ctc",
        "caption_alignment_ctc_model": "auto",
        "caption_alignment_padding_ms": 2_000,
        "caption_alignment_batch_seconds": 30,
        "caption_alignment_min_confidence": 0.5,
        "caption_alignment_fallback_coverage": 0.9,
    }
    return {"effective": {**defaults, **override}, "override": {}, "revision": 3}


def _alignment_metadata(**override):
    options = {
        "caption_alignment_method": "ctc",
        "caption_alignment_ctc_model": "auto",
        "caption_alignment_padding_ms": 2_000,
        "caption_alignment_batch_seconds": 30,
        "caption_alignment_min_confidence": 0.5,
        "caption_alignment_fallback_coverage": 0.9,
    }
    return {
        "alignment_method": "ctc_cue_alignment",
        "authoritative_transcript_artifact_id": "caption-1",
        "source_artifact_id": "video-1",
        "vad_enabled": True,
        "vad_options": {
            "crispasr_vad_model": "silero",
            "crispasr_vad_threshold": 0.5,
            "crispasr_vad_min_speech_ms": 250,
            "crispasr_vad_min_silence_ms": 800,
            "crispasr_vad_max_speech_seconds": 300,
            "crispasr_vad_speech_pad_ms": 30,
        },
        "ctc_options": options,
        **override,
    }


def _state(*, plan=True, transcript=True, timing=True, reviewed=False):
    readiness = {
        "source_media_artifact": {"id": "video-1"},
        "external_transcript_artifact": {"id": "caption-1"} if transcript else None,
        "transcription_artifact": {"id": "transcript-1"} if transcript else None,
        "timing_artifact": {"id": "timing-1"} if timing else None,
    }
    active = (
        {
            "plan_id": "plan-1",
            "revision_id": "revision-1",
            "revision": 1,
            "source_media_artifact": {"id": "video-1"},
            "editorial_transcript_artifact": {"id": "caption-1"},
            "timing_artifact": {"id": "timing-1"},
            "instructions": "Trim the setup chatter.",
            "keep_ranges": [{"id": "keep-1", "start_ms": 0, "end_ms": 1_000}],
            "evidence": {},
            "operation": {"type": "prepare"},
            "reviewed": reviewed,
        }
        if plan
        else None
    )
    return {"readiness": readiness, "plan": active}


class _Application:
    def __init__(
        self,
        *,
        media=None,
        workflow=None,
        settings=None,
        runs=None,
        artifacts=None,
        work=None,
    ):
        self.media = media or _state()
        self.workflow = workflow or {
            "revision": 4,
            "stages": [
                {
                    "key": "transcribe",
                    "status": "complete",
                    "artifact": {
                        "id": "transcript-1",
                        "metadata_json": _alignment_metadata(),
                    },
                },
                {"key": "edit_media", "status": "ready"},
            ],
        }
        self.settings = settings or _ready_settings()
        self.runs = list(runs or [])
        self.artifacts = list(artifacts or [])
        self.work = list(work or [])
        self.calls = []

    def get_session(self, session_id):
        self.calls.append(("session", session_id))
        return {
            "id": session_id,
            "name": "Media edit",
            "workflow_kind": "media_edit",
            "status": "ready",
            "revision": 4,
        }

    def get_workflow(self, session_id):
        self.calls.append(("workflow", session_id))
        return self.workflow

    def get_media_edit(self, session_id):
        self.calls.append(("media_edit", session_id))
        return self.media

    def get_session_settings(self, session_id, section):
        self.calls.append(("settings", session_id, section))
        return self.settings

    def list_media_edit_dispatch_runs(self, session_id, *, limit=50):
        self.calls.append(("runs", session_id, limit))
        return {"items": self.runs}

    def list_artifacts(self, *, session_id=None, limit=50):
        self.calls.append(("artifacts", session_id, limit))
        return {"items": self.artifacts}

    def list_work(self, *, session_id=None, kinds=(), states=(), limit=50):
        self.calls.append(("work", session_id, kinds, states, limit))
        return {"items": self.work}


def _plan(application, **kwargs):
    runtime = SimpleNamespace(require_application=lambda: application)
    outcome = plan_media_edit_workflow(
        runtime,
        PlanMediaEditWorkflowInput(
            session_id="session-1",
            instructions="Trim the setup chatter.",
            **kwargs,
        ),
    )
    action_models = {
        "pandrator_attach_existing_source": AttachExistingSourceInput,
        "pandrator_browse_local_sources": BrowseLocalSourcesInput,
        "pandrator_create_media_edit_dispatch_run": CreateMediaEditDispatchRunInput,
        "pandrator_download_artifact": DownloadArtifactInput,
        "pandrator_get_media_edit": GetMediaEditArguments,
        "pandrator_get_media_edit_dispatch_run": GetMediaEditDispatchRunInput,
        "pandrator_get_work": GetWorkInput,
        "pandrator_import_local_source": ImportLocalSourceInput,
        "pandrator_list_artifacts": ListArtifactsInput,
        "pandrator_plan_workflow": PlanWorkflowInput,
        "pandrator_prepare_media_edit": PrepareMediaEditArguments,
        "pandrator_render_media_edit": RenderMediaEditArguments,
        "pandrator_update_media_edit": UpdateMediaEditArguments,
        "pandrator_update_session_settings": UpdateSessionSettingsInput,
    }
    actions = [outcome.result.get("next_action")]
    actions.extend(
        phase.get("approval_action") for phase in outcome.result.get("phases", [])
    )
    for action in actions:
        if not action:
            continue
        action_models[action["tool"]].model_validate(action["arguments"])
    return outcome


class MediaEditWorkflowSchemaTests(unittest.TestCase):
    def test_source_roles_and_edit_media_stage_are_strict(self):
        for model, values in (
            (
                ImportLocalSourceInput,
                {
                    "root": "media",
                    "relative_path": "video.mp4",
                },
            ),
            (
                CreateTextSourceInput,
                {"text": "caption text"},
            ),
            (
                AttachExistingSourceInput,
                {"source_asset_id": "asset-1"},
            ),
        ):
            common = {
                "session_id": "session-1",
                "expected_session_revision": 4,
                "idempotency_key": "source-role-key",
                **values,
            }
            self.assertEqual("transcript", model(role="transcript", **common).role)
            with self.assertRaises(ValidationError):
                model(role="unknown", **common)
        self.assertEqual(
            ("edit_media",),
            CreateSessionInput(
                name="Media edit",
                workflow_kind="media_edit",
                included_stages=("edit_media",),
                idempotency_key="session-key",
            ).included_stages,
        )
        self.assertEqual(
            ("edit_media",),
            UpdateSessionInput(
                session_id="session-1",
                expected_revision=4,
                included_stages=("edit_media",),
                idempotency_key="session-update-key",
            ).included_stages,
        )
        with self.assertRaises(ValidationError):
            CreateSessionInput(
                name="Media edit",
                included_stages=("unknown",),
                idempotency_key="session-key",
            )

    def test_planner_schema_rejects_unsafe_sources_and_overrides(self):
        valid = {
            "session_id": "session-1",
            "instructions": "Trim",
        }
        for source in (
            {
                "source_asset_id": "asset-1",
                "root": "media",
                "relative_path": "video.mp4",
            },
            {"root": "media"},
            {"root": "media", "relative_path": "../video.mp4"},
        ):
            with self.assertRaises(ValidationError):
                PlanMediaEditWorkflowInput(**valid, recording_source=source)
        for override in (
            {"api_key": "secret"},
            {"model_path": "/tmp/model"},
            {"endpoint": "x"},
        ):
            with self.assertRaises(ValidationError):
                PlanMediaEditWorkflowInput(**valid, stt_overrides=override)
        with self.assertRaises(ValidationError):
            PlanMediaEditWorkflowInput(
                **valid,
                caption_alignment_ctc_model="/tmp/aligner.onnx",
            )
        with self.assertRaises(ValidationError):
            PlanMediaEditWorkflowInput(
                **valid,
                caption_alignment_ctc_model="unknown-aligner",
            )
        with self.assertRaises(ValidationError):
            PlanMediaEditWorkflowInput(
                **valid,
                transcript_mode="asr",
                transcript_source={"source_asset_id": "caption-asset"},
            )
        with self.assertRaises(ValidationError):
            PlanMediaEditWorkflowInput(**valid, filename="output.mp4")
        self.assertEqual(
            "output.mp4",
            PlanMediaEditWorkflowInput(
                **valid,
                materialize=True,
                filename=" output.mp4 ",
            ).filename,
        )


class MediaEditWorkflowPlannerTests(unittest.TestCase):
    def test_missing_primary_video_recommends_browse_or_exact_import(self):
        application = _Application(media={"readiness": {}, "plan": None})
        result = _plan(application)
        self.assertEqual(
            "pandrator_browse_local_sources", result.result["next_action"]["tool"]
        )
        imported = _plan(
            application,
            recording_source={"root": "media", "relative_path": "video.mp4"},
        )
        action = imported.result["next_action"]
        self.assertEqual("pandrator_import_local_source", action["tool"])
        self.assertEqual("primary", action["arguments"]["role"])
        self.assertEqual(4, action["arguments"]["expected_session_revision"])

    def test_caption_transcript_source_and_settings_merge(self):
        media = {
            "readiness": {"source_media_artifact": {"id": "video-1"}},
            "plan": None,
        }
        application = _Application(media=media)
        result = _plan(
            application,
            transcript_mode="captions",
            transcript_source={"source_asset_id": "caption-asset"},
        )
        action = result.result["next_action"]
        self.assertEqual("pandrator_attach_existing_source", action["tool"])
        self.assertEqual("transcript", action["arguments"]["role"])

        media = _state()
        settings = {
            "effective": {"caption_alignment_method": "ctc", "keep": "effective"},
            "override": {"keep": "effective", "unrelated": "preserve"},
            "revision": 7,
        }
        application = _Application(media=media, settings=settings)
        result = _plan(application, stt_overrides={"stt_engine": "moss"})
        # auto + attached captions uses caption settings; unrelated override keys survive.
        action = result.result["next_action"]
        self.assertEqual("pandrator_update_session_settings", action["tool"])
        self.assertEqual(7, action["arguments"]["expected_revision"])
        self.assertEqual("preserve", action["arguments"]["value"]["unrelated"])
        self.assertEqual("moss", action["arguments"]["value"]["stt_engine"])

    def test_ctc_and_asr_execution_needed_return_native_transcribe_plan(self):
        media = _state(timing=False)
        application = _Application(media=media)
        result = _plan(application)
        self.assertEqual(
            "pandrator_plan_workflow", result.result["next_action"]["tool"]
        )
        self.assertEqual(
            "transcribe", result.result["next_action"]["arguments"]["target_stage"]
        )

        media = _state(transcript=False, timing=False)
        application = _Application(
            media=media, settings={"effective": {}, "override": {}, "revision": 0}
        )
        result = _plan(application, transcript_mode="asr")
        self.assertEqual(
            "pandrator_plan_workflow", result.result["next_action"]["tool"]
        )
        self.assertEqual(
            {}, result.result["next_action"]["arguments"]["overrides"]["stt"]
        )

        former_caption_media = {
            "readiness": {
                "source_media_artifact": {"id": "video-1"},
                "external_transcript_artifact": None,
                "transcription_artifact": {"id": "transcript-1"},
                "timing_artifact": {"id": "timing-1"},
            },
            "plan": None,
        }
        result = _plan(
            _Application(media=former_caption_media),
            transcript_mode="asr",
        )
        self.assertEqual(
            "pandrator_plan_workflow", result.result["next_action"]["tool"]
        )

        for status in ("stale", "failed"):
            with self.subTest(status=status):
                retry_workflow = _Application().workflow
                retry_workflow["stages"][0]["status"] = status
                result = _plan(_Application(workflow=retry_workflow))
                self.assertEqual(
                    "pandrator_plan_workflow", result.result["next_action"]["tool"]
                )

    def test_ctc_asr_fallback_artifact_matches_by_its_recorded_options(self):
        workflow = _Application().workflow
        workflow["stages"][0]["artifact"]["metadata_json"] = _alignment_metadata(
            alignment_method="ctc_with_asr_fallback",
            fallback_triggered=True,
            ctc_options={
                **_alignment_metadata()["ctc_options"],
                "caption_alignment_method": "ctc_asr_fallback",
            },
        )
        settings = _ready_settings(caption_alignment_method="ctc_asr_fallback")
        result = _plan(
            _Application(workflow=workflow, settings=settings),
            caption_alignment_method="ctc_asr_fallback",
        )
        self.assertEqual(
            "pandrator_create_media_edit_dispatch_run",
            result.result["next_action"]["tool"],
        )

    def test_retained_vad_settings_are_part_of_alignment_identity(self):
        settings = _ready_settings(crispasr_vad_threshold=0.35)
        settings["override"] = {"crispasr_vad_threshold": 0.35}
        result = _plan(_Application(settings=settings))
        self.assertEqual(
            "pandrator_plan_workflow",
            result.result["next_action"]["tool"],
        )

        workflow = _Application().workflow
        metadata = workflow["stages"][0]["artifact"]["metadata_json"]
        metadata["vad_options"]["crispasr_vad_threshold"] = 0.35
        result = _plan(_Application(workflow=workflow, settings=settings))
        self.assertEqual(
            "pandrator_create_media_edit_dispatch_run",
            result.result["next_action"]["tool"],
        )

    def test_prepare_active_dispatch_and_create_dispatch(self):
        stale = _state()
        stale["plan"]["timing_artifact"] = {"id": "old-timing"}
        application = _Application(media=stale)
        result = _plan(application)
        self.assertEqual(
            "pandrator_prepare_media_edit", result.result["next_action"]["tool"]
        )
        self.assertTrue(result.result["next_action"]["arguments"]["force"])

        unprepared = _plan(_Application(media=_state(plan=False)))
        self.assertEqual(
            "pandrator_prepare_media_edit", unprepared.result["next_action"]["tool"]
        )
        self.assertFalse(unprepared.result["next_action"]["arguments"]["force"])

        active = _Application(
            runs=[
                {
                    "id": "run-1",
                    "status": "running",
                    "source_revision_id": "revision-1",
                    "instructions": "Trim the setup chatter.",
                }
            ]
        )
        result = _plan(active)
        self.assertEqual(
            "pandrator_get_media_edit_dispatch_run",
            result.result["next_action"]["tool"],
        )

        created = _plan(_Application())
        self.assertEqual(
            "pandrator_create_media_edit_dispatch_run",
            created.result["next_action"]["tool"],
        )

    def test_review_gate_render_and_materialization(self):
        completed = {
            "id": "run-1",
            "status": "completed",
            "source_revision_id": "revision-0",
            "result_revision_id": "revision-1",
            "instructions": "Trim the setup chatter.",
        }
        proposed = _state()
        proposed["plan"]["evidence"] = {
            "passive_dispatch": {"dispatch_run_id": "run-1"}
        }
        proposed["plan"]["operation"] = {
            "type": "passive_dispatch",
            "dispatch_run_id": "run-1",
        }
        application = _Application(media=proposed, runs=[completed])
        result = _plan(application)
        self.assertEqual(
            "pandrator_get_media_edit", result.result["next_action"]["tool"]
        )
        self.assertEqual(
            "pandrator_update_media_edit",
            result.result["phases"][5]["approval_action"]["tool"],
        )

        reviewed = _state(reviewed=True)
        reviewed["plan"]["evidence"] = {
            "passive_dispatch": {"dispatch_run_id": "run-1"}
        }
        reviewed["plan"]["operation"] = {"type": "manual_update"}
        workflow = {
            "revision": 4,
            "stages": [
                {
                    "key": "transcribe",
                    "status": "complete",
                    "artifact": {
                        "id": "transcript-1",
                        "metadata_json": _alignment_metadata(),
                    },
                },
                {"key": "edit_media", "status": "ready"},
            ],
        }
        application = _Application(media=reviewed, workflow=workflow, runs=[completed])
        result = _plan(application, wait_seconds=12)
        self.assertEqual(
            "pandrator_render_media_edit", result.result["next_action"]["tool"]
        )
        self.assertTrue(result.result["next_action"]["arguments"]["wait"])
        self.assertEqual(
            12, result.result["next_action"]["arguments"]["timeout_seconds"]
        )

        workflow["stages"][1]["status"] = "complete"
        application = _Application(
            media=reviewed,
            workflow=workflow,
            runs=[completed],
            artifacts=[
                {"id": "media-1", "role": "media_edit_media", "state": "current"}
            ],
        )
        result = _plan(application, materialize=True, filename="final.mp4")
        self.assertEqual(
            "pandrator_download_artifact", result.result["next_action"]["tool"]
        )
        self.assertEqual(
            "media-1", result.result["next_action"]["arguments"]["artifact_id"]
        )
        self.assertEqual(
            "final.mp4", result.result["next_action"]["arguments"]["filename"]
        )

        already_reviewed = _plan(_Application(media=_state(reviewed=True)))
        self.assertEqual(
            "pandrator_render_media_edit",
            already_reviewed.result["next_action"]["tool"],
        )

    def test_alignment_provenance_and_active_render_are_reinspected(self):
        workflow = _Application().workflow
        workflow["stages"][0]["artifact"]["metadata_json"][
            "authoritative_transcript_artifact_id"
        ] = "old-caption"
        result = _plan(_Application(workflow=workflow))
        self.assertEqual(
            "pandrator_plan_workflow", result.result["next_action"]["tool"]
        )

        workflow = _Application().workflow
        result = _plan(
            _Application(workflow=workflow),
            stt_overrides={"crispasr_vad_threshold": 0.35},
        )
        self.assertEqual(
            "pandrator_update_session_settings", result.result["next_action"]["tool"]
        )
        application = _Application(
            workflow=workflow,
            settings=_ready_settings(crispasr_vad_threshold=0.35),
        )
        result = _plan(
            application,
            stt_overrides={"crispasr_vad_threshold": 0.35},
        )
        self.assertEqual(
            "pandrator_plan_workflow", result.result["next_action"]["tool"]
        )

        completed = {
            "id": "run-1",
            "status": "completed",
            "source_revision_id": "revision-0",
            "result_revision_id": "revision-1",
            "instructions": "Trim the setup chatter.",
        }
        reviewed = _state(reviewed=True)
        reviewed["plan"]["evidence"] = {
            "passive_dispatch": {"dispatch_run_id": "run-1"}
        }
        result = _plan(
            _Application(
                media=reviewed,
                runs=[completed],
                work=[{"id": "job-render", "state": "running"}],
            ),
            wait_seconds=9,
        )
        self.assertEqual("pandrator_get_work", result.result["next_action"]["tool"])
        self.assertEqual(
            "job-render", result.result["next_action"]["arguments"]["work_id"]
        )

    def test_wrong_kind_attached_caption_block_and_idempotency(self):
        application = _Application()
        application.get_session = lambda _session_id: {"workflow_kind": "subtitles"}
        with self.assertRaises(PandratorMcpError):
            _plan(application)

        blocked = _plan(_Application(), transcript_mode="asr")
        self.assertIsNone(blocked.result["next_action"])
        self.assertIn("authoritative", blocked.result["blocking_reason"])

        first = _plan(_Application()).result["next_action"]["arguments"][
            "idempotency_key"
        ]
        second = _plan(_Application()).result["next_action"]["arguments"][
            "idempotency_key"
        ]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
