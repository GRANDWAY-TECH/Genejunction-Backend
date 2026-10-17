import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from gj_common.db import Base, Timestamps, UUIDPk
from gj_common.events import OutboxEvent, ProcessedEvent  # noqa: F401


class Payment(UUIDPk, Timestamps, Base):
    __tablename__ = "payments"
    __table_args__ = (Index("ix_payments_reference", "purpose", "reference_id"),
                      Index("ix_payments_status_expires", "status", "expires_at"))

    purpose: Mapped[str] = mapped_column(String(20))  # order | credit_topup | subscription | wholesale
    reference_id: Mapped[str] = mapped_column(String(64))
    amount: Mapped[Decimal]
    currency: Mapped[str] = mapped_column(String(3), default="MYR")
    # requires_action -> succeeded | failed | cancelled | expired ; succeeded -> partially_refunded | refunded
    status: Mapped[str] = mapped_column(String(20), default="requires_action")
    method: Mapped[str] = mapped_column(String(20))  # fpx | card | ewallet
    provider: Mapped[str] = mapped_column(String(20))
    provider_ref: Mapped[str | None] = mapped_column(String(100), unique=True)
    checkout_url: Mapped[str | None]
    return_url: Mapped[str | None]
    payer_type: Mapped[str] = mapped_column(String(20))  # user | partner | tenant
    payer_id: Mapped[str] = mapped_column(String(64), index=True)
    tenant_id: Mapped[uuid.UUID | None]
    description: Mapped[str] = mapped_column(String(300))
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    expires_at: Mapped[datetime]
    paid_at: Mapped[datetime | None]
    refunded_amount: Mapped[Decimal] = mapped_column(default=Decimal("0.00"))
    failure_reason: Mapped[str | None]


class Refund(UUIDPk, Base):
    __tablename__ = "refunds"

    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"), index=True)
    amount: Mapped[Decimal]
    reason: Mapped[str]
    reference: Mapped[str] = mapped_column(String(120), unique=True)  # idempotency
    status: Mapped[str] = mapped_column(String(20), default="pending")
    provider_ref: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class WebhookEvent(UUIDPk, Base):
    __tablename__ = "webhook_events"

    provider: Mapped[str] = mapped_column(String(20))
    provider_event_id: Mapped[str] = mapped_column(String(120), unique=True)
    payload: Mapped[dict]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
