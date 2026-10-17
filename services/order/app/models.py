import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gj_common.db import Base, Timestamps, UUIDPk
from gj_common.events import OutboxEvent, ProcessedEvent  # noqa: F401


class Cart(UUIDPk, Timestamps, Base):
    __tablename__ = "carts"
    __table_args__ = (UniqueConstraint("user_id", "tenant_id"),)

    user_id: Mapped[uuid.UUID]
    tenant_id: Mapped[uuid.UUID]
    items: Mapped[list["CartItem"]] = relationship(back_populates="cart", lazy="selectin",
                                                   cascade="all, delete-orphan", order_by="CartItem.created_at")


class CartItem(UUIDPk, Base):
    __tablename__ = "cart_items"
    __table_args__ = (UniqueConstraint("cart_id", "offer_id"),)

    cart_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("carts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # product | consultation
    offer_id: Mapped[str | None] = mapped_column(String(64))
    qty: Mapped[int] = mapped_column(default=1)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | prescription
    prescription_id: Mapped[uuid.UUID | None]
    consultation: Mapped[dict | None]  # {doctor_id, slot_id, ...} for consultation items
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    cart: Mapped[Cart] = relationship(back_populates="items")


class Prescription(UUIDPk, Timestamps, Base):
    """E-prescription received from the existing consultation system."""

    __tablename__ = "prescriptions"

    user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    tenant_id: Mapped[uuid.UUID]
    legacy_consult_id: Mapped[str] = mapped_column(String(64), unique=True)
    doctor_name: Mapped[str | None] = mapped_column(String(200))
    items: Mapped[list]
    document_url: Mapped[str | None]
    status: Mapped[str] = mapped_column(String(20), default="received")  # received | carted | ordered
    issued_at: Mapped[datetime]


class Order(UUIDPk, Timestamps, Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key"),
        Index("ix_orders_user_created", "user_id", "created_at"),
        Index("ix_orders_status_created", "status", "created_at"),
    )

    number: Mapped[str] = mapped_column(String(32), unique=True)
    user_id: Mapped[uuid.UUID]
    tenant_id: Mapped[uuid.UUID] = mapped_column(index=True)
    channel: Mapped[str] = mapped_column(String(10))  # B2C | B2B2C
    # pending_payment -> paid -> completed | partially_cancelled ; cancelled | payment_failed | expired | refunded
    status: Mapped[str] = mapped_column(String(30), default="pending_payment")
    currency: Mapped[str] = mapped_column(String(3), default="MYR")
    subtotal: Mapped[Decimal]
    delivery_fee: Mapped[Decimal]
    total: Mapped[Decimal]
    refunded_amount: Mapped[Decimal] = mapped_column(default=Decimal("0.00"))
    delivery_address: Mapped[dict | None]
    contact: Mapped[dict]
    payment_id: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    paid_at: Mapped[datetime | None]
    cancelled_at: Mapped[datetime | None]
    cancel_reason: Mapped[str | None]

    sub_orders: Mapped[list["SubOrder"]] = relationship(back_populates="order", lazy="selectin",
                                                        cascade="all, delete-orphan", order_by="SubOrder.seq")


class SubOrder(UUIDPk, Timestamps, Base):
    """The part of an order one partner fulfils. Partners only ever see their sub-orders."""

    __tablename__ = "sub_orders"
    __table_args__ = (Index("ix_sub_orders_partner_status", "partner_id", "status", "created_at"),)

    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int]
    partner_id: Mapped[uuid.UUID | None]
    partner_name: Mapped[str | None] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))  # pharmacy | lab | consultation
    fulfilment: Mapped[str] = mapped_column(String(20))  # delivery | pickup | home_collection | walk_in | teleconsult
    status: Mapped[str] = mapped_column(String(30), default="pending_payment")
    subtotal: Mapped[Decimal]
    delivery_fee: Mapped[Decimal]
    total: Mapped[Decimal]
    requires_prescription: Mapped[bool] = mapped_column(default=False)
    prescription_id: Mapped[uuid.UUID | None]
    prescription_document_key: Mapped[str | None]
    schedule: Mapped[dict | None]  # lab collection slot or consultation slot
    tracking: Mapped[dict | None]
    external_ref: Mapped[str | None] = mapped_column(String(64))  # e.g. legacy booking id

    order: Mapped[Order] = relationship(back_populates="sub_orders")
    items: Mapped[list["OrderItem"]] = relationship(back_populates="sub_order", lazy="selectin",
                                                    cascade="all, delete-orphan")


class OrderItem(UUIDPk, Base):
    __tablename__ = "order_items"

    sub_order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sub_orders.id", ondelete="CASCADE"), index=True)
    offer_id: Mapped[str | None] = mapped_column(String(64))
    product_id: Mapped[str | None] = mapped_column(String(64))
    sku: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(300))
    product_type: Mapped[str] = mapped_column(String(20))
    qty: Mapped[int]
    unit_price: Mapped[Decimal]
    line_total: Mapped[Decimal]
    requires_prescription: Mapped[bool] = mapped_column(default=False)
    meta: Mapped[dict] = mapped_column(default=dict)

    sub_order: Mapped[SubOrder] = relationship(back_populates="items")


class OrderStatusHistory(UUIDPk, Base):
    __tablename__ = "order_status_history"

    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    sub_order_id: Mapped[uuid.UUID | None]
    from_status: Mapped[str | None] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30))
    actor: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ExternalOrder(UUIDPk, Base):
    """Orders made in the existing GeneJunction system, synced so patients see one history."""

    __tablename__ = "external_orders"

    user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    legacy_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str]
    total: Mapped[Decimal]
    status: Mapped[str] = mapped_column(String(30))
    ordered_at: Mapped[datetime]
    details: Mapped[dict] = mapped_column(default=dict)
