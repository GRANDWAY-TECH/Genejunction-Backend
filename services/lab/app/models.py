import uuid
from datetime import date, datetime

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from gj_common.db import Base, Timestamps, UUIDPk
from gj_common.events import OutboxEvent, ProcessedEvent  # noqa: F401


class LabBooking(UUIDPk, Timestamps, Base):
    __tablename__ = "lab_bookings"
    __table_args__ = (Index("ix_lab_bookings_partner_status", "partner_id", "status", "collection_date"),)

    sub_order_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    order_id: Mapped[uuid.UUID]
    order_number: Mapped[str] = mapped_column(String(32))
    partner_id: Mapped[uuid.UUID]
    partner_name: Mapped[str | None] = mapped_column(String(200))
    tenant_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    channel: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(30), default="placed")
    collection_type: Mapped[str] = mapped_column(String(20))  # home_collection | walk_in
    collection_date: Mapped[date]
    collection_slot: Mapped[str] = mapped_column(String(11))
    address: Mapped[dict | None]
    contact: Mapped[dict]
    tests: Mapped[list]
    phlebotomist: Mapped[dict | None]
    report_key: Mapped[str | None]
    report_uploaded_at: Mapped[datetime | None]
    ai_summary: Mapped[str | None]
    ai_summary_status: Mapped[str] = mapped_column(String(20), default="none")  # none | done | skipped | failed
    rejection_reason: Mapped[str | None]


class SlotCapacity(Base):
    __tablename__ = "lab_slot_capacity"

    partner_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(primary_key=True)
    slot: Mapped[str] = mapped_column(String(11), primary_key=True)
    capacity: Mapped[int]
    booked: Mapped[int] = mapped_column(default=0)
