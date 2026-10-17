import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class Branding(BaseModel):
    app_name: str | None = None
    logo_url: str | None = None
    primary_color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    secondary_color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    support_email: EmailStr | None = None
    support_phone: str | None = None


class DomainOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    domain: str
    verified: bool
    is_primary: bool
    verification_token: str


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    type: str
    status: str
    clinic_group_partner_id: uuid.UUID | None
    plan_code: str | None
    branding: dict
    features: dict
    mobile_app: dict
    domains: list[DomainOut] = []


class PublicTenant(BaseModel):
    """What the React website and Flutter app load at start-up to theme themselves."""

    id: uuid.UUID
    slug: str
    name: str
    type: str
    channel: str
    branding: dict
    features: dict


class TenantCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
    name: str
    plan_code: str = "white_label_standard"
    clinic_group_partner_id: uuid.UUID | None = None
    admin_email: EmailStr
    admin_name: str | None = None
    branding: Branding | None = None


class TenantPatch(BaseModel):
    name: str | None = None
    features: dict[str, bool] | None = None
    mobile_app: dict | None = None
    plan_code: str | None = None


class DomainCreate(BaseModel):
    domain: str = Field(min_length=4, max_length=253, pattern=r"^([a-z0-9-]+\.)+[a-z]{2,}$")
    is_primary: bool = False


class AdminInvite(BaseModel):
    email: EmailStr
    full_name: str | None = None


class MemberCreate(BaseModel):
    member_type: Literal["pharmacy", "lab", "doctor", "stockist"]
    partner_id: uuid.UUID | None = None
    legacy_doctor_id: str | None = None
    display_name: str
    listed_on_marketplace: bool = False
    commission_pct: Decimal | None = Field(None, ge=0, le=100)


class MemberPatch(BaseModel):
    listed_on_marketplace: bool | None = None
    commission_pct: Decimal | None = Field(None, ge=0, le=100)
    status: Literal["active", "inactive"] | None = None
    display_name: str | None = None


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    member_type: str
    partner_id: uuid.UUID | None
    legacy_doctor_id: str | None
    display_name: str
    listed_on_marketplace: bool
    commission_pct: Decimal | None
    status: str
