from __future__ import annotations

import os
from logging.config import fileConfig
from urllib.parse import urlsplit

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.db import db
from app import models  # noqa: F401 - register all model tables

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def database_url() -> str:
    value = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not value:
        raise RuntimeError("DATABASE_URL is required for Alembic")
    value = value.strip()
    if value.startswith(("postgres://", "postgresql://")):
        prefix = "postgres://" if value.startswith("postgres://") else "postgresql://"
        value = "postgresql+psycopg://" + value[len(prefix) :]
    if urlsplit(value).scheme not in {"postgresql", "postgresql+psycopg"}:
        raise RuntimeError("DATABASE_URL must use PostgreSQL")
    return value


target_metadata = db.metadata


def run_migrations_offline() -> None:
    url = database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
