"""Model-safe session reads and revision-safe writes."""

from __future__ import annotations

from typing import Any

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas import (
    AttachExistingSourceInput,
    CreateSessionInput,
    GetSessionInput,
    GetSessionSettingsInput,
    GetWorkflowInput,
    ListSessionsInput,
    ListSourcesInput,
    UpdateSessionInput,
    UpdateSessionSettingsInput,
)

_PRIVATE_SETTING_KEYS = frozenset(
    {
        "api_base",
        "api_key",
        "authorization",
        "base_url",
        "ca_bundle",
        "command",
        "connection",
        "credential",
        "credential_reference",
        "endpoint",
        "external_path",
        "host",
        "origin",
        "password",
        "path",
        "port",
        "private_key",
        "proxy",
        "proxy_origin",
        "secret",
        "token",
        "url",
        "workspace",
    }
)


def _session_projection(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "workflow_kind": item.get("workflow_kind"),
        "source_language": item.get("source_language"),
        "target_language": item.get("target_language"),
        "workflow_preset": item.get("workflow_preset"),
        "included_stages": list(
            item.get("included_stages")
            or item.get("included_stages_json")
            or ()
        )[:20],
        "status": item.get("status"),
        "revision": item.get("revision"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def _safe_setting_value(
    value: Any,
    *,
    depth: int = 0,
) -> Any:
    if depth > 10:
        return None
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:500]:
            key = str(raw_key)[:120]
            normalized = key.strip().lower().replace("-", "_")
            if (
                normalized in _PRIVATE_SETTING_KEYS
                or normalized.endswith(
                    (
                        "_api_key",
                        "_command",
                        "_credential",
                        "_endpoint",
                        "_origin",
                        "_password",
                        "_path",
                        "_private_key",
                        "_proxy",
                        "_secret",
                        "_token",
                        "_url",
                    )
                )
            ):
                continue
            result[key] = _safe_setting_value(
                item,
                depth=depth + 1,
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _safe_setting_value(item, depth=depth + 1)
            for item in list(value)[:500]
        ]
    if isinstance(value, str):
        return value[:20_000]
    return value


def _artifact_projection(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    return {
        key: item.get(key)
        for key in (
            "id",
            "version",
            "kind",
            "role",
            "mime_type",
            "size_bytes",
            "state",
            "settings_hash",
            "parent_ids",
            "created_at",
            "is_selected",
        )
        if key in item
    }


def _workflow_projection(payload: dict[str, Any]) -> dict[str, Any]:
    stages: list[dict[str, Any]] = []
    source_stages = (
        payload.get("stages")
        if isinstance(payload.get("stages"), list)
        else []
    )
    for source in source_stages[:50]:
        if not isinstance(source, dict):
            continue
        stage = {
            key: source.get(key)
            for key in (
                "number",
                "key",
                "title",
                "explanation",
                "status",
                "executable",
                "toggle",
                "toggle_only",
                "enabled",
                "optimization_timing",
                "included",
                "required",
                "selected_artifact_id",
                "selection_revision",
                "artifact_history_total",
                "artifact_history_has_more",
                "artifact_history_next_before_version",
                "job_id",
                "progress",
                "usage",
            )
            if key in source
        }
        stage["artifact"] = _artifact_projection(
            source.get("artifact")
        )
        history = (
            source.get("artifacts")
            if isinstance(source.get("artifacts"), list)
            else []
        )
        stage["artifacts"] = [
            projected
            for item in history[:20]
            if (projected := _artifact_projection(item))
            is not None
        ]
        stages.append(stage)
    sources: list[dict[str, Any]] = []
    source_items = (
        payload.get("sources")
        if isinstance(payload.get("sources"), list)
        else []
    )
    for item in source_items[:50]:
        if not isinstance(item, dict):
            continue
        sources.append(
            {
                "id": item.get("id"),
                "filename": str(item.get("filename") or "")[:255],
                "kind": item.get("kind"),
                "role": item.get("role"),
            }
        )
    return {
        "schema_version": "1",
        "session_id": payload.get("session_id"),
        "workflow_kind": payload.get("workflow_kind"),
        "workflow_preset": payload.get("workflow_preset"),
        "revision": payload.get("revision"),
        "stages": stages,
        "sources": sources,
    }


def list_sessions(
    runtime: McpRuntime,
    arguments: ListSessionsInput,
) -> dict[str, Any]:
    payload = runtime.require_application().list_sessions(
        limit=arguments.limit
    )
    items = (
        payload.get("items")
        if isinstance(payload.get("items"), list)
        else []
    )
    filtered = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if (
            arguments.workflow_kind
            and item.get("workflow_kind")
            != arguments.workflow_kind
        ):
            continue
        if (
            arguments.state
            and item.get("status") != arguments.state
        ):
            continue
        filtered.append(_session_projection(item))
    return {
        "schema_version": "1",
        "items": filtered[: arguments.limit],
    }


def get_session(
    runtime: McpRuntime,
    arguments: GetSessionInput,
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        **_session_projection(
            runtime.require_application().get_session(
                arguments.session_id
            )
        ),
    }


def get_workflow(
    runtime: McpRuntime,
    arguments: GetWorkflowInput,
) -> dict[str, Any]:
    return _workflow_projection(
        runtime.require_application().get_workflow(
            arguments.session_id
        )
    )


def get_session_settings(
    runtime: McpRuntime,
    arguments: GetSessionSettingsInput,
) -> dict[str, Any]:
    payload = runtime.require_application().get_session_settings(
        arguments.session_id,
        arguments.section,
    )
    return {
        "schema_version": "1",
        "session_id": arguments.session_id,
        "section": payload.get("section") or arguments.section,
        "override": (
            _safe_setting_value(payload.get("override"))
            if isinstance(payload.get("override"), dict)
            else {}
        ),
        "effective": (
            _safe_setting_value(payload.get("effective"))
            if isinstance(payload.get("effective"), dict)
            else {}
        ),
        "session_context": (
            _safe_setting_value(payload.get("session_context"))
            if isinstance(payload.get("session_context"), dict)
            else {}
        ),
        "revision": payload.get("revision"),
        "global_revision": payload.get("global_revision"),
    }


def list_sources(
    runtime: McpRuntime,
    arguments: ListSourcesInput,
) -> dict[str, Any]:
    payload = runtime.require_application().list_sources(
        include_trashed=arguments.state == "trashed"
    )
    source = (
        payload.get("items")
        if isinstance(payload.get("items"), list)
        else []
    )
    items: list[dict[str, Any]] = []
    for item in source:
        if not isinstance(item, dict):
            continue
        if item.get("state") != arguments.state:
            continue
        items.append(
            {
                "source_asset_id": item.get("id"),
                "artifact_id": item.get("artifact_id"),
                "display_name": item.get("display_name"),
                "kind": item.get("kind"),
                "mime_type": item.get("mime_type"),
                "size_bytes": item.get("size_bytes"),
                "content_hash": item.get("content_hash"),
                "state": item.get("state"),
                "revision": item.get("revision"),
                "reference_count": item.get("reference_count"),
                "current_reference_count": item.get(
                    "current_reference_count"
                ),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            }
        )
        if len(items) >= arguments.limit:
            break
    return {"schema_version": "1", "items": items}


def create_session(
    runtime: McpRuntime,
    arguments: CreateSessionInput,
) -> ToolOutcome:
    result = runtime.require_application().create_session(
        name=arguments.name,
        workflow_kind=arguments.workflow_kind,
        source_language=arguments.source_language,
        target_language=arguments.target_language,
        workflow_preset=arguments.workflow_preset,
        included_stages=arguments.included_stages,
        idempotency_key=arguments.idempotency_key,
    )
    session = {
        "schema_version": "1",
        **_session_projection(result),
    }
    return ToolOutcome(
        result=session,
        next_actions=[
            NextAction(
                tool="pandrator_list_sources",
                arguments={"state": "current", "limit": 50},
                reason=(
                    "Inspect reusable sources before attaching one "
                    "to the new session."
                ),
            )
        ],
    )


def update_session(
    runtime: McpRuntime,
    arguments: UpdateSessionInput,
) -> ToolOutcome:
    result = runtime.require_application().update_session(
        arguments.session_id,
        expected_revision=arguments.expected_revision,
        changes=arguments.changes(),
        idempotency_key=arguments.idempotency_key,
    )
    session = {
        "schema_version": "1",
        **_session_projection(result),
    }
    return ToolOutcome(
        result=session,
        next_actions=[
            NextAction(
                tool="pandrator_get_workflow",
                arguments={"session_id": arguments.session_id},
                reason=(
                    "Inspect how the revised session changes workflow "
                    "prerequisites."
                ),
            )
        ],
    )


def attach_existing_source(
    runtime: McpRuntime,
    arguments: AttachExistingSourceInput,
) -> ToolOutcome:
    result = runtime.require_application().attach_existing_source(
        arguments.session_id,
        source_asset_id=arguments.source_asset_id,
        role=arguments.role,
        expected_session_revision=(
            arguments.expected_session_revision
        ),
        idempotency_key=arguments.idempotency_key,
    )
    safe = {
        "schema_version": "1",
        "attachment_id": result.get("id"),
        "session_id": result.get("session_id"),
        "source_asset_id": result.get("source_asset_id"),
        "role": result.get("role"),
        "is_current": bool(result.get("is_current")),
        "attachment_revision": result.get("revision"),
        "session_revision": result.get("session_revision"),
    }
    return ToolOutcome(
        result=safe,
        next_actions=[
            NextAction(
                tool="pandrator_get_workflow",
                arguments={"session_id": arguments.session_id},
                reason=(
                    "Inspect the workflow and prerequisites after the "
                    "source attachment."
                ),
            )
        ],
    )


def update_session_settings(
    runtime: McpRuntime,
    arguments: UpdateSessionSettingsInput,
) -> ToolOutcome:
    result = runtime.require_application().update_session_settings(
        arguments.session_id,
        section=arguments.section,
        value=arguments.value,
        expected_revision=arguments.expected_revision,
        idempotency_key=arguments.idempotency_key,
    )
    safe = {
        "schema_version": "1",
        "session_id": arguments.session_id,
        "section": result.get("section") or arguments.section,
        "override": (
            _safe_setting_value(result.get("override"))
            if isinstance(result.get("override"), dict)
            else {}
        ),
        "revision": result.get("revision"),
    }
    return ToolOutcome(
        result=safe,
        next_actions=[
            NextAction(
                tool="pandrator_get_session_settings",
                arguments={
                    "session_id": arguments.session_id,
                    "section": arguments.section,
                },
                reason=(
                    "Review the effective settings and new revision "
                    "before planning execution."
                ),
            )
        ],
    )
