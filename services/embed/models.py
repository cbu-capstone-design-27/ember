"""SQLAlchemy models for the tables the embed worker touches (EMBER-77, EMBER-166).

It maps docs/ingestion/architecture.md (Storage) and nothing more. The dbmate migrations in
infra/k8s/apps/postgres/migrations own the schema, so never call create_all on it.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity, Text, UniqueConstraint, cast, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

DIMENSION = 1024  # ADR 0001; services/embedding/README.md, Model identity


class Vector(UserDefinedType):
    """pgvector's vector, sent and read as its text form ('[1,2,3]') so asyncpg needs no codec for it."""

    cache_ok = True

    def get_col_spec(self, **kw) -> str:
        return f"vector({DIMENSION})"

    def bind_processor(self, dialect):
        return lambda value: None if value is None else "[" + ",".join(map(str, value)) + "]"

    def bind_expression(self, bindvalue):
        return cast(cast(bindvalue, Text), self)

    def column_expression(self, column):
        return cast(column, Text)

    def result_processor(self, dialect, coltype):
        return lambda value: None if value is None else json.loads(value)


class Base(DeclarativeBase):
    pass


class SourceRecord(Base):
    __tablename__ = "source_records"
    __table_args__ = (UniqueConstraint("source", "tenant", "object_id", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    tenant: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    object_id: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    record: Mapped[dict] = mapped_column(JSONB)
    raw_event_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("raw_events.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    embedding: Mapped[list[float] | None] = mapped_column(Vector())
    embed_text: Mapped[str | None] = mapped_column(Text)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GraphNode(Base):
    """graph.nodes, the columns the worker reads and writes (EMBER-164's table)."""

    __tablename__ = "nodes"
    __table_args__ = {"schema": "graph"}

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    embed_text: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector())
    deleted: Mapped[bool] = mapped_column(Boolean)


class GraphEdge(Base):
    """graph.edges, the columns the worker reads and writes (EMBER-164's table)."""

    __tablename__ = "edges"
    __table_args__ = {"schema": "graph"}

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    fact: Mapped[str | None] = mapped_column(Text)
    fact_embedding: Mapped[list[float] | None] = mapped_column(Vector())
