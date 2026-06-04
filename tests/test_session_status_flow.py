import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pandrator.app_logic import AppLogic, DEFAULT_SESSION_ACTIVITY_SNAPSHOT
from pandrator.gui.widgets.session_tab import SessionTab


class _DummySignal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class _ActivityHarness:
    def __init__(self):
        self._state_lock = threading.RLock()
        self._session_activity_snapshot = copy.deepcopy(DEFAULT_SESSION_ACTIVITY_SNAPSHOT)
        self.session_activity_updated = _DummySignal()
        self._normalize_session_activity_tone = AppLogic._normalize_session_activity_tone


class _DubbingStepsHarness:
    def __init__(self, session_name="Demo Session", run=None):
        self.state = SimpleNamespace(session_name=session_name)
        self._run = run
        self._active_dubbing_run_id = str((run or {}).get("run_id") or "")

    def _is_named_session_active(self):
        return bool(self.state.session_name and self.state.session_name != "Untitled Session")

    def _synchronize_active_dubbing_run(self):
        return self._run


class _LifecycleHarness:
    def __init__(self):
        self.generation_running = False
        self.preprocessing_running = False
        self.regeneration_running = False
        self.rvc_running = False
        self.cancel_generation_flag = threading.Event()
        self.stop_generation_flag = threading.Event()

    def _is_generation_running(self):
        return self.generation_running

    def is_text_preprocessing_running(self):
        return self.preprocessing_running

    def _is_regeneration_running(self):
        return self.regeneration_running

    def _is_rvc_processing_running(self):
        return self.rvc_running


class _TaskStatusPanelRecorder:
    def __init__(self):
        self.activity_calls = []
        self.stage_calls = []

    def set_activity(self, headline, detail, tone):
        self.activity_calls.append((headline, detail, tone))

    def set_dubbing_stage_states(self, stage_states, visible):
        self.stage_calls.append((stage_states, visible))


class _LabelRecorder:
    def __init__(self):
        self.text = None

    def setText(self, text):
        self.text = text


