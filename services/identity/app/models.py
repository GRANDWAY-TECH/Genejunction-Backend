import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from gj_common.db import Base, Timestamps, UUIDPk
from gj_common.events import OutboxEvent, ProcessedEvent  # noqa: F401  (registers tables)


class User(UUIDPk, Timestamps, Base):
    __tablename__ = "users"

    phone: Mapped[str | None] = mapped_column(String(20), unique=True)
    email: Mapped[str | None] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str | None]
    full_name: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="active")
    # Link to the patient record in the existing (PHP) GeneJunction system.
    legacy_patient_id: Mapped[str | None] = mapped_column(String(64), index=True)
    last_login_at: Mapped[datetime | None]


class UserTenant(Base):
    """A user is one global identity; membership records which brands they use."""

    __tablename__ = "user_tenants"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, index=True)
    joined_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RoleAssignment(UUIDPk, Base):
    __tablename__ = "role_assignments"
    __table_args__ = (UniqueConstraint("user_id", "role", "scope_type", "scope_id", name="uq_role_assignment"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(40))
    scope_type: Mapped[str] = mapped_column(String(20))  # platform | tenant | partner
    scope_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RefreshToken(UUIDPk, Base):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[uuid.UUID]
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SigningKey(Base):
    __tablename__ = "signing_keys"

    kid: Mapped[str] = mapped_column(String(64), primary_key=True)
    private_pem: Mapped[str]
    public_jwk: Mapped[dict]
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
