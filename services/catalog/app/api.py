import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Query, Request
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app import stock, visibility
from app.config import settings
from app.mongo import Mongo, from_mongo, to_mongo
from app.schemas import (
    FeatureRequest,
    OfferOut,
    OfferPage,
    PartnerOfferOut,
    ProductDetail,
    ProductOut,
    ProductPage,
    SearchPage,
    StockUpdated,
    OfferCreate,
    OfferPatch,
    PrescriptionMatchRequest,
    ProductCreate,
    ProductPatch,
    QuoteRequest,
    ReservationRequest,
    StockUpdate,
)
from gj_common.auth import GJ_ADMIN, GJ_OPS, CurrentUser, InternalOnly, OptionalUser, require_roles
from gj_common.errors import Conflict, Forbidden, NotFound, ValidationFailed
from gj_common.db import uuid7
from gj_common.logging import get_logger
from gj_common.money import money
from gj_common.tenancy import CurrentTenant, TenantContext

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])
internal = APIRouter(prefix="/internal/catalog", tags=["internal"], dependencies=[InternalOnly])

_IN_STOCK = {"$or": [{"stock": None}, {"stock": {"$gt": 0}}]}
_HIDDEN_FIELDS = {"visibility": 0}


def _mongo(request: Request) -> Mongo:
    return request.app.state.mongo


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _product_snapshot(p: dict) -> dict:
    return {
        "name": p["name"], "type": p["type"], "category": p["category"], "brand": p.get("brand"),
        "generic_name": p.get("generic_name"), "requires_prescription": p.get("requires_prescription", False),
        "sku": p["sku"], "lab": p.get("lab"), "image": (p.get("images") or [None])[0],
    }


# ---------------------------------------------------------------- browse & search
@router.get("/search", response_model=SearchPage)
async def search(
    request: Request,
    tenant: CurrentTenant,
    q: str | None = Query(None, max_length=100),
    type: str | None = None,
    category: str | None = None,
    requires_prescription: bool | None = None,
    partner_id: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    radius_km: float = Query(25, gt=0, le=200),
    sort: str = Query("relevance", pattern="^(relevance|price_asc|price_desc)$"),
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0, le=5000),
):
    """Search medicines, lab tests and wellness products. Returns one row per product with its best offer and
    how many providers sell it. Only providers visible in the caller's app are included: every listed provider on
    GeneJunction, or only the clinic group's own network in a white-label app. Pass `lat`/`lng` to limit to
    providers near the patient."""
    match: dict = {"channel": "retail", "active": True, **visibility.tenant_filter(tenant), **_IN_STOCK}
    if q:
        match["$text"] = {"$search": q}
    if type:
        match["product.type"] = type
    if category:
        match["product.category"] = category
    if requires_prescription is not None:
        match["product.requires_prescription"] = requires_prescription
    if partner_id:
        match["partner_id"] = partner_id
    if lat is not None and lng is not None:
        match["location"] = {"$geoWithin": {"$centerSphere": [[lng, lat], radius_km / 6378.1]}}
    group_sort = {"relevance": {"featured": -1, "score": -1, "min_price": 1},
                  "price_asc": {"min_price": 1}, "price_desc": {"min_price": -1}}[sort]
    pipeline = [
        {"$match": match},
        {"$addFields": {"featured": {"$gt": ["$featured_until", _now()]},
                        "score": {"$meta": "textScore"} if q else 0}},
        {"$sort": {"featured": -1, "score": -1, "price": 1}},
        {"$group": {"_id": "$product_id", "product": {"$first": "$product"}, "best_offer": {"$first": "$$ROOT"},
                    "min_price": {"$min": "$price"}, "offer_count": {"$sum": 1},
                    "featured": {"$max": "$featured"}, "score": {"$max": "$score"}}},
        {"$sort": {**group_sort, "_id": 1}},
        {"$facet": {"items": [{"$skip": offset}, {"$limit": limit}], "total": [{"$count": "n"}]}},
    ]
    result = await (await _mongo(request).offers.aggregate(pipeline)).to_list(1)
    facet = result[0] if result else {"items": [], "total": []}
    items = []
    for row in facet["items"]:
        row = from_mongo(row)
        offer = row["best_offer"]
        offer.pop("visibility", None)
        items.append({"product_id": row["id"], "product": row["product"], "min_price": row["min_price"],
                      "offer_count": row["offer_count"], "featured": row["featured"], "best_offer": offer})
    total = facet["total"][0]["n"] if facet["total"] else 0
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/categories", response_model=list[str])
async def categories(request: Request, tenant: CurrentTenant, type: str | None = None):
    match = {"channel": "retail", "active": True, **visibility.tenant_filter(tenant)}
    if type:
        match["product.type"] = type
    return sorted(c for c in await _mongo(request).offers.distinct("product.category", match) if c)


