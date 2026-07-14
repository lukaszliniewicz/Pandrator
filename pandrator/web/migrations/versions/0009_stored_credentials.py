"""Add a write-only database credential store and scrub legacy inline secrets."""

from __future__ import annotations

import json
import re
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "0009_stored_credentials"
down_revision = "0008_generation_paragraph_boundaries"
branch_labels = None
depends_on = None


_SECRET_FIELD = re.compile(r"(^|_)(api_key|password|secret|credential)s?$", re.IGNORECASE)


def _is_secret_field(key: object) -> bool:
    normalized = re.sub(r"[-\s]+", "_", str(key or "").strip().lower())
    return bool(_SECRET_FIELD.search(normalized)) or normalized.endswith(("_token", "_private_key", "_secret_key")) or normalized in {
        "access_token",
        "refresh_token",
        "azure_ad_token",
        "hf_token",
        "auth_token",
        "bearer_token",
        "token",
        "private_key",
        "secret_key",
        "subscription_key",
        "authorization",
        "proxy_authorization",
    }


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items() if not _is_secret_field(key)}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _first_named_value(value: Any, field_name: str) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).strip().lower() == field_name:
                resolved = str(item or "").strip()
                if resolved:
                    return resolved
            nested = _first_named_value(item, field_name)
            if nested:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = _first_named_value(item, field_name)
            if nested:
                return nested
    return ""


def _load(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _credential_key(prefix: str, raw_id: object) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", str(raw_id or "").strip().lower()).strip("-")
    if prefix == "tts":
        normalized = normalized.replace("-", "_")
    return f"{prefix}:{normalized or 'default'}"


def _upsert_credential(connection, key: str, label: str, secret: object) -> None:
    value = str(secret or "").strip()
    if not value:
        return
    connection.execute(
        sa.text(
            "INSERT INTO stored_credentials (key, label, secret_value, created_at, updated_at) "
            "VALUES (:key, :label, :secret, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET label = excluded.label, "
            "secret_value = excluded.secret_value, updated_at = CURRENT_TIMESTAMP"
        ),
        {"key": key, "label": label, "secret": value},
    )


def _migrate_tts_settings(connection) -> None:
    inspector = sa.inspect(connection)
    if "app_settings" not in inspector.get_table_names():
        return
    rows = connection.execute(sa.text("SELECT key, value_json FROM app_settings")).fetchall()
    for setting_key, raw_value in rows:
        payload = _load(raw_value)
        if not isinstance(payload, dict):
            continue
        deepl_key = _first_named_value(payload, "deepl_api_key")
        if deepl_key:
            _upsert_credential(connection, "aux:deepl", "DeepL API key", deepl_key)
        providers = payload.get("provider_configs") if str(setting_key).endswith(".tts") else None
        if isinstance(providers, list):
            for record in providers:
                if not isinstance(record, dict):
                    continue
                inline_key = record.get("api_key")
                if str(inline_key or "").strip():
                    service_id = record.get("id") or record.get("name") or record.get("provider")
                    key = _credential_key("tts", service_id)
                    _upsert_credential(connection, key, f"{service_id} API key", inline_key)
                    record["secret_ref"] = f"db:{key}"
                record.pop("api_key", None)
                record.pop("clear_api_key", None)
        scrubbed = _scrub(payload)
        # Keep the references added above; they are identifiers, not secrets.
        if isinstance(providers, list):
            scrubbed["provider_configs"] = [_scrub(record) for record in providers]
        connection.execute(
            sa.text("UPDATE app_settings SET value_json = :value WHERE key = :key"),
            {"value": _dump(scrubbed), "key": setting_key},
        )


def _migrate_provider_options(connection) -> None:
    inspector = sa.inspect(connection)
    if "providers" not in inspector.get_table_names():
        return
    rows = connection.execute(sa.text("SELECT id, label, secret_ref, options_json FROM providers")).fetchall()
    for provider_id, label, secret_ref, raw_options in rows:
        options = _load(raw_options)
        if not isinstance(options, dict):
            continue
        inline_key = options.get("api_key")
        next_ref = secret_ref
        if str(inline_key or "").strip():
            key = _credential_key("llm", provider_id)
            _upsert_credential(connection, key, f"{label} API key", inline_key)
            next_ref = f"db:{key}"
        scrubbed = _scrub(options)
        connection.execute(
            sa.text("UPDATE providers SET secret_ref = :secret_ref, options_json = :options WHERE id = :id"),
            {"secret_ref": next_ref, "options": _dump(scrubbed), "id": provider_id},
        )


def _scrub_json_columns(connection) -> None:
    inspector = sa.inspect(connection)
    skip = {"app_settings", "providers", "stored_credentials"}
    for table_name in inspector.get_table_names():
        if table_name in skip:
            continue
        columns = inspector.get_columns(table_name)
        json_columns = [column["name"] for column in columns if "JSON" in str(column.get("type") or "").upper()]
        primary_keys = inspector.get_pk_constraint(table_name).get("constrained_columns") or []
        if not primary_keys:
            continue
        for column_name in json_columns:
            selected_columns = ", ".join(f'"{key}"' for key in primary_keys)
            rows = connection.execute(
                sa.text(f'SELECT {selected_columns}, "{column_name}" FROM "{table_name}"')
            ).fetchall()
            for row in rows:
                key_values = tuple(row[: len(primary_keys)])
                raw_value = row[len(primary_keys)]
                payload = _load(raw_value)
                if not isinstance(payload, (dict, list)):
                    continue
                scrubbed = _scrub(payload)
                if scrubbed == payload:
                    continue
                where_clause = " AND ".join(
                    f'"{key}" = :pk_{index}' for index, key in enumerate(primary_keys)
                )
                parameters = {
                    **{f"pk_{index}": item for index, item in enumerate(key_values)},
                    "value": _dump(scrubbed),
                }
                connection.execute(
                    sa.text(
                        f'UPDATE "{table_name}" SET "{column_name}" = :value '
                        f"WHERE {where_clause}"
                    ),
                    parameters,
                )


def upgrade() -> None:
    connection = op.get_bind()
    if "stored_credentials" not in sa.inspect(connection).get_table_names():
        op.create_table(
            "stored_credentials",
            sa.Column("key", sa.String(length=255), nullable=False),
            sa.Column("label", sa.String(length=160), nullable=False),
            sa.Column("secret_value", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
            sa.PrimaryKeyConstraint("key"),
        )
    _migrate_tts_settings(connection)
    _migrate_provider_options(connection)
    _scrub_json_columns(connection)


def downgrade() -> None:
    if "stored_credentials" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("stored_credentials")
