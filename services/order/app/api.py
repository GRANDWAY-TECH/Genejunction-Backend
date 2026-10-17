import uuid
from decimal import Decimal

from fastapi import APIRouter, Header, Query, Request
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import domain
from app.models import CartItem, ExternalOrder, Order, OrderStatusHistory, Prescription, SubOrder
from app.schemas import (
    AddConsultation,
    AddItem,
    CancelRequest,
    CheckoutRequest,
    LegacyOrderImport,
    OrderOut,
    PartnerOrderOut,
    PrescriptionOut,
    StatusChange,
    SubOrderOut,
    UpdateQty,
)
from gj_common.auth import CurrentUser, InternalOnly, staff_only
from gj_common.db import SessionDep
from gj_common.errors import NotFound, ValidationFailed
from gj_common.pagination import Page, PageDep, paginate
from gj_common.tenancy import CurrentTenant

cart_router = APIRouter(prefix="/api/v1/cart", tags=["cart"])
orders = APIRouter(prefix="/api/v1/orders", tags=["orders"])
partner = APIRouter(prefix="/api/v1/partner-orders", tags=["partner orders"])
admin = APIRouter(prefix="/api/v1/admin/orders", tags=["ops"], dependencies=[staff_only()])
rx_router = APIRouter(prefix="/api/v1/prescriptions", tags=["prescriptions"])
internal = APIRouter(prefix="/internal", tags=["internal"], dependencies=[InternalOnly])


# ---------------------------------------------------------------- cart
async def _cart_view(request: Request, cart, tenant_id: uuid.UUID) -> dict:
    products = [i for i in cart.items if i.kind == "product"]
    lines, problems = {}, []
    if products:
        quote = await request.app.state.catalog.post("/internal/catalog/quote", json={
            "tenant_id": str(tenant_id), "channel": "retail", "strict": False,
            "items": [{"offer_id": i.offer_id, "qty": i.qty} for i in products]})
        lines = {l["offer_id"]: l for l in quote["lines"]}
        problems = quote["problems"]
    items = []
    for i in cart.items:
        entry = {"id": i.id, "kind": i.kind, "qty": i.qty, "source": i.source, "prescription_id": i.prescription_id}
        if i.kind == "product":
            entry |= {"offer_id": i.offer_id, "line": lines.get(i.offer_id)}
        else:
            entry |= {"consultation": i.consultation}
        items.append(entry)
    subtotal = sum((Decimal(str(l["line_total"])) for l in lines.values()), start=Decimal("0.00"))
    return {"id": cart.id, "items": items, "problems": problems, "products_subtotal": str(subtotal)}


@cart_router.get("")
async def view_cart(principal: CurrentUser, tenant: CurrentTenant, request: Request, session: SessionDep):
    cart = await domain.get_cart(session, principal.user_id, tenant.id)
    await session.commit()
    return await _cart_view(request, cart, tenant.id)


@cart_router.post("/items", status_code=201)
async def add_item(body: AddItem, principal: CurrentUser, tenant: CurrentTenant, request: Request, session: SessionDep):
    # Validate the offer is sellable in this app before adding it.
    await request.app.state.catalog.post("/internal/catalog/quote", json={
        "tenant_id": str(tenant.id), "channel": "retail", "items": [{"offer_id": body.offer_id, "qty": body.qty}]})
    cart = await domain.get_cart(session, principal.user_id, tenant.id)
    existing = next((i for i in cart.items if i.offer_id == body.offer_id), None)
    if existing:
        existing.qty = body.qty
    else:
        cart.items.append(CartItem(kind="product", offer_id=body.offer_id, qty=body.qty))
    await session.commit()
    return await _cart_view(request, cart, tenant.id)


@cart_router.post("/consultation", status_code=201)
async def add_consultation(body: AddConsultation, principal: CurrentUser, tenant: CurrentTenant, request: Request,
                           session: SessionDep):
    cart = await domain.get_cart(session, principal.user_id, tenant.id)
    if any(i.kind == "consultation" for i in cart.items):
        raise ValidationFailed("Only one consultation can be booked per checkout", code="one_consultation")
    cart.items.append(CartItem(kind="consultation", qty=1, consultation=body.model_dump()))
    await session.commit()
    return await _cart_view(request, cart, tenant.id)


