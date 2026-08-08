"""Database extension and PostgreSQL configuration shared by the app and Alembic."""

from urllib.parse import urlsplit

from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


def normalize_database_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("DATABASE_URL is required")
    value = value.strip()
    if value.startswith(("postgres://", "postgresql://")):
        prefix = "postgres://" if value.startswith("postgres://") else "postgresql://"
        value = "postgresql+psycopg://" + value[len(prefix) :]
    if urlsplit(value).scheme not in {"postgresql", "postgresql+psycopg"}:
        raise RuntimeError("DATABASE_URL must use PostgreSQL")
    return value
