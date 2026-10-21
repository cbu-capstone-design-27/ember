"""SQLAlchemy models for the tables intake touches (EMBER-62).

They map docs/ingestion/architecture.md (Storage) and nothing more. The dbmate migrations in
infra/k8s/apps/postgres/migrations own the schema, so never call create_all on these.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Identity, Text, UniqueConstraint, func
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
