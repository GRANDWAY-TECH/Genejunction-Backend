import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from gj_common.contracts import PaymentRef


class Address(BaseModel):
    recipient: str
    phone: str
    line1: str
    line2: str | None = None
    city: str
    state: str
    postcode: str = Field(pattern=r"^\d{5}$")
    latitude: float | None = None
    longitude: float | None = None


class AddItem(BaseModel):
    offer_id: str
    qty: int = Field(1, ge=1, le=100)


class AddConsultation(BaseModel):
    doctor_id: str
    slot_id: str


class UpdateQty(BaseModel):
    qty: int = Field(ge=1, le=100)


class LabCollection(BaseModel):
    type: Literal["home_collection", "walk_in"] = "home_collection"
    date: date
    slot: str = Field(pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$", examples=["08:00-10:00"])


class CheckoutRequest(BaseModel):
    payment_method: Literal["fpx", "card", "ewallet"]
    return_url: str | None = None
    delivery_address: Address | None = None
    pharmacy_fulfilment: Literal["delivery", "pickup"] = "delivery"
    lab_collection: LabCollection | None = None
    prescription_document_key: str | None = Field(None, description="Uploaded prescription, if not from a GJ consult")


class CancelRequest(BaseModel):
    reason: str = Field(max_length=500)


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    offer_id: str | None
    product_id: str | None
    sku: str | None
    name: str
    product_type: str
    qty: int
    unit_price: Decimal
    line_total: Decimal
    requires_prescription: bool
    meta: dict


class SubOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    seq: int
    partner_id: uuid.UUID | None
    partner_name: str | None
    kind: str
    fulfilment: str
    status: str
    subtotal: Decimal
    delivery_fee: Decimal
    total: Decimal
    requires_prescription: bool
    prescription_id: uuid.UUID | None
    schedule: dict | None
    tracking: dict | None
    external_ref: str | None
    items: list[OrderItemOut]


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: str
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    channel: str
    status: str
    currency: str
    subtotal: Decimal
    delivery_fee: Decimal
    total: Decimal
    refunded_amount: Decimal
    delivery_address: dict | None
    payment_id: str | None
    paid_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    created_at: datetime
    sub_orders: list[SubOrderOut]


class PartnerOrderOut(BaseModel):
    """A sub-order as a partner sees it, tagged with where it came from."""

    sub_order: SubOrderOut
    order_id: uuid.UUID
    order_number: str
    channel: str
    tenant_id: uuid.UUID
    placed_at: datetime
    delivery_address: dict | None
    contact: dict


class PrescriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    legacy_consult_id: str
    doctor_name: str | None
    items: list
    document_url: str | None
    status: str
    issued_at: datetime


class LegacyOrderImport(BaseModel):
    user_id: uuid.UUID
    legacy_order_id: str
    description: str
    total: Decimal
    status: str
    ordered_at: datetime
    details: dict = {}


class StatusChange(BaseModel):
    status: str
    actor: str = "service"
    note: str | None = None
    tracking: dict | None = None
    schedule: dict | None = None
    external_ref: str | None = None


# ---------------------------------------------------------------- cart & checkout responses
class QuoteLine(BaseModel):
    """Live price and availability for a cart line (from the catalog)."""

    offer_id: str
    product_id: str
    partner_id: str
    partner_type: str | None
    partner_name: str | None
    product: dict = Field(description="Product snapshot: name, type, category, requires_prescription, sku, ...")
    qty: int
    unit_price: Decimal
    mrp: Decimal | None = None
    line_total: Decimal


class CartItemView(BaseModel):
    id: uuid.UUID
    kind: Literal["product", "consultation"]
    qty: int
    source: Literal["manual", "prescription"]
    prescription_id: uuid.UUID | None
    offer_id: str | None = None
    line: QuoteLine | None = Field(None, description="null when the item can no longer be bought (see `problems`)")
    consultation: dict | None = Field(None, description="{doctor_id, slot_id} for consultation items")


class CartProblem(BaseModel):
    model_config = ConfigDict(extra="allow")

    offer_id: str
    problem: str = Field(description="unavailable | not_available_in_this_app | below_min_order_qty | insufficient_stock")


class CartView(BaseModel):
    id: uuid.UUID
    items: list[CartItemView]
    problems: list[CartProblem]
    products_subtotal: Decimal = Field(description="Products only; delivery, collection and consult fees are added at checkout")


class CheckoutResponse(BaseModel):
    order: OrderOut
    payment: PaymentRef | None = Field(description="Redirect the patient to `payment.checkout_url`")


class HistoryEntry(BaseModel):
    source: Literal["marketplace", "genejunction"] = Field(description="genejunction = ordered in the existing system")
    id: uuid.UUID
    number: str
    status: str
    total: Decimal
    at: datetime
    description: str


class TimelineEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    sub_order_id: uuid.UUID | None = Field(description="null = whole order")
    from_status: str | None = Field(alias="from")
    to: str
    note: str | None
    at: datetime
