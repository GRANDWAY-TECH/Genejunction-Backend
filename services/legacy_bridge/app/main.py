import uuid
from datetime import timedelta
from decimal import Decimal

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api import consult, folio, internal, webhooks
from app.config import settings
from app.legacy import HttpLegacyClient, MockLegacyClient
from app.models import ConsultBooking, ConsultHold, DoctorPartner, PatientLink
from gj_common.app_factory import ServiceSpec, create_service_app
from gj_common.errors import AppError
from gj_common.events import EventEnvelope, emit
from gj_common.http import ServiceClient
from gj_common.logging import get_logger
from gj_common.storage import ObjectStorage

log = get_logger(__name__)
state: dict = {}


async def startup(app: FastAPI) -> None:
    state["app"] = app
    app.state.legacy = (MockLegacyClient(app.state.redis) if settings.legacy_mode == "mock"
                        else HttpLegacyClient(settings.legacy_base_url, settings.legacy_api_key))
    app.state.identity = ServiceClient(settings.identity_service_url, settings.internal_api_key)
    app.state.orders = ServiceClient(settings.order_service_url, settings.internal_api_key, timeout=15)
    app.state.storage = ObjectStorage(settings)


async def shutdown(app: FastAPI) -> None:
    await app.state.identity.aclose()
    await app.state.orders.aclose()


async def link_patient(session, user_id: uuid.UUID, phone: str | None, name: str | None, email: str | None) -> str:
    if link := await session.get(PatientLink, user_id):
        return link.legacy_patient_id
    patient_id = await state["app"].state.legacy.find_or_create_patient(phone, name, email)
    await session.execute(pg_insert(PatientLink).values(user_id=user_id, legacy_patient_id=patient_id)
                          .on_conflict_do_nothing())
    emit(session, "legacy.patient.linked", key=user_id, data={"user_id": user_id, "legacy_patient_id": patient_id})
    return patient_id


async def on_user_registered(event: EventEnvelope, session) -> None:
    d = event.data
    if d.get("phone"):  # patients sign up by phone; business-only email accounts are not patients
        await link_patient(session, uuid.UUID(d["user_id"]), d["phone"], d.get("full_name"), d.get("email"))


async def on_order_paid(event: EventEnvelope, session) -> None:
    """Payment taken by the marketplace: now book the consultation in the existing system."""
    app = state["app"]
    order = event.data
    for sub in order["sub_orders"]:
        if sub["kind"] != "consultation":
            continue
        sub_id = uuid.UUID(sub["sub_order_id"])
        if await session.scalar(select(ConsultBooking.id).where(ConsultBooking.sub_order_id == sub_id)):
            continue
        hold = await session.get(ConsultHold, uuid.UUID(sub["schedule"]["hold_id"]), with_for_update=True)
        try:
            user = await app.state.identity.get(f"/internal/users/{order['user_id']}")
            patient_id = await link_patient(session, uuid.UUID(order["user_id"]), user.get("phone"),
                                            user.get("full_name"), user.get("email"))
            booked = await app.state.legacy.book(doctor_id=hold.doctor_id, slot_id=hold.slot_id, patient_id=patient_id,
                                                 external_ref=str(sub_id), amount=Decimal(str(sub["total"])))
        except AppError as exc:
            log.warning("legacy_booking_failed", sub_order=str(sub_id), error=exc.message)
            hold.status = "released"
            await app.state.orders.post(f"/internal/sub-orders/{sub_id}/status", json={
                "status": "booking_failed", "actor": "legacy-bridge", "note": exc.message})
            continue
        hold.status = "booked"
        hold.expires_at = hold.slot_end + timedelta(days=1)
        session.add(ConsultBooking(sub_order_id=sub_id, hold_id=hold.id, legacy_booking_id=booked["booking_id"],
                                   user_id=uuid.UUID(order["user_id"]), tenant_id=uuid.UUID(order["tenant_id"]),
                                   doctor_id=hold.doctor_id, doctor_name=hold.doctor_name, slot_start=hold.slot_start,
                                   video_url=booked.get("video_url")))
        await app.state.orders.post(f"/internal/sub-orders/{sub_id}/status", json={
            "status": "booked", "actor": "legacy-bridge", "external_ref": booked["booking_id"],
            "schedule": {"video_url": booked.get("video_url")}})


async def on_report_ready(event: EventEnvelope, session) -> None:
    d = event.data
    link = await session.get(PatientLink, uuid.UUID(d["user_id"]))
    if link is None:
        raise RuntimeError("patient not linked yet")  # retried; linking runs on registration
    await state["app"].state.legacy.add_folio_record(link.legacy_patient_id, {
        "type": "lab_report", "title": ", ".join(t["name"] for t in d["tests"]), "date": d["collected_on"],
        "source": d.get("partner_name"), "document_key": d["report_key"],
        "document_url": state["app"].state.storage.presign_download(d["report_key"], expires=7 * 86400),
        "ai_summary": d.get("ai_summary"), "marketplace_order": d["order_number"]})


async def on_order_completed(event: EventEnvelope, session) -> None:
    """Order history both ways: completed marketplace orders appear in the existing system too."""
    d = event.data
    link = await session.get(PatientLink, uuid.UUID(d["user_id"]))
    if link is None:
        return
    await state["app"].state.legacy.push_order(link.legacy_patient_id, {
        "external_order_id": d["number"], "total": d["total"], "status": d["status"],
        "description": ", ".join(s.get("partner_name") or s["kind"] for s in d["sub_orders"])})


async def on_partner_verified(event: EventEnvelope, session) -> None:
    d = event.data
    if d["type"] == "doctor" and d.get("legacy_doctor_id"):
        await session.execute(pg_insert(DoctorPartner).values(
            legacy_doctor_id=d["legacy_doctor_id"], partner_id=uuid.UUID(d["partner_id"]))
            .on_conflict_do_update(index_elements=["legacy_doctor_id"], set_={"partner_id": uuid.UUID(d["partner_id"])}))


async def on_doctor_network_changed(event: EventEnvelope, session) -> None:
    """A clinic group added/removed/listed a doctor: drop cached visibility filters right away."""
    if event.data.get("member_type") != "doctor":
        return
    redis = state["app"].state.redis
    keys = [k async for k in redis.scan_iter("bridge:doctor-filter:*", count=500)]
    if keys:
        await redis.delete(*keys)


app = create_service_app(
    ServiceSpec(
        settings=settings,
        title="GeneJunction Consult & Folio Bridge",
        description="The only path to the existing GeneJunction (PHP) system: doctors, slots, bookings, "
                    "prescriptions, health folio, patient linking and order-history sync.",
        routers=[consult, folio, webhooks, internal],
        on_startup=[startup],
        on_shutdown=[shutdown],
        event_handlers={
            "identity.user.registered": on_user_registered,
            "order.paid": on_order_paid,
            "lab.report.ready": on_report_ready,
            "order.completed": on_order_completed,
            "order.partially_completed": on_order_completed,
            "partner.verified": on_partner_verified,
            "tenant.network.member_upserted": on_doctor_network_changed,
            "tenant.network.member_removed": on_doctor_network_changed,
        },
    )
)