@router.get("/products/{product_id}", response_model=ProductDetail)
async def get_product(product_id: str, request: Request, tenant: CurrentTenant):
    mongo = _mongo(request)
    product = await mongo.products.find_one({"_id": product_id})
    if product is None:
        raise NotFound("Product not found")
    cursor = mongo.offers.find({"product_id": product_id, "channel": "retail", "active": True,
                                **visibility.tenant_filter(tenant)}, _HIDDEN_FIELDS).sort("price", 1).limit(50)
    offers = [from_mongo(o) for o in await cursor.to_list(50)]
    return {**from_mongo(product), "offers": offers}


@router.get("/offers/{offer_id}", response_model=OfferOut)
async def get_offer(offer_id: str, request: Request, tenant: CurrentTenant):
    offer = await _mongo(request).offers.find_one({"_id": offer_id})
    if offer is None or not visibility.visible_to(offer, tenant):
        raise NotFound("Offer not found")
    offer.pop("visibility", None)
    return from_mongo(offer)


# ---------------------------------------------------------------- master products
@router.post("/products", status_code=201, response_model=ProductOut)
async def create_product(body: ProductCreate, request: Request, principal: CurrentUser,
                         partner_id: str | None = Query(None, description="Partner proposing the product")):
    if principal.is_staff:
        status = "active"
    elif partner_id and principal.can_act_for_partner(uuid.UUID(partner_id)):
        status = "pending_review"
    else:
        raise Forbidden("Only GeneJunction staff or partners can add products")
    now = _now()
    doc = {"_id": str(uuid7()), **body.model_dump(), "slug": _slug(body.name), "status": status,
           "proposed_by_partner_id": partner_id, "created_at": now, "updated_at": now}
    try:
        await _mongo(request).products.insert_one(to_mongo(doc))
    except DuplicateKeyError as exc:
        raise Conflict("SKU already exists", code="duplicate_sku") from exc
    return from_mongo(doc)


@router.get("/products", response_model=ProductPage)
async def list_products(request: Request, q: str | None = None, type: str | None = None,
                        status: str = "active", limit: int = Query(20, le=100), offset: int = 0):
    """The master product list partners pick from when adding offers."""
    flt: dict = {"status": status}
    if q:
        flt["$text"] = {"$search": q}
    if type:
        flt["type"] = type
    mongo = _mongo(request)
    docs = await mongo.products.find(flt).sort("name", 1).skip(offset).limit(limit).to_list(limit)
    return {"items": [from_mongo(d) for d in docs], "total": await mongo.products.count_documents(flt),
            "limit": limit, "offset": offset}


@router.patch("/products/{product_id}", response_model=ProductOut, dependencies=[require_roles(GJ_ADMIN, GJ_OPS)])
async def patch_product(product_id: str, body: ProductPatch, request: Request):
    mongo = _mongo(request)
    changes = body.model_dump(exclude_unset=True)
    doc = await mongo.products.find_one_and_update(
        {"_id": product_id}, {"$set": to_mongo({**changes, "updated_at": _now()})}, return_document=ReturnDocument.AFTER)
    if doc is None:
        raise NotFound("Product not found")
    await mongo.offers.update_many({"product_id": product_id}, {"$set": {"product": _product_snapshot(doc)}})
    if doc["status"] == "active":
        await mongo.offers.update_many({"product_id": product_id, "awaiting_product_approval": True},
                                       {"$set": {"active": True}, "$unset": {"awaiting_product_approval": ""}})
    else:
        await mongo.offers.update_many({"product_id": product_id}, {"$set": {"active": False}})
    return from_mongo(doc)


# ---------------------------------------------------------------- partner offers
async def _partner_guard(request: Request, principal, partner_id: str) -> dict:
    principal.ensure_partner(uuid.UUID(partner_id))
    st = request.app.state
    return await visibility.ensure_partner(st.mongo, st.partners, st.tenants, partner_id)


@router.get("/partners/{partner_id}/offers", response_model=OfferPage)
async def partner_offers(partner_id: str, request: Request, principal: CurrentUser, channel: str | None = None,
                         low_stock: bool = False, limit: int = Query(50, le=200), offset: int = 0):
    """Partner portal "Products & stock": the partner's own offers, including where each is visible."""
    principal.ensure_partner(uuid.UUID(partner_id))
    flt: dict = {"partner_id": partner_id}
    if channel:
        flt["channel"] = channel
    if low_stock:
        flt["stock"] = {"$lte": settings.low_stock_threshold}
    mongo = _mongo(request)
    docs = await mongo.offers.find(flt).sort("product.name", 1).skip(offset).limit(limit).to_list(limit)
    return {"items": [from_mongo(d) for d in docs], "total": await mongo.offers.count_documents(flt),
            "limit": limit, "offset": offset}


