import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

ProductType = Literal["medicine", "lab_test", "wellness", "device"]
Channel = Literal["retail", "wholesale"]


class LabTestInfo(BaseModel):
    sample_type: str = "blood"
    fasting_required: bool = False
    turnaround_hours: int = 24
    home_collection: bool = True
    tests_included: list[str] = []


class ProductCreate(BaseModel):
    sku: str = Field(min_length=2, max_length=64)
    type: ProductType
    name: str
    generic_name: str | None = None
    brand: str | None = None
    category: str
    description: str | None = None
    requires_prescription: bool = False
    attributes: dict = {}  # strength, dosage form, pack size, MAL registration no, ...
    lab: LabTestInfo | None = None
    images: list[str] = []

    @model_validator(mode="after")
    def _lab(self):
        if self.type == "lab_test" and self.lab is None:
            self.lab = LabTestInfo()
        return self


class ProductPatch(BaseModel):
    name: str | None = None
    generic_name: str | None = None
    brand: str | None = None
    category: str | None = None
    description: str | None = None
    requires_prescription: bool | None = None
    attributes: dict | None = None
    lab: LabTestInfo | None = None
    images: list[str] | None = None
    status: Literal["active", "inactive", "rejected"] | None = None


class WholesaleTier(BaseModel):
    min_qty: int = Field(ge=1)
    price: Decimal = Field(gt=0, decimal_places=2)


class OfferCreate(BaseModel):
    product_id: str
    channel: Channel = "retail"
    price: Decimal = Field(gt=0, decimal_places=2)
    mrp: Decimal | None = Field(None, gt=0, decimal_places=2)
    stock: int | None = Field(None, ge=0, description="null = not stock-tracked (e.g. lab tests)")
    min_order_qty: int = Field(1, ge=1)
    wholesale_tiers: list[WholesaleTier] = []
    active: bool = True


class OfferPatch(BaseModel):
    price: Decimal | None = Field(None, gt=0, decimal_places=2)
    mrp: Decimal | None = Field(None, gt=0, decimal_places=2)
    stock: int | None = Field(None, ge=0)
    min_order_qty: int | None = Field(None, ge=1)
    wholesale_tiers: list[WholesaleTier] | None = None
    active: bool | None = None


class StockUpdate(BaseModel):
    offer_id: str
    stock: int = Field(ge=0)


class QuoteItem(BaseModel):
    offer_id: str
    qty: int = Field(ge=1, le=1000)


class QuoteRequest(BaseModel):
    tenant_id: uuid.UUID | None = None
    channel: Channel = "retail"
    items: list[QuoteItem] = Field(min_length=1, max_length=100)
    strict: bool = True  # False: return problems instead of failing (cart previews)


class ReservationRequest(BaseModel):
    reservation_id: str
    items: list[QuoteItem] = Field(min_length=1, max_length=100)


class PrescriptionItem(BaseModel):
    kind: Literal["medicine", "lab_test"]
    sku: str | None = None
    name: str | None = None
    qty: int = 1


class PrescriptionMatchRequest(BaseModel):
    tenant_id: uuid.UUID
    items: list[PrescriptionItem]
    latitude: float | None = None
    longitude: float | None = None


class FeatureRequest(BaseModel):
    partner_id: uuid.UUID
    until: datetime


# ---------------------------------------------------------------- responses
class ProductSnapshot(BaseModel):
    """Product fields copied onto each offer so search needs no join."""

    name: str
    type: ProductType
    category: str
    brand: str | None = None
    generic_name: str | None = None
    requires_prescription: bool = False
    sku: str
    lab: LabTestInfo | None = None
    image: str | None = None


class ProductOut(BaseModel):
    id: str
    sku: str
    type: ProductType
    name: str
    slug: str
    generic_name: str | None = None
    brand: str | None = None
    category: str
    description: str | None = None
    requires_prescription: bool
    attributes: dict = {}
    lab: LabTestInfo | None = None
    images: list[str] = []
    status: str = Field(description="pending_review | active | inactive | rejected")
    proposed_by_partner_id: str | None = None
    created_at: datetime
    updated_at: datetime


class OfferOut(BaseModel):
    id: str
    product_id: str
    partner_id: str
    partner_name: str | None = None
    partner_type: str | None = None
    channel: Channel
    price: Decimal = Field(examples=["38.00"])
    mrp: Decimal | None = None
    stock: int | None = Field(None, description="null = not stock-tracked (lab tests)")
    min_order_qty: int = 1
    wholesale_tiers: list[WholesaleTier] = []
    active: bool
    featured_until: datetime | None = None
    product: ProductSnapshot
    created_at: datetime
    updated_at: datetime


class PartnerOfferOut(OfferOut):
    visibility: dict | None = Field(None, description="Where this offer is shown: marketplace and/or tenant ids")
    awaiting_product_approval: bool | None = None


class SearchHit(BaseModel):
    product_id: str
    product: ProductSnapshot
    min_price: Decimal
    offer_count: int
    featured: bool
    best_offer: OfferOut = Field(description="Featured first, then cheapest")


class SearchPage(BaseModel):
    items: list[SearchHit]
    total: int
    limit: int
    offset: int


class ProductDetail(ProductOut):
    offers: list[OfferOut] = Field(description="Offers visible in the caller's app, cheapest first")


class ProductPage(BaseModel):
    items: list[ProductOut]
    total: int
    limit: int
    offset: int


class OfferPage(BaseModel):
    items: list[PartnerOfferOut]
    total: int
    limit: int
    offset: int


class StockUpdated(BaseModel):
    updated: int
