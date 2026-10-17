import secrets
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import POLine, PurchaseOrder
from gj_common.auth import CurrentUser, Principal
from gj_common.db import SessionDep, uuid7
from gj_common.errors import AppError, Conflict, NotFound, ValidationFailed
from gj_common.events import emit
from gj_common.money import money
from gj_common.pagination import PageDep, paginate

router = APIRouter(prefix="/api/v1/wholesale", tags=["wholesale"])


class POItem(BaseModel):
    offer_id: str
    qty: int = Field(ge=1, le=100_000)


class POCreate(BaseModel):
    buyer_partner_id: uuid.UUID
    items: list[POItem] = Field(min_length=1, max_length=200)
    payment_terms: Literal["prepaid", "net30"] = "prepaid"
    payment_method: Literal["fpx", "card"] = "fpx"
    delivery_address: dict
    notes: str | None = None
    return_url: str | None = None


class LineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    offer_id: str
    product_id: str
    sku: str | None
    name: str
    qty: int
    unit_price: Decimal
    line_total: Decimal


class POOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: str
    buyer_partner_id: uuid.UUID
    stockist_partner_id: uuid.UUID
    stockist_name: str | None
    status: str
    payment_terms: str
    payment_status: str
    payment_id: str | None
    subtotal: Decimal
    total: Decimal
    delivery_address: dict
    notes: str | None
    tracking: dict | None
    created_at: datetime
    received_at: datetime | None
    lines: list[LineOut]


class Reason(BaseModel):
    reason: str


class Shipment(BaseModel):
    courier: str
    tracking_no: str


def _payload(po: PurchaseOrder) -> dict:
    return {"po_id": po.id, "number": po.number, "buyer_partner_id": po.buyer_partner_id,
            "stockist_partner_id": po.stockist_partner_id, "status": po.status, "total": po.total,
            "payment_terms": po.payment_terms,
            "lines": [{"product_id": l.product_id, "sku": l.sku, "name": l.name, "qty": l.qty,
                       "unit_price": l.unit_price} for l in po.lines]}


async def _po(session: AsyncSession, po_id: uuid.UUID) -> PurchaseOrder:
    po = await session.get(PurchaseOrder, po_id, with_for_update=True)
    if po is None:
        raise NotFound("Purchase order not found")
    return po


def _as_stockist(principal: Principal, po: PurchaseOrder) -> None:
    principal.ensure_partner(po.stockist_partner_id)


def _as_buyer(principal: Principal, po: PurchaseOrder) -> None:
    principal.ensure_partner(po.buyer_partner_id)


@router.post("/purchase-orders", status_code=201)
async def create_po(body: POCreate, principal: CurrentUser, request: Request, session: SessionDep):
    """A pharmacy, lab or clinic restocks from one stockist. Tier prices come from the catalog."""
    principal.ensure_partner(body.buyer_partner_id)
    quote = await request.app.state.catalog.post("/internal/catalog/quote", json={
        "channel": "wholesale", "items": [i.model_dump() for i in body.items]})
    stockists = {l["partner_id"] for l in quote["lines"]}
    if len(stockists) != 1:
        raise ValidationFailed("A purchase order must contain items from a single stockist", code="multiple_stockists")
    stockist_id = uuid.UUID(stockists.pop())
    if stockist_id == body.buyer_partner_id:
        raise ValidationFailed("You cannot buy from yourself")
    po = PurchaseOrder(id=uuid7(), number=f"PO{datetime.now(timezone.utc):%y%m%d}-{secrets.token_hex(3).upper()}",
                       buyer_partner_id=body.buyer_partner_id, stockist_partner_id=stockist_id,
                       stockist_name=quote["lines"][0]["partner_name"], payment_terms=body.payment_terms,
                       subtotal=money(sum(Decimal(str(l["line_total"])) for l in quote["lines"])),
                       delivery_address=body.delivery_address, notes=body.notes, created_by=principal.user_id,
                       lines=[POLine(offer_id=l["offer_id"], product_id=l["product_id"], sku=l["product"].get("sku"),
                                     name=l["product"]["name"], qty=l["qty"], unit_price=money(l["unit_price"]),
                                     line_total=money(l["line_total"])) for l in quote["lines"]])
    po.total = po.subtotal
    session.add(po)
    emit(session, "wholesale.po.submitted", key=po.id, data=_payload(po))
    await session.commit()

    catalog = request.app.state.catalog
    try:
        await catalog.post("/internal/catalog/reservations", json={
            "reservation_id": f"po:{po.id}", "items": [i.model_dump() for i in body.items]})
    except AppError:
        po.status = "cancelled"
        await session.commit()
        raise
    payment = None
    if body.payment_terms == "prepaid":
        try:
            payment = await request.app.state.payments.post("/internal/payments", json={
                "purpose": "wholesale", "reference_id": str(po.id), "amount": str(po.total), "currency": "MYR",
                "method": body.payment_method, "payer_type": "partner", "payer_id": str(po.buyer_partner_id),
                "description": f"Purchase order {po.number}", "return_url": body.return_url,
                "idempotency_key": f"po:{po.id}", "expires_in_minutes": 1440})
        except AppError:
            await catalog.post(f"/internal/catalog/reservations/po:{po.id}/release")
            po.status = "cancelled"
            await session.commit()
            raise
        po.payment_id = payment["id"]
        await session.commit()
    return {"purchase_order": POOut.model_validate(po), "payment": payment}