@router.post("/partners/{partner_id}/offers", status_code=201, response_model=PartnerOfferOut)
async def create_offer(partner_id: str, body: OfferCreate, request: Request, principal: CurrentUser):
    """Sell a product from the master list. Pharmacies sell medicine (retail), labs sell lab tests, stockists
    sell on the wholesale channel with optional quantity tiers. Offers go live once the partner is verified."""
    pv = await _partner_guard(request, principal, partner_id)
    mongo = _mongo(request)
    product = await mongo.products.find_one({"_id": body.product_id})
    if product is None or product["status"] not in ("active", "pending_review"):
        raise NotFound("Product not found")
    if body.channel == "wholesale" and pv["type"] != "stockist":
        raise ValidationFailed("Only stockists sell wholesale")
    if body.channel == "retail" and pv["type"] == "stockist":
        raise ValidationFailed("Stockists sell on the wholesale channel")
    if product["type"] == "lab_test" and pv["type"] != "lab":
        raise ValidationFailed("Only labs can offer lab tests")
    if product["type"] == "medicine" and body.channel == "retail" and pv["type"] != "pharmacy":
        raise ValidationFailed("Only pharmacies sell medicine to patients")
    now = _now()
    doc = {
        "_id": str(uuid7()), "partner_id": partner_id, "product_id": body.product_id,
        **body.model_dump(exclude={"product_id"}),
        "wholesale_tiers": sorted([t.model_dump() for t in body.wholesale_tiers], key=lambda t: t["min_qty"]),
        "product": _product_snapshot(product), "featured_until": None, "created_at": now, "updated_at": now,
    }
    if product["status"] != "active":
        doc["active"] = False  # goes live once ops approves the product
        doc["awaiting_product_approval"] = True
    try:
        await mongo.offers.insert_one(to_mongo(doc))
    except DuplicateKeyError as exc:
        raise Conflict("You already have an offer for this product on this channel") from exc
    await visibility.apply_to_offers(mongo, partner_id)
    return from_mongo(await mongo.offers.find_one({"_id": doc["_id"]}))


@router.patch("/partners/{partner_id}/offers/{offer_id}", response_model=PartnerOfferOut)
async def patch_offer(partner_id: str, offer_id: str, body: OfferPatch, request: Request, principal: CurrentUser):
    principal.ensure_partner(uuid.UUID(partner_id))
    changes = body.model_dump(exclude_unset=True)
    if "wholesale_tiers" in changes:
        changes["wholesale_tiers"] = sorted(changes["wholesale_tiers"], key=lambda t: t["min_qty"])
    doc = await _mongo(request).offers.find_one_and_update(
        {"_id": offer_id, "partner_id": partner_id}, {"$set": to_mongo({**changes, "updated_at": _now()})},
        return_document=ReturnDocument.AFTER)
    if doc is None:
        raise NotFound("Offer not found")
    return from_mongo(doc)


@router.post("/partners/{partner_id}/offers/stock", response_model=StockUpdated)
async def bulk_stock(partner_id: str, body: list[StockUpdate], request: Request, principal: CurrentUser):
    principal.ensure_partner(uuid.UUID(partner_id))
    mongo = _mongo(request)
    updated = 0
    for item in body[:500]:
        res = await mongo.offers.update_one({"_id": item.offer_id, "partner_id": partner_id},
                                            {"$set": {"stock": item.stock, "updated_at": _now()}})
        updated += res.modified_count
    return {"updated": updated}


@router.get("/wholesale/search", response_model=OfferPage)
async def wholesale_search(request: Request, principal: CurrentUser, q: str | None = None,
                           stockist_id: str | None = None, limit: int = Query(20, le=50), offset: int = 0):
    """Stockist catalogue for pharmacies, labs and clinics restocking (B2B)."""
    if not (principal.partner_ids or principal.is_staff):
        raise Forbidden("Business accounts only")
    flt: dict = {"channel": "wholesale", "active": True, "visibility.verified": True, **_IN_STOCK}
    if q:
        flt["$text"] = {"$search": q}
    if stockist_id:
        flt["partner_id"] = stockist_id
    mongo = _mongo(request)
    docs = await mongo.offers.find(flt, _HIDDEN_FIELDS).sort("product.name", 1).skip(offset).limit(limit).to_list(limit)
    return {"items": [from_mongo(d) for d in docs], "total": await mongo.offers.count_documents(flt),
            "limit": limit, "offset": offset}


