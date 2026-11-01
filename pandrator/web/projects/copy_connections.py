"""Finite destination LLM selections for current-project copies; no credentials or I/O."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from pandrator.logic.llm_handler import BUILTIN_PROVIDER_CONFIGS, _normalize_provider_id

from ..models import Provider, ProviderModel, SessionRecord
from ..outcome_plans import derive_legacy_outcome, resolve_pipeline
from ..provider_identity import NONINITIAL_LLM_PROVIDER_KEYS, provider_namespace
from .copy_contracts import project_copy_error

MAX_CHOICES = 512
MAX_OPTIONS = 1000
_HASH = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_PURPOSES = {
    "correction": ("correction", "model_name", ("model_name", "correction_model", "correct_model")),
    "translation": (
        "translation",
        "model_name",
        ("model_name", "translation_model", "translate_model"),
    ),
    "speech_preparation": (
        "text",
        "tts_optimization_model",
        ("tts_optimization_model", "llm_model"),
    ),
}
_STAGE = {
    "correct": "correction",
    "edit_media": "correction",
    "translate": "translation",
    "optimize_tts": "speech_preparation",
}
_BINDING = {
    "id",
    "purpose",
    "targets",
    "provider_id",
    "provider_model_id",
    "selector",
    "provider_revision",
    "model_revision",
}
_CONNECTION_KEYS = frozenset(
    {
        "provider_id",
        "provider",
        "connection_id",
        "base_url",
        "api_base",
        "endpoint",
        "external_server_url",
        "secret_ref",
        "credential_ref",
        "services",
        "tts_services",
        "service_configs",
        "providers",
        "endpoints",
    }
)


def connection_key(key: str) -> bool:
    value = key.lower()
    return value in _CONNECTION_KEYS or value.endswith(
        ("_secret_ref", "_credential_ref", "_base_url", "_api_base", "_endpoint")
    )


def private_model_selector(value: Any) -> bool:
    return type(value) is str and value.strip().startswith("custom:")


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _uuid(value: Any) -> bool:
    try:
        return type(value) is str and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _text(value: Any, limit: int = 1024) -> bool:
    return type(value) is str and bool(value) and value == value.strip() and len(value) <= limit


def normalize_destination_connections(value: Any = None) -> list[dict[str, str]]:
    if value is None:
        return []
    if type(value) is not list or len(value) > MAX_CHOICES:
        raise project_copy_error("copy_input_invalid")
    result, seen = [], set()
    for choice in value:
        if (
            type(choice) is not dict
            or set(choice) != {"requirement_id", "provider_id", "provider_model_id"}
            or type(choice["requirement_id"]) is not str
            or _HASH.fullmatch(choice["requirement_id"]) is None
            or not _uuid(choice["provider_id"])
            or not _uuid(choice["provider_model_id"])
            or choice["requirement_id"] in seen
        ):
            raise project_copy_error("copy_input_invalid")
        seen.add(choice["requirement_id"])
        result.append(dict(choice))
    return sorted(result, key=lambda item: item["requirement_id"])


def _selector(settings: Mapping[str, Any], aliases: tuple[str, ...]) -> str:
    selected = ""
    for key in aliases:
        selected = selected or str(settings.get(key) or "").strip()
    return "" if selected == "default" else selected


def collect_requirements(
    rows: dict[str, Any], blockers: set[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Clear source selectors on detached rows, retaining only finite target groups."""
    settings = {
        (item["session_id"], item["section"]): item["value_json"]
        for item in rows["session_settings"]
    }
    outcomes = {item["session_id"]: item["value_json"] for item in rows.get("outcome_plans", [])}
    groups: dict[str, dict[str, Any]] = {}
    for record in rows["sessions"]:
        sid = record["id"]
        text = settings.get((sid, "text"), {})
        if text.get("model_name") not in (None, ""):
            blockers.add(("unsupported_state", "custom_settings"))
        text.pop("model_name", None)
        if sid in outcomes:
            outcome = outcomes[sid]
        else:
            outcome = derive_legacy_outcome(
                SessionRecord(
                    **{
                        key: record.get(key)
                        for key in (
                            "workflow_kind",
                            "workflow_preset",
                            "included_stages_json",
                        )
                    }
                )
            )
            outcome["transformations"].update(
                {
                    key: bool(text.get(key))
                    for key in (
                        "llm_tts_optimization",
                        "llm_tts_document_optimization",
                    )
                }
            )
        active = {
            _STAGE[item["key"]] for item in resolve_pipeline(outcome) if item["key"] in _STAGE
        }
        for purpose, (section, field, aliases) in _PURPOSES.items():
            values = settings.get((sid, section))
            if values is None:
                raise project_copy_error("copy_graph_invalid")
            selector = _selector(values, aliases)
            explicit = any(
                str(values.get(key) or "").strip() not in {"", "default"} for key in aliases
            )
            for key in aliases:
                values.pop(key, None)
            if purpose not in active and not explicit:
                continue
            identifier = hashlib.sha256(
                _canonical({"purpose": purpose, "selector": selector}).encode()
            ).hexdigest()
            group = groups.setdefault(
                identifier,
                {
                    "id": identifier,
                    "purpose": purpose,
                    "targets": [],
                    "provider_id": None,
                    "provider_model_id": None,
                    "selector": None,
                    "provider_revision": None,
                    "model_revision": None,
                },
            )
            group["targets"].append({"session_id": sid, "section": section, "field": field})
    result = sorted(groups.values(), key=lambda item: item["id"])
    for group in result:
        group["targets"].sort(key=lambda item: (item["session_id"], item["section"], item["field"]))
    if len(result) > MAX_CHOICES:
        raise project_copy_error("copy_limit_exceeded")
    return result


