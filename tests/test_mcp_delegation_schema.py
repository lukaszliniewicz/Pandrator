from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from pandrator_mcp.schemas.delegation import execution_policy_json_schema
from pandrator_mcp.schemas.dispatch import CreateDispatchRunInput
from pandrator_mcp.schemas.speech_optimization_dispatch import (
    CreateSpeechOptimizationDispatchRunInput,
)
from pandrator_mcp.schemas.workflow import PlanOrchestratedWorkflowInput

MIXIN_USERS = (
    CreateDispatchRunInput,
    CreateSpeechOptimizationDispatchRunInput,
    PlanOrchestratedWorkflowInput,
)


def _valid_arguments(model, *, execution_mode: str, max_parallel_batches: int):
    common = {
        "session_id": "session-1",
        "execution_mode": execution_mode,
        "max_parallel_batches": max_parallel_batches,
    }
    if model is CreateDispatchRunInput:
        return {
            **common,
            "kind": "translation",
            "idempotency_key": "dispatch-123",
        }
    if model is CreateSpeechOptimizationDispatchRunInput:
        return {**common, "idempotency_key": "speech-123"}
    return {**common, "goal": "Prepare and export this session."}


@pytest.mark.parametrize("model", MIXIN_USERS)
def test_execution_conditionals_keep_flat_arguments_once(model):
    schema = model.model_json_schema()
    Draft202012Validator.check_schema(schema)
    assert "oneOf" not in schema
    assert len(schema["allOf"]) == 2
    for constraint in schema["allOf"]:
        assert set(constraint["then"]["properties"]) == {"max_parallel_batches"}
    assert "session_id" in schema["properties"]
    assert "session_id" in schema["required"]


@pytest.mark.parametrize("model", MIXIN_USERS)
def test_execution_width_constraints_match_runtime_and_json_schema(model):
    schema = model.model_json_schema()
    validator = Draft202012Validator(schema)

    for execution_mode, width in (
        ("serial", 2),
        ("parallel", 1),
        ("serial", 0),
        ("parallel", 9),
    ):
        arguments = _valid_arguments(
            model,
            execution_mode=execution_mode,
            max_parallel_batches=width,
        )
        with pytest.raises(ValidationError):
            model.model_validate(arguments)
        assert not validator.is_valid(arguments)

    for execution_mode, width in (("serial", 1), ("parallel", 2), ("parallel", 8)):
        arguments = _valid_arguments(
            model,
            execution_mode=execution_mode,
            max_parallel_batches=width,
        )
        model.model_validate(arguments)
        assert validator.is_valid(arguments)


def test_schema_mutator_preserves_flat_tool_arguments_and_required_fields():
    schema = {
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "execution_mode": {"enum": ["serial", "parallel"]},
            "max_parallel_batches": {"type": "integer", "minimum": 1, "maximum": 8},
        },
        "required": ["session_id"],
        "additionalProperties": False,
    }

    execution_policy_json_schema(schema)

    assert "oneOf" not in schema
    assert schema["required"] == ["session_id"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"session_id", "execution_mode", "max_parallel_batches"}

    validator = Draft202012Validator(schema)
    assert validator.is_valid(
        {"session_id": "session-1", "execution_mode": "serial", "max_parallel_batches": 1}
    )
    assert not validator.is_valid(
        {"session_id": "session-1", "execution_mode": "serial", "max_parallel_batches": 2}
    )


@pytest.mark.parametrize("model", MIXIN_USERS)
def test_execution_defaults_still_obey_conditional_schema(model):
    arguments = _valid_arguments(model, execution_mode="serial", max_parallel_batches=1)
    del arguments["execution_mode"]
    validator = Draft202012Validator(model.model_json_schema())
    assert validator.is_valid(arguments)
    arguments["max_parallel_batches"] = 2
    assert not validator.is_valid(arguments)
    with pytest.raises(ValidationError):
        model.model_validate(arguments)