class SessionStatusFlowTests(unittest.TestCase):
    def test_set_session_activity_updates_snapshot_and_dedupes_repeats(self):
        harness = _ActivityHarness()

        AppLogic._set_session_activity(harness, "Generating audio", "Sentence 1 of 10.", "active")

        self.assertEqual(1, len(harness.session_activity_updated.calls))
        emitted_payload = harness.session_activity_updated.calls[0][0]
        self.assertEqual("Generating audio", emitted_payload["headline"])
        self.assertEqual("active", emitted_payload["tone"])

        emitted_payload["headline"] = "Mutated"
        snapshot = AppLogic.get_session_activity_snapshot(harness)
        self.assertEqual("Generating audio", snapshot["headline"])

        AppLogic._set_session_activity(harness, "Generating audio", "Sentence 1 of 10.", "active")
        self.assertEqual(1, len(harness.session_activity_updated.calls))

        AppLogic._set_session_activity(harness, "Generation complete", "", "success")
        self.assertEqual(2, len(harness.session_activity_updated.calls))
        self.assertEqual("success", AppLogic.get_session_activity_snapshot(harness)["tone"])

    def test_get_active_dubbing_step_states_returns_active_run_mapping(self):
        harness = _DubbingStepsHarness(run={"run_id": "run-123"})

        with patch("pandrator.app_logic.state_db_handler.get_dubbing_steps") as get_steps:
            get_steps.return_value = [
                {"step_key": "transcribe", "status": "running"},
                {"step_key": "translate", "status": "completed"},
                {"step_key": "", "status": "failed"},
            ]
            result = AppLogic.get_active_dubbing_step_states(harness)

        self.assertEqual(
            {
                "transcribe": "running",
                "translate": "completed",
            },
            result,
        )

    def test_get_active_dubbing_step_states_returns_empty_without_named_session_or_run(self):
        unnamed_session_harness = _DubbingStepsHarness(session_name="Untitled Session", run={"run_id": "run-1"})
        no_run_harness = _DubbingStepsHarness(run=None)

        self.assertEqual({}, AppLogic.get_active_dubbing_step_states(unnamed_session_harness))
        self.assertEqual({}, AppLogic.get_active_dubbing_step_states(no_run_harness))

    def test_get_lifecycle_status_prioritizes_generation_related_states(self):
        harness = _LifecycleHarness()

        scenarios = [
            (
                {
                    "generation_running": True,
                    "preprocessing_running": True,
                    "regeneration_running": True,
                    "rvc_running": True,
                },
                {},
                "Processing Text",
            ),
            (
                {
                    "generation_running": True,
                },
                {"stop": True},
                "Stopping",
            ),
            (
                {
                    "generation_running": True,
                    "preprocessing_running": True,
                },
                {"cancel": True, "stop": True},
                "Cancelling",
            ),
            (
                {
                    "preprocessing_running": True,
                    "regeneration_running": True,
                    "rvc_running": True,
                },
                {},
                "Processing Text",
            ),
            (
                {
                    "regeneration_running": True,
                    "rvc_running": True,
                },
                {},
                "Regenerating",
            ),
            (
                {
                    "rvc_running": True,
                },
                {},
                "RVC Processing",
            ),
            (
                {},
                {},
                "Idle",
            ),
        ]

        for flags, events, expected in scenarios:
            with self.subTest(expected=expected):
                harness.generation_running = flags.get("generation_running", False)
                harness.preprocessing_running = flags.get("preprocessing_running", False)
                harness.regeneration_running = flags.get("regeneration_running", False)
                harness.rvc_running = flags.get("rvc_running", False)
                harness.cancel_generation_flag.clear()
                harness.stop_generation_flag.clear()
                if events.get("cancel"):
                    harness.cancel_generation_flag.set()
                if events.get("stop"):
                    harness.stop_generation_flag.set()

                self.assertEqual(expected, AppLogic.get_lifecycle_status(harness))

    def test_session_tab_apply_session_activity_uses_payload_or_snapshot(self):
        panel = _TaskStatusPanelRecorder()
        logic = SimpleNamespace(
            get_session_activity_snapshot=lambda: {
                "headline": "Snapshot headline",
                "detail": "Snapshot detail",
                "tone": "warning",
            }
        )
        harness = SimpleNamespace(logic=logic, task_status_panel=panel)

        SessionTab._apply_session_activity(
            harness,
            {"headline": "Payload headline", "detail": "Payload detail", "tone": "active"},
        )
        SessionTab._apply_session_activity(harness, None)

        self.assertEqual(
            [
                ("Payload headline", "Payload detail", "active"),
                ("Snapshot headline", "Snapshot detail", "warning"),
            ],
            panel.activity_calls,
        )

    def test_session_tab_update_task_status_panel_routes_stage_visibility(self):
        panel = _TaskStatusPanelRecorder()

        active_stage_calls = []
        active_logic = SimpleNamespace(
            is_dubbing_mode_active=lambda: True,
            get_active_dubbing_step_states=lambda: active_stage_calls.append("called") or {
                "transcribe": "running",
                "render": "completed",
            },
        )
        active_harness = SimpleNamespace(logic=active_logic, task_status_panel=panel)
        SessionTab._update_task_status_panel(active_harness)

        self.assertEqual(["called"], active_stage_calls)
        self.assertEqual(
            [({"transcribe": "running", "render": "completed"}, True)],
            panel.stage_calls,
        )

        panel.stage_calls.clear()
        inactive_stage_calls = []
        inactive_logic = SimpleNamespace(
            is_dubbing_mode_active=lambda: False,
            get_active_dubbing_step_states=lambda: inactive_stage_calls.append("called") or {
                "transcribe": "running",
            },
        )
        inactive_harness = SimpleNamespace(logic=inactive_logic, task_status_panel=panel)
        SessionTab._update_task_status_panel(inactive_harness)

        self.assertEqual([], inactive_stage_calls)
        self.assertEqual([({}, False)], panel.stage_calls)

    def test_session_tab_update_lifecycle_indicator_maps_user_facing_labels(self):
        cases = {
            "Idle": "Idle",
            "Processing Text": "Processing...",
            "Generating": "Generating",
            "Regenerating": "Regenerating",
            "RVC Processing": "RVC Processing",
            "Stopping": "Stopping",
            "Cancelling": "Cancelling",
            "Custom State": "Custom State",
        }

        for lifecycle_status, expected_label in cases.items():
            with self.subTest(lifecycle_status=lifecycle_status):
                label = _LabelRecorder()
                logic = SimpleNamespace(get_lifecycle_status=lambda status=lifecycle_status: status)
                harness = SimpleNamespace(logic=logic, lifecycle_status_label=label)

                SessionTab._update_lifecycle_indicator(harness)

                self.assertEqual(expected_label, label.text)


if __name__ == "__main__":
    unittest.main()
