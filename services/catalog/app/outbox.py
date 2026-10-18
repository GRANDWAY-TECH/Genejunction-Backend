"""Outbox for the Mongo-backed catalog: events are stored first, then relayed to Kafka, so a Kafka
outage delays them but never loses them."""

from datetime import datetime, timezone

from fastapi import FastAPI

from app.config import settings
from app.mongo import Mongo
from gj_common.db import uuid7
from gj_common.events import EventEnvelope, topic_for
from gj_common.logging import get_logger

log = get_logger(__name__)


async def stage(mongo: Mongo, event_id: str, event_type: str, key: str, data: dict) -> None:
    """Idempotent on `event_id`: staging the same event twice keeps one copy."""
    env = EventEnvelope(id=uuid7(), type=event_type, occurred_at=datetime.now(timezone.utc), source="catalog",
                        key=key, data=data)
    await mongo.db["outbox"].update_one(
        {"_id": event_id},
        {"$setOnInsert": {"topic": topic_for(event_type), "key": key, "payload": env.model_dump(mode="json"),
                          "published": False, "created_at": datetime.now(timezone.utc)}},
        upsert=True)


async def relay(app: FastAPI) -> None:
    mongo: Mongo = app.state.mongo
    pending = await mongo.db["outbox"].find({"published": False}).sort("created_at", 1).to_list(200)
    if not pending:
        return
    await app.state.bus.publish_many([(d["topic"], d["key"], d["payload"]) for d in pending])
    await mongo.db["outbox"].update_many({"_id": {"$in": [d["_id"] for d in pending]}},
                                         {"$set": {"published": True, "published_at": datetime.now(timezone.utc)}})


async def low_stock_alerts(app: FastAPI) -> None:
    """Raise one alert each time an offer drops to the threshold; re-arm once it is restocked above it."""
    mongo: Mongo = app.state.mongo
    threshold = settings.low_stock_threshold
    await mongo.offers.update_many({"low_stock_alerted": True, "stock": {"$gt": threshold}},
                                   {"$set": {"low_stock_alerted": False}})
    low = await mongo.offers.find(
        {"stock": {"$ne": None, "$lte": threshold}, "low_stock_alerted": {"$ne": True}, "active": True},
        {"partner_id": 1, "stock": 1, "product.name": 1, "low_stock_alerts": 1}).to_list(500)
    for offer in low:
        seq = offer.get("low_stock_alerts", 0) + 1
        await stage(mongo, f"low-stock:{offer['_id']}:{seq}", "catalog.stock.low", offer["partner_id"], {
            "partner_id": offer["partner_id"], "offer_id": offer["_id"], "product_name": offer["product"]["name"],
            "stock": offer["stock"]})
        await mongo.offers.update_one({"_id": offer["_id"]},
                                      {"$set": {"low_stock_alerted": True, "low_stock_alerts": seq}})
