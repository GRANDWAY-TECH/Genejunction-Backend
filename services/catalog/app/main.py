from fastapi import FastAPI

from app import outbox, stock, visibility
from app.api import internal, router
from app.config import settings
from app.mongo import Mongo
from gj_common.app_factory import ServiceSpec, create_service_app
from gj_common.events import EventEnvelope
from gj_common.http import ServiceClient
from gj_common.jobs import PeriodicJob

state: dict = {}

# Stock was sold to this sub-order; these statuses mean the pharmacy never sent it.
RESTOCK_FROM = {"placed", "accepted", "rx_verified"}


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
    """Restock from a stockist: add received quantities to the buyer's retail offers, once per line."""
    d = event.data
    for line in d["lines"]:
        await stock.apply_once(state["mongo"],
                               {"partner_id": d["buyer_partner_id"], "product_id": line["product_id"],
                                "channel": "retail"},
                               int(line["qty"]), f"po-received:{d['po_id']}:{line['product_id']}")


async def on_sub_order_status(event: EventEnvelope, _session) -> None:
    """A pharmacy rejected (or the order cancelled) a part it had not sent: put that stock back, once."""
    d = event.data
    if d["kind"] != "pharmacy" or d["status"] not in ("rejected", "cancelled") or d.get("old_status") not in RESTOCK_FROM:
        return
    for item in d["items"]:
        if item.get("offer_id"):
            await stock.apply_once(state["mongo"], {"_id": item["offer_id"]}, int(item["qty"]),
                                   f"restock:{d['sub_order_id']}:{item['offer_id']}")


async def expire_reservations(app: FastAPI) -> None:
    await stock.expire(app.state.mongo)


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
            "order.sub_order.status_changed": on_sub_order_status,
        },
        jobs=[
            PeriodicJob("expire-reservations", 60, expire_reservations),
            PeriodicJob("catalog-outbox-relay", 2, outbox.relay),
            PeriodicJob("low-stock-alerts", 60, outbox.low_stock_alerts),
        ],
    )
)
