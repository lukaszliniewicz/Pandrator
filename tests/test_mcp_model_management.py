from __future__ import annotations

import copy
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from pandrator_manager.application import create_application
from pandrator_manager.components.audiocpp import SUPPORTED_MODEL_IDS
from pandrator_manager.models import (
    ComponentInspection,
    ComponentState,
    ComputeVariant,
    DesiredComponentState,
    OperationKind,
)
from pandrator_manager.planning import Planner, PlannerValidationError
from pandrator_mcp.schemas.manager import (
    ManagerDesiredComponentInput,
    PlanComponentChangeInput,
)
from pandrator_mcp.tools.manager import manager_plan_projection, manager_status


class ManagerModelSelectionTests(unittest.TestCase):
    def test_schema_allows_large_audio_cpp_model_lists_only(self):
        model_ids = [f"model_{index}" for index in range(33)]
        selection = ManagerDesiredComponentInput(
            component_id="audio_cpp",
            options={"models": model_ids},
        )
        self.assertEqual(model_ids, selection.options["models"])

        invalid_options = (
            {"models": []},
            {"models": ["model", "model"]},
            {"models": ["../model"]},
            {"models": "model"},
            {"add_models": ["model"], "models": ["other"]},
            {"custom": ["model"]},
        )
        for options in invalid_options:
            with self.subTest(options=options), self.assertRaises(ValidationError):
                ManagerDesiredComponentInput(
                    component_id="audio_cpp",
                    options=options,
                )

        with self.assertRaises(ValidationError):
            ManagerDesiredComponentInput(
                component_id="silero",
                options={"models": ["model"]},
            )

    def test_planner_accepts_full_registry_and_additive_unions_over_32(self):
        supported = list(SUPPORTED_MODEL_IDS)
        self.assertGreater(len(supported), 35)

        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            planner = application.planner
            self.assertEqual(
                supported,
                ManagerDesiredComponentInput(
                    component_id="audio_cpp",
                    options={"models": supported},
                ).options["models"],
            )
            self.assertEqual(
                supported,
                planner._validated_model_ids(
                    supported,
                    option="models",
                    supported=set(supported),
                ),
            )

            installed = supported[:31]
            added = supported[31:33]
            with patch.object(
                planner,
                "inspect",
                return_value=ComponentInspection(
                    component_id="audio_cpp",
                    state=ComponentState.PRESENT,
                    installed_model_ids=tuple(installed),
                ),
            ):
                normalized = planner._normalize_audio_cpp_model_options(
                    {
                        "audio_cpp": DesiredComponentState(
                            options={"add_models": added}
                        )
                    }
                )
            self.assertEqual(
                [*installed, *added],
                normalized["audio_cpp"].options["models"],
            )

            installed_over_limit = supported[:33]
            persisted = DesiredComponentState(
                options={"models": supported[30:65]}
            )
            with patch.object(
                planner,
                "inspect",
                return_value=ComponentInspection(
                    component_id="audio_cpp",
                    state=ComponentState.PRESENT,
                    installed_model_ids=tuple(installed_over_limit),
                ),
            ):
                normalized = planner._normalize_audio_cpp_model_options(
                    {
                        "audio_cpp": DesiredComponentState(
                            options={"add_models": supported[64:67]}
                        )
                    },
                    persisted_desired={"audio_cpp": persisted},
                )
            self.assertEqual(
                supported[:67],
                normalized["audio_cpp"].options["models"],
            )

    def test_planner_rejects_empty_duplicate_invalid_and_unsupported_ids(self):
        supported = set(SUPPORTED_MODEL_IDS)
        invalid_selections = (
            [],
            ["../model"],
            ["voxcpm2_q8_0", "voxcpm2_q8_0"],
            ["unsupported-model"],
        )
        for selection in invalid_selections:
            with self.subTest(selection=selection), self.assertRaises(
                PlannerValidationError
            ):
                Planner._validated_model_ids(
                    selection,
                    option="models",
                    supported=supported,
                )

    def test_plan_canonicalizes_additive_models_before_digest_and_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            desired = {
                "audio_cpp": DesiredComponentState(
                    compute=ComputeVariant.CPU,
                    options={"add_models": ["breeze_tts_2_q8_0"]},
                )
            }
            persisted = {
                "audio_cpp": DesiredComponentState(
                    compute=ComputeVariant.CPU,
                    options={"models": ["voxcpm2_q8_0"]},
                )
            }
            plan = application.planner.create_plan(
                kind=OperationKind.INSTALL,
                desired=desired,
                expected_revision=0,
                actual_revision=0,
                persisted_desired=persisted,
            )

        state = plan.desired["audio_cpp"]
        self.assertEqual(
            ["voxcpm2_q8_0", "breeze_tts_2_q8_0"],
            state.options["models"],
        )
        self.assertNotIn("add_models", state.options)
        self.assertNotIn("models", desired["audio_cpp"].options)
        stage = next(task for task in plan.tasks if task.kind == "stage_audio_cpp")
        projection = manager_plan_projection(
            plan.model_dump(mode="json"),
            target={},
        )
        projected_component = next(
            item
            for item in projection["components"]
            if item["component_id"] == "audio_cpp"
        )
        self.assertEqual(
            state.options["models"], projected_component["options"]["models"]
        )
        projected_stage = next(
            item for item in projection["tasks"] if item["id"] == stage.id
        )
        self.assertEqual(
            stage.inputs["model_changes"], projected_stage["model_changes"]
        )
        self.assertEqual(
            stage.estimated_download_bytes,
            projected_stage["estimated_download_bytes"],
        )
        self.assertNotIn("inputs", projected_stage)

    def test_additive_plan_inherits_persisted_options_and_honors_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            desired_state = DesiredComponentState(
                compute=ComputeVariant.CPU,
                options={
                    "add_models": ["breeze_tts_2_q8_0"],
                    "overlaid_option": {"source": ["request"]},
                },
            )
            persisted_state = DesiredComponentState(
                compute=ComputeVariant.CPU,
                options={
                    "models": ["voxcpm2_q8_0", "kokoro_82m_q8_0"],
                    "kokoro_japanese": "enabled",
                    "persisted_option": {"nested": ["retain"]},
                    "overlaid_option": {"source": ["persisted"]},
                },
            )
            desired_before = copy.deepcopy(desired_state)
            persisted_before = copy.deepcopy(persisted_state)

            inherited_plan = application.planner.create_plan(
                kind=OperationKind.INSTALL,
                desired={"audio_cpp": desired_state},
                expected_revision=0,
                actual_revision=0,
                persisted_desired={"audio_cpp": persisted_state},
            )
            inherited = inherited_plan.desired["audio_cpp"].options
            inherited_stage = next(
                task for task in inherited_plan.tasks if task.kind == "stage_audio_cpp"
            )

            self.assertEqual(
                ["voxcpm2_q8_0", "kokoro_82m_q8_0", "breeze_tts_2_q8_0"],
                inherited["models"],
            )
            self.assertNotIn("add_models", inherited)
            self.assertEqual("enabled", inherited["kokoro_japanese"])
            self.assertTrue(inherited_stage.inputs["kokoro_japanese"])
            self.assertEqual({"nested": ["retain"]}, inherited["persisted_option"])
            self.assertEqual({"source": ["request"]}, inherited["overlaid_option"])
            self.assertIsNot(
                inherited["persisted_option"],
                persisted_state.options["persisted_option"],
            )
            self.assertIsNot(
                inherited["persisted_option"]["nested"],
                persisted_state.options["persisted_option"]["nested"],
            )
            self.assertIsNot(
                inherited["overlaid_option"],
                desired_state.options["overlaid_option"],
            )

            disabled_state = DesiredComponentState(
                compute=ComputeVariant.CPU,
                options={
                    "add_models": ["breeze_tts_2_q8_0"],
                    "kokoro_japanese": "disabled",
                },
            )
            disabled_before = copy.deepcopy(disabled_state)
            disabled_plan = application.planner.create_plan(
                kind=OperationKind.INSTALL,
                desired={"audio_cpp": disabled_state},
                expected_revision=0,
                actual_revision=0,
                persisted_desired={"audio_cpp": persisted_state},
            )
            disabled_stage = next(
                task for task in disabled_plan.tasks if task.kind == "stage_audio_cpp"
            )
            self.assertEqual(
                "disabled", disabled_plan.desired["audio_cpp"].options["kokoro_japanese"]
            )
            self.assertFalse(disabled_stage.inputs["kokoro_japanese"])
            self.assertNotIn("add_models", disabled_plan.desired["audio_cpp"].options)

            self.assertEqual(desired_before, desired_state)
            self.assertEqual(persisted_before, persisted_state)
            self.assertEqual(disabled_before, disabled_state)

    def test_exact_models_replacement_and_fresh_defaults_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            exact_plan = application.planner.create_plan(
                kind=OperationKind.INSTALL,
                desired={
                    "audio_cpp": DesiredComponentState(
                        compute=ComputeVariant.CPU,
                        options={"models": ["breeze_tts_2_q8_0"]},
                    )
                },
                expected_revision=0,
                actual_revision=0,
                persisted_desired={
                    "audio_cpp": DesiredComponentState(
                        compute=ComputeVariant.CPU,
                        options={
                            "models": ["kokoro_82m_q8_0"],
                            "kokoro_japanese": "enabled",
                        },
                    )
                },
            )
            exact_stage = next(
                task for task in exact_plan.tasks if task.kind == "stage_audio_cpp"
            )
            self.assertEqual(
                ["breeze_tts_2_q8_0"],
                exact_plan.desired["audio_cpp"].options["models"],
            )
            self.assertFalse(exact_stage.inputs["kokoro_japanese"])

            fresh_plan = application.planner.create_plan(
                kind=OperationKind.INSTALL,
                desired={
                    "audio_cpp": DesiredComponentState(
                        compute=ComputeVariant.CPU,
                        options={"models": ["kokoro_82m_q8_0"]},
                    )
                },
                expected_revision=0,
                actual_revision=0,
            )
            fresh_stage = next(
                task for task in fresh_plan.tasks if task.kind == "stage_audio_cpp"
            )
            self.assertNotIn(
                "kokoro_japanese", fresh_plan.desired["audio_cpp"].options
            )
            self.assertFalse(fresh_stage.inputs["kokoro_japanese"])

    def test_retry_payload_and_exact_replacement_remain_distinct(self):
        arguments = PlanComponentChangeInput(
            kind="update",
            components=(
                ManagerDesiredComponentInput(
                    component_id="audio_cpp",
                    options={"add_models": ["voxcpm2_q8_0"]},
                ),
            ),
            expected_revision=4,
            idempotency_key="manager-model-retry",
        )
        self.assertEqual(
            arguments.model_dump(mode="json"),
            PlanComponentChangeInput.model_validate(
                arguments.model_dump(mode="json")
            ).model_dump(mode="json"),
        )

        exact = ManagerDesiredComponentInput(
            component_id="audio_cpp",
            options={"models": ["voxcpm2_q8_0"]},
        )
        self.assertNotIn("add_models", exact.options)

    def test_additive_planning_fails_closed_on_unknown_installed_model(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            inspection = ComponentInspection(
                component_id="audio_cpp",
                state=ComponentState.ABSENT,
                installed_model_ids=("unknown-model",),
            )
            with patch.object(
                application.planner,
                "inspect",
                return_value=inspection,
            ), self.assertRaisesRegex(
                ValueError,
                "installed.*unsupported",
            ):
                application.planner.create_plan(
                    kind=OperationKind.INSTALL,
                    desired={
                        "audio_cpp": DesiredComponentState(
                            options={
                                "add_models": ["voxcpm2_q8_0"],
                            }
                        )
                    },
                    expected_revision=0,
                    actual_revision=0,
                )

    def test_additive_planning_retains_verified_installed_models(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            inspection = ComponentInspection(
                component_id="audio_cpp",
                state=ComponentState.PRESENT,
                installed_model_ids=("voxcpm2_q8_0",),
            )
            with patch.object(
                application.planner,
                "inspect",
                return_value=inspection,
            ):
                plan = application.planner.create_plan(
                    kind=OperationKind.UPDATE,
                    desired={
                        "audio_cpp": DesiredComponentState(
                            compute=ComputeVariant.CPU,
                            options={
                                "add_models": ["breeze_tts_2_q8_0"],
                            },
                        )
                    },
                    expected_revision=0,
                    actual_revision=0,
                )
        self.assertEqual(
            ["voxcpm2_q8_0", "breeze_tts_2_q8_0"],
            plan.desired["audio_cpp"].options["models"],
        )
        stage = next(task for task in plan.tasks if task.kind == "stage_audio_cpp")
        self.assertEqual(
            {
                "add": ["breeze_tts_2_q8_0"],
                "remove": [],
                "retain": ["voxcpm2_q8_0"],
            },
            stage.inputs["model_changes"],
        )

    def test_status_inventory_is_bounded_and_metadata_only(self):
        model_ids = [f"model_{index}" for index in range(40)]
        models = [
            {
                "id": model_id,
                "label": f"Model {index}",
                "license_name": "MIT",
                "license_url": "https://example.test/license",
                "estimated_download_bytes": 10,
            }
            for index, model_id in enumerate(model_ids)
        ]

        class Manager:
            @staticmethod
            def status():
                return {"ready": True, "configuration_revision": 2}

            @staticmethod
            def components():
                return {"items": [
                    {
                        "definition": {
                            "id": "audio_cpp",
                            "models": models,
                        },
                        "inspection": {
                            "installed_model_ids": model_ids,
                        },
                        "desired": {
                            "options": {"models": model_ids},
                        },
                    }
                ]}

        status = manager_status(type("Runtime", (), {"manager": Manager()})())
        inventory = status["model_inventory"]
        self.assertEqual(model_ids, inventory[0]["installed_model_ids"])
        self.assertEqual(model_ids, inventory[0]["desired_model_ids"])
        self.assertEqual(model_ids, [model["id"] for model in inventory[0]["known_models"]])
        self.assertEqual(40, inventory[0]["known_model_count"])
        self.assertFalse(inventory[0]["known_models_truncated"])
        self.assertEqual(
            10,
            inventory[0]["known_models"][0]["estimated_download_bytes"],
        )
        self.assertNotIn("definition", inventory[0])

    def test_application_plan_preserves_persisted_desired_models(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            desired = DesiredComponentState(compute=ComputeVariant.CPU, options={"models": ["voxcpm2_q8_0"]})
            inspection = application.planner.inspect("audio_cpp", desired)
            application.store.save_component(inspection, desired=desired)
            plan = application.plan(kind=OperationKind.INSTALL, desired={
                "audio_cpp": DesiredComponentState(compute=ComputeVariant.CPU, options={"add_models": ["breeze_tts_2_q8_0"]}),
            })
            self.assertEqual(["voxcpm2_q8_0", "breeze_tts_2_q8_0"], plan.desired["audio_cpp"].options["models"])
            saved = application.store.get_plan(plan.id)
            self.assertEqual(plan.digest, saved.digest)
            self.assertNotIn("add_models", saved.desired["audio_cpp"].options)


if __name__ == "__main__":
    unittest.main()
