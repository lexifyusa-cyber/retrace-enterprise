"""Dialect-portable SQLAlchemy expressions."""

from sqlalchemy.sql.elements import ColumnElement


def json_bool_true(json_column, key: str) -> ColumnElement[bool]:
    """Return a portable boolean JSON predicate.

    SQLAlchemy compiles JSON index/as_boolean for SQLite and PostgreSQL,
    avoiding raw SQLite-only json_extract(...) text fragments.
    """
    return json_column[key].as_boolean().is_(True)
