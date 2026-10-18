"""Crash-safe stock moves.

Every change is a single-document atomic update that records itself on the offer, so repeating it
(a retried call, a redelivered event, a crash half-way through a multi-item order) never moves stock twice:

* a reservation is listed in the offer's `holds` while it holds stock;
* any other change (restock after a rejection, a received purchase order, a release after commit)
  leaves its key in the offer's `applied_ops`.

Reservations not paid within `reservation_ttl_minutes` are released by a job, which also cleans up after
a crash in the middle of reserving.
"""

from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

from app.config import settings
from app.mongo import Mongo
from gj_common.errors import Conflict, NotFound

APPLIED_KEEP = 500  # newest operation keys kept per offer (redeliveries arrive long before 500 newer changes)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def apply_once(mongo: Mongo, offer_filter: dict, delta: int, op_key: str) -> bool:
    """Add `delta` to a stock-tracked offer exactly once for `op_key`. Returns False if already applied."""
    res = await mongo.offers.update_one(
        {**offer_filter, "stock": {"$ne": None}, "applied_ops": {"$ne": op_key}},
        {"$inc": {"stock": delta}, "$push": {"applied_ops": {"$each": [op_key], "$slice": -APPLIED_KEEP}},
         "$set": {"updated_at": _now()}})
    return res.modified_count == 1


async def _take(mongo: Mongo, rid: str, items: list[dict]) -> str | None:
    """Hold stock for every item. Returns the offer id that ran out, or None when all are held."""
    for item in items:
        offer = await mongo.offers.find_one({"_id": item["offer_id"]}, {"stock": 1, "holds": 1})
        if offer is None:
            return item["offer_id"]
        if offer.get("stock") is None or rid in (offer.get("holds") or []):
            continue  # not stock-tracked, or already held by this reservation (a retry)
        res = await mongo.offers.update_one(
            {"_id": item["offer_id"], "stock": {"$gte": item["qty"]}, "holds": {"$ne": rid}},
            {"$inc": {"stock": -item["qty"]}, "$push": {"holds": rid}, "$set": {"updated_at": _now()}})
        if res.modified_count == 0:
            return item["offer_id"]
    return None


async def reserve(mongo: Mongo, rid: str, items: list[dict]) -> dict:
    """All-or-nothing hold. Safe to call again with the same id (continues a half-finished attempt)."""
    try:
        await mongo.reservations.insert_one({
            "_id": rid, "status": "pending", "items": items, "created_at": _now(),
            "expires_at": _now() + timedelta(minutes=settings.reservation_ttl_minutes)})
    except DuplicateKeyError:
        existing = await mongo.reservations.find_one({"_id": rid})
        if existing["status"] != "pending":
            return {"reservation_id": rid, "status": existing["status"]}
        items = existing["items"]
    failed = await _take(mongo, rid, items)
    if failed:
        await release(mongo, rid, final_status="failed")
        raise Conflict("Item went out of stock", code="out_of_stock", details=[{"offer_id": failed}])
    await mongo.reservations.update_one({"_id": rid, "status": "pending"}, {"$set": {"status": "reserved"}})
    return {"reservation_id": rid, "status": "reserved"}


async def release(mongo: Mongo, rid: str, final_status: str = "released") -> bool:
    """Give the stock back. Works for held and for already-committed reservations, and is repeatable."""
    res = await mongo.reservations.find_one({"_id": rid})
    if res is None or res["status"] in ("released", "failed"):
        return False
    for item in res["items"]:
        freed = await mongo.offers.update_one(
            {"_id": item["offer_id"], "holds": rid},
            {"$inc": {"stock": item["qty"]}, "$pull": {"holds": rid}, "$set": {"updated_at": _now()}})
        if freed.modified_count == 0 and res["status"] == "committed":
            await apply_once(mongo, {"_id": item["offer_id"]}, item["qty"], f"release:{rid}")
    await mongo.reservations.update_one({"_id": rid}, {"$set": {"status": final_status, "closed_at": _now()}})
    return True


async def commit(mongo: Mongo, rid: str) -> str:
    """The order was paid: the held stock is sold. Repeatable."""
    res = await mongo.reservations.find_one({"_id": rid})
    if res is None:
        raise NotFound("Reservation not found")
    if res["status"] == "committed":
        return "committed"
    if res["status"] in ("released", "failed"):
        # Paid after the hold lapsed: take the stock again if it is still there.
        await mongo.reservations.update_one({"_id": rid}, {"$set": {"status": "pending"}})
        if await _take(mongo, rid, res["items"]):
            await release(mongo, rid, final_status="failed")
            return "out_of_stock"  # the pharmacy will reject that part, which refunds it
    for item in res["items"]:
        await mongo.offers.update_one({"_id": item["offer_id"], "holds": rid}, {"$pull": {"holds": rid}})
    await mongo.reservations.update_one({"_id": rid}, {"$set": {"status": "committed"}})
    return "committed"


async def expire(mongo: Mongo) -> int:
    stale = await mongo.reservations.find(
        {"status": {"$in": ["pending", "reserved"]}, "expires_at": {"$lt": _now()}}, {"_id": 1}).to_list(500)
    for r in stale:
        await release(mongo, r["_id"])
    return len(stale)
