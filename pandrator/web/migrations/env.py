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
    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        if is_sqlite:
            # Alembic's SQLite batch mode replaces a table by copying it and
            # dropping the original.  Enforced inbound FKs can either reject
            # that DROP or cascade-delete dependent rows.  Disable enforcement
            # before the migration transaction, then validate the finished
            # graph before allowing it to commit.
            connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
            connection.commit()
            if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one():
                raise RuntimeError(
                    "SQLite foreign-key enforcement could not be suspended "
                    "for batch migrations."
                )
            connection.commit()

        try:
            # SQLAlchemy 2 starts an outer transaction for the first statement.
            # Keep Alembic's version write and SQLite DDL in that same explicit
            # transaction so a failed migration rolls back as one unit.
            with connection.begin():
                context.configure(
                    connection=connection,
                    target_metadata=target_metadata,
                    render_as_batch=True,
                )
                with context.begin_transaction():
                    context.run_migrations()
                if is_sqlite:
                    violations = connection.exec_driver_sql(
                        "PRAGMA foreign_key_check"
                    ).fetchmany(10)
                    if violations:
                        raise RuntimeError(
                            "SQLite foreign-key validation failed after "
                            f"migration: {violations!r}"
                        )
        finally:
            if is_sqlite:
                connection.exec_driver_sql("PRAGMA foreign_keys = ON")
                connection.commit()
                if not connection.exec_driver_sql(
                    "PRAGMA foreign_keys"
                ).scalar_one():
                    raise RuntimeError(
                        "SQLite foreign-key enforcement could not be restored "
                        "after migrations."
                    )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
