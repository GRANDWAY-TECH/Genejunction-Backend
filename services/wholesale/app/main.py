import uuid

from fastapi import FastAPI

from app.api import router
from app.config import settings
from app.models import PurchaseOrder
from gj_common.app_factory import ServiceSpec, create_service_app
from gj_common.events import EventEnvelope
from gj_common.http import ServiceClient


async def startup(app: FastAPI) -> None:
    app.state.catalog = ServiceClient(settings.catalog_service_url, settings.internal_api_key)
    app.state.payments = ServiceClient(settings.payment_service_url, settings.internal_api_key, timeout=15)


async def shutdown(app: FastAPI) -> None:
    await app.state.catalog.aclose()
    await app.state.payments.aclose()


async def on_payment_succeeded(event: EventEnvelope, session) -> None:
    if event.data["purpose"] != "wholesale":
        return
    po = await session.get(PurchaseOrder, uuid.UUID(event.data["reference_id"]), with_for_update=True)
    if po is not None and po.payment_status == "unpaid":
        po.payment_status = "paid"


app = create_service_app(
    ServiceSpec(
        settings=settings,
        title="GeneJunction Wholesale Service",
        description="Stockist B2B purchase orders with tier pricing, prepaid or net-30 terms.",
        routers=[router],
        on_startup=[startup],
        on_shutdown=[shutdown],
        event_handlers={"payment.succeeded": on_payment_succeeded},
    )
)
