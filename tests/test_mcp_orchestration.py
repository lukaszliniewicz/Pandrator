import unittest
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import Mock, patch

from pydantic import ValidationError

from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.schemas import (
    CreateDispatchRunInput,
    CreateSpeechOptimizationDispatchRunInput,
    DescribeParametersInput,
    GetWorkInput,
    PlanOrchestratedWorkflowInput,
)
from pandrator_mcp.tools.inventory import describe_parameters
from pandrator_mcp.tools.work import get_work
from pandrator_mcp.tools.workflow import plan_orchestrated_workflow


class _Application:
    def __init__(self, work_states=None):
        self.calls = []
        self.work_states = list(work_states or [])
        self.settings = {
            "correction": {"effective": {}},
            "translation": {"effective": {}},
            "text": {"effective": {}},
            "tts": {"effective": {}},
        }

    def get_session(self, session_id):
        self.calls.append(("session", session_id))
        return {
            "id": session_id,
            "name": "Course",
            "workflow_kind": "voiceover",
            "revision": 4,
            "status": "ready",
            "source_language": "en",
            "target_language": "pl",
            "credential": "must not leak",
        }

    def get_workflow(self, session_id):
        self.calls.append(("workflow", session_id))
        return {
            "revision": 4,
            "stages": [
                {"key": "translate", "status": "ready", "secret": "private"},
                {"key": "generate_audio", "status": "unavailable"},
            ],
        }

    def get_session_settings(self, session_id, section):
        self.calls.append(("settings", session_id, section))
        return self.settings.get(section, {"effective": {}})

    def get_work(self, work_id):
        self.calls.append(("work", work_id))
        if self.work_states:
            state = self.work_states.pop(0)
        else:
            state = "running"
        return {"id": work_id, "state": state, "poll_after_ms": 12_000}

    def get_work_events(self, work_id, *, limit):
        self.calls.append(("events", work_id, limit))
        return {"items": []}


class _Manager:
    def __init__(self, states):
        self.states = list(states)
        self.calls = []

    def operation(self, operation_id):
        self.calls.append(("operation", operation_id))
        state = self.states.pop(0) if self.states else "running"
        return {"id": operation_id, "kind": "install", "state": state}

    def operation_tasks(self, operation_id):
        self.calls.append(("tasks", operation_id))
        return {"items": []}


class OrchestrationSchemaTests(unittest.TestCase):
    def test_planner_guards_and_canonicalizes_inputs(self):
        configured = PlanOrchestratedWorkflowInput(
            session_id="session-1",
            goal="Produce the final course.",
            passive_stages=("speech_optimization", "translation", "correction"),
            materialize=True,
            filename="course.mp4",
        )
        self.assertEqual(
            ("correction", "translation", "speech_optimization"),
            configured.passive_stages,
        )
        for kwargs in (
            {"goal": "  "},
            {"passive_stages": ("translation", "translation")},
            {"overrides": {"tts": {"api_key": "secret"}}},
            {"filename": "../course.mp4", "materialize": True},
            {"filename": "course.mp4"},
        ):
            with self.assertRaises(ValidationError):
                PlanOrchestratedWorkflowInput(session_id="session-1", **kwargs)
        with self.assertRaises(ValidationError):
            PlanOrchestratedWorkflowInput(
                session_id="session-1",
                goal="Audio only",
                final_stage="generate_audio",
                materialize=True,
            )
        with self.assertRaises(ValidationError):
            PlanOrchestratedWorkflowInput(
                session_id="session-1",
                goal="Audio only",
                final_stage="generate_audio",
                filename="audio.mp3",
                materialize=True,
            )

    def test_parameter_discovery_requires_a_filter(self):
        with self.assertRaises(ValidationError):
            DescribeParametersInput()
        with self.assertRaises(ValidationError):
            DescribeParametersInput(names=("  ",))
        filtered = DescribeParametersInput(sections=("tts",), names=("voice",))
        self.assertEqual(("tts",), filtered.sections)


