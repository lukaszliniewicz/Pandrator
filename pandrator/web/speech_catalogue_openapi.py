"""Public workspace speech catalogue and explicit connection refresh contract."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def _scope_parameter(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "in": "query",
        "required": False,
        "schema": {"type": "string", "minLength": 1, "maxLength": 80},
        "description": (
            "Workspace to read; omitted roots use the caller's personal workspace."
            if name == "workspace_id"
            else "Project used to resolve a workspace; independent workspace access is required."
        ),
    }


def _authority(definition: dict[str, Any], scopes: list[str]) -> None:
    definition["x-required-scopes"] = scopes
    definition["security"] = [
        {"cookieAuth": []}, {"bearerToken": []}, {"nativeOAuth": scopes},
    ]


def extend_speech_catalogue_paths(paths: dict[str, Any]) -> None:
    for path in (
        "/api/v1/services/tts", "/api/v1/services/tts/{serviceId}",
        "/api/v1/services/stt", "/api/v1/voices",
    ):
        definition = paths[path]["get"]
        definition.setdefault("parameters", []).extend(
            _scope_parameter(name) for name in ("workspace_id", "session_id")
        )
        definition["description"] = (
            "Read the caller's workspace catalogue without probing providers. "
            "Members receive approved choices; workspace managers can inspect configuration. "
            "A project share alone does not grant workspace catalogue access."
        )
        definition.setdefault("responses", {}).update({
            "404": {"description": "Workspace or project is unavailable to this caller"},
            "422": {"description": "Invalid workspace or project selector"},
        })
        for parameter in definition["parameters"]:
            if path == "/api/v1/services/tts" and parameter["name"] == "view":
                parameter["schema"]["enum"] = ["full", "compact", "summary"]
                parameter["description"] = (
                    "Full configuration, compact UI choices, or a small approved summary "
                    "without connection credentials, large catalogues or model schemas."
                )
            if parameter["name"] == "refresh":
                parameter["description"] = (
                    "Compatibility parameter. A true value returns 409; use explicit POST refresh."
                )
                definition["responses"]["409"] = {
                    "description": "catalogue_refresh_requires_post: use explicit POST refresh"
                }

    for path, operation_id in (
        ("/api/v1/services/tts", "refreshTtsServices"),
        ("/api/v1/services/tts/{serviceId}", "refreshTtsServiceDetail"),
    ):
        definition = deepcopy(paths[path]["get"])
        definition["operationId"] = operation_id
        definition["description"] = (
            "Explicitly probe up to 20 configured workspace connections. Requires current "
            "workspace management authority and all delegated scopes; no session selector or body."
        )
        definition["parameters"] = [
            item for item in definition["parameters"]
            if item["name"] not in {"refresh", "session_id"}
        ]
        definition["responses"].update({
            "403": {"description": "Workspace management authority and all scopes required"},
            "409": {"description": "catalogue_changed: connection or default settings changed"},
            "422": {"description": "Invalid selectors, session selector, or more than 20 services"},
        })
        _authority(definition, ["app.read", "app.write", "app.run", "app.credentials.write"])
        definition["x-required-workspace-action"] = "manage"
        paths[f"{path}/refresh"] = {"post": definition}

    for path, method, scopes, action in (
        ("/api/v1/services/tts/discover", "post",
         ["app.admin", "app.read", "app.write", "app.run", "app.credentials.write"], "manage"),
        ("/api/v1/services/tts/xtts/models", "get", ["app.admin", "app.read"], "manage_read"),
        ("/api/v1/services/tts/xtts/models", "post", ["app.admin", "app.read", "app.write"], "manage"),
        ("/api/v1/services/tts/xtts/models/{modelId}", "delete",
         ["app.admin", "app.read", "app.write"], "manage"),
    ):
        definition = paths[path][method]
        definition.setdefault("parameters", []).append(_scope_parameter("workspace_id"))
        definition["x-required-role"] = "administrator"
        definition["x-required-workspace-action"] = action
        _authority(definition, scopes)
        definition.setdefault("responses", {}).setdefault("403", {
            "description": "Current Administrator, workspace authority and all scopes required",
        })
