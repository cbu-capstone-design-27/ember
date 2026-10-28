"""SQLAlchemy models for the tables the extract and load workers touch (EMBER-85).

They map docs/ingestion/architecture.md (Storage) and nothing more. The dbmate migrations in
infra/k8s/apps/postgres/migrations own the schema, so never call create_all on them. Columns the
workers never read (source_records' embedding columns) are left out.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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
    raw_event_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Episode(Base):
    __tablename__ = "episodes"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    key: Mapped[str] = mapped_column(Text, unique=True)
    tenant: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    text: Mapped[str] = mapped_column(Text)
    attrs: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    source_record_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_records.id", ondelete="CASCADE"))
    extractor_version: Mapped[int] = mapped_column(Integer)
    loaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def async_url(database_url: str):
    """DATABASE_URL (libpq form, as Secret postgres-app's uri) for SQLAlchemy's asyncpg driver."""
    url = make_url(database_url).set(drivername="postgresql+asyncpg")
    if "sslmode" in url.query:  # libpq's name; asyncpg takes the same values as `ssl`
        url = url.difference_update_query(["sslmode"]).update_query_dict({"ssl": url.query["sslmode"]})
    return url


def engine(database_url: str, connections: int) -> AsyncEngine:
    return create_async_engine(async_url(database_url), pool_size=connections, max_overflow=2, pool_pre_ping=True)
