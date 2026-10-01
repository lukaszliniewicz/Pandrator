"""Portable MCP input schemas and application delegation."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from pydantic import ValidationError

from pandrator_mcp.schemas.workflow_inputs import (
    GetWorkflowInputsInput,
    SelectWorkflowInputInput,
)
from pandrator_mcp.tools.workflow_inputs import (
    get_workflow_inputs,
    select_workflow_input,
)


class WorkflowInputsMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application = Mock()
        self.runtime = SimpleNamespace(require_application=lambda: self.application)

    def test_get_handler_calls_application_and_returns_compact_manifest(self) -> None:
        manifest = {
            "session_id": "session-1",
            "outcome_revision": 3,
            "inputs": {"translation": "correction", "generation": "source"},
            "consumers": {},
            "translation_settings_revision": 2,
            "blocking_reasons": [],
        }
        self.application.get_workflow_inputs.return_value = manifest
        arguments = GetWorkflowInputsInput(session_id="session-1")

        result = get_workflow_inputs(self.runtime, arguments)

        self.application.get_workflow_inputs.assert_called_once_with("session-1")
        self.assertIs(manifest, result)

    def test_select_handler_forwards_exact_revision_and_idempotency_guards(self) -> None:
        arguments = SelectWorkflowInputInput(
            session_id="session-1",
            consumer="translation",
            role="correction",
            artifact_id="artifact-1",
            expected_outcome_revision=5,
            expected_selection_revision=2,
            expected_translation_settings_revision=4,
            idempotency_key="workflow-input-123",
        )
        expected = {"selected": {"artifact_id": "artifact-1"}}
        self.application.select_workflow_input.return_value = expected

        result = select_workflow_input(self.runtime, arguments)

        self.application.select_workflow_input.assert_called_once_with(
            "session-1",
            consumer="translation",
            role="correction",
            artifact_id="artifact-1",
            expected_outcome_revision=5,
            expected_selection_revision=2,
            expected_translation_settings_revision=4,
            idempotency_key="workflow-input-123",
        )
        self.assertIs(expected, result)

    def test_schema_requires_translation_revision_and_forbids_translation_loop(self) -> None:
        with self.assertRaises(ValidationError):
            SelectWorkflowInputInput(
                session_id="session-1",
                consumer="translation",
                role="source",
                artifact_id="artifact-1",
                expected_outcome_revision=1,
                expected_selection_revision=0,
                idempotency_key="workflow-input-123",
            )

        with self.assertRaises(ValidationError):
            SelectWorkflowInputInput(
                session_id="session-1",
                consumer="translation",
                role="translation",
                artifact_id="artifact-1",
                expected_outcome_revision=1,
                expected_selection_revision=0,
                expected_translation_settings_revision=0,
                idempotency_key="workflow-input-123",
            )

    def test_schema_rejects_translation_revision_for_generation(self) -> None:
        with self.assertRaises(ValidationError):
            SelectWorkflowInputInput(
                session_id="session-1",
                consumer="generation",
                role="correction",
                artifact_id="artifact-1",
                expected_outcome_revision=1,
                expected_selection_revision=0,
                expected_translation_settings_revision=0,
                idempotency_key="workflow-input-123",
            )


if __name__ == "__main__":
    unittest.main()
