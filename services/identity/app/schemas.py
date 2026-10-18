import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class OtpRequest(BaseModel):
    phone: str = Field(examples=["+60123456789"])


class OtpRequested(BaseModel):
    sent: bool = True
    expires_in: int
    debug_otp: str | None = None


class OtpVerify(BaseModel):
    phone: str
    code: str = Field(min_length=6, max_length=6)
    full_name: str | None = None


class PasswordLogin(BaseModel):
    email: EmailStr
    password: str


class BusinessSignup(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10)
    full_name: str
    phone: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    phone: str | None
    email: str | None
    full_name: str | None
    status: str
    legacy_patient_id: str | None
    created_at: datetime


class UserUpdate(BaseModel):
    full_name: str | None = None
    email: EmailStr | None = None


class RoleGrant(BaseModel):
    role: str
    scope_type: Literal["platform", "tenant", "partner"]
    scope_id: uuid.UUID | None = None


class UserLookup(BaseModel):
    email: EmailStr | None = None
    phone: str | None = None
    full_name: str | None = None
    create: bool = True


class LegacyLink(BaseModel):
    legacy_patient_id: str


class TokenUser(BaseModel):
    id: uuid.UUID
    full_name: str | None
    phone: str | None
    email: str | None
    roles: list[str] = Field(description="patient, partner_admin, partner_staff, tenant_admin, gj_admin, gj_ops, gj_finance")
    partner_ids: list[uuid.UUID] = Field(description="Partners this user can act for")
    tenant_id: uuid.UUID = Field(description="Brand the session belongs to")


class TokenResponse(BaseModel):
    access_token: str = Field(description="RS256 JWT, send as `Authorization: Bearer <token>`")
    refresh_token: str = Field(description="Opaque; single use — every refresh returns a new one")
    token_type: str = "Bearer"
    expires_in: int = Field(description="Access-token lifetime in seconds", examples=[900])
    user: TokenUser


class Jwks(BaseModel):
    keys: list[dict]


class Granted(BaseModel):
    granted: bool = True
