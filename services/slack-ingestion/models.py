"""SQLAlchemy models for the enrich:slack worker (EMBER-67).

They match docs/ingestion/architecture.md (Storage), the source of truth for the ingestion
schema. A schema change starts in that doc and a migration under
infra/k8s/apps/postgres/migrations; these models follow it. The worker never creates or
alters tables: the migrations do. test_enrich_db.py checks the models against the schema the
migrations build.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TIMESTAMPTZ = TIMESTAMP(timezone=True)


class Base(DeclarativeBase):
    pass


class RawEvent(Base):
    """One delivery as intake (EMBER-62) stored it. Enrich reads it and marks it enriched."""

    __tablename__ = "raw_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    delivery_id: Mapped[str] = mapped_column(Text)
    account_id: Mapped[str | None] = mapped_column(Text)
    tenant: Mapped[str | None] = mapped_column(Text)
    event_name: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=text("now()"))
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    envelope: Mapped[dict] = mapped_column(JSONB)
    # Set by the enrich worker: null means still to do (EMBER-75)
    enriched_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    # The source's time (EMBER-63), set by whoever writes the row: intake, or seed standing in for it
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("source", "delivery_id"),
        CheckConstraint("origin in ('webhook', 'reconcile', 'backfill')"),
        Index("raw_events_unenriched", "source", "id", postgresql_where=text("enriched_at is null")),
        Index(
            "raw_events_unenriched_occurred_at", "source", "occurred_at", "id", postgresql_where=text("enriched_at is null")
        ),
    )


class SourceRecord(Base):
    """A complete source object at one version: the payload plus what enrich fetched."""

    __tablename__ = "source_records"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    tenant: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    object_id: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    record: Mapped[dict] = mapped_column(JSONB)
    raw_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("raw_events.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=text("now()"))
    # EMBER-77: written by services/embed, never read or written by enrich. The column is pgvector's
    # vector(1024), which asyncpg has no codec for, so it is mapped as text and deferred so it never loads.
    embedding: Mapped[str | None] = mapped_column(Text, deferred=True)
    embed_text: Mapped[str | None] = mapped_column(Text)
    embedded_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)

    __table_args__ = (UniqueConstraint("source", "tenant", "object_id", "version"),)


class RateLimit(Base):
    """Shared rate-limit state. Slack: one row per (tenant, "<team_id>/<Web API method>") (EMBER-148)."""

    __tablename__ = "rate_limits"

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant: Mapped[str] = mapped_column(Text, primary_key=True)
    bucket: Mapped[str] = mapped_column(Text, primary_key=True)
    next_allowed_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    remaining: Mapped[int | None] = mapped_column(Integer)


class Installation(Base):
    """Which tenant a workspace (account_id = team_id) feeds. The web app writes it (ADR 0010);
    enrich only sets disconnected_at, on app_uninstalled or tokens_revoked (EMBER-148)."""

    __tablename__ = "installations"

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant: Mapped[str] = mapped_column(Text)
    added_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=text("now()"))
    disconnected_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    label: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    connected_by: Mapped[str | None] = mapped_column(Text)


class SourceCredential(Base):
    """A workspace's bot token, sealed by Connect Slack (packages/source-credentials). Read-only here."""

    __tablename__ = "source_credentials"
    __table_args__ = (
        CheckConstraint("kind in ('api', 'webhook')"),
        ForeignKeyConstraint(
            ["source", "account_id"], ["installations.source", "installations.account_id"], ondelete="CASCADE"
        ),
    )

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    key_id: Mapped[str] = mapped_column(Text)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=text("now()"))
