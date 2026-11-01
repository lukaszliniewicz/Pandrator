"""Bound destination-model fixtures for project-copy graph tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pandrator.web import models as m
from pandrator.web.model_base import new_id
from pandrator.web.projects.copy_snapshot import capture_current_project
from pandrator.web.settings_policy import BUILTIN_DEFAULTS


def _model_cache(case: Any) -> dict[str, tuple[str, str]]:
    cache = getattr(case, "_copy_test_models", None)
    if cache is None:
        cache = {}
        case._copy_test_models = cache
    return cache


def seed_copy_model(case: Any, workspace_id: str) -> tuple[str, str]:
    """Seed one offline destination model during fixture setup, once per workspace."""
    cache = _model_cache(case)
    if workspace_id in cache:
        return cache[workspace_id]

    provider_id = new_id()
    provider_model_id = new_id()
    with case.database.immediate_session() as session:
        session.add(
            m.Provider(
                id=provider_id,
                workspace_id=workspace_id,
                kind="llm",
                provider_key="openai",
                label="Project copy fixture model",
                enabled=True,
                options_json={"is_custom": True},
            )
        )
        session.flush()
        session.add(
            m.ProviderModel(
                id=provider_model_id,
                provider_id=provider_id,
                model_id="project-copy-fixture-model",
                is_active=True,
                is_default=True,
                input_modalities_json=["text"],
                output_modalities_json=["text"],
            )
        )
        session.flush()
    cache[workspace_id] = (provider_id, provider_model_id)
    return cache[workspace_id]


def _seeded_model(case: Any, workspace_id: str) -> tuple[str, str]:
    pair = _model_cache(case).get(workspace_id)
    if pair is None:
        raise AssertionError("Fixture must seed the destination copy model before capture.")
    return pair


def _connection_choices(
    case: Any, workspace_id: str, requirements: list[dict[str, Any]]
) -> list[dict[str, str]]:
    provider_id, provider_model_id = _seeded_model(case, workspace_id)
    return [
        {
            "requirement_id": item["id"],
            "provider_id": provider_id,
            "provider_model_id": provider_model_id,
        }
        for item in requirements
    ]


def capture_with_connections(
    case: Any,
    session: Any,
    context: Any,
    source_project_id: str,
    destination_workspace_id: str,
    **kwargs: Any,
):
    """Capture through the real read path, binding only after its requirements are known."""
    if "destination_connections" in kwargs:
        return capture_current_project(
            session, context, source_project_id, destination_workspace_id, **kwargs
        )

    initial = capture_current_project(
        session, context, source_project_id, destination_workspace_id, **kwargs
    )
    requirements = initial.manifest()["connection_requirements"]
    if not requirements:
        return initial
    choices = _connection_choices(case, destination_workspace_id, requirements)
    return capture_current_project(
        session,
        context,
        source_project_id,
        destination_workspace_id,
        **kwargs,
        destination_connections=choices,
    )


def _choice_cache(case: Any) -> dict[tuple[str, str], list[dict[str, str]]]:
    cache = getattr(case, "_copy_destination_choices", None)
    if cache is None:
        cache = {}
        case._copy_destination_choices = cache
    return cache


def snapshot_destination_connections(snapshot: Any) -> list[dict[str, str]]:
    """Extract the selected destination choices from a ready captured snapshot."""
    return [
        {
            "requirement_id": item["id"],
            "provider_id": item["selected_provider_id"],
            "provider_model_id": item["selected_provider_model_id"],
        }
        for item in snapshot.manifest()["connection_requirements"]
    ]


def cached_destination_connections(
    case: Any, source_project_id: str, destination_workspace_id: str
) -> list[dict[str, str]]:
    """Return the choices paired with the latest reviewed preview, without refreshing it."""
    choices = _choice_cache(case).get((source_project_id, destination_workspace_id), [])
    return deepcopy(choices)


def preview_with_connections(
    case: Any, context: Any, source_project_id: str, destination_workspace_id: str
) -> dict[str, Any]:
    """Preview once, then bind its exact requirements through the real preview service."""
    initial = case.copies.preview(context, source_project_id, destination_workspace_id)
    requirements = initial["connection_requirements"]
    if not requirements:
        _choice_cache(case)[(source_project_id, destination_workspace_id)] = []
        return initial

    choices = _connection_choices(case, destination_workspace_id, requirements)
    preview = case.copies.preview(
        context,
        source_project_id,
        destination_workspace_id,
        destination_connections=choices,
    )
    _choice_cache(case)[(source_project_id, destination_workspace_id)] = deepcopy(choices)
    return preview


def prepare_legacy_copy_state(state: dict[str, Any]) -> None:
    """Remove v6 bindings and restore their canonical pre-binding defaults in test state."""
    for family in (
        "generation_plans", "generation_plan_revisions", "generation_segments", "speech_plan_reviews",
        "audio_takes", "project_sound_plans", "project_sound_cues", "sound_variants",
    ):
        state["rows"].pop(family, None)
    bindings = state.pop("destination_connections", [])
    settings = {
        (row["session_id"], row["section"]): row for row in state["rows"]["session_settings"]
    }
    for binding in bindings:
        for target in binding["targets"]:
            section = target["section"]
            field = target["field"]
            setting = settings[(target["session_id"], section)]["value_json"]
            setting[field] = deepcopy(BUILTIN_DEFAULTS[section][field])
