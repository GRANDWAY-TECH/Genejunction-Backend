import uuid

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api import router, storage_factory
from app.config import settings
from app.models import Fulfilment
from gj_common.app_factory import ServiceSpec, create_service_app
from gj_common.events import EventEnvelope
from gj_common.http import ServiceClient


async def startup(app: FastAPI) -> None:
    app.state.orders = ServiceClient(settings.order_service_url, settings.internal_api_key, timeout=15)
    app.state.storage = storage_factory(settings)


async def shutdown(app: FastAPI) -> None:
    await app.state.orders.aclose()


async def on_order_paid(event: EventEnvelope, session) -> None:
    order = event.data
    for sub in order["sub_orders"]:
        if sub["kind"] != "pharmacy":
            continue
        await session.execute(pg_insert(Fulfilment).values(
            id=uuid.uuid4(), sub_order_id=uuid.UUID(sub["sub_order_id"]), order_id=uuid.UUID(order["order_id"]),
            order_number=order["number"], partner_id=uuid.UUID(sub["partner_id"]),
            tenant_id=uuid.UUID(order["tenant_id"]), user_id=uuid.UUID(order["user_id"]), channel=order["channel"],
            status="placed", method=sub["fulfilment"], items=sub["items"], total=str(sub["total"]),
            delivery_address=order.get("delivery_address"), contact=order["contact"],
            requires_prescription=sub["requires_prescription"],
            prescription_id=uuid.UUID(sub["prescription_id"]) if sub.get("prescription_id") else None,
            prescription_document_key=sub.get("prescription_document_key"),
            rx_status="pending" if sub["requires_prescription"] else "not_required",
        ).on_conflict_do_nothing(index_elements=["sub_order_id"]))


async def on_sub_order_status(event: EventEnvelope, session) -> None:
    """Reconcile: the order service is the source of truth for status (e.g. customer cancellations)."""
    if event.data["kind"] != "pharmacy":
        return
    f = await session.scalar(select(Fulfilment).where(Fulfilment.sub_order_id == uuid.UUID(event.data["sub_order_id"])))
    if f is not None:
        f.status = event.data["status"]


app = create_service_app(
    ServiceSpec(
        settings=settings,
        title="GeneJunction Pharmacy Service",
        description="Pharmacy fulfilment: accept, pharmacist prescription check, pack, courier dispatch, delivery.",
        routers=[router],
        on_startup=[startup],
        on_shutdown=[shutdown],
        event_handlers={"order.paid": on_order_paid, "order.sub_order.status_changed": on_sub_order_status},
    )
)