@router.get("/purchase-orders")
async def list_pos(partner_id: uuid.UUID, principal: CurrentUser, session: SessionDep, page: PageDep,
                   role: Literal["buyer", "stockist"] = "buyer", status: str | None = None):
    principal.ensure_partner(partner_id)
    col = PurchaseOrder.buyer_partner_id if role == "buyer" else PurchaseOrder.stockist_partner_id
    stmt = select(PurchaseOrder).where(col == partner_id).order_by(PurchaseOrder.created_at.desc())
    if status:
        stmt = stmt.where(PurchaseOrder.status == status)
    return await paginate(session, stmt, page, POOut.model_validate)


@router.get("/purchase-orders/{po_id}", response_model=POOut)
async def get_po(po_id: uuid.UUID, principal: CurrentUser, session: SessionDep):
    po = await session.get(PurchaseOrder, po_id)
    if po is None or not (principal.can_act_for_partner(po.buyer_partner_id)
                          or principal.can_act_for_partner(po.stockist_partner_id)):
        raise NotFound("Purchase order not found")
    return po


@router.post("/purchase-orders/{po_id}/accept", response_model=POOut)
async def accept(po_id: uuid.UUID, principal: CurrentUser, session: SessionDep):
    po = await _po(session, po_id)
    _as_stockist(principal, po)
    if po.status != "submitted":
        raise Conflict(f"Purchase order is {po.status}")
    po.status = "accepted"
    emit(session, "wholesale.po.accepted", key=po.id, data=_payload(po))
    await session.commit()
    return po


async def _close_unfulfilled(request: Request, session: AsyncSession, po: PurchaseOrder, status: str, reason: str) -> None:
    await request.app.state.catalog.post(f"/internal/catalog/reservations/po:{po.id}/release")
    if po.payment_status == "paid":
        await request.app.state.payments.post(f"/internal/payments/{po.payment_id}/refunds", json={
            "amount": str(po.total), "reason": reason, "reference": f"po:{po.id}"})
        po.payment_status = "refunded"
    elif po.payment_id:
        await request.app.state.payments.post(f"/internal/payments/{po.payment_id}/cancel")
    po.status = status
    emit(session, f"wholesale.po.{status}", key=po.id, data={**_payload(po), "reason": reason})
    await session.commit()


@router.post("/purchase-orders/{po_id}/reject", response_model=POOut)
async def reject(po_id: uuid.UUID, body: Reason, principal: CurrentUser, request: Request, session: SessionDep):
    po = await _po(session, po_id)
    _as_stockist(principal, po)
    if po.status != "submitted":
        raise Conflict(f"Purchase order is {po.status}")
    await _close_unfulfilled(request, session, po, "rejected", body.reason)
    return po


@router.post("/purchase-orders/{po_id}/cancel", response_model=POOut)
async def cancel(po_id: uuid.UUID, body: Reason, principal: CurrentUser, request: Request, session: SessionDep):
    po = await _po(session, po_id)
    _as_buyer(principal, po)
    if po.status not in ("submitted", "accepted"):
        raise Conflict("Already shipped; arrange a return with the stockist")
    await _close_unfulfilled(request, session, po, "cancelled", body.reason)
    return po


@router.post("/purchase-orders/{po_id}/ship", response_model=POOut)
async def ship(po_id: uuid.UUID, body: Shipment, principal: CurrentUser, request: Request, session: SessionDep):
    po = await _po(session, po_id)
    _as_stockist(principal, po)
    if po.status != "accepted":
        raise Conflict("Accept the purchase order first")
    if po.payment_terms == "prepaid" and po.payment_status != "paid":
        raise Conflict("Waiting for the buyer's payment", code="awaiting_payment")
    await request.app.state.catalog.post(f"/internal/catalog/reservations/po:{po.id}/commit")
    po.status = "shipped"
    po.tracking = body.model_dump()
    emit(session, "wholesale.po.shipped", key=po.id, data=_payload(po))
    await session.commit()
    return po


@router.post("/purchase-orders/{po_id}/receive", response_model=POOut)
async def receive(po_id: uuid.UUID, principal: CurrentUser, session: SessionDep):
    """Buyer confirms delivery: their retail stock goes up and the stockist's sale is settled."""
    po = await _po(session, po_id)
    _as_buyer(principal, po)
    if po.status != "shipped":
        raise Conflict(f"Purchase order is {po.status}")
    po.status = "received"
    po.received_at = datetime.now(timezone.utc)
    emit(session, "wholesale.po.received", key=po.id, data=_payload(po))
    await session.commit()
    return po
