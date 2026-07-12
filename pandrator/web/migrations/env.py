from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from pandrator.web.models import Base


config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    # SQLAlchemy 2 starts an outer transaction for the first statement.  Use
    # ``begin`` here so Alembic's version-table writes are committed together
    # with SQLite's DDL instead of being rolled back when the connection closes.
    with connectable.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

