import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gj_common.db import Base, Timestamps, UUIDPk
from gj_common.events import OutboxEvent, ProcessedEvent  # noqa: F401

DEFAULT_BRANDING = {
    "app_name": "GeneJunction",
    "logo_url": None,
    "primary_color": "#0f766e",
    "secondary_color": "#dcf2ee",
    "support_email": "support@genejunction.com",
    "support_phone": None,
}
DEFAULT_FEATURES = {"consult": True, "lab": True, "pharmacy": True, "health_folio": True}


class Tenant(UUIDPk, Timestamps, Base):
    __tablename__ = "tenants"

    slug: Mapped[str] = mapped_column(String(63), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(20))  # platform | white_label
    status: Mapped[str] = mapped_column(String(20), default="onboarding")  # onboarding | active | suspended
    clinic_group_partner_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    plan_code: Mapped[str | None] = mapped_column(String(40))
    branding: Mapped[dict] = mapped_column(default=lambda: dict(DEFAULT_BRANDING))
    features: Mapped[dict] = mapped_column(default=lambda: dict(DEFAULT_FEATURES))
    mobile_app: Mapped[dict] = mapped_column(default=dict)  # bundle ids, store links for the branded build

    domains: Mapped[list["TenantDomain"]] = relationship(back_populates="tenant", lazy="selectin", cascade="all, delete-orphan")


class TenantDomain(UUIDPk, Timestamps, Base):
    __tablename__ = "tenant_domains"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    domain: Mapped[str] = mapped_column(String(253), unique=True)
    verified: Mapped[bool] = mapped_column(default=False)
    verification_token: Mapped[str] = mapped_column(String(64))
    is_primary: Mapped[bool] = mapped_column(default=False)

    tenant: Mapped[Tenant] = relationship(back_populates="domains")


class NetworkMember(UUIDPk, Timestamps, Base):
    """A provider in a tenant's network. Marketplace partners join a white-label network,
    and a clinic group's own providers can optionally also be listed on the GeneJunction marketplace."""

    __tablename__ = "network_members"
    __table_args__ = (
        UniqueConstraint("tenant_id", "partner_id", name="uq_network_partner"),
        UniqueConstraint("tenant_id", "legacy_doctor_id", name="uq_network_doctor"),
        Index("ix_network_members_listed", "member_type", postgresql_where=text("listed_on_marketplace")),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    member_type: Mapped[str] = mapped_column(String(20))  # pharmacy | lab | doctor | stockist
    partner_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    legacy_doctor_id: Mapped[str | None] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    listed_on_marketplace: Mapped[bool] = mapped_column(default=False)
    commission_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    status: Mapped[str] = mapped_column(String(20), default="active")
