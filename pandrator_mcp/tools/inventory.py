"""Read-only, model-safe provider, artifact, and voice projections."""

from __future__ import annotations

from typing import Any

from ..context import McpRuntime
from ..schemas import (
    ListArtifactsInput,
    ProviderStatusInput,
    VoiceCatalogInput,
)


def list_artifacts(
    runtime: McpRuntime,
    arguments: ListArtifactsInput,
) -> dict[str, Any]:
    payload = runtime.require_application().list_artifacts(
        session_id=arguments.session_id,
        limit=arguments.limit,
    )
    source = payload.get("items")
    items: list[dict[str, Any]] = []
    if isinstance(source, list):
        for item in source:
            if not isinstance(item, dict):
                continue
            if arguments.kind and item.get("kind") != arguments.kind:
                continue
            if arguments.role and item.get("role") != arguments.role:
                continue
            items.append(
                {
                    "id": item.get("id"),
                    "session_id": item.get("session_id"),
                    "kind": item.get("kind"),
                    "role": item.get("role"),
                    "mime_type": item.get("mime_type"),
                    "size_bytes": item.get("size_bytes"),
                    "state": item.get("state"),
                    "created_at": item.get("created_at"),
                }
            )
            if len(items) >= arguments.limit:
                break
    return {"schema_version": "1", "items": items}


def provider_status(
    runtime: McpRuntime,
    arguments: ProviderStatusInput,
) -> dict[str, Any]:
    payload = runtime.require_application().list_providers()
    source = payload.get("items")
    items: list[dict[str, Any]] = []
    if isinstance(source, list):
        for item in source:
            if not isinstance(item, dict):
                continue
            if not arguments.include_disabled and not item.get("enabled"):
                continue
            items.append(
                {
                    "id": item.get("id"),
                    "kind": item.get("kind"),
                    "provider_key": item.get("provider_key"),
                    "label": item.get("label"),
                    "enabled": bool(item.get("enabled")),
                    "base_url": item.get("base_url"),
                    "credential_backend": item.get("credential_backend"),
                    "credential_configured": bool(item.get("credential_configured")),
                    "revision": item.get("revision"),
                }
            )
    return {"schema_version": "1", "items": items}


def voice_catalog(
    runtime: McpRuntime,
    arguments: VoiceCatalogInput,
) -> dict[str, Any]:
    payload = runtime.require_application().list_voices()
    source = payload.get("items")
    items: list[dict[str, Any]] = []
    if isinstance(source, list):
        for item in source:
            if not isinstance(item, dict):
                continue
            if arguments.language and item.get("language") != arguments.language:
                continue
            items.append(
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "language": item.get("language"),
                    "has_rvc_model": bool(item.get("rvc_model_ref")),
                    "revision": item.get("revision"),
                }
            )
            if len(items) >= arguments.limit:
                break
    return {"schema_version": "1", "items": items}
