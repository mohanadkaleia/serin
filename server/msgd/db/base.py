"""Declarative base with a fixed constraint/index naming convention.

The naming convention is load-bearing for Alembic: without it, unnamed
constraints get server-assigned names and every ``alembic check`` /
``compare_metadata`` run churns non-deterministically. Pinning it makes model
metadata and generated migrations stable (ENG-63 D-3).
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Common declarative base for all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
