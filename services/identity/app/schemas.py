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