@cart_router.patch("/items/{item_id}")
async def update_item(item_id: uuid.UUID, body: UpdateQty, principal: CurrentUser, tenant: CurrentTenant,
                      request: Request, session: SessionDep):
    cart = await domain.get_cart(session, principal.user_id, tenant.id)
    item = next((i for i in cart.items if i.id == item_id), None)
    if item is None:
        raise NotFound("Cart item not found")
    item.qty = body.qty if item.kind == "product" else 1
    await session.commit()
    return await _cart_view(request, cart, tenant.id)


@cart_router.delete("/items/{item_id}")
async def remove_item(item_id: uuid.UUID, principal: CurrentUser, tenant: CurrentTenant, request: Request,
                      session: SessionDep):
    cart = await domain.get_cart(session, principal.user_id, tenant.id)
    cart.items = [i for i in cart.items if i.id != item_id]
    await session.commit()
    return await _cart_view(request, cart, tenant.id)


@cart_router.delete("", status_code=204)
async def clear_cart(principal: CurrentUser, tenant: CurrentTenant, session: SessionDep):
    cart = await domain.get_cart(session, principal.user_id, tenant.id)
    cart.items = []
    await session.commit()


# ---------------------------------------------------------------- orders (patient)
@orders.post("/checkout", status_code=201)
async def checkout(body: CheckoutRequest, principal: CurrentUser, tenant: CurrentTenant, request: Request,
                   session: SessionDep, idempotency_key: str = Header(..., alias="Idempotency-Key", max_length=100)):
    """One checkout for medicines, lab tests and a consultation. Returns the payment to complete."""
    result = await domain.checkout(request.app, session, principal, tenant, body, idempotency_key)
    return {"order": OrderOut.model_validate(result["order"]), "payment": result["payment"]}


@orders.get("", response_model=Page[OrderOut])
async def my_orders(principal: CurrentUser, tenant: CurrentTenant, session: SessionDep, page: PageDep,
                    status: str | None = None):
    stmt = select(Order).where(Order.user_id == principal.user_id, Order.tenant_id == tenant.id) \
        .order_by(Order.created_at.desc())
    if status:
        stmt = stmt.where(Order.status == status)
    return await paginate(session, stmt, page)


@orders.get("/history")
async def full_history(principal: CurrentUser, session: SessionDep, limit: int = Query(50, le=200)):
    """Marketplace orders plus orders made in the existing GeneJunction system, newest first."""
    own = (await session.scalars(select(Order).where(Order.user_id == principal.user_id)
                                 .order_by(Order.created_at.desc()).limit(limit))).all()
    ext = (await session.scalars(select(ExternalOrder).where(ExternalOrder.user_id == principal.user_id)
                                 .order_by(ExternalOrder.ordered_at.desc()).limit(limit))).all()
    rows = [{"source": "marketplace", "id": o.id, "number": o.number, "status": o.status, "total": o.total,
             "at": o.created_at, "description": ", ".join(s.partner_name or s.kind for s in o.sub_orders)} for o in own]
    rows += [{"source": "genejunction", "id": e.id, "number": e.legacy_order_id, "status": e.status, "total": e.total,
              "at": e.ordered_at, "description": e.description} for e in ext]
    return sorted(rows, key=lambda r: r["at"], reverse=True)[:limit]


async def _own_order(session, principal, order_id: uuid.UUID, lock: bool = False) -> Order:
    order = await session.get(Order, order_id, with_for_update=lock)
    if order is None or (order.user_id != principal.user_id and not principal.is_staff):
        raise NotFound("Order not found")
    return order


@orders.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: uuid.UUID, principal: CurrentUser, session: SessionDep):
    return await _own_order(session, principal, order_id)


@orders.get("/{order_id}/timeline")
async def order_timeline(order_id: uuid.UUID, principal: CurrentUser, session: SessionDep):
    await _own_order(session, principal, order_id)
    rows = (await session.scalars(select(OrderStatusHistory).where(OrderStatusHistory.order_id == order_id)
                                  .order_by(OrderStatusHistory.created_at))).all()
    return [{"sub_order_id": r.sub_order_id, "from": r.from_status, "to": r.to_status, "note": r.note,
             "at": r.created_at} for r in rows]


@orders.post("/{order_id}/cancel", response_model=OrderOut)
async def cancel_order(order_id: uuid.UUID, body: CancelRequest, principal: CurrentUser, request: Request,
                       session: SessionDep):
    order = await _own_order(session, principal, order_id, lock=True)
    await domain.customer_cancel(request.app, session, order, body.reason)
    await session.commit()
    return order


