"""Response shapes shared by several services (documented once in the OpenAPI spec)."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class PaymentRef(BaseModel):
    """A payment to complete. Send the user to `checkout_url` (FPX bank / card page / e-wallet)."""

    model_config = ConfigDict(extra="ignore")

    id: uuid.UUID
    purpose: str = Field(description="order | credit_topup | subscription | wholesale")
    reference_id: str
    amount: Decimal = Field(examples=["228.00"])
    currency: str = "MYR"
    status: str = Field(description="requires_action | succeeded | failed | cancelled | expired | "
                                    "partially_refunded | refunded")
    method: str
    checkout_url: str | None
    return_url: str | None = None
    expires_at: datetime


class UploadTicket(BaseModel):
    """Upload the file directly to storage: `PUT upload_url` with the given headers and the raw file as body."""

    upload_url: str
    method: str = "PUT"
    headers: dict[str, str]
    document_id: uuid.UUID | None = None


class Accepted(BaseModel):
    ok: bool = True


class RoleGranted(BaseModel):
    user_id: uuid.UUID
    role: str
