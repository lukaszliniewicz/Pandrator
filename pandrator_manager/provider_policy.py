"""Manager-local provider catalogue policy.

The manager is distributed independently from the Pandrator application, so
this module deliberately loads the policy from its own package resources and
has no application-package imports.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Literal

CatalogueRole = Literal["primary", "compatibility"]


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    """Catalogue metadata projected onto a manager component definition."""

    catalogue_role: CatalogueRole = "primary"
    replacement_component_id: str | None = None


def _policy_document() -> dict[str, object]:
    policy_path = resources.files("pandrator_manager").joinpath("provider_policy.json")
    document = json.loads(policy_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Provider policy must contain a JSON object.")
    return document


@lru_cache(maxsize=1)
def _metadata_by_component() -> dict[str, ProviderMetadata]:
    document = _policy_document()
    providers = document.get("providers", ())
    if not isinstance(providers, list):
        raise ValueError("Provider policy providers must be a JSON array.")

    service_to_component: dict[str, str] = {}
    for provider in providers:
        if not isinstance(provider, dict):
            raise ValueError("Provider policy entries must be JSON objects.")
        service_id = provider.get("service_id")
        component_id = provider.get("component_id")
        if isinstance(service_id, str) and isinstance(component_id, str):
            service_to_component[service_id] = component_id

    metadata: dict[str, ProviderMetadata] = {}
    for provider in providers:
        if not isinstance(provider, dict):
            raise ValueError("Provider policy entries must be JSON objects.")
        component_id = provider.get("component_id")
        if not isinstance(component_id, str):
            raise ValueError("Provider policy component_id must be a string.")
        role = provider.get("catalogue_role", "primary")
        if role not in ("primary", "compatibility"):
            raise ValueError(f"Unsupported provider catalogue role: {role!r}")
        replacement_service_id = provider.get("replacement_service_id")
        replacement_component_id = (
            service_to_component.get(replacement_service_id)
            if isinstance(replacement_service_id, str)
            else None
        )
        metadata[component_id] = ProviderMetadata(
            catalogue_role=role,
            replacement_component_id=replacement_component_id,
        )
    return metadata


def provider_metadata_for(component_id: str) -> ProviderMetadata:
    """Return policy metadata for a component, with a safe default."""

    return _metadata_by_component().get(component_id, ProviderMetadata())


__all__ = ["CatalogueRole", "ProviderMetadata", "provider_metadata_for"]