def _naming_scalar(column, name: str):
    # Do not hydrate a config/credential body hidden in a malformed naming field.
    kinds = ("text", "integer", "real", "true", "false", "null")
    return case(
        (func.json_type(column, "$." + name).in_(kinds), column[name].as_json()), else_=None
    )


def destination_options(session: Session, workspace_id: str) -> list[dict[str, Any]]:
    try:
        namespace = provider_namespace(session, workspace_id=workspace_id)
    except (RuntimeError, ValueError) as error:
        raise project_copy_error("copy_connection_unavailable") from error
    columns = [
        Provider.id.label("provider_id"),
        Provider.provider_key,
        Provider.label,
        Provider.revision.label("provider_revision"),
        ProviderModel.id.label("provider_model_id"),
        ProviderModel.model_id,
        ProviderModel.revision.label("model_revision"),
        ProviderModel.input_modalities_json,
        ProviderModel.output_modalities_json,
    ]
    if namespace.legacy_host_access:
        columns += [
            _naming_scalar(Provider.options_json, "is_custom").label("is_custom"),
            _naming_scalar(Provider.options_json, "provider_id").label("runtime_id"),
            func.json_type(Provider.options_json, "$.is_custom").label("custom_type"),
            func.json_type(Provider.options_json, "$.provider_id").label("runtime_type"),
        ]
    statement = (
        select(*columns)
        .join(ProviderModel, ProviderModel.provider_id == Provider.id)
        .where(
            Provider.workspace_id == namespace.workspace_id,
            Provider.kind == "llm",
            Provider.enabled.is_(True),
            ProviderModel.is_active.is_(True) | ProviderModel.is_default.is_(True),
        )
        .limit(MAX_OPTIONS + 1)
    )
    headers = [dict(row) for row in session.execute(statement).mappings()]
    if len(headers) > MAX_OPTIONS:
        raise project_copy_error("copy_limit_exceeded")
    runtime_ids: dict[str, str | None] = {}
    selectors: dict[str, str | None] = {}
    for row in headers:
        key, pid = row["provider_key"], row["provider_id"]
        model = row["model_id"]
        if not namespace.legacy_host_access:
            selectors[row["provider_model_id"]] = (
                f"custom:{pid}/{model}"
                if key.strip().lower() in NONINITIAL_LLM_PROVIDER_KEYS
                and _text(model, 255)
                and model != "<model-id>"
                else None
            )
            continue
        scalar_types = {None, "null", "text", "integer", "real", "true", "false"}
        if row["custom_type"] not in scalar_types or row["runtime_type"] not in scalar_types:
            runtime_ids[pid] = None
            selectors[row["provider_model_id"]] = None
            continue
        custom = bool(row["is_custom"] or key not in {"openai", "gemini", "anthropic"})
        runtime = str(row["runtime_id"] or (pid if custom else key))
        runtime_ids[pid] = runtime
        if (
            not _text(model, 255)
            or model == "<model-id>"
            or not _text(runtime, 255)
            or _normalize_provider_id(runtime) != runtime
            or (custom and runtime in BUILTIN_PROVIDER_CONFIGS)
            or (not custom and runtime != key)
            or (not custom and "@" in model)
        ):
            selectors[row["provider_model_id"]] = None
        else:
            selectors[row["provider_model_id"]] = (
                f"custom:{runtime}/{model}" if custom else f"{key}/{model}"
            )
    duplicates = Counter(value for value in runtime_ids.values() if value is not None)
    alias_counts = Counter(value for value in selectors.values() if value is not None)
    result = []
    for row in headers:
        selector = selectors.get(row["provider_model_id"])
        if (
            not _uuid(row["provider_id"])
            or not _uuid(row["provider_model_id"])
            or any(
                type(row[key]) is not int or row[key] < 1
                for key in ("provider_revision", "model_revision")
            )
            or selector is None
            or alias_counts[selector] != 1
            or (
                namespace.legacy_host_access
                and duplicates.get(runtime_ids.get(row["provider_id"]) or "", 0) != 1
            )
            or any(
                type(row[key]) is not list
                or "text" not in row[key]
                or any(type(item) is not str for item in row[key])
                for key in ("input_modalities_json", "output_modalities_json")
            )
        ):
            continue
        result.append(
            {
                "provider_id": row["provider_id"],
                "provider_model_id": row["provider_model_id"],
                "provider_label": row["label"],
                "model_name": row["model_id"],
                "selector": selector,
                "provider_revision": row["provider_revision"],
                "model_revision": row["model_revision"],
            }
        )
    if len(result) > MAX_OPTIONS:
        raise project_copy_error("copy_limit_exceeded")
    return sorted(result, key=lambda item: (item["provider_id"], item["provider_model_id"]))


