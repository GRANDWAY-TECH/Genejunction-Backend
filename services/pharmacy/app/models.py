import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from gj_common.db import Base, Timestamps, UUIDPk
from gj_common.events import OutboxEvent, ProcessedEvent  # noqa: F401


class Fulfilment(UUIDPk, Timestamps, Base):
    """A pharmacy's work item for one sub-order: Rx check, pack, dispatch, deliver."""

    __tablename__ = "fulfilments"
    __table_args__ = (Index("ix_fulfilments_partner_status", "partner_id", "status", "created_at"),)

    sub_order_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    order_id: Mapped[uuid.UUID]
    order_number: Mapped[str] = mapped_column(String(32))
    partner_id: Mapped[uuid.UUID]
    tenant_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID]
    channel: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(30), default="placed")
    method: Mapped[str] = mapped_column(String(20))  # delivery | pickup
    items: Mapped[list]
    total: Mapped[str] = mapped_column(String(20))
    delivery_address: Mapped[dict | None]
    contact: Mapped[dict]
    requires_prescription: Mapped[bool]
    prescription_id: Mapped[uuid.UUID | None]
    prescription_document_key: Mapped[str | None]
    rx_status: Mapped[str] = mapped_column(String(20))  # not_required | pending | approved | rejected
    pharmacist_id: Mapped[uuid.UUID | None]
    pharmacist_reg_no: Mapped[str | None] = mapped_column(String(40))
    rx_notes: Mapped[str | None]
    courier: Mapped[str | None] = mapped_column(String(30))
    tracking_no: Mapped[str | None] = mapped_column(String(60), unique=True)
    tracking_url: Mapped[str | None]
    rejection_reason: Mapped[str | None]


class FulfilmentLog(UUIDPk, Base):
    __tablename__ = "fulfilment_log"

    fulfilment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fulfilments.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(30))
    actor: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