class OrchestrationToolTests(unittest.TestCase):
    def test_planner_is_live_read_only_and_merges_typed_export_options(self):
        application = _Application()
        runtime = SimpleNamespace(require_application=lambda: application)
        arguments = PlanOrchestratedWorkflowInput(
            session_id="session-1",
            goal="Produce the final course.",
            passive_stages=("translation", "correction"),
            overrides={"output": {"custom": "kept"}},
            subtitle_mode="burned",
            audio_mode="dubbing_only",
            materialize=True,
            filename="course.mp4",
            wait_seconds=19,
        )
        outcome = plan_orchestrated_workflow(runtime, arguments)
        self.assertEqual(
            ["correction", "translation", "export", "materialize"],
            outcome.result["phase_order"],
        )
        self.assertEqual("pandrator_create_dispatch_run", outcome.next_actions[0].tool)
        self.assertEqual("correction", outcome.next_actions[0].arguments["kind"])
        final = outcome.result["phases"][2]
        self.assertEqual("pandrator_plan_workflow", final["tool"])
        self.assertEqual("export", final["final_stage"])
        self.assertEqual(
            {
                "custom": "kept",
                "export_mode": "media",
                "audio_mode": "dubbing_only",
                "subtitle_mode": "burned",
                "subtitle_selection": "translation",
                "subtitle_format": "srt",
            },
            final["arguments"]["overrides"]["output"],
        )
        self.assertNotIn(("plan", "session-1"), application.calls)
        self.assertIn(
            "not an immutable execution snapshot", outcome.result["immutability"]
        )

    def test_delivery_controls_are_not_native_output_settings(self):
        application = _Application()
        runtime = SimpleNamespace(require_application=lambda: application)
        outcome = plan_orchestrated_workflow(
            runtime,
            PlanOrchestratedWorkflowInput(
                session_id="session-1",
                goal="Export the course",
                materialize=True,
                filename="course.mp4",
            ),
        )
        final = outcome.result["phases"][0]
        self.assertNotIn("materialize", final["arguments"]["overrides"]["output"])
        self.assertNotIn("filename", final["arguments"]["overrides"]["output"])
        delivery = outcome.result["phases"][1]
        self.assertEqual("course.mp4", delivery["download_arguments"]["filename"])

        no_filename = plan_orchestrated_workflow(
            runtime,
            PlanOrchestratedWorkflowInput(
                session_id="session-1",
                goal="Export the course",
                materialize=True,
            ),
        )
        self.assertNotIn(
            "filename", no_filename.result["phases"][1]["download_arguments"]
        )

    def test_passive_packets_inherit_effective_settings_and_explicit_overrides(self):
        application = _Application()
        application.settings.update(
            {
                "correction": {
                    "effective": {
                        "instructions": "persisted correction",
                        "char_limit": 4_000,
                        "max_segments_per_batch": 12,
                        "context_before": 5,
                    }
                },
                "translation": {
                    "effective": {
                        "source_language": "de",
                        "target_language": "fr",
                        "instructions": "persisted translation",
                        "glossary_enabled": True,
                        "glossary": {"Hello": "Bonjour"},
                    }
                },
                "text": {
                    "effective": {
                        "combined_prompt": "persisted prompt",
                        "llm_tts_document_batch_size": 0,
                    }
                },
                "tts": {"effective": {"language": "pl", "service": "Kokoro"}},
            }
        )
        runtime = SimpleNamespace(require_application=lambda: application)
        outcome = plan_orchestrated_workflow(
            runtime,
            PlanOrchestratedWorkflowInput(
                session_id="session-1",
                goal="Prepare passive outputs",
                passive_stages=("speech_optimization", "translation", "correction"),
                overrides={
                    "correction": {"char_limit": 5_000, "source_artifact_id": "a1"},
                    "translation": {
                        "instructions": "explicit translation",
                        "glossary": [{"term": "World", "translation": "Monde"}],
                    },
                    "text": {"combined_prompt": "explicit speech", "ignored": True},
                    "tts": {"language": "en", "service": "XTTS"},
                },
            ),
        )
        phases = {phase["stage"]: phase for phase in outcome.result["phases"]}
        correction = phases["correction"]["create_arguments"]
        self.assertEqual(5_000, correction["char_limit"])
        self.assertEqual("a1", correction["source_artifact_id"])
        self.assertIsNone(correction["target_language"])
        self.assertEqual({}, correction["glossary"])
        translation = phases["translation"]["create_arguments"]
        self.assertEqual("de", translation["source_language"])
        self.assertEqual("fr", translation["target_language"])
        self.assertEqual({"World": "Monde"}, translation["glossary"])
        speech = phases["speech_optimization"]["create_arguments"]
        self.assertEqual("explicit speech", speech["instructions"])
        self.assertEqual("en", speech["voice_language"])
        self.assertEqual("XTTS", speech["tts_service"])
        self.assertEqual(1, speech["max_units_per_batch"])
        self.assertIsNone(speech["language"])
        self.assertNotIn("ignored", speech)
        self.assertTrue(set(correction) <= set(CreateDispatchRunInput.model_fields))
        self.assertTrue(
            set(speech) <= set(CreateSpeechOptimizationDispatchRunInput.model_fields)
        )

    def test_parallel_delegation_and_shared_capsule_flow_into_every_passive_stage(self):
        application = _Application()
        runtime = SimpleNamespace(require_application=lambda: application)
        outcome = plan_orchestrated_workflow(
            runtime,
            PlanOrchestratedWorkflowInput(
                session_id="session-1",
                goal="Keep names and style consistent",
                passive_stages=("translation", "speech_optimization"),
                execution_mode="parallel",
                max_parallel_batches=3,
                context_capsule={
                    "overview": "A course narrated by Alice.",
                    "entities": {"Alice": "narrator"},
                    "style_rules": ["Keep headings concise."],
                },
            ),
        )

        for phase in outcome.result["phases"][:2]:
            arguments = phase["create_arguments"]
            self.assertEqual("parallel", arguments["execution_mode"])
            self.assertEqual(3, arguments["max_parallel_batches"])
            self.assertEqual(
                "narrator", arguments["context_capsule"]["entities"]["Alice"]
            )

    def test_planner_rejects_workflow_incompatible_passive_and_final_stages(self):
        application = _Application()
        runtime = SimpleNamespace(require_application=lambda: application)
        original = application.get_session

        application.get_session = lambda session_id: {
            **original(session_id),
            "workflow_kind": "audiobook",
        }
        with self.assertRaises(PandratorMcpError):
            plan_orchestrated_workflow(
                runtime,
                PlanOrchestratedWorkflowInput(
                    session_id="session-1",
                    goal="Invalid subtitle translation",
                    passive_stages=("translation",),
                ),
            )

        application.get_session = lambda session_id: {
            **original(session_id),
            "workflow_kind": "subtitles",
        }
        with self.assertRaises(PandratorMcpError):
            plan_orchestrated_workflow(
                runtime,
                PlanOrchestratedWorkflowInput(
                    session_id="session-1",
                    goal="Invalid audio generation",
                    final_stage="generate_audio",
                ),
            )

    def test_translation_glossary_safe_normalization_and_gate(self):
        application = _Application()
        application.settings["translation"] = {
            "effective": {
                "glossary_enabled": False,
                "glossary": {"Persisted": "No"},
            }
        }
        runtime = SimpleNamespace(require_application=lambda: application)
        for value, expected in (
            ({" A ": " B "}, {"A": "B"}),
            ([{"source": "C", "value": "D"}], {"C": "D"}),
            ('{"E": "F"}', {"E": "F"}),
            ("G=H\ninvalid", {"G": "H"}),
        ):
            application.settings["translation"]["effective"]["glossary_enabled"] = False
            outcome = plan_orchestrated_workflow(
                runtime,
                PlanOrchestratedWorkflowInput(
                    session_id="session-1",
                    goal="Glossary test",
                    passive_stages=("translation",),
                    overrides={"translation": {"glossary": value}},
                ),
            )
            self.assertEqual(
                expected, outcome.result["phases"][0]["create_arguments"]["glossary"]
            )
        application.settings["translation"]["effective"]["glossary_enabled"] = True
        application.settings["translation"]["effective"]["glossary"] = {
            "Persisted": "Yes"
        }
        outcome = plan_orchestrated_workflow(
            runtime,
            PlanOrchestratedWorkflowInput(
                session_id="session-1",
                goal="Glossary test",
                passive_stages=("translation",),
            ),
        )
        self.assertEqual(
            {"Persisted": "Yes"},
            outcome.result["phases"][0]["create_arguments"]["glossary"],
        )

    def test_retry_identity_is_stable_and_tracks_live_inputs(self):
        application = _Application()
        runtime = SimpleNamespace(require_application=lambda: application)

        def make(goal="Retry", override=None):
            return plan_orchestrated_workflow(
                runtime,
                PlanOrchestratedWorkflowInput(
                    session_id="session-1",
                    goal=goal,
                    passive_stages=("correction",),
                    overrides={"correction": override or {}},
                ),
            ).result["phases"][0]["create_arguments"]["idempotency_key"]

        first = make()
        self.assertEqual(first, make())
        self.assertNotEqual(first, make(goal="Changed"))
        self.assertNotEqual(first, make(override={"char_limit": 1234}))
        application.settings["correction"]["effective"]["char_limit"] = 1235
        self.assertNotEqual(first, make())
        application.get_workflow = lambda session_id: {
            "revision": 5,
            "stages": [],
        }
        self.assertNotEqual(first, make())

    def test_stage_projection_excludes_secret_and_path_metadata(self):
        application = _Application()
        application.get_workflow = lambda session_id: {
            "revision": 4,
            "stages": [
                {
                    "key": "translate",
                    "status": "ready",
                    "included": True,
                    "job_id": "job-1",
                    "selected_artifact_id": "art-1",
                    "selection_revision": 2,
                    "provider_api_key": "secret",
                    "path": "/private/file",
                    "artifact": {
                        "id": "art-1",
                        "role": "translation",
                        "state": "current",
                        "content_hash": "hash",
                        "path": "/private/file",
                        "metadata": {"token": "secret"},
                    },
                }
            ],
        }
        runtime = SimpleNamespace(require_application=lambda: application)
        result = plan_orchestrated_workflow(
            runtime,
            PlanOrchestratedWorkflowInput(
                session_id="session-1",
                goal="Safe projection",
            ),
        ).result["current_stage_statuses"][0]
        self.assertEqual(
            {
                "stage": "translate",
                "key": "translate",
                "status": "ready",
                "included": True,
                "job_id": "job-1",
                "selected_artifact_id": "art-1",
                "selection_revision": 2,
                "artifact": {
                    "id": "art-1",
                    "role": "translation",
                    "state": "current",
                    "content_hash": "hash",
                },
            },
            result,
        )

    def test_describe_parameters_passes_only_typed_filters(self):
        application = SimpleNamespace(
            describe_parameters=lambda **kwargs: {"schema_version": "1", **kwargs}
        )
        runtime = SimpleNamespace(require_application=lambda: application)
        result = describe_parameters(
            runtime,
            DescribeParametersInput(
                sections=("tts",),
                names=("voice",),
                workflow_kind="voiceover",
                query="speech",
                limit=7,
            ),
        )
        self.assertEqual(("tts",), result["sections"])
        self.assertEqual("voiceover", result["workflow_kind"])
        self.assertEqual(7, result["limit"])

    def test_application_parameter_client_repeats_section_and_name_filters(self):
        client = object.__new__(ApplicationClient)
        request = Mock(return_value={"schema_version": "1", "items": []})
        client._request_json = request
        client.describe_parameters(
            sections=("tts", "audio"),
            names=("voice", "speed"),
            workflow_kind="voiceover",
            query="speech",
            limit=9,
        )
        request.assert_called_once_with(
            "/api/v1/parameter-definitions",
            parameters=[
                ("section", "tts"),
                ("section", "audio"),
                ("name", "voice"),
                ("name", "speed"),
                ("workflow_kind", "voiceover"),
                ("query", "speech"),
                ("limit", 9),
            ],
        )


