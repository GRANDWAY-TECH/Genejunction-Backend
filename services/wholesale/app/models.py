import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gj_common.db import Base, Timestamps, UUIDPk
from gj_common.events import OutboxEvent, ProcessedEvent  # noqa: F401


class PurchaseOrder(UUIDPk, Timestamps, Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (Index("ix_po_buyer", "buyer_partner_id", "created_at"),
                      Index("ix_po_stockist", "stockist_partner_id", "status"))

    number: Mapped[str] = mapped_column(String(32), unique=True)
    buyer_partner_id: Mapped[uuid.UUID]
    stockist_partner_id: Mapped[uuid.UUID]
    stockist_name: Mapped[str | None] = mapped_column(String(200))
    # submitted -> accepted -> shipped -> received ; submitted -> rejected ; submitted|accepted -> cancelled
    status: Mapped[str] = mapped_column(String(20), default="submitted")
    payment_terms: Mapped[str] = mapped_column(String(10))  # prepaid | net30
    payment_status: Mapped[str] = mapped_column(String(10), default="unpaid")  # unpaid | paid | refunded
    payment_id: Mapped[str | None] = mapped_column(String(64))
    subtotal: Mapped[Decimal]
    total: Mapped[Decimal]
    delivery_address: Mapped[dict]
    notes: Mapped[str | None]
    tracking: Mapped[dict | None]
    created_by: Mapped[uuid.UUID]
    received_at: Mapped[datetime | None]

    lines: Mapped[list["POLine"]] = relationship(back_populates="po", lazy="selectin", cascade="all, delete-orphan")


class POLine(UUIDPk, Base):
    __tablename__ = "purchase_order_lines"

    po_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("purchase_orders.id", ondelete="CASCADE"), index=True)
    offer_id: Mapped[str] = mapped_column(String(64))
    product_id: Mapped[str] = mapped_column(String(64))
    sku: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(300))
    qty: Mapped[int]
    unit_price: Mapped[Decimal]
    line_total: Mapped[Decimal]

    po: Mapped[PurchaseOrder] = relationship(back_populates="lines")
