from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from sqlalchemy import select

from app import domain
from app.api import admin, cart_router, internal, orders, partner, rx_router
from app.config import settings
from app.models import Order
from gj_common.app_factory import ServiceSpec, create_service_app
from gj_common.events import EventEnvelope, emit
from gj_common.http import ServiceClient
from gj_common.jobs import PeriodicJob

state: dict = {}


async def startup(app: FastAPI) -> None:
    state["app"] = app
    app.state.catalog = ServiceClient(settings.catalog_service_url, settings.internal_api_key)
    app.state.payments = ServiceClient(settings.payment_service_url, settings.internal_api_key, timeout=15)
    app.state.bridge = ServiceClient(settings.bridge_service_url, settings.internal_api_key, timeout=15)


async def shutdown(app: FastAPI) -> None:
    for client in (app.state.catalog, app.state.payments, app.state.bridge):
        await client.aclose()


async def on_payment_succeeded(event: EventEnvelope, session) -> None:
    if event.data["purpose"] == "order":
        await domain.on_payment_succeeded(state["app"], session, event.data)


async def on_payment_failed(event: EventEnvelope, session) -> None:
    if event.data["purpose"] == "order":
        await domain.on_payment_failed(state["app"], session, event.data)


async def on_prescription(event: EventEnvelope, session) -> None:
    await domain.on_prescription_issued(state["app"], session, event.data)


async def expire_unpaid(app: FastAPI) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.payment_window_minutes + 5)
    async with app.state.db.sessionmaker() as session:
        ids = (await session.scalars(select(Order.id).where(Order.status == "pending_payment", Order.created_at < cutoff)
                                     .limit(200))).all()
    for order_id in ids:
        async with app.state.db.sessionmaker() as session, session.begin():
            order = await session.get(Order, order_id, with_for_update=True)
            if order is None or order.status != "pending_payment":
                continue
            await domain.release_resources(app, order)
            if order.payment_id:
                await app.state.payments.post(f"/internal/payments/{order.payment_id}/cancel")
            await domain.set_order_status(session, order, "expired", "system", "Payment not completed in time")
            emit(session, "order.expired", key=order.id, tenant_id=order.tenant_id, data=domain.order_payload(order))


app = create_service_app(
    ServiceSpec(
        settings=settings,
        title="GeneJunction Order Service",
        description="Cart, checkout, orders split per partner, partner order inbox, prescriptions to cart.",
        routers=[cart_router, orders, rx_router, partner, admin, internal],
        on_startup=[startup],
        on_shutdown=[shutdown],
        event_handlers={
            "payment.succeeded": on_payment_succeeded,
            "payment.failed": on_payment_failed,
            "consult.prescription.issued": on_prescription,
        },
        jobs=[PeriodicJob("expire-unpaid-orders", 60, expire_unpaid)],
    )
)
