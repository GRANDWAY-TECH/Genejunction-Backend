import json
import uuid

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.courier import get_courier, verify_signature
from app.models import Fulfilment, FulfilmentLog
from gj_common.auth import CurrentUser, Principal
from gj_common.db import SessionDep
from gj_common.errors import Conflict, NotFound, Unauthorized, ValidationFailed
from gj_common.pagination import PageDep, paginate
from gj_common.storage import ObjectStorage

router = APIRouter(prefix="/api/v1/pharmacy", tags=["pharmacy"])


class FulfilmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sub_order_id: uuid.UUID
    order_number: str
    channel: str
    status: str
    method: str
    items: list
    total: str
    delivery_address: dict | None
    contact: dict
    requires_prescription: bool
    prescription_id: uuid.UUID | None
    rx_status: str
    rx_notes: str | None
    courier: str | None
    tracking_no: str | None
    tracking_url: str | None
    rejection_reason: str | None


class Reason(BaseModel):
    reason: str


class RxDecision(BaseModel):
    approved: bool
    pharmacist_registration_no: str
    notes: str | None = None


async def _load(session: AsyncSession, principal: Principal, fulfilment_id: uuid.UUID) -> Fulfilment:
    f = await session.get(Fulfilment, fulfilment_id, with_for_update=True)
    if f is None:
        raise NotFound("Fulfilment not found")
    principal.ensure_partner(f.partner_id)
    return f


async def _move(request: Request, session: AsyncSession, f: Fulfilment, status: str, actor: str,
                note: str | None = None, tracking: dict | None = None) -> Fulfilment:
    """The order service validates the transition (and refunds on rejection); we mirror it locally."""
    await request.app.state.orders.post(f"/internal/sub-orders/{f.sub_order_id}/status", json={
        "status": status, "actor": actor, "note": note, "tracking": tracking})
    f.status = status
    session.add(FulfilmentLog(fulfilment_id=f.id, action=status, actor=actor, note=note))
    await session.commit()
    return f


@router.get("/{partner_id}/fulfilments")
async def list_fulfilments(partner_id: uuid.UUID, principal: CurrentUser, session: SessionDep, page: PageDep,
                           status: str | None = None, channel: str | None = None):
    principal.ensure_partner(partner_id)
    stmt = select(Fulfilment).where(Fulfilment.partner_id == partner_id).order_by(Fulfilment.created_at.desc())
    if status:
        stmt = stmt.where(Fulfilment.status == status)
    if channel:
        stmt = stmt.where(Fulfilment.channel == channel)
    return await paginate(session, stmt, page, FulfilmentOut.model_validate)


@router.get("/fulfilments/{fulfilment_id}")
async def get_fulfilment(fulfilment_id: uuid.UUID, principal: CurrentUser, request: Request, session: SessionDep):
    f = await _load(session, principal, fulfilment_id)
    out = FulfilmentOut.model_validate(f).model_dump()
    if f.prescription_document_key:
        out["prescription_document_url"] = request.app.state.storage.presign_download(f.prescription_document_key)
    return out


@router.post("/fulfilments/{fulfilment_id}/accept", response_model=FulfilmentOut)
async def accept(fulfilment_id: uuid.UUID, principal: CurrentUser, request: Request, session: SessionDep):
    f = await _load(session, principal, fulfilment_id)
    return await _move(request, session, f, "accepted", f"user:{principal.user_id}")


@router.post("/fulfilments/{fulfilment_id}/reject", response_model=FulfilmentOut)
async def reject(fulfilment_id: uuid.UUID, body: Reason, principal: CurrentUser, request: Request, session: SessionDep):
    """Out of stock / cannot fulfil. The customer is refunded for this part of the order automatically."""
    f = await _load(session, principal, fulfilment_id)
    f.rejection_reason = body.reason
    return await _move(request, session, f, "rejected", f"user:{principal.user_id}", body.reason)


