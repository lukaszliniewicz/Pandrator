"""Let provider catalogues retain inactive models without cluttering selectors."""

import copy
import json

from alembic import op
import sqlalchemy as sa


revision = "0011_active_provider_models"
down_revision = "0010_named_generation_runs"
branch_labels = None
depends_on = None

_SHARED_PROVIDERS = {"openai", "gemini", "vertex_ai"}


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}


def _copy_credential(connection, credentials, old_key: str, new_key: str, label: str) -> bool:
    if old_key == new_key:
        return True
    existing = connection.execute(
        sa.select(credentials).where(credentials.c.key == new_key)
    ).mappings().first()
    if existing is not None:
        return True
    if not old_key:
        return False
    source = connection.execute(
        sa.select(credentials).where(credentials.c.key == old_key)
    ).mappings().first()
    if source is None:
        return False
    connection.execute(
        credentials.insert().values(
            key=new_key,
            label=label,
            secret_value=source["secret_value"],
            created_at=source["created_at"],
            updated_at=source["updated_at"],
        )
    )
    return True


def _consolidate_shared_credentials(connection) -> None:
    inspector = sa.inspect(connection)
    required = {"providers", "stored_credentials", "app_settings"}
    if not required.issubset(set(inspector.get_table_names())):
        return
    metadata = sa.MetaData()
    providers = sa.Table("providers", metadata, autoload_with=connection)
    credentials = sa.Table("stored_credentials", metadata, autoload_with=connection)
    settings = sa.Table("app_settings", metadata, autoload_with=connection)

    for provider in connection.execute(sa.select(providers)).mappings():
        provider_key = str(provider.get("provider_key") or "").strip().lower().replace("-", "_")
        if provider_key not in _SHARED_PROVIDERS:
            continue
        options = _json_object(provider.get("options_json"))
        profile_id = str(options.get("profile_id") or "").strip().lower()
        if options.get("is_custom") or profile_id in {"custom-openai", "lm-studio", "ollama"}:
            continue
        reference = str(provider.get("secret_ref") or "")
        old_key = reference[3:] if reference.startswith("db:") else ""
        shared_key = f"shared:{provider_key}"
        if _copy_credential(connection, credentials, old_key, shared_key, f"{provider['label']} shared credentials"):
            connection.execute(
                providers.update()
                .where(providers.c.id == provider["id"])
                .values(secret_ref=f"db:{shared_key}")
            )

    setting_rows = connection.execute(
        sa.select(settings).where(settings.c.key.in_(["services.tts", "defaults.tts"]))
    ).mappings()
    for setting in setting_rows:
        value = _json_object(setting.get("value_json"))
        records = value.get("provider_configs")
        if not isinstance(records, list):
            continue
        changed = False
        for record in records:
            if not isinstance(record, dict):
                continue
            service_id = str(record.get("id") or record.get("provider") or "").strip().lower().replace("-", "_")
            if service_id not in _SHARED_PROVIDERS:
                continue
            reference = str(record.get("secret_ref") or "")
            old_key = reference[3:] if reference.startswith("db:") else ""
            shared_key = f"shared:{service_id}"
            if _copy_credential(connection, credentials, old_key, shared_key, f"{record.get('name') or service_id} shared credentials"):
                new_reference = f"db:{shared_key}"
                if reference != new_reference:
                    record["secret_ref"] = new_reference
                    changed = True
        if changed:
            connection.execute(
                settings.update()
                .where(settings.c.key == setting["key"])
                .values(value_json=value)
            )


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("provider_models")}
    if "is_active" not in columns:
        with op.batch_alter_table("provider_models") as batch:
            batch.add_column(sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    _consolidate_shared_credentials(op.get_bind())


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("provider_models")}
    if "is_active" in columns:
        with op.batch_alter_table("provider_models") as batch:
            batch.drop_column("is_active")
