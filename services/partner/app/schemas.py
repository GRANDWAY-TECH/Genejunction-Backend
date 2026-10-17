import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

PartnerType = Literal["pharmacy", "lab", "stockist", "doctor", "clinic_group"]
DocType = Literal["ssm_certificate", "pharmacy_licence", "lab_accreditation", "mmc_registration", "poison_licence", "bank_statement", "other"]


class Address(BaseModel):
    line1: str
    line2: str | None = None
    city: str
    state: str
    postcode: str = Field(pattern=r"^\d{5}$")
    country: str = "MY"


class PartnerCreate(BaseModel):
    type: PartnerType
    legal_name: str
    display_name: str
    registration_no: str
    licence_no: str | None = None
    licence_expiry: datetime | None = None
    contact_email: EmailStr
    contact_phone: str
    address: Address
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    service_radius_km: int = Field(10, ge=1, le=200)
    owner_tenant_id: uuid.UUID | None = None
    legacy_doctor_id: str | None = None


class PartnerPatch(BaseModel):
    display_name: str | None = None
    contact_email: EmailStr | None = None
    contact_phone: str | None = None
    address: Address | None = None
    latitude: float | None = None
    longitude: float | None = None
    service_radius_km: int | None = Field(None, ge=1, le=200)
    operating_hours: dict | None = None
    licence_no: str | None = None
    licence_expiry: datetime | None = None


class BankAccount(BaseModel):
    bank_name: str
    account_name: str
    account_no: str = Field(pattern=r"^\d{6,20}$")


class PartnerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    legal_name: str
    display_name: str
    registration_no: str
    licence_no: str | None
    licence_expiry: datetime | None
    status: str
    owner_tenant_id: uuid.UUID | None
    legacy_doctor_id: str | None
    contact_email: str
    contact_phone: str
    address: dict
    latitude: float | None
    longitude: float | None
    service_radius_km: int
    operating_hours: dict
    bank_name: str | None
    bank_account_name: str | None
    bank_account_last4: str | None
    rejection_reason: str | None
    verified_at: datetime | None
    created_at: datetime


class DocumentUpload(BaseModel):
    doc_type: DocType
    file_name: str
    content_type: Literal["application/pdf", "image/jpeg", "image/png"]


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    doc_type: str
    file_name: str
    content_type: str
    status: str
    created_at: datetime
    download_url: str | None = None


class ReviewDecision(BaseModel):
    note: str | None = None


class StaffInvite(BaseModel):
    email: EmailStr
    full_name: str | None = None
    role: Literal["partner_admin", "partner_staff"] = "partner_staff"