class WaitForWorkTests(unittest.TestCase):
    def test_immediate_terminal_wait_has_no_sleep_or_next_action(self):
        class Clock:
            def monotonic(self):
                return 12.0

            def sleep(self, seconds):
                raise AssertionError("terminal work must not sleep")

        application = _Application(["succeeded"])
        runtime = SimpleNamespace(require_application=lambda: application)
        with (
            patch(
                "pandrator_mcp.tools.work.time.monotonic",
                side_effect=Clock().monotonic,
            ),
            patch("pandrator_mcp.tools.work.time.sleep", side_effect=Clock().sleep),
        ):
            outcome = get_work(
                runtime,
                GetWorkInput(work_id="job-1", wait_seconds=5),
            )
        self.assertEqual(0, outcome.result["wait"]["poll_count"])
        self.assertFalse(outcome.result["wait"]["timed_out"])
        self.assertFalse(outcome.next_actions)

    def test_transition_timeout_clamping_and_events(self):
        class Clock:
            now = 0.0
            sleeps: ClassVar[list[float]] = []

            def __init__(self):
                self.now = 0.0
                self.sleeps = []

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                self.sleeps.append(seconds)
                self.now += seconds

        clock = Clock()
        application = _Application(["running", "succeeded"])
        runtime = SimpleNamespace(require_application=lambda: application)
        with (
            patch(
                "pandrator_mcp.tools.work.time.monotonic", side_effect=clock.monotonic
            ),
            patch("pandrator_mcp.tools.work.time.sleep", side_effect=clock.sleep),
        ):
            completed = get_work(
                runtime,
                GetWorkInput(work_id="job-1", wait_seconds=5, include_events=True),
            )
        self.assertEqual(1, completed.result["wait"]["poll_count"])
        self.assertFalse(completed.result["wait"]["timed_out"])
        self.assertEqual([5.0], clock.sleeps)
        self.assertEqual(
            1, len([call for call in application.calls if call[0] == "events"])
        )

        clock = Clock()
        application = _Application(["running", "running", "running"])
        runtime = SimpleNamespace(require_application=lambda: application)
        with (
            patch(
                "pandrator_mcp.tools.work.time.monotonic", side_effect=clock.monotonic
            ),
            patch("pandrator_mcp.tools.work.time.sleep", side_effect=clock.sleep),
        ):
            timed_out = get_work(runtime, GetWorkInput(work_id="job-2", wait_seconds=1))
        self.assertTrue(timed_out.result["wait"]["timed_out"])
        self.assertTrue(all(0.25 <= value <= 1.0 for value in clock.sleeps))
        self.assertEqual("pandrator_get_work", timed_out.next_actions[0].tool)

    def test_manager_wait_uses_manager_projection_and_terminal_has_no_next_action(self):
        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                self.now += seconds

        clock = Clock()
        manager = _Manager(["running", "succeeded"])
        runtime = SimpleNamespace(manager=manager)
        with (
            patch(
                "pandrator_mcp.tools.work.time.monotonic", side_effect=clock.monotonic
            ),
            patch("pandrator_mcp.tools.work.time.sleep", side_effect=clock.sleep),
        ):
            outcome = get_work(
                runtime,
                GetWorkInput(
                    work_type="manager_operation",
                    work_id="operation-1",
                    include_events=True,
                    wait_seconds=2,
                ),
            )
        self.assertEqual("succeeded", outcome.work.state)
        self.assertFalse(outcome.next_actions)
        self.assertEqual(1, len([call for call in manager.calls if call[0] == "tasks"]))


if __name__ == "__main__":
    unittest.main()
