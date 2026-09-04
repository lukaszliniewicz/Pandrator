"""Shared contract tests for serial/parallel delegated context handoff."""

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from pandrator.web.dispatch_context import (
    context_capsule_for_wave,
    execution_policy,
    merge_context_capsule,
    store_context_delta,
    wave_bounds,
)
from pandrator.web.schemas import (
    DispatchContextCapsule,
    DispatchContextDelta,
    DispatchRunCreateRequest,
    SpeechOptimizationDispatchRunCreateRequest,
)


def test_merge_is_ordinal_not_completion_order_and_deduplicates_lists():
    capsule = merge_context_capsule(
        {
            "overview": "A historical novel.",
            "terminology": {"prior": "kept"},
            "style_rules": ["Keep titles formal."],
        },
        [
            (
                2,
                {
                    "terminology": {"shared": "later ordinal"},
                    "notes": ["second", "shared"],
                },
            ),
            (
                1,
                {
                    "terminology": {"shared": "earlier ordinal"},
                    "notes": ["first", "shared"],
                },
            ),
        ],
    )

    assert capsule["overview"] == "A historical novel."
    assert capsule["terminology"] == {
        "prior": "kept",
        "shared": "later ordinal",
    }
    assert capsule["notes"] == ["first", "shared", "second"]


def test_wave_capsule_excludes_current_wave_deltas():
    settings = {
        "execution_mode": "parallel",
        "max_parallel_batches": 3,
        "context_capsule": {"overview": "Shared"},
    }
    settings = store_context_delta(
        settings,
        ordinal=0,
        delta={"entities": {"Alice": "narrator"}},
    )
    settings = store_context_delta(
        settings,
        ordinal=3,
        delta={"entities": {"Bob": "not visible yet"}},
    )

    capsule = context_capsule_for_wave(settings, wave_start=3)

    assert capsule["entities"] == {"Alice": "narrator"}


@pytest.mark.parametrize(
    ("settings", "expected_policy", "expected_bounds"),
    [
        ({}, ("serial", 1), (4, 4, 5)),
        (
            {"execution_mode": "parallel", "max_parallel_batches": 3},
            ("parallel", 3),
            (1, 3, 6),
        ),
    ],
)
def test_execution_policy_and_wave_bounds(settings, expected_policy, expected_bounds):
    assert execution_policy(settings) == expected_policy
    assert wave_bounds(4, settings) == expected_bounds


@pytest.mark.parametrize(
    "schema",
    [DispatchRunCreateRequest, SpeechOptimizationDispatchRunCreateRequest],
)
def test_create_contract_requires_a_width_matching_the_execution_mode(schema):
    base = {"kind": "correction"} if schema is DispatchRunCreateRequest else {}
    with pytest.raises(ValidationError):
        schema.model_validate(
            {**base, "execution_mode": "serial", "max_parallel_batches": 2}
        )
    with pytest.raises(ValidationError):
        schema.model_validate(
            {**base, "execution_mode": "parallel", "max_parallel_batches": 1}
        )
    accepted = schema.model_validate(
        {**base, "execution_mode": "parallel", "max_parallel_batches": 3}
    )
    assert accepted.max_parallel_batches == 3


@pytest.mark.parametrize(
    "schema",
    [DispatchRunCreateRequest, SpeechOptimizationDispatchRunCreateRequest],
)
def test_create_json_schema_advertises_only_valid_execution_policies(schema):
    contract = schema.model_json_schema()
    validator = Draft202012Validator(contract)
    base = {"kind": "correction"} if schema is DispatchRunCreateRequest else {}

    assert not list(validator.iter_errors(base))
    assert not list(
        validator.iter_errors(
            {**base, "execution_mode": "parallel", "max_parallel_batches": 3}
        )
    )
    assert list(
        validator.iter_errors(
            {**base, "execution_mode": "serial", "max_parallel_batches": 2}
        )
    )
    assert list(
        validator.iter_errors(
            {**base, "execution_mode": "parallel", "max_parallel_batches": 1}
        )
    )


def test_context_contract_is_strict_and_bounded():
    with pytest.raises(ValidationError):
        DispatchContextCapsule.model_validate({"secret_blob": "not a supported field"})
    with pytest.raises(ValidationError):
        DispatchContextDelta.model_validate({"notes": ["x" * 2_001]})
    with pytest.raises(ValidationError):
        DispatchContextCapsule.model_validate(
            {"terminology": {f"term-{index}": "x" * 300 for index in range(500)}}
        )


def test_merge_rejects_individually_valid_deltas_that_overfill_the_capsule():
    initial = DispatchContextCapsule.model_validate(
        {"notes": [f"initial-{index}-" + "x" * 1_800 for index in range(62)]}
    ).model_dump(mode="json")
    delta = DispatchContextDelta.model_validate(
        {"notes": [f"delta-{index}-" + "y" * 1_800 for index in range(12)]}
    ).model_dump(mode="json")

    with pytest.raises(ValueError, match="Merged context capsule exceeds"):
        merge_context_capsule(initial, [(0, delta)])
