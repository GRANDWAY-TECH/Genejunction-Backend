import hashlib
import hmac
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ConsultBooking, ConsultHold, DoctorPartner, PatientLink
from gj_common.auth import CurrentUser, InternalOnly
from gj_common.db import SessionDep
from gj_common.errors import Conflict, NotFound, Unauthorized
from gj_common.events import emit
from gj_common.tenancy import CurrentTenant, TenantContext

consult = APIRouter(prefix="/api/v1/consult", tags=["consultations (existing system)"])
folio = APIRouter(prefix="/api/v1/folio", tags=["health folio (existing system)"])
webhooks = APIRouter(prefix="/api/v1/legacy/webhooks", tags=["legacy webhooks"], include_in_schema=False)
internal = APIRouter(prefix="/internal/consult", tags=["internal"], dependencies=[InternalOnly])


class HoldRequest(BaseModel):
    doctor_id: str
    slot_id: str
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    reference: str


class PrescribeRequest(BaseModel):
    items: list[dict]
    doctor_notes: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def doctor_filter(request: Request, tenant: TenantContext) -> tuple[str, set[str]]:
    key = f"bridge:doctor-filter:{tenant.id}"
    redis = request.app.state.redis
    if cached := await redis.get(key):
        data = json.loads(cached)
    else:
        data = await request.app.state.tenant_client.get(f"/internal/tenants/{tenant.id}/doctor-filter")
        await redis.set(key, json.dumps(data), ex=60)
    return data["mode"], set(data["doctor_ids"])


def _visible(mode: str, ids: set[str], doctor_id: str) -> bool:
    return doctor_id in ids if mode == "include" else doctor_id not in ids


async def _visible_doctor(request: Request, tenant: TenantContext, doctor_id: str) -> dict:
    mode, ids = await doctor_filter(request, tenant)
    if not _visible(mode, ids, doctor_id):
        raise NotFound("Doctor not found")
    return await request.app.state.legacy.get_doctor(doctor_id)


async def taken_slots(session: AsyncSession, doctor_id: str) -> set[str]:
    rows = await session.scalars(select(ConsultHold.slot_id).where(
        ConsultHold.doctor_id == doctor_id, ConsultHold.status.in_(["held", "booked"]), ConsultHold.expires_at > _now()))
    return set(rows)


# ---------------------------------------------------------------- doctors & slots
@consult.get("/doctors")
async def list_doctors(request: Request, tenant: CurrentTenant, specialty: str | None = None, q: str | None = None):
    """Doctors from the existing system, filtered to the caller's network (all listed doctors on the marketplace)."""
    mode, ids = await doctor_filter(request, tenant)
    docs = await request.app.state.legacy.list_doctors(specialty, q)
    return [d for d in docs if _visible(mode, ids, d["id"])]


@consult.get("/doctors/{doctor_id}")
async def get_doctor(doctor_id: str, request: Request, tenant: CurrentTenant):
    return await _visible_doctor(request, tenant, doctor_id)


@consult.get("/doctors/{doctor_id}/slots")
async def slots(doctor_id: str, request: Request, tenant: CurrentTenant, session: SessionDep,
                day: date | None = None):
    await _visible_doctor(request, tenant, doctor_id)
    day = day or date.today()
    taken = await taken_slots(session, doctor_id)
    return [s for s in await request.app.state.legacy.get_slots(doctor_id, day) if s["slot_id"] not in taken]


@consult.get("/bookings")
async def my_bookings(principal: CurrentUser, session: SessionDep):
    rows = (await session.scalars(select(ConsultBooking).where(ConsultBooking.user_id == principal.user_id)
                                  .order_by(ConsultBooking.slot_start.desc()).limit(100))).all()
    return [{"id": b.id, "doctor_id": b.doctor_id, "doctor_name": b.doctor_name, "slot_start": b.slot_start,
             "status": b.status, "video_url": b.video_url, "sub_order_id": b.sub_order_id,
             "legacy_booking_id": b.legacy_booking_id} for b in rows]


# ---------------------------------------------------------------- health folio
async def _patient_id(session: AsyncSession, user_id: uuid.UUID) -> str:
    link = await session.get(PatientLink, user_id)
    if link is None:
        raise NotFound("Your health folio is being set up. Please try again shortly.", code="folio_not_linked")
    return link.legacy_patient_id


@folio.get("")
async def my_folio(principal: CurrentUser, request: Request, session: SessionDep):
    """Records, prescriptions and lab reports, read from the existing system (shown in any brand's app)."""
    return await request.app.state.legacy.folio(await _patient_id(session, principal.user_id))


