import uuid
from datetime import datetime

from sqlalchemy import Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from gj_common.db import Base


class OutboxEvent(Base):
    """Events written in the same transaction as the state change, then relayed to Kafka."""

    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    topic: Mapped[str]
    key: Mapped[str]
    event_type: Mapped[str]
    payload: Mapped[dict]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    published_at: Mapped[datetime | None]

    __table_args__ = (
        Index("ix_outbox_events_unpublished", "created_at", postgresql_where=text("published_at IS NULL")),
    )


class ProcessedEvent(Base):
    """Consumer-side dedupe: one row per (event, consumer group) makes handlers exactly-once in effect."""

    __tablename__ = "processed_events"

    event_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    consumer: Mapped[str] = mapped_column(primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(server_default=func.now())