@router.post("/fulfilments/{fulfilment_id}/verify-prescription", response_model=FulfilmentOut)
async def verify_prescription(fulfilment_id: uuid.UUID, body: RxDecision, principal: CurrentUser, request: Request,
                              session: SessionDep):
    f = await _load(session, principal, fulfilment_id)
    if not f.requires_prescription:
        raise ValidationFailed("This order has no prescription items")
    if f.status != "accepted":
        raise Conflict("Accept the order before verifying the prescription")
    f.pharmacist_id = principal.user_id
    f.pharmacist_reg_no = body.pharmacist_registration_no
    f.rx_notes = body.notes
    if body.approved:
        f.rx_status = "approved"
        return await _move(request, session, f, "rx_verified", f"pharmacist:{body.pharmacist_registration_no}", body.notes)
    f.rx_status = "rejected"
    f.rejection_reason = body.notes or "Prescription not valid"
    return await _move(request, session, f, "rejected", f"pharmacist:{body.pharmacist_registration_no}", f.rejection_reason)


@router.post("/fulfilments/{fulfilment_id}/pack", response_model=FulfilmentOut)
async def pack(fulfilment_id: uuid.UUID, principal: CurrentUser, request: Request, session: SessionDep):
    f = await _load(session, principal, fulfilment_id)
    if f.requires_prescription and f.rx_status != "approved":
        raise Conflict("A pharmacist must verify the prescription first", code="rx_unverified")
    return await _move(request, session, f, "packed", f"user:{principal.user_id}")


@router.post("/fulfilments/{fulfilment_id}/dispatch", response_model=FulfilmentOut)
async def dispatch(fulfilment_id: uuid.UUID, principal: CurrentUser, request: Request, session: SessionDep):
    f = await _load(session, principal, fulfilment_id)
    if f.method != "delivery":
        raise ValidationFailed("This order is for pickup; mark it ready for pickup instead")
    courier = get_courier()
    shipment = await courier.create_shipment(reference=f.order_number, pickup={"partner_id": str(f.partner_id)},
                                             dropoff=f.delivery_address or {}, contact=f.contact)
    f.courier, f.tracking_no, f.tracking_url = courier.name, shipment["tracking_no"], shipment["tracking_url"]
    return await _move(request, session, f, "dispatched", f"user:{principal.user_id}",
                       tracking={"courier": courier.name, **shipment})


@router.post("/fulfilments/{fulfilment_id}/ready-for-pickup", response_model=FulfilmentOut)
async def ready_for_pickup(fulfilment_id: uuid.UUID, principal: CurrentUser, request: Request, session: SessionDep):
    f = await _load(session, principal, fulfilment_id)
    return await _move(request, session, f, "ready_for_pickup", f"user:{principal.user_id}")


@router.post("/fulfilments/{fulfilment_id}/delivered", response_model=FulfilmentOut)
async def delivered(fulfilment_id: uuid.UUID, principal: CurrentUser, request: Request, session: SessionDep):
    """Handed over at the counter (pickup) or confirmed manually."""
    f = await _load(session, principal, fulfilment_id)
    await _move(request, session, f, "delivered", f"user:{principal.user_id}")
    return await _move(request, session, f, "completed", "system")


@router.post("/courier/webhooks/{provider}", include_in_schema=False)
async def courier_webhook(provider: str, request: Request, session: SessionDep, x_signature: str = Header("")):
    body = await request.body()
    if not verify_signature(body, x_signature):
        raise Unauthorized("Bad signature")
    data = json.loads(body)
    f = await session.scalar(select(Fulfilment).where(Fulfilment.tracking_no == data["tracking_no"]).with_for_update())
    if f is None:
        raise NotFound("Unknown tracking number")
    if data["status"] == "delivered" and f.status == "dispatched":
        await _move(request, session, f, "delivered", f"courier:{provider}")
        await _move(request, session, f, "completed", "system")
    elif data["status"] == "failed" and f.status == "dispatched":
        await _move(request, session, f, "delivery_failed", f"courier:{provider}", data.get("reason"))
    return {"status": f.status}


def storage_factory(settings) -> ObjectStorage:
    return ObjectStorage(settings)
