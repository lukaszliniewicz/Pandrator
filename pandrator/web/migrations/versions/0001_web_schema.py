"""Initial authoritative web schema."""

from alembic import op
from sqlalchemy import MetaData

from pandrator.web.models import Base

revision = "0001_web_schema"
down_revision = None
branch_labels = None
depends_on = None

# Later ownership migrations validate this nullable expansion shape before
# contracting it in 0075. Keep the bootstrap independent of current ORM
# nullability without changing the earlier adoption/backfill contracts.
_EXPANDED_OWNERSHIP_ROOTS = (
    ("providers", "workspace_id"),
    ("sessions", "project_id"),
    ("translation_projects", "project_root_id"),
    ("project_folders", "workspace_id"),
    ("project_folder_memberships", "project_id"),
    ("source_assets", "workspace_id"),
    ("jobs", "workspace_id"),
    ("artifacts", "workspace_id"),
    ("upload_sessions", "workspace_id"),
    ("research_cache_entries", "workspace_id"),
    ("pronunciation_entries", "workspace_id"),
    ("voices", "workspace_id"),
    ("voice_samples", "workspace_id"),
    ("training_runs", "workspace_id"),
    ("usage_events", "workspace_id"),
    ("voice_collections", "workspace_id"),
    ("voice_collection_members", "workspace_id"),
    ("sound_items", "workspace_id"),
    ("sound_variants", "workspace_id"),
)


def upgrade() -> None:
    # 0009 owns this table's historical shape and legacy-secret extraction.
    # Creating its current namespace constraints here would run them before
    # ownership is seeded and before 0009 can migrate legacy inline values.
    deferred_auth_tables = {
        "stored_credentials",
        "user_credentials",
        "browser_auth_sessions",
        "password_reset_grants",
        "job_actors",
        "audit_actors",
        # 0081 owns creation and legacy backfill of credential resource ceilings.
        "api_token_workspace_grants",
        "api_token_project_grants",
        "automation_client_workspace_grants",
        "automation_client_project_grants",
        "browser_auth_workspace_grants",
        "browser_auth_project_grants",
        "automation_code_workspace_grants",
        "automation_code_project_grants",
        # 0082 owns conservative historical private-transcription attribution.
        "quick_transcription_owners",
        # 0083 owns account-targeted workspace invitations.
        "workspace_invitations",
        # 0085 owns saved project speech-reference creation.
        "project_speech_references",
        # 0086 owns lease-fenced output storage reservations.
        "storage_reservations",
        # 0087 owns this bridge table. Its browser_auth_sessions target is created
        # by 0073, so bootstrapping it here would give an early pinned schema an
        # inbound foreign key to a missing table. SQLite then fails cascades that
        # traverse projects instead of reporting a dangling reference.
        "project_link_sessions",
    }
    bootstrap_metadata = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(bootstrap_metadata)
    # 0084 owns the durable provider receipt column, including predecessor shape.
    jobs = bootstrap_metadata.tables["jobs"]
    jobs._columns.remove(jobs.c.external_effect_json)
    # 0087 owns the default-preserving staged-upload attachment flag.
    uploads = bootstrap_metadata.tables["upload_sessions"]
    uploads._columns.remove(uploads.c.attach_to_session)
    for table_name, column_name in _EXPANDED_OWNERSHIP_ROOTS:
        bootstrap_metadata.tables[table_name].c[column_name].nullable = True
    tables = [
        table
        for table in bootstrap_metadata.tables.values()
        if table.name not in deferred_auth_tables
    ]
    bootstrap_metadata.create_all(bind=op.get_bind(), tables=tables)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())

