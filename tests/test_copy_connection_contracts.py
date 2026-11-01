"""Pure destination-connection copy contracts, without database or credentials."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest

from pandrator.web.projects.copy_connections import (
    collect_requirements,
    connection_key,
    normalize_destination_connections,
    validate_connection_state,
)
from pandrator.web.projects.copy_contracts import (
    COPY_STATE_VERSION,
    ProjectCopyError,
    ProjectCopySnapshot,
)


def uid() -> str:
    return str(uuid4())


def rows(
    *, stages=(), correction=None, translation=None, text=None, names=("English",)
) -> dict[str, Any]:
    result: dict[str, Any] = {"sessions": [], "session_settings": [], "outcome_plans": []}
    for name in names:
        sid = uid()
        result["sessions"].append(
            {
                "id": sid,
                "name": name,
                "workflow_kind": "subtitles",
                "workflow_preset": "custom",
                "included_stages_json": list(stages),
            }
        )
        for section, value in (
            ("correction", correction),
            ("translation", translation),
            ("text", text),
        ):
            result["session_settings"].append(
                {"session_id": sid, "section": section, "value_json": deepcopy(value or {})}
            )
    return result


def bound(data: dict[str, Any]) -> list[dict[str, Any]]:
    bindings = collect_requirements(data, set())
    for item in bindings:
        item.update(
            provider_id=uid(),
            provider_model_id=uid(),
            selector=f"custom:{uid()}/approved-model",
            provider_revision=1,
            model_revision=2,
        )
        for target in item["targets"]:
            value = next(
                row["value_json"]
                for row in data["session_settings"]
                if (row["session_id"], row["section"]) == (target["session_id"], target["section"])
            )
            value[target["field"]] = item["selector"]
    return bindings


def state(data, bindings, version=6):
    value = {
        "schema_version": version,
        "rows": data,
        "binding_fields": {},
        "unsupported_counts": {},
    }
    if version in {6, 7, 8, 9, 10}:
        value["destination_connections"] = bindings
    return value


@pytest.mark.parametrize(
    "purpose,section,settings,selected",
    [
        (
            "correction",
            "correction",
            {"model_name": "  custom:source/model  ", "correction_model": "ignored"},
            "custom:source/model",
        ),
        ("translation", "translation", {"translate_model": " legacy "}, "legacy"),
        (
            "speech_preparation",
            "text",
            {"tts_optimization_model": " default ", "llm_model": "ignored"},
            "",
        ),
    ],
)
def test_selector_precedence_grouping_and_alias_removal(purpose, section, settings, selected):
    data = rows(**{section: settings}, names=("Polish", "English", "Polish"))
    result = collect_requirements(data, set())
    item = next(item for item in result if item["purpose"] == purpose)
    digest = hashlib.sha256(
        json.dumps(
            {"purpose": purpose, "selector": selected},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert item["id"] == digest and len(item["targets"]) == 3
    assert all(not row["value_json"] for row in data["session_settings"])
    snapshot = ProjectCopySnapshot(
        uid(), uid(), "copy", json.dumps(state(data, result)), "digest", (), (), 0
    )
    public = next(
        item
        for item in snapshot.manifest()["connection_requirements"]
        if item["purpose"] == purpose
    )
    assert public["session_count"] == 3 and public["session_names"] == ["English", "Polish"]
    assert "selector" not in public and public["selected_provider_id"] is None


def test_active_legacy_stages_and_effective_text_flags_require_defaults():
    data = rows(stages=("correct", "translate"), text={"llm_tts_optimization": True})
    result = collect_requirements(data, set())
    assert {item["purpose"] for item in result} == {
        "correction",
        "translation",
        "speech_preparation",
    }
    validate_connection_state(state(data, result), data, ready=False)
    with pytest.raises(ProjectCopyError):
        validate_connection_state(state(data, result), data)


@pytest.mark.parametrize("canonical", [False, True])
def test_unproven_text_model_name_remains_blocked_even_with_supported_binding(canonical):
    data = rows(
        text={
            "model_name": "default",
            **({"tts_optimization_model": "custom:source/model"} if canonical else {}),
        }
    )
    blockers: set[tuple[str, str]] = set()
    groups = collect_requirements(data, blockers)
    assert ("unsupported_state", "custom_settings") in blockers
    assert all(
        "model_name" not in row["value_json"]
        for row in data["session_settings"]
        if row["section"] == "text"
    )
    assert bool(groups) is canonical


@pytest.mark.parametrize(
    "mutate", ["extra", "duplicate", "uppercase", "bad_provider", "bad_model", "not_list", "limit"]
)
def test_choices_are_strict_finite_and_canonical(mutate):
    choice: dict[str, Any] = {
        "requirement_id": "a" * 64,
        "provider_id": uid(),
        "provider_model_id": uid(),
    }
    value: Any = [choice]
    if mutate == "extra":
        choice["field"] = "endpoint"
    elif mutate == "duplicate":
        value.append(dict(choice))
    elif mutate == "uppercase":
        choice["requirement_id"] = "A" * 64
    elif mutate == "bad_provider":
        choice["provider_id"] = True
    elif mutate == "bad_model":
        choice["provider_model_id"] = "model-name"
    elif mutate == "not_list":
        value = {"choices": value}
    else:
        value = [choice] * 513
    with pytest.raises(ProjectCopyError):
        normalize_destination_connections(value)
    assert normalize_destination_connections(None) == normalize_destination_connections([]) == []


@pytest.mark.parametrize(
    "mutate",
    [
        "extra",
        "foreign",
        "duplicate",
        "section",
        "field",
        "revision",
        "selector",
        "nested",
        "stt",
        "purpose",
    ],
)
def test_frozen_targets_and_exact_saved_selectors_are_validated(mutate):
    data = rows(stages=("correct",))
    bindings = bound(data)
    item = bindings[0]
    if mutate == "extra":
        item["extra"] = "untrusted"
    elif mutate == "foreign":
        item["targets"][0]["session_id"] = uid()
    elif mutate == "duplicate":
        item["targets"].append(dict(item["targets"][0]))
    elif mutate == "section":
        item["targets"][0]["section"] = "source_cleaning"
    elif mutate == "field":
        item["targets"][0]["field"] = "web_research_model_name"
    elif mutate == "revision":
        item["provider_revision"] = True
    elif mutate == "purpose":
        item["purpose"] = []
    else:
        values = next(
            row["value_json"] for row in data["session_settings"] if row["section"] == "correction"
        )
        if mutate == "selector":
            values["model_name"] = "wrong"
        elif mutate == "nested":
            values["nested"] = {"model_name": "custom:foreign/model"}
        else:
            values["stt_api_base"] = "https://private.invalid"
    with pytest.raises(ProjectCopyError):
        validate_connection_state(state(data, bindings), data)


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
def test_frozen_versions_retain_exact_binding_key_set(version):
    data = rows()
    value = state(data, [], version)
    assert validate_connection_state(value, data) == []
    changed = deepcopy(value)
    if version in {6, 7, 8, 9, 10}:
        changed.pop("destination_connections")
    else:
        changed["destination_connections"] = []
    with pytest.raises(ProjectCopyError):
        validate_connection_state(changed, data)
    data["session_settings"][0]["value_json"]["model_name"] = "custom:private/model"
    with pytest.raises(ProjectCopyError):
        validate_connection_state(value, data)
    assert COPY_STATE_VERSION == 10


def test_ready_state_cannot_drop_active_requirement_or_binding():
    data = rows(stages=("correct",))
    bindings = bound(data)
    validate_connection_state(state(data, bindings), data)
    with pytest.raises(ProjectCopyError):
        validate_connection_state(state(data, []), data)
    data["session_settings"][0]["value_json"].clear()
    with pytest.raises(ProjectCopyError):
        validate_connection_state(state(data, []), data)
    assert connection_key("stt_api_base") and connection_key("some_API_BASE")
    assert not connection_key("web_research_model_name")


@pytest.mark.parametrize("custom", [False, True], ids=["builtin-at", "explicit-custom-at"])
def test_bound_at_selector_requires_explicit_custom(custom):
    data = rows(stages=("correct",))
    bindings = bound(data)
    selector = ("custom:approved-runtime/" if custom else "openai/") + "chosen@other-runtime"
    bindings[0]["selector"] = selector
    data["session_settings"][0]["value_json"]["model_name"] = selector
    if custom:
        validate_connection_state(state(data, bindings), data)
    else:
        with pytest.raises(ProjectCopyError) as error:
            validate_connection_state(state(data, bindings), data)
        assert error.value.code == "copy_graph_invalid"
