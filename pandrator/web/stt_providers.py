"""Cloud speech-recognition profile catalogue for the web control plane."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy.orm import Session

from pandrator.logic.dubbing.stt_provider_profiles import list_stt_provider_profiles
from pandrator.runtime import DataPaths

from .credentials import (
    STT_SERVICE_ENVS,
    credential_backend,
    credential_reference_input,
    database_reference,
    provider_credential_status,
    redact_inline_secrets,
    stt_service_credential_key,
)
from .database import Database
from .settings_identity import settings_workspace_id, workspace_setting_in_workspace
from .speech_catalogue_projection import approved_stt_records


def _service_id(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _enrich_credential(
    database: Database,
    paths: DataPaths,
    source: dict[str, Any],
    *,
    workspace_id: str,
) -> dict[str, Any]:
    record = deepcopy(source)
    service_id = _service_id(record.get("id"))
    expected_environment = STT_SERVICE_ENVS.get(service_id, "")
    if expected_environment:
        record["api_key_env"] = expected_environment
    else:
        record.pop("api_key_env", None)
    reference = str(record.get("secret_ref") or "").strip()
    if not reference:
        reference = database_reference(stt_service_credential_key(service_id))
    status = provider_credential_status(
        database,
        paths,
        service_id,
        reference,
        fallback_environment_variable=expected_environment,
        shared=False,
        workspace_id=workspace_id,
    )
    record.update(status)
    record["credential_backend"] = credential_backend(reference)
    record["credential_reference"] = credential_reference_input(reference)
    return redact_inline_secrets(record)


def stt_catalogue_settings_in_session(
    session: Session, *, workspace_id: str
) -> tuple[dict[str, Any], int]:
    workspace_id = settings_workspace_id(session, workspace_id=workspace_id)
    setting = workspace_setting_in_workspace(session, workspace_id, "services.stt")
    return (
        deepcopy(setting.value_json)
        if setting is not None and isinstance(setting.value_json, dict)
        else {"provider_configs": []},
        int(setting.revision) if setting is not None else 0,
    )


def stt_catalogue_snapshot(
    database: Database,
    paths: DataPaths,
    *,
    session_id: str | None = None,
    workspace_id: str | None = None,
    settings_snapshot: tuple[dict[str, Any], int] | None = None,
    approved_only: bool = False,
) -> tuple[dict[str, Any], int]:
    """Return profiles plus materialized connections without exposing secrets."""

    if settings_snapshot is not None:
        if workspace_id is None:
            raise ValueError("Captured STT settings require a workspace.")
        value, revision = deepcopy(settings_snapshot)
    else:
        with database.snapshot_session() as session:
            workspace_id = settings_workspace_id(
                session, session_id=session_id, workspace_id=workspace_id
            )
            value, revision = stt_catalogue_settings_in_session(session, workspace_id=workspace_id)
    if approved_only:
        records = approved_stt_records(value.get("provider_configs", []))
        return {
            "services": records,
            "profiles": [],
            "value": {},
            "revision": revision,
            "default_service": str(
                value.get("service")
                or value.get("stt_engine")
                or (records[0].get("id") if records else "")
            ),
        }, revision
    records = [
        _enrich_credential(database, paths, item, workspace_id=workspace_id)
        for item in value.get("provider_configs", [])
        if isinstance(item, dict)
    ]
    profiles = [
        _enrich_credential(database, paths, item, workspace_id=workspace_id)
        for item in list_stt_provider_profiles()
    ]
    safe_value = redact_inline_secrets(value)
    safe_value["provider_configs"] = records
    default_service = str(
        safe_value.get("service")
        or safe_value.get("stt_engine")
        or (records[0].get("id") if records else "")
    )
    return (
        {
            "services": records,
            "profiles": profiles,
            "value": safe_value,
            "revision": revision,
            "default_service": default_service,
        },
        revision,
    )
