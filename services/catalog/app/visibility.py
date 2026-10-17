"""Who can see a partner's offers.

The marketplace (B2C) shows verified partners that are independent, or that a clinic
group has chosen to list. A white-label tenant (B2B2C) shows only its own network.
The result is denormalised onto every offer so search is a single indexed query.
"""

from app.mongo import Mongo
from gj_common.http import ServiceClient
from gj_common.tenancy import TenantContext


def compute_visibility(pv: dict) -> dict:
    verified = pv.get("status") == "verified"
    members = {t: m for t, m in (pv.get("tenants") or {}).items() if m.get("status", "active") == "active"}
    independent = pv.get("owner_tenant_id") is None
    return {
        "verified": verified,
        "marketplace": verified and (independent or any(m.get("listed") for m in members.values())),
        "tenants": sorted(members) if verified else [],
    }


def tenant_filter(tenant: TenantContext) -> dict:
    if tenant.is_platform:
        return {"visibility.marketplace": True}
    return {"visibility.tenants": str(tenant.id)}


def visible_to(offer: dict, tenant: TenantContext) -> bool:
    vis = offer.get("visibility") or {}
    if tenant.is_platform:
        return bool(vis.get("marketplace"))
    return str(tenant.id) in (vis.get("tenants") or [])


def _location(pv: dict) -> dict | None:
    if pv.get("latitude") is None or pv.get("longitude") is None:
        return None
    return {"type": "Point", "coordinates": [pv["longitude"], pv["latitude"]]}


async def apply_to_offers(mongo: Mongo, partner_id: str) -> None:
    pv = await mongo.visibility.find_one({"_id": partner_id})
    if pv is None:
        return
    update: dict = {
        "visibility": compute_visibility(pv),
        "partner_name": pv.get("display_name"),
        "partner_type": pv.get("type"),
    }
    loc = _location(pv)
    ops = {"$set": update}
    if loc:
        update["location"] = loc
    else:
        ops["$unset"] = {"location": ""}
    await mongo.offers.update_many({"partner_id": partner_id}, ops)


async def upsert_partner(mongo: Mongo, data: dict) -> None:
    pid = str(data["partner_id"])
    await mongo.visibility.update_one(
        {"_id": pid},
        {"$set": {
            "type": data["type"], "display_name": data["display_name"], "status": data["status"],
            "owner_tenant_id": data.get("owner_tenant_id"), "latitude": data.get("latitude"),
            "longitude": data.get("longitude"), "service_radius_km": data.get("service_radius_km"),
        }},
        upsert=True,
    )
    await apply_to_offers(mongo, pid)


async def upsert_membership(mongo: Mongo, data: dict, removed: bool = False) -> None:
    if not data.get("partner_id"):
        return  # doctors are filtered by the consult bridge, not the catalog
    pid, tid = str(data["partner_id"]), str(data["tenant_id"])
    if removed:
        change = {"$unset": {f"tenants.{tid}": ""}}
    else:
        change = {"$set": {f"tenants.{tid}": {"listed": bool(data["listed_on_marketplace"]), "status": data["status"]}}}
    await mongo.visibility.update_one({"_id": pid}, change, upsert=True)
    await apply_to_offers(mongo, pid)


async def ensure_partner(mongo: Mongo, partners: ServiceClient, tenants: ServiceClient, partner_id: str) -> dict:
    """Backfill visibility for a partner whose events we have not seen yet (e.g. a fresh catalog DB)."""
    pv = await mongo.visibility.find_one({"_id": partner_id})
    if pv is not None:
        return pv
    partner = await partners.get(f"/internal/partners/{partner_id}")
    await upsert_partner(mongo, {**partner, "partner_id": partner["id"]})
    for m in await tenants.get(f"/internal/partners/{partner_id}/memberships"):
        await upsert_membership(mongo, m)
    return await mongo.visibility.find_one({"_id": partner_id})