# ---------------------------------------------------------------- internal: holds for checkout
@internal.post("/holds", status_code=201)
async def create_hold(body: HoldRequest, request: Request, session: SessionDep):
    tenant = await request.app.state.tenant_resolver.get_by_id(body.tenant_id)
    doctor = await _visible_doctor(request, tenant, body.doctor_id)
    slot = next((s for s in await request.app.state.legacy.get_slots(
        body.doctor_id, datetime.fromisoformat(body.slot_id.split("@")[-1]).date()
        if "@" in body.slot_id else date.today()) if s["slot_id"] == body.slot_id), None)
    if slot is None:
        raise Conflict("That time slot is no longer available", code="slot_unavailable")
    # Free any expired hold on this slot before claiming it.
    stale = await session.scalar(select(ConsultHold).where(ConsultHold.slot_id == body.slot_id,
                                                            ConsultHold.status == "held", ConsultHold.expires_at <= _now()))
    if stale:
        stale.status = "released"
        await session.flush()
    partner_id = await session.scalar(select(DoctorPartner.partner_id).where(DoctorPartner.legacy_doctor_id == body.doctor_id))
    hold = ConsultHold(doctor_id=body.doctor_id, doctor_name=doctor["name"], specialty=doctor.get("specialty"),
                       partner_id=partner_id, slot_id=body.slot_id, slot_start=datetime.fromisoformat(slot["start"]),
                       slot_end=datetime.fromisoformat(slot["end"]), fee=Decimal(str(doctor["fee"])),
                       user_id=body.user_id, tenant_id=body.tenant_id, reference=body.reference,
                       expires_at=_now() + timedelta(minutes=settings.hold_minutes))
    session.add(hold)
    try:
        await session.commit()
    except IntegrityError as exc:
        raise Conflict("That time slot was just taken", code="slot_unavailable") from exc
    return {"hold_id": str(hold.id), "fee": str(hold.fee), "doctor_name": hold.doctor_name, "specialty": hold.specialty,
            "slot_start": hold.slot_start.isoformat(), "slot_end": hold.slot_end.isoformat(),
            "partner_id": str(partner_id) if partner_id else None}


@internal.post("/holds/{hold_id}/release")
async def release_hold(hold_id: uuid.UUID, session: SessionDep):
    hold = await session.get(ConsultHold, hold_id, with_for_update=True)
    if hold and hold.status == "held":
        hold.status = "released"
        await session.commit()
    return {"released": True}


# ---------------------------------------------------------------- webhooks from the existing system
def _verify(body: bytes, signature: str) -> dict:
    expected = hmac.new(settings.legacy_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise Unauthorized("Bad signature")
    return json.loads(body)


async def handle_prescription(request: Request, session: AsyncSession, data: dict) -> dict:
    booking = await session.scalar(select(ConsultBooking).where(
        ConsultBooking.legacy_booking_id == data["booking_id"]).with_for_update())
    if booking is None:
        raise NotFound("Unknown booking")
    emit(session, "consult.prescription.issued", key=booking.user_id, tenant_id=booking.tenant_id, data={
        "legacy_consult_id": data.get("consult_id") or data["booking_id"], "user_id": booking.user_id,
        "tenant_id": booking.tenant_id, "doctor_name": data.get("doctor_name") or booking.doctor_name,
        "items": data["items"], "document_url": data.get("document_url"),
        "issued_at": data.get("issued_at") or _now().isoformat()})
    if booking.status == "booked":
        booking.status = "completed"
        await request.app.state.orders.post(f"/internal/sub-orders/{booking.sub_order_id}/status",
                                            json={"status": "completed", "actor": "legacy:consult"})
    await session.commit()
    return {"accepted": True}


@webhooks.post("/prescription-issued")
async def prescription_issued(request: Request, session: SessionDep, x_signature: str = Header("")):
    """The doctor finished the consult and wrote a prescription / test order in the existing system."""
    return await handle_prescription(request, session, _verify(await request.body(), x_signature))


@webhooks.post("/consult-status")
async def consult_status(request: Request, session: SessionDep, x_signature: str = Header("")):
    data = _verify(await request.body(), x_signature)
    booking = await session.scalar(select(ConsultBooking).where(
        ConsultBooking.legacy_booking_id == data["booking_id"]).with_for_update())
    if booking is None:
        raise NotFound("Unknown booking")
    status = {"completed": "completed", "no_show": "no_show", "cancelled": "cancelled"}[data["status"]]
    if booking.status != status:
        await request.app.state.orders.post(f"/internal/sub-orders/{booking.sub_order_id}/status",
                                            json={"status": status, "actor": "legacy:consult"})
        booking.status = status
        await session.commit()
    return {"status": status}


@webhooks.post("/order-created")
async def legacy_order(request: Request, session: SessionDep, x_signature: str = Header("")):
    """An order placed in the existing system: copy it into the patient's unified history."""
    data = _verify(await request.body(), x_signature)
    user_id = await session.scalar(select(PatientLink.user_id).where(PatientLink.legacy_patient_id == data["patient_id"]))
    if user_id is None:
        return {"skipped": "patient not linked"}
    await request.app.state.orders.post("/internal/orders/import-legacy", json={
        "user_id": str(user_id), "legacy_order_id": data["order_id"], "description": data["description"],
        "total": str(data["total"]), "status": data["status"], "ordered_at": data["ordered_at"],
        "details": data.get("details", {})})
    return {"imported": True}


# ---------------------------------------------------------------- dev helper (mock mode)
@consult.post("/dev/bookings/{legacy_booking_id}/prescribe", tags=["dev"])
async def dev_prescribe(legacy_booking_id: str, body: PrescribeRequest, request: Request, session: SessionDep):
    """Mock mode only: simulates the doctor issuing an e-prescription at the end of a video consult."""
    if settings.legacy_mode != "mock":
        raise NotFound()
    return await handle_prescription(request, session, {
        "booking_id": legacy_booking_id, "consult_id": f"cn-{legacy_booking_id}", "items": body.items})
