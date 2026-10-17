import uuid
from datetime import date

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api import reserve_slot, router
from app.config import settings
from app.models import LabBooking
from gj_common.app_factory import ServiceSpec, create_service_app
from gj_common.events import EventEnvelope
from gj_common.http import ServiceClient
from gj_common.storage import ObjectStorage


async def startup(app: FastAPI) -> None:
    app.state.orders = ServiceClient(settings.order_service_url, settings.internal_api_key, timeout=15)
    app.state.billing = ServiceClient(settings.billing_service_url, settings.internal_api_key)
    app.state.storage = ObjectStorage(settings)


async def shutdown(app: FastAPI) -> None:
    await app.state.orders.aclose()
    await app.state.billing.aclose()


async def on_order_paid(event: EventEnvelope, session) -> None:
    order = event.data
    for sub in order["sub_orders"]:
        if sub["kind"] != "lab":
            continue
        schedule = sub["schedule"]
        day = date.fromisoformat(schedule["date"])
        result = await session.execute(pg_insert(LabBooking).values(
            id=uuid.uuid4(), sub_order_id=uuid.UUID(sub["sub_order_id"]), order_id=uuid.UUID(order["order_id"]),
            order_number=order["number"], partner_id=uuid.UUID(sub["partner_id"]), partner_name=sub["partner_name"],
            tenant_id=uuid.UUID(order["tenant_id"]), user_id=uuid.UUID(order["user_id"]), channel=order["channel"],
            status="placed", collection_type=sub["fulfilment"], collection_date=day, collection_slot=schedule["slot"],
            address=order.get("delivery_address"), contact=order["contact"],
            tests=[{"name": i["name"], "sku": i["sku"], "qty": i["qty"]} for i in sub["items"]],
        ).on_conflict_do_nothing(index_elements=["sub_order_id"]).returning(LabBooking.id))
        if result.first() is not None:
            await reserve_slot(session, uuid.UUID(sub["partner_id"]), day, schedule["slot"], 1)


async def on_sub_order_status(event: EventEnvelope, session) -> None:
    if event.data["kind"] != "lab":
        return
    b = await session.scalar(select(LabBooking).where(LabBooking.sub_order_id == uuid.UUID(event.data["sub_order_id"])))
    if b is not None:
        b.status = event.data["status"]


app = create_service_app(
    ServiceSpec(
        settings=settings,
        title="GeneJunction Lab Service",
        description="Lab bookings: collection slots, home sample pickup, report upload, AI report summaries.",
        routers=[router],
        on_startup=[startup],
        on_shutdown=[shutdown],
        event_handlers={"order.paid": on_order_paid, "order.sub_order.status_changed": on_sub_order_status},
    )
)
