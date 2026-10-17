import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from gj_common.db import Base, Timestamps, UUIDPk
from gj_common.events import OutboxEvent, ProcessedEvent  # noqa: F401


class PatientLink(Base):
    """New marketplace account <-> patient record in the existing system."""

    __tablename__ = "patient_links"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    legacy_patient_id: Mapped[str] = mapped_column(String(64), unique=True)
    linked_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DoctorPartner(Base):
    """Doctors that are also marketplace partners (so their consult fees are settled to them)."""

    __tablename__ = "doctor_partners"

    legacy_doctor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    partner_id: Mapped[uuid.UUID]


class ConsultHold(UUIDPk, Timestamps, Base):
    __tablename__ = "consult_holds"
    __table_args__ = (
        Index("uq_active_slot", "slot_id", unique=True, postgresql_where=text("status IN ('held', 'booked')")),
    )

    doctor_id: Mapped[str] = mapped_column(String(64))
    doctor_name: Mapped[str] = mapped_column(String(200))
    specialty: Mapped[str | None] = mapped_column(String(100))
    partner_id: Mapped[uuid.UUID | None]
    slot_id: Mapped[str] = mapped_column(String(100))
    slot_start: Mapped[datetime]
    slot_end: Mapped[datetime]
    fee: Mapped[Decimal]
    user_id: Mapped[uuid.UUID]
    tenant_id: Mapped[uuid.UUID]
    reference: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(10), default="held")  # held | booked | released
    expires_at: Mapped[datetime]


class ConsultBooking(UUIDPk, Timestamps, Base):
    __tablename__ = "consult_bookings"

    sub_order_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    hold_id: Mapped[uuid.UUID]
    legacy_booking_id: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    tenant_id: Mapped[uuid.UUID]
    doctor_id: Mapped[str] = mapped_column(String(64))
    doctor_name: Mapped[str] = mapped_column(String(200))
    slot_start: Mapped[datetime]
    video_url: Mapped[str | None]
    status: Mapped[str] = mapped_column(String(20), default="booked")  # booked | completed | no_show | cancelled
