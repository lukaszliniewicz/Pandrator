"""Cloud speech-recognition profile catalogue for the web control plane."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

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
from .models import AppSetting


def _service_id(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _enrich_credential(
    database: Database,
    paths: DataPaths,
    source: dict[str, Any],
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
    )
    record.update(status)
    record["credential_backend"] = credential_backend(reference)
    record["credential_reference"] = credential_reference_input(reference)
    return redact_inline_secrets(record)


def stt_catalogue_snapshot(
    database: Database,
    paths: DataPaths,
) -> tuple[dict[str, Any], int]:
    """Return profiles plus materialized connections without exposing secrets."""

    with database.session() as session:
        setting = session.get(AppSetting, "services.stt")
        value = (
            deepcopy(setting.value_json)
            if setting is not None and isinstance(setting.value_json, dict)
            else {"provider_configs": []}
        )
        revision = int(setting.revision) if setting is not None else 0
    records = [
        _enrich_credential(database, paths, item)
        for item in value.get("provider_configs", [])
        if isinstance(item, dict)
    ]
    profiles = [
        _enrich_credential(database, paths, item)
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
