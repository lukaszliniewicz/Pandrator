"""Stable application identity for enrolled API and automation clients."""

from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict

from pandrator.version import PANDRATOR_VERSION

from .database import Database
from .models import AppSetting

IDENTITY_SETTING_KEY = "system.identity"
IDENTITY_SCHEMA_VERSION = "1"
API_VERSION = "v1"


def canonical_origin(value: str) -> str:
    """Validate and normalize one exact HTTP(S) origin."""

    candidate = str(value or "").strip()
    parsed = urlsplit(candidate)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Canonical origin must be an HTTP(S) origin without credentials, "
            "a path, a query, or a fragment."
        )
    try:
        _ = parsed.port
    except ValueError as error:
        raise ValueError("Canonical origin contains an invalid port.") from error
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            "",
            "",
            "",
        )
    )


class ApplicationIdentityDocument(BaseModel):
    """Versioned identity response pinned by remote MCP target profiles."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = IDENTITY_SCHEMA_VERSION
    service: str = "pandrator"
    instance_id: str
    application_version: str = PANDRATOR_VERSION
    api_version: str = API_VERSION
    protocol_version: str = API_VERSION
    canonical_origin: str
    managed: bool
    manager_instance_id: str | None = None


class ApplicationIdentityService:
    """Own the workspace-persistent application ID and public origin contract."""

    def __init__(
        self,
        database: Database,
        *,
        public_origin: str | None = None,
        manager_instance_id: str | None = None,
    ) -> None:
        self.database = database
        self.public_origin = canonical_origin(public_origin) if public_origin else None
        self.manager_instance_id = (
            str(
                manager_instance_id
                if manager_instance_id is not None
                else os.environ.get("PANDRATOR_MANAGER_INSTANCE", "")
            ).strip()
            or None
        )
        self.instance_id = self._load_or_create_instance_id()

    def _load_or_create_instance_id(self) -> str:
        with self.database.immediate_session() as session:
            setting = session.get(AppSetting, IDENTITY_SETTING_KEY)
            if setting is None:
                instance_id = str(uuid.uuid4())
                session.add(
                    AppSetting(
                        key=IDENTITY_SETTING_KEY,
                        value_json={
                            "schema_version": IDENTITY_SCHEMA_VERSION,
                            "instance_id": instance_id,
                        },
                    )
                )
                return instance_id
            value = setting.value_json if isinstance(setting.value_json, dict) else {}
            instance_id = str(value.get("instance_id") or "").strip()
            try:
                return str(uuid.UUID(instance_id))
            except ValueError as error:
                raise RuntimeError(
                    "The durable Pandrator application identity is invalid."
                ) from error

    def snapshot(
        self,
        *,
        observed_origin: str | None = None,
    ) -> ApplicationIdentityDocument:
        origin = self.public_origin
        if origin is None:
            if not observed_origin:
                raise ValueError(
                    "An observed origin is required when no public origin is configured."
                )
            origin = canonical_origin(observed_origin)
        return ApplicationIdentityDocument(
            instance_id=self.instance_id,
            canonical_origin=origin,
            managed=self.manager_instance_id is not None,
            manager_instance_id=self.manager_instance_id,
        )
