import uuid
from datetime import datetime

from sqlalchemy import Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from gj_common.db import Base, Timestamps, UUIDPk
from gj_common.events import OutboxEvent, ProcessedEvent  # noqa: F401


class Notification(UUIDPk, Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_recipient", "recipient_type", "recipient_id", "created_at"),)

    tenant_id: Mapped[uuid.UUID | None]
    recipient_type: Mapped[str] = mapped_column(String(10))  # user | partner | tenant
    recipient_id: Mapped[uuid.UUID]
    channel: Mapped[str] = mapped_column(String(10))  # in_app | push | email | sms | whatsapp
    address: Mapped[str | None] = mapped_column(String(254))
    template: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str]
    data: Mapped[dict] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(10))  # sent | failed | skipped
    charged_to: Mapped[str | None] = mapped_column(String(80))
    error: Mapped[str | None]
    read_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DeviceToken(UUIDPk, Base):
    __tablename__ = "device_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    tenant_id: Mapped[uuid.UUID]
    platform: Mapped[str] = mapped_column(String(10))  # ios | android | web
    token: Mapped[str] = mapped_column(String(512), unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Campaign(UUIDPk, Timestamps, Base):
    __tablename__ = "campaigns"

    tenant_id: Mapped[uuid.UUID] = mapped_column(index=True)
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str]
    channels: Mapped[list]
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued | sending | sent | failed
    recipients: Mapped[int] = mapped_column(default=0)
    created_by: Mapped[uuid.UUID]
