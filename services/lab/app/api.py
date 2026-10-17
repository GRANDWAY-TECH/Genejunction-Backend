import uuid
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import LabBooking, SlotCapacity
from app.summarizer import summarizer
from gj_common.auth import CurrentUser, Principal
from gj_common.db import SessionDep
from gj_common.errors import AppError, Conflict, NotFound
from gj_common.events import emit
from gj_common.logging import get_logger
from gj_common.pagination import PageDep, paginate

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1/lab", tags=["lab"])


class BookingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sub_order_id: uuid.UUID
    order_number: str
    partner_id: uuid.UUID
    partner_name: str | None
    channel: str
    status: str
    collection_type: str
    collection_date: date
    collection_slot: str
    address: dict | None
    contact: dict
    tests: list
    phlebotomist: dict | None
    report_uploaded_at: datetime | None
    ai_summary: str | None
    ai_summary_status: str
    rejection_reason: str | None


class SlotConfig(BaseModel):
    day: date
    slot: str = Field(pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$")
    capacity: int = Field(ge=0, le=500)


class Schedule(BaseModel):
    date: date
    slot: str = Field(pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$")
    phlebotomist_name: str | None = None
    phlebotomist_phone: str | None = None


class Reason(BaseModel):
    reason: str


class ReportReady(BaseModel):
    ai_summary: bool = True


class UploadRequest(BaseModel):
    content_type: Literal["application/pdf"] = "application/pdf"


async def reserve_slot(session: AsyncSession, partner_id: uuid.UUID, day: date, slot: str, delta: int) -> None:
    await session.execute(pg_insert(SlotCapacity).values(
        partner_id=partner_id, day=day, slot=slot, capacity=settings.default_slot_capacity, booked=max(delta, 0))
        .on_conflict_do_update(index_elements=["partner_id", "day", "slot"],
                               set_={"booked": SlotCapacity.booked + delta}))


async def _load(session: AsyncSession, principal: Principal, booking_id: uuid.UUID) -> LabBooking:
    b = await session.get(LabBooking, booking_id, with_for_update=True)
    if b is None:
        raise NotFound("Booking not found")
    principal.ensure_partner(b.partner_id)
    return b


async def _move(request: Request, session: AsyncSession, b: LabBooking, status: str, actor: str,
                note: str | None = None, schedule: dict | None = None) -> LabBooking:
    await request.app.state.orders.post(f"/internal/sub-orders/{b.sub_order_id}/status", json={
        "status": status, "actor": actor, "note": note, "schedule": schedule})
    b.status = status
    await session.commit()
    return b


# ---------------------------------------------------------------- slots (patients pick, labs configure)
@router.get("/{partner_id}/slots")
async def slots(partner_id: uuid.UUID, session: SessionDep, day: date = Query(..., alias="date")):
    rows = {r.slot: r for r in (await session.scalars(select(SlotCapacity).where(
        SlotCapacity.partner_id == partner_id, SlotCapacity.day == day))).all()}
    out = []
    for slot in sorted(set(settings.default_slots) | set(rows)):
        row = rows.get(slot)
        capacity = row.capacity if row else settings.default_slot_capacity
        booked = row.booked if row else 0
        out.append({"slot": slot, "capacity": capacity, "available": max(capacity - booked, 0)})
    return {"date": day, "slots": out}


@router.put("/{partner_id}/slots")
async def configure_slots(partner_id: uuid.UUID, body: list[SlotConfig], principal: CurrentUser, session: SessionDep):
    principal.ensure_partner(partner_id)
    for s in body[:200]:
        await session.execute(pg_insert(SlotCapacity).values(partner_id=partner_id, day=s.day, slot=s.slot,
                                                             capacity=s.capacity, booked=0)
                              .on_conflict_do_update(index_elements=["partner_id", "day", "slot"],
                                                     set_={"capacity": s.capacity}))
    await session.commit()
    return {"updated": len(body[:200])}


# ---------------------------------------------------------------- lab portal
@router.get("/{partner_id}/bookings")
async def list_bookings(partner_id: uuid.UUID, principal: CurrentUser, session: SessionDep, page: PageDep,
                        status: str | None = None, day: date | None = Query(None, alias="date")):
    principal.ensure_partner(partner_id)
    stmt = select(LabBooking).where(LabBooking.partner_id == partner_id).order_by(
        LabBooking.collection_date, LabBooking.collection_slot)
    if status:
        stmt = stmt.where(LabBooking.status == status)
    if day:
        stmt = stmt.where(LabBooking.collection_date == day)
    return await paginate(session, stmt, page, BookingOut.model_validate)


@router.get("/bookings/{booking_id}", response_model=BookingOut)
async def get_booking(booking_id: uuid.UUID, principal: CurrentUser, session: SessionDep):
    return await _load(session, principal, booking_id)


@router.post("/bookings/{booking_id}/accept", response_model=BookingOut)
async def accept(booking_id: uuid.UUID, principal: CurrentUser, request: Request, session: SessionDep):
    b = await _load(session, principal, booking_id)
    return await _move(request, session, b, "accepted", f"user:{principal.user_id}")


@router.post("/bookings/{booking_id}/reject", response_model=BookingOut)
async def reject(booking_id: uuid.UUID, body: Reason, principal: CurrentUser, request: Request, session: SessionDep):
    b = await _load(session, principal, booking_id)
    b.rejection_reason = body.reason
    await reserve_slot(session, b.partner_id, b.collection_date, b.collection_slot, -1)
    return await _move(request, session, b, "rejected", f"user:{principal.user_id}", body.reason)


@router.post("/bookings/{booking_id}/schedule", response_model=BookingOut)
async def schedule(booking_id: uuid.UUID, body: Schedule, principal: CurrentUser, request: Request, session: SessionDep):
    b = await _load(session, principal, booking_id)
    if (body.date, body.slot) != (b.collection_date, b.collection_slot):
        await reserve_slot(session, b.partner_id, b.collection_date, b.collection_slot, -1)
        await reserve_slot(session, b.partner_id, body.date, body.slot, 1)
        b.collection_date, b.collection_slot = body.date, body.slot
    if body.phlebotomist_name:
        b.phlebotomist = {"name": body.phlebotomist_name, "phone": body.phlebotomist_phone}
    return await _move(request, session, b, "scheduled", f"user:{principal.user_id}", schedule={
        "date": body.date.isoformat(), "slot": body.slot, "type": b.collection_type, "phlebotomist": b.phlebotomist})


@router.post("/bookings/{booking_id}/sample-collected", response_model=BookingOut)
async def sample_collected(booking_id: uuid.UUID, principal: CurrentUser, request: Request, session: SessionDep):
    b = await _load(session, principal, booking_id)
    return await _move(request, session, b, "sample_collected", f"user:{principal.user_id}")


@router.post("/bookings/{booking_id}/processing", response_model=BookingOut)
async def processing(booking_id: uuid.UUID, principal: CurrentUser, request: Request, session: SessionDep):
    b = await _load(session, principal, booking_id)
    return await _move(request, session, b, "processing", f"user:{principal.user_id}")


@router.post("/bookings/{booking_id}/report-upload")
async def report_upload(booking_id: uuid.UUID, body: UploadRequest, principal: CurrentUser, request: Request,
                        session: SessionDep):
    b = await _load(session, principal, booking_id)
    if b.status != "processing":
        raise Conflict("Mark the sample as processing first")
    b.report_key = f"lab-reports/{b.partner_id}/{b.id}/report.pdf"
    await session.commit()
    return {"upload_url": request.app.state.storage.presign_upload(b.report_key, body.content_type), "method": "PUT",
            "headers": {"Content-Type": body.content_type}}


@router.post("/bookings/{booking_id}/report-ready", response_model=BookingOut)
async def report_ready(booking_id: uuid.UUID, body: ReportReady, principal: CurrentUser, request: Request,
                       session: SessionDep):
    b = await _load(session, principal, booking_id)
    if not b.report_key:
        raise Conflict("Upload the report first", code="report_missing")
    b.report_uploaded_at = datetime.now(timezone.utc)
    if body.ai_summary:
        await _ai_summary(request, b)
    emit(session, "lab.report.ready", key=b.user_id, tenant_id=b.tenant_id, data={
        "booking_id": b.id, "sub_order_id": b.sub_order_id, "order_number": b.order_number, "user_id": b.user_id,
        "tenant_id": b.tenant_id, "partner_id": b.partner_id, "partner_name": b.partner_name, "tests": b.tests,
        "report_key": b.report_key, "ai_summary": b.ai_summary, "collected_on": b.collection_date})
    await _move(request, session, b, "report_ready", f"user:{principal.user_id}")
    return await _move(request, session, b, "completed", "system")


async def _ai_summary(request: Request, b: LabBooking) -> None:
    """Paid extra: charged to the clinic group in its white-label app, otherwise to the lab."""
    owner_type, owner_id = ("tenant", b.tenant_id) if b.channel == "B2B2C" else ("partner", b.partner_id)
    billing = request.app.state.billing
    reference = f"ai-summary:{b.id}"
    try:
        await billing.post("/internal/credits/charge", json={
            "owner_type": owner_type, "owner_id": str(owner_id), "action_code": "ai_report_summary",
            "quantity": "1", "reference": reference, "description": f"AI summary · {b.order_number}"})
    except AppError as exc:
        if exc.status_code == 402:
            b.ai_summary_status = "skipped"
            return
        raise
    try:
        b.ai_summary = await summarizer.summarize(
            tests=b.tests, report_url=request.app.state.storage.presign_download(b.report_key))
        b.ai_summary_status = "done"
    except Exception as exc:
        log.warning("ai_summary_failed", booking=str(b.id), error=str(exc))
        b.ai_summary_status = "failed"
        await billing.post("/internal/credits/refund", json={
            "owner_type": owner_type, "owner_id": str(owner_id), "reference": reference, "reason": "Summary failed"})


# ---------------------------------------------------------------- patients
@router.get("/my-bookings")
async def my_bookings(principal: CurrentUser, session: SessionDep, page: PageDep):
    stmt = select(LabBooking).where(LabBooking.user_id == principal.user_id).order_by(LabBooking.created_at.desc())
    return await paginate(session, stmt, page, BookingOut.model_validate)


@router.get("/my-bookings/{booking_id}/report")
async def my_report(booking_id: uuid.UUID, principal: CurrentUser, request: Request, session: SessionDep):
    b = await session.get(LabBooking, booking_id)
    if b is None or b.user_id != principal.user_id:
        raise NotFound("Booking not found")
    if not b.report_uploaded_at:
        raise Conflict("Report not ready yet", code="report_not_ready")
    return {"download_url": request.app.state.storage.presign_download(b.report_key), "ai_summary": b.ai_summary}
