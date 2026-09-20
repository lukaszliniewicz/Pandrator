from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from pandrator_manager.application import create_application
from pandrator_manager.models import (
    ComponentInspection,
    ComponentState,
    ComputeVariant,
    DesiredComponentState,
    OperationKind,
)
from pandrator_mcp.schemas.manager import (
    ManagerDesiredComponentInput,
    PlanComponentChangeInput,
)
from pandrator_mcp.tools.manager import manager_plan_projection, manager_status


class ManagerModelSelectionTests(unittest.TestCase):
    def test_schema_allows_bounded_audio_cpp_model_lists_only(self):
        selection = ManagerDesiredComponentInput(
            component_id="audio_cpp",
            options={"models": ["Qwen.Base", "voxcpm2_q8_0"]},
        )
        self.assertEqual(
            ["Qwen.Base", "voxcpm2_q8_0"], selection.options["models"]
        )

        invalid_options = (
            {"models": []},
            {"models": ["model"] * 33},
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
                            "models": [
                                {
                                    "id": "model_a",
                                    "label": "Model A",
                                    "license_name": "MIT",
                                    "license_url": "https://example.test/license",
                                    "estimated_download_bytes": 10,
                                }
                            ],
                        },
                        "inspection": {
                            "installed_model_ids": ["model_a"],
                        },
                        "desired": {
                            "options": {"models": ["model_a"]},
                        },
                    }
                ]}

        status = manager_status(type("Runtime", (), {"manager": Manager()})())
        inventory = status["model_inventory"]
        self.assertEqual(["model_a"], inventory[0]["installed_model_ids"])
        self.assertEqual(["model_a"], inventory[0]["desired_model_ids"])
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
