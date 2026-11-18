"""SQLAlchemy models for the ingestion tables, shared by every component in this image.

They map docs/ingestion/architecture.md (Storage) and nothing more. The dbmate migrations in
services/backend/migrations own the schema, so never call create_all on these. A table is
mapped here once a component uses it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Identity, Integer,
                        LargeBinary, Text, UniqueConstraint, func)
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
    # EMBER-63. Intake sets it from the payload; left out, the default makes it received_at
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Installation(Base):
    __tablename__ = "installations"

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant: Mapped[str] = mapped_column(Text)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # EMBER-130 (ADR 0010). The web app writes these; intake reads only disconnected_at
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    label: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    connected_by: Mapped[str | None] = mapped_column(Text)


class SourceCredential(Base):
    """Sealed with packages/source-credentials (ADR 0010); this model never sees plaintext."""

    __tablename__ = "source_credentials"
    __table_args__ = (
        CheckConstraint("kind in ('api', 'webhook')"),
        ForeignKeyConstraint(["source", "account_id"], ["installations.source", "installations.account_id"],
                             ondelete="CASCADE"),
    )

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    key_id: Mapped[str] = mapped_column(Text)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceRecord(Base):
    """What enrich writes: one row per object version (architecture.md, Storage)."""

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
    # EMBER-77: embed's columns; enrich never reads or writes them. The column is pgvector's
    # vector(1024), which asyncpg has no codec for, so it is mapped as text and deferred so it never loads
    embedding: Mapped[str | None] = mapped_column(Text, deferred=True)
    embed_text: Mapped[str | None] = mapped_column(Text)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RateLimit(Base):
    """A source API's shared rate limit per (tenant, bucket): every replica waits on the same row."""

    __tablename__ = "rate_limits"

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant: Mapped[str] = mapped_column(Text, primary_key=True)
    bucket: Mapped[str] = mapped_column(Text, primary_key=True)
    next_allowed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    remaining: Mapped[int | None] = mapped_column(Integer)
