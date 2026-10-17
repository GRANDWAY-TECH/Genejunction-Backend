from fastapi import FastAPI

from app import handlers
from app.api import router
from app.config import settings
from app.dispatcher import Dispatcher
from gj_common.app_factory import ServiceSpec, create_service_app
from gj_common.http import ServiceClient


async def startup(app: FastAPI) -> None:
    app.state.identity = ServiceClient(settings.identity_service_url, settings.internal_api_key)
    app.state.billing = ServiceClient(settings.billing_service_url, settings.internal_api_key)
    app.state.background = set()
    handlers.dispatcher = Dispatcher(app)


async def shutdown(app: FastAPI) -> None:
    await app.state.identity.aclose()
    await app.state.billing.aclose()


app = create_service_app(
    ServiceSpec(
        settings=settings,
        title="GeneJunction Notification Service",
        description="Branded SMS / WhatsApp / email / push, business inboxes, patient campaigns.",
        routers=[router],
        on_startup=[startup],
        on_shutdown=[shutdown],
        event_handlers=handlers.HANDLERS,
    )
)