def bind_destination_connections(
    session: Session,
    rows: dict[str, Any],
    workspace_id: str,
    bindings: list[dict[str, Any]],
    choices: Any,
    blockers: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    normalized = normalize_destination_connections(choices)
    by_id = {item["requirement_id"]: item for item in normalized}
    if not set(by_id) <= {item["id"] for item in bindings}:
        raise project_copy_error("copy_input_invalid")
    if not bindings:
        return []
    options = destination_options(session, workspace_id)
    indexed = {(item["provider_id"], item["provider_model_id"]): item for item in options}
    settings = {
        (item["session_id"], item["section"]): item["value_json"]
        for item in rows["session_settings"]
    }
    for binding in bindings:
        choice = by_id.get(binding["id"])
        if choice is None:
            blockers.add(("destination_binding_required", binding["purpose"]))
            continue
        option = indexed.get((choice["provider_id"], choice["provider_model_id"]))
        if option is None:
            raise project_copy_error("copy_connection_unavailable")
        for key in (
            "provider_id",
            "provider_model_id",
            "selector",
            "provider_revision",
            "model_revision",
        ):
            binding[key] = option[key]
        for target in binding["targets"]:
            settings[(target["session_id"], target["section"])][target["field"]] = option[
                "selector"
            ]
    return [
        {
            key: item[key]
            for key in ("provider_id", "provider_model_id", "provider_label", "model_name")
        }
        for item in options
    ]


def validate_connection_graph(
    rows: Mapping[str, Any], bindings: Any, *, ready: bool = True
) -> None:
    if type(bindings) is not list or len(bindings) > MAX_CHOICES:
        raise project_copy_error("copy_graph_invalid")
    sessions = {item["id"] for item in rows.get("sessions", [])}
    settings = {
        (item["session_id"], item["section"]): item["value_json"]
        for item in rows.get("session_settings", [])
    }
    seen_ids, seen_targets, approved = set(), set(), {}
    for item in bindings:
        if (
            type(item) is not dict
            or set(item) != _BINDING
            or type(item["id"]) is not str
            or _HASH.fullmatch(item["id"]) is None
            or item["id"] in seen_ids
            or type(item["purpose"]) is not str
            or item["purpose"] not in _PURPOSES
            or type(item["targets"]) is not list
            or not item["targets"]
            or len(item["targets"]) > 101
        ):
            raise project_copy_error("copy_graph_invalid")
        seen_ids.add(item["id"])
        section, field, _aliases = _PURPOSES[item["purpose"]]
        pending = all(
            item[key] is None
            for key in (
                "provider_id",
                "provider_model_id",
                "selector",
                "provider_revision",
                "model_revision",
            )
        )
        if pending and ready:
            raise project_copy_error("copy_graph_invalid")
        if not pending and (
            not _uuid(item["provider_id"])
            or not _uuid(item["provider_model_id"])
            or not _text(item["selector"])
            or (not item["selector"].startswith("custom:") and "@" in item["selector"])
            or any(
                type(item[key]) is not int or item[key] < 1
                for key in ("provider_revision", "model_revision")
            )
        ):
            raise project_copy_error("copy_graph_invalid")
        for target in item["targets"]:
            if type(target) is not dict or set(target) != {"session_id", "section", "field"}:
                raise project_copy_error("copy_graph_invalid")
            if any(not _text(target[key], 255) for key in ("session_id", "section", "field")):
                raise project_copy_error("copy_graph_invalid")
            key = (target["session_id"], target["section"], target["field"])
            if (
                target["session_id"] not in sessions
                or target["section"] != section
                or target["field"] != field
                or key in seen_targets
                or (target["session_id"], section) not in settings
            ):
                raise project_copy_error("copy_graph_invalid")
            seen_targets.add(key)
            value = settings[(target["session_id"], section)]
            if pending:
                if field in value:
                    raise project_copy_error("copy_graph_invalid")
            elif value.get(field) != item["selector"]:
                raise project_copy_error("copy_graph_invalid")
            if any(alias in value for alias in _aliases if alias != field):
                raise project_copy_error("copy_graph_invalid")
            approved[key] = item["selector"]
    for (sid, section), values in settings.items():

        def inspect(value: Any, path: tuple[str, ...] = (), *, _sid=sid, _section=section) -> None:
            if type(value) is dict:
                for key, child in value.items():
                    if connection_key(key) or (key == "model_name" and _section == "text"):
                        if child not in (None, "", {}, []):
                            raise project_copy_error("copy_graph_invalid")
                    if private_model_selector(child) and (
                        path or approved.get((_sid, _section, key)) != child
                    ):
                        raise project_copy_error("copy_graph_invalid")
                    inspect(child, (*path, key))
            elif type(value) is list:
                for child in value:
                    if private_model_selector(child):
                        raise project_copy_error("copy_graph_invalid")
                    inspect(child, path)

        inspect(values)


def validate_connection_state(
    state: Mapping[str, Any], rows: Mapping[str, Any], *, ready: bool = True
) -> list[dict[str, Any]]:
    version = state.get("schema_version")
    expected = {"schema_version", "rows", "binding_fields", "unsupported_counts"}
    if version in {6, 7, 8, 9, 10, 11, 12, 13, 14, 15}:
        expected.add("destination_connections")
    if set(state) != expected:
        raise project_copy_error("copy_graph_invalid")
    bindings = state.get("destination_connections", [])
    validate_connection_graph(rows, bindings, ready=ready)
    if version in {6, 7, 8, 9, 10, 11, 12, 13, 14, 15}:
        subset = {
            key: deepcopy(rows.get(key, []))
            for key in ("sessions", "session_settings", "outcome_plans")
        }
        required = collect_requirements(subset, set())
        needed = {
            (target["session_id"], target["section"], target["field"])
            for binding in required
            for target in binding["targets"]
        }
        present = {
            (target["session_id"], target["section"], target["field"])
            for binding in bindings
            for target in binding["targets"]
        }
        if (ready and needed != present) or not needed <= present:
            raise project_copy_error("copy_graph_invalid")
    return bindings


def validate_current_connections(
    session: Session, workspace_id: str, bindings: list[dict[str, Any]]
) -> None:
    if not bindings:
        return
    available = {
        (item["provider_id"], item["provider_model_id"]): item
        for item in destination_options(session, workspace_id)
    }
    for binding in bindings:
        option = available.get((binding["provider_id"], binding["provider_model_id"]))
        if option is None or any(
            binding[key] != option[key]
            for key in ("selector", "provider_revision", "model_revision")
        ):
            raise project_copy_error("copy_connection_unavailable")
