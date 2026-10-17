from datetime import datetime, timezone

from fastapi import FastAPI

from app import visibility
from app.api import internal, release_reservation, router
from app.config import settings
from app.mongo import Mongo
from gj_common.app_factory import ServiceSpec, create_service_app
from gj_common.events import EventEnvelope
from gj_common.http import ServiceClient
from gj_common.jobs import PeriodicJob

state: dict = {}


async def startup(app: FastAPI) -> None:
    app.state.mongo = state["mongo"] = Mongo()
    await app.state.mongo.ensure_indexes()
    app.state.partners = ServiceClient(settings.partner_service_url, settings.internal_api_key)
    app.state.tenants = app.state.tenant_client


async def shutdown(app: FastAPI) -> None:
    await app.state.partners.aclose()
    await app.state.mongo.close()


async def on_partner_changed(event: EventEnvelope, _session) -> None:
    await visibility.upsert_partner(state["mongo"], event.data)


async def on_member_upserted(event: EventEnvelope, _session) -> None:
    await visibility.upsert_membership(state["mongo"], event.data)


async def on_member_removed(event: EventEnvelope, _session) -> None:
    await visibility.upsert_membership(state["mongo"], event.data, removed=True)


async def on_po_received(event: EventEnvelope, _session) -> None:
    """Restock from a stockist: add received quantities to the buyer's own retail offers.
    Idempotent via a marker on the purchase order id."""
    mongo = state["mongo"]
    marker = f"po-received:{event.data['po_id']}"
    if await mongo.reservations.find_one({"_id": marker}):
        return
    for line in event.data["lines"]:
        await mongo.offers.update_one(
            {"partner_id": event.data["buyer_partner_id"], "product_id": line["product_id"], "channel": "retail",
             "stock": {"$ne": None}},
            {"$inc": {"stock": int(line["qty"])}})
    await mongo.reservations.insert_one({"_id": marker, "status": "applied", "items": [],
                                         "expires_at": datetime(2100, 1, 1, tzinfo=timezone.utc)})


async def expire_reservations(app: FastAPI) -> None:
    mongo: Mongo = app.state.mongo
    stale = await mongo.reservations.find(
        {"status": "reserved", "expires_at": {"$lt": datetime.now(timezone.utc)}}, {"_id": 1}).to_list(500)
    for r in stale:
        await release_reservation(mongo, r["_id"])


partner_events = ["partner.registered", "partner.updated", "partner.verified", "partner.rejected", "partner.suspended"]

app = create_service_app(
    ServiceSpec(
        settings=settings,
        title="GeneJunction Catalog Service",
        description="Master products, partner offers (retail + wholesale), tenant-aware search, stock reservations.",
        routers=[router, internal],
        on_startup=[startup],
        on_shutdown=[shutdown],
        uses_postgres=False,
        event_handlers={
            **{e: on_partner_changed for e in partner_events},
            "tenant.network.member_upserted": on_member_upserted,
            "tenant.network.member_removed": on_member_removed,
            "wholesale.po.received": on_po_received,
        },
        jobs=[PeriodicJob("expire-reservations", 60, expire_reservations)],
    )
)
