from fastapi import FastAPI

from app.api import internal, router
from app.config import settings
from gj_common.app_factory import ServiceSpec, create_service_app
from gj_common.http import ServiceClient
from gj_common.storage import ObjectStorage


async def startup(app: FastAPI) -> None:
    app.state.identity = ServiceClient(settings.identity_service_url, settings.internal_api_key)
    app.state.storage = ObjectStorage(settings)


async def shutdown(app: FastAPI) -> None:
    await app.state.identity.aclose()


app = create_service_app(
    ServiceSpec(
        settings=settings,
        title="GeneJunction Partner Service",
        description="Partner onboarding (pharmacy, lab, stockist, doctor, clinic group), KYC documents and verification.",
        routers=[router, internal],
        on_startup=[startup],
        on_shutdown=[shutdown],
    )
)
