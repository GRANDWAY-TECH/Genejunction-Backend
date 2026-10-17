import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from gj_common.db import Base, Timestamps, UUIDPk
from gj_common.events import OutboxEvent, ProcessedEvent  # noqa: F401

PARTNER_TYPES = ("pharmacy", "lab", "stockist", "doctor", "clinic_group")


class Partner(UUIDPk, Timestamps, Base):
    __tablename__ = "partners"

    type: Mapped[str] = mapped_column(String(20), index=True)
    legal_name: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str] = mapped_column(String(200))
    registration_no: Mapped[str] = mapped_column(String(50))  # SSM number
    licence_no: Mapped[str | None] = mapped_column(String(80))
    licence_expiry: Mapped[datetime | None]
    # pending_documents -> under_review -> verified | rejected ; verified -> suspended
    status: Mapped[str] = mapped_column(String(30), default="pending_documents", index=True)
    owner_tenant_id: Mapped[uuid.UUID | None] = mapped_column(index=True)  # clinic-group-owned provider
    legacy_doctor_id: Mapped[str | None] = mapped_column(String(64))  # doctors live in the existing system
    contact_email: Mapped[str] = mapped_column(String(254))
    contact_phone: Mapped[str] = mapped_column(String(20))
    address: Mapped[dict] = mapped_column(default=dict)
    latitude: Mapped[float | None]
    longitude: Mapped[float | None]
    service_radius_km: Mapped[int] = mapped_column(default=10)
    operating_hours: Mapped[dict] = mapped_column(default=dict)
    bank_name: Mapped[str | None] = mapped_column(String(100))
    bank_account_name: Mapped[str | None] = mapped_column(String(200))
    bank_account_no_enc: Mapped[str | None]
    bank_account_last4: Mapped[str | None] = mapped_column(String(4))
    rejection_reason: Mapped[str | None]
    verified_at: Mapped[datetime | None]
    verified_by: Mapped[uuid.UUID | None]
    created_by: Mapped[uuid.UUID]


class PartnerDocument(UUIDPk, Base):
    __tablename__ = "partner_documents"

    partner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("partners.id", ondelete="CASCADE"), index=True)
    doc_type: Mapped[str] = mapped_column(String(40))
    file_name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    s3_key: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(20), default="uploaded")  # uploaded | accepted | rejected
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PartnerUser(Base):
    __tablename__ = "partner_users"
    __table_args__ = (UniqueConstraint("partner_id", "user_id"),)

    partner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("partners.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, index=True)
    role: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ReviewLog(UUIDPk, Base):
    __tablename__ = "partner_review_log"

    partner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("partners.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(30))
    actor_id: Mapped[uuid.UUID]
    note: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