# ---------------------------------------------------------------- internal: checkout support
def _unit_price(offer: dict, qty: int) -> Decimal:
    price = offer["price"]
    for tier in offer.get("wholesale_tiers") or []:
        if qty >= tier["min_qty"]:
            price = tier["price"]
    return money(price)


@internal.post("/quote")
async def quote(body: QuoteRequest, request: Request):
    """Authoritative prices and availability for a cart. Raises 409 listing every problem."""
    mongo = _mongo(request)
    tenant: TenantContext | None = None
    if body.tenant_id and body.channel == "retail":
        tenant = await request.app.state.tenant_resolver.get_by_id(body.tenant_id)
    ids = [i.offer_id for i in body.items]
    offers = {o["_id"]: from_mongo(o) for o in await mongo.offers.find({"_id": {"$in": ids}}).to_list(len(ids))}
    lines, problems = [], []
    for item in body.items:
        offer = offers.get(item.offer_id)
        if offer is None or not offer["active"] or offer["channel"] != body.channel:
            problems.append({"offer_id": item.offer_id, "problem": "unavailable"})
            continue
        vis = offer.get("visibility") or {}
        if not vis.get("verified") or (tenant and not visibility.visible_to({"visibility": vis}, tenant)):
            problems.append({"offer_id": item.offer_id, "problem": "not_available_in_this_app"})
            continue
        if item.qty < offer.get("min_order_qty", 1):
            problems.append({"offer_id": item.offer_id, "problem": "below_min_order_qty", "min": offer["min_order_qty"]})
            continue
        if offer.get("stock") is not None and offer["stock"] < item.qty:
            problems.append({"offer_id": item.offer_id, "problem": "insufficient_stock", "available": offer["stock"]})
            continue
        unit = _unit_price(offer, item.qty)
        lines.append({
            "offer_id": offer["id"], "product_id": offer["product_id"], "partner_id": offer["partner_id"],
            "partner_type": offer.get("partner_type"), "partner_name": offer.get("partner_name"),
            "product": offer["product"], "qty": item.qty, "unit_price": unit, "mrp": offer.get("mrp"),
            "line_total": money(unit * item.qty),
        })
    if problems and body.strict:
        raise Conflict("Some items cannot be ordered", code="cart_invalid", details=problems)
    return {"lines": lines, "problems": problems, "currency": "MYR"}


@internal.post("/reservations")
async def reserve(body: ReservationRequest, request: Request):
    """All-or-nothing stock hold for a checkout; safe to repeat with the same id."""
    return await stock.reserve(_mongo(request), body.reservation_id, [i.model_dump() for i in body.items])


@internal.post("/reservations/{reservation_id}/release")
async def release(reservation_id: str, request: Request):
    return {"released": await stock.release(_mongo(request), reservation_id)}


@internal.post("/reservations/{reservation_id}/commit")
async def commit(reservation_id: str, request: Request):
    return {"status": await stock.commit(_mongo(request), reservation_id)}


@internal.post("/prescription-match")
async def prescription_match(body: PrescriptionMatchRequest, request: Request):
    """Turn a doctor's prescription into concrete offers the patient can buy in their app."""
    mongo = _mongo(request)
    tenant = await request.app.state.tenant_resolver.get_by_id(body.tenant_id)
    matched, unmatched = [], []
    for item in body.items:
        pflt: dict = {"type": item.kind, "status": "active"}
        if item.sku:
            pflt["sku"] = item.sku
        elif item.name:
            pflt["$text"] = {"$search": item.name}
        else:
            unmatched.append(item.model_dump())
            continue
        products = await mongo.products.find(pflt, {"_id": 1}).limit(5).to_list(5)
        if not products:
            unmatched.append(item.model_dump())
            continue
        oflt: dict = {"product_id": {"$in": [p["_id"] for p in products]}, "channel": "retail", "active": True,
                      **visibility.tenant_filter(tenant),
                      "$or": [{"stock": None}, {"stock": {"$gte": item.qty}}]}
        if body.latitude is not None and body.longitude is not None:
            oflt["location"] = {"$geoWithin": {"$centerSphere": [[body.longitude, body.latitude], 25 / 6378.1]}}
        offer = await mongo.offers.find_one(oflt, _HIDDEN_FIELDS, sort=[("price", 1)])
        if offer is None:
            unmatched.append(item.model_dump())
        else:
            matched.append({"item": item.model_dump(), "offer": from_mongo(offer)})
    return {"matched": matched, "unmatched": unmatched}


@internal.post("/featured")
async def feature_partner(body: FeatureRequest, request: Request):
    """Featured listing paid with credits (billing calls this after charging)."""
    res = await _mongo(request).offers.update_many(
        {"partner_id": str(body.partner_id), "channel": "retail"}, {"$set": {"featured_until": body.until}})
    return {"offers_featured": res.modified_count}
