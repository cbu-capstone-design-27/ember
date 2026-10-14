"""SQLAlchemy models for the ingestion tables enrich:jira touches (EMBER-66).

They map docs/ingestion/architecture.md (Storage) and nothing more. The dbmate migrations in
infra/k8s/apps/postgres/migrations own the schema, so never call create_all on these.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TIMESTAMPTZ = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


class RawEvent(Base):
    __tablename__ = "raw_events"
    __table_args__ = (
        UniqueConstraint("source", "delivery_id"),
        CheckConstraint("origin in ('webhook', 'reconcile', 'backfill')"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    delivery_id: Mapped[str] = mapped_column(Text)
    account_id: Mapped[Optional[str]] = mapped_column(Text)
    tenant: Mapped[Optional[str]] = mapped_column(Text)
    event_name: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB)
    enriched_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)


class SourceRecord(Base):
    __tablename__ = "source_records"
    __table_args__ = (UniqueConstraint("source", "tenant", "object_id", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    tenant: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    object_id: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    record: Mapped[dict[str, Any]] = mapped_column(JSONB)
    raw_event_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("raw_events.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())


class RateLimit(Base):
    __tablename__ = "rate_limits"

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant: Mapped[str] = mapped_column(Text, primary_key=True)
    bucket: Mapped[str] = mapped_column(Text, primary_key=True)
    next_allowed_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    remaining: Mapped[Optional[int]] = mapped_column(Integer)
