from datetime import date, datetime
from decimal import Decimal
from typing import Any

from bson.decimal128 import Decimal128
from pymongo import ASCENDING, DESCENDING, TEXT, AsyncMongoClient, IndexModel
from pymongo.asynchronous.database import AsyncDatabase

from app.config import settings


def to_mongo(value: Any) -> Any:
    if isinstance(value, Decimal):
        return Decimal128(value)
    if isinstance(value, dict):
        return {k: to_mongo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_mongo(v) for v in value]
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day)
    return value


def from_mongo(value: Any) -> Any:
    if isinstance(value, Decimal128):
        return value.to_decimal()
    if isinstance(value, dict):
        out = {k: from_mongo(v) for k, v in value.items()}
        if "_id" in out:
            out["id"] = out.pop("_id")
        return out
    if isinstance(value, list):
        return [from_mongo(v) for v in value]
    return value


class Mongo:
    def __init__(self) -> None:
        self.client = AsyncMongoClient(settings.mongo_url, uuidRepresentation="standard", tz_aware=True)
        self.db: AsyncDatabase = self.client[settings.mongo_db]
        self.products = self.db["products"]
        self.offers = self.db["offers"]
        self.visibility = self.db["partner_visibility"]
        self.reservations = self.db["stock_reservations"]

    async def ensure_indexes(self) -> None:
        await self.products.create_indexes([
            IndexModel([("sku", ASCENDING)], unique=True),
            IndexModel([("type", ASCENDING), ("status", ASCENDING), ("category", ASCENDING)]),
            IndexModel([("name", TEXT), ("generic_name", TEXT), ("brand", TEXT), ("category", TEXT)],
                       weights={"name": 10, "generic_name": 6, "brand": 3, "category": 1}, name="product_text"),
        ])
        await self.offers.create_indexes([
            IndexModel([("partner_id", ASCENDING), ("product_id", ASCENDING), ("channel", ASCENDING)], unique=True),
            IndexModel([("product_id", ASCENDING), ("active", ASCENDING)]),
            IndexModel([("channel", ASCENDING), ("active", ASCENDING), ("visibility.marketplace", ASCENDING),
                        ("product.type", ASCENDING), ("price", ASCENDING)]),
            IndexModel([("visibility.tenants", ASCENDING), ("channel", ASCENDING), ("active", ASCENDING)]),
            IndexModel([("featured_until", DESCENDING)], sparse=True),
            IndexModel([("location", "2dsphere")]),
            IndexModel([("product.name", TEXT), ("product.generic_name", TEXT), ("product.brand", TEXT),
                        ("product.category", TEXT)],
                       weights={"product.name": 10, "product.generic_name": 6, "product.brand": 3}, name="offer_text"),
        ])
        await self.reservations.create_indexes([IndexModel([("expires_at", ASCENDING)])])

    async def close(self) -> None:
        await self.client.close()
