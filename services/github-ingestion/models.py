"""SQLAlchemy models for the tables enrich:github touches (EMBER-65).

They map docs/ingestion/architecture.md (Storage) and nothing more. The dbmate migrations in
infra/k8s/apps/postgres/migrations own the schema, so never call create_all on these.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class RawEvent(Base):
    __tablename__ = "raw_events"
    __table_args__ = (UniqueConstraint("source", "delivery_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    delivery_id: Mapped[str] = mapped_column(Text)
    account_id: Mapped[str | None] = mapped_column(Text)
    tenant: Mapped[str | None] = mapped_column(Text)
    event_name: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    envelope: Mapped[dict] = mapped_column(JSONB)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # EMBER-63. Not read yet: enrich still computes ordering_value() until intake fills the column
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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


class RateLimit(Base):
    __tablename__ = "rate_limits"

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant: Mapped[str] = mapped_column(Text, primary_key=True)
    bucket: Mapped[str] = mapped_column(Text, primary_key=True)
    next_allowed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    remaining: Mapped[int | None] = mapped_column(Integer)