# ---------------------------------------------------------------- prescriptions
@rx_router.get("", response_model=list[PrescriptionOut])
async def my_prescriptions(principal: CurrentUser, session: SessionDep):
    return (await session.scalars(select(Prescription).where(Prescription.user_id == principal.user_id)
                                  .order_by(Prescription.issued_at.desc()).limit(100))).all()


# ---------------------------------------------------------------- partner portal
def _partner_view(sub: SubOrder, order: Order) -> PartnerOrderOut:
    return PartnerOrderOut(sub_order=SubOrderOut.model_validate(sub), order_id=order.id, order_number=order.number,
                           channel=order.channel, tenant_id=order.tenant_id, placed_at=order.created_at,
                           delivery_address=order.delivery_address, contact=order.contact)


@partner.get("", response_model=Page[PartnerOrderOut])
async def partner_orders(partner_id: uuid.UUID, principal: CurrentUser, session: SessionDep, page: PageDep,
                         status: str | None = None, channel: str | None = None):
    """Orders from every app (GJ app, white-label apps), each tagged with its channel and tenant."""
    principal.ensure_partner(partner_id)
    stmt = (select(SubOrder, Order).join(Order, SubOrder.order_id == Order.id)
            .where(SubOrder.partner_id == partner_id, SubOrder.status != "pending_payment")
            .order_by(SubOrder.created_at.desc()))
    if status:
        stmt = stmt.where(SubOrder.status == status)
    if channel:
        stmt = stmt.where(Order.channel == channel)
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = (await session.execute(stmt.limit(page.limit).offset(page.offset))).all()
    return {"items": [_partner_view(s, o) for s, o in rows], "total": total or 0, "limit": page.limit, "offset": page.offset}


@partner.get("/{sub_order_id}", response_model=PartnerOrderOut)
async def partner_order(sub_order_id: uuid.UUID, principal: CurrentUser, session: SessionDep):
    sub = await session.get(SubOrder, sub_order_id)
    if sub is None or sub.partner_id is None:
        raise NotFound("Order not found")
    principal.ensure_partner(sub.partner_id)
    return _partner_view(sub, await session.get(Order, sub.order_id))


# ---------------------------------------------------------------- ops console
@admin.get("", response_model=Page[OrderOut])
async def admin_orders(session: SessionDep, page: PageDep, status: str | None = None, tenant_id: uuid.UUID | None = None,
                       channel: str | None = None, number: str | None = None):
    stmt = select(Order).order_by(Order.created_at.desc())
    if status:
        stmt = stmt.where(Order.status == status)
    if tenant_id:
        stmt = stmt.where(Order.tenant_id == tenant_id)
    if channel:
        stmt = stmt.where(Order.channel == channel)
    if number:
        stmt = stmt.where(Order.number == number)
    return await paginate(session, stmt, page)


# ---------------------------------------------------------------- internal
@internal.get("/orders/{order_id}", response_model=OrderOut)
async def internal_order(order_id: uuid.UUID, session: SessionDep):
    order = await session.get(Order, order_id)
    if order is None:
        raise NotFound("Order not found")
    return order


@internal.get("/sub-orders/{sub_order_id}")
async def internal_sub_order(sub_order_id: uuid.UUID, session: SessionDep):
    sub = await session.get(SubOrder, sub_order_id)
    if sub is None:
        raise NotFound("Sub-order not found")
    order = await session.get(Order, sub.order_id)
    return {**domain.sub_payload(sub), "order_id": order.id, "number": order.number, "user_id": order.user_id,
            "tenant_id": order.tenant_id, "channel": order.channel, "delivery_address": order.delivery_address,
            "contact": order.contact}


@internal.post("/sub-orders/{sub_order_id}/status")
async def internal_set_status(sub_order_id: uuid.UUID, body: StatusChange, request: Request, session: SessionDep):
    """Synchronous variant used by fulfilment services when the caller needs the transition validated now."""
    sub = await domain.apply_sub_order_status(
        request.app, session, sub_order_id, body.status, body.actor,
        note=body.note, tracking=body.tracking, schedule=body.schedule, external_ref=body.external_ref)
    await session.commit()
    return {"sub_order_id": sub.id, "status": sub.status}


@internal.post("/orders/import-legacy", status_code=201)
async def import_legacy(body: LegacyOrderImport, session: SessionDep):
    await session.execute(pg_insert(ExternalOrder).values(**body.model_dump()).on_conflict_do_update(
        index_elements=["legacy_order_id"], set_={"status": body.status, "details": body.details}))
    await session.commit()
    return {"imported": True}
