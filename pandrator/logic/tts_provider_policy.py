"""Provider lifecycle policy, separate from runtime compatibility support.

provider_policy.json is the source of truth. The independently distributable
Manager carries a verified copy instead of importing the Pandrator application.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

_POLICY = json.loads(
    files("pandrator").joinpath("provider_policy.json").read_text("utf-8")
)
DEFAULT_TTS_SERVICE_ID: str = _POLICY["default_service_id"]
PROVIDER_POLICIES: dict[str, dict[str, Any]] = {
    item["service_id"]: item for item in _POLICY["providers"]
}
PRIMARY_LOCAL_SERVICE_IDS = tuple(
    key
    for key, value in PROVIDER_POLICIES.items()
    if value["catalogue_role"] == "primary"
)
COMPATIBILITY_SERVICE_IDS = frozenset(
    key
    for key, value in PROVIDER_POLICIES.items()
    if value["catalogue_role"] == "compatibility"
)


def provider_policy(service_id: str) -> dict[str, Any]:
    """Return public metadata for an already-normalized stable provider ID."""
    policy = PROVIDER_POLICIES.get(service_id, {})
    return {
        "catalogue_role": policy.get("catalogue_role", "external"),
        "replacement_service_id": policy.get("replacement_service_id"),
        "replacement_model_family": policy.get("replacement_model_family"),
    }
