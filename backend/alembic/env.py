from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from app.config import get_settings
from app.db import create_database_engine
from app.models.tables import metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    provided_connection = config.attributes.get("connection")
    if provided_connection is not None:
        context.configure(
            connection=provided_connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = create_database_engine(get_settings().database_url)
    try:
        with connectable.connect() as connection:
            is_sqlite = connection.dialect.name == "sqlite"
            if is_sqlite:
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
                connection.commit()
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                render_as_batch=True,
            )
            try:
                with context.begin_transaction():
                    context.run_migrations()
            finally:
                if is_sqlite:
                    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
