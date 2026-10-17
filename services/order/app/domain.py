"""Order lifecycle: checkout saga, payment outcomes, sub-order transitions, refunds."""

import secrets
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Cart, CartItem, Order, OrderItem, OrderStatusHistory, Prescription, SubOrder
from app.schemas import CheckoutRequest
from app.state import CUSTOMER_CANCELLABLE, TERMINAL, can_transition
from gj_common.auth import Principal
from gj_common.db import uuid7
from gj_common.errors import AppError, Conflict, NotFound, ValidationFailed
from gj_common.events import emit
from gj_common.logging import get_logger
from gj_common.money import ZERO, money
from gj_common.tenancy import TenantContext

log = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_order_number() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return f"GJ{_now():%y%m%d}-" + "".join(secrets.choice(alphabet) for _ in range(6))


def _history(session: AsyncSession, order: Order, sub: SubOrder | None, old: str | None, new: str,
             actor: str, note: str | None = None) -> None:
    session.add(OrderStatusHistory(order_id=order.id, sub_order_id=sub.id if sub else None,
                                   from_status=old, to_status=new, actor=actor, note=note))


def order_payload(order: Order) -> dict:
    return {
        "order_id": order.id, "number": order.number, "user_id": order.user_id, "tenant_id": order.tenant_id,
        "channel": order.channel, "status": order.status, "subtotal": order.subtotal,
        "delivery_fee": order.delivery_fee, "total": order.total, "delivery_address": order.delivery_address,
        "contact": order.contact, "payment_id": order.payment_id,
        "sub_orders": [sub_payload(s) for s in order.sub_orders],
    }


def sub_payload(s: SubOrder) -> dict:
    return {
        "sub_order_id": s.id, "seq": s.seq, "partner_id": s.partner_id, "partner_name": s.partner_name,
        "kind": s.kind, "fulfilment": s.fulfilment, "status": s.status, "subtotal": s.subtotal,
        "delivery_fee": s.delivery_fee, "total": s.total, "requires_prescription": s.requires_prescription,
        "prescription_id": s.prescription_id, "prescription_document_key": s.prescription_document_key,
        "schedule": s.schedule, "external_ref": s.external_ref,
        "items": [{"offer_id": i.offer_id, "product_id": i.product_id, "sku": i.sku, "name": i.name,
                   "product_type": i.product_type, "qty": i.qty, "unit_price": i.unit_price,
                   "line_total": i.line_total, "requires_prescription": i.requires_prescription, "meta": i.meta}
                  for i in s.items],
    }


async def get_cart(session: AsyncSession, user_id: uuid.UUID, tenant_id: uuid.UUID, *, create: bool = True) -> Cart | None:
    cart = await session.scalar(select(Cart).where(Cart.user_id == user_id, Cart.tenant_id == tenant_id))
    if cart is None and create:
        cart = Cart(user_id=user_id, tenant_id=tenant_id, items=[])
        session.add(cart)
        await session.flush()
    return cart


# ---------------------------------------------------------------- checkout saga
async def checkout(app: FastAPI, session: AsyncSession, principal: Principal, tenant: TenantContext,
                   body: CheckoutRequest, idempotency_key: str) -> dict:
    existing = await session.scalar(select(Order).where(Order.user_id == principal.user_id,
                                                        Order.idempotency_key == idempotency_key))
    if existing is not None:
        payment = await app.state.payments.get(f"/internal/payments/{existing.payment_id}") if existing.payment_id else None
        return {"order": existing, "payment": payment}

    cart = await get_cart(session, principal.user_id, tenant.id, create=False)
    if cart is None or not cart.items:
        raise ValidationFailed("Your cart is empty", code="cart_empty")
    product_items = [i for i in cart.items if i.kind == "product"]
    consult_items = [i for i in cart.items if i.kind == "consultation"]

    order_id = uuid7()
    order = Order(id=order_id, number=new_order_number(), user_id=principal.user_id, tenant_id=tenant.id,
                  channel=tenant.channel, status="pending_payment", subtotal=ZERO, delivery_fee=ZERO, total=ZERO,
                  delivery_address=body.delivery_address.model_dump() if body.delivery_address else None,
                  contact={"name": principal.name, "phone": principal.phone, "email": principal.email},
                  idempotency_key=idempotency_key, sub_orders=[])
    seq = 0

    if product_items:
        quote = await app.state.catalog.post("/internal/catalog/quote", json={
            "tenant_id": str(tenant.id), "channel": "retail",
            "items": [{"offer_id": i.offer_id, "qty": i.qty} for i in product_items]})
        rx_by_offer = {i.offer_id: i.prescription_id for i in product_items if i.prescription_id}
        groups: dict[str, list[dict]] = defaultdict(list)
        for line in quote["lines"]:
            groups[line["partner_id"]].append(line)
        for partner_id, lines in groups.items():
            seq += 1
            kind = "lab" if lines[0]["partner_type"] == "lab" else "pharmacy"
            subtotal = money(sum(Decimal(str(l["line_total"])) for l in lines))
            requires_rx = any(l["product"].get("requires_prescription") for l in lines)
            prescription_id = next((rx_by_offer[l["offer_id"]] for l in lines if l["offer_id"] in rx_by_offer), None)
            if kind == "pharmacy":
                fulfilment = body.pharmacy_fulfilment
                if fulfilment == "delivery" and body.delivery_address is None:
                    raise ValidationFailed("A delivery address is required", code="address_required")
                fee = settings.pharmacy_delivery_fee if fulfilment == "delivery" and subtotal < settings.free_delivery_threshold else ZERO
                schedule = None
                if requires_rx and not (prescription_id or body.prescription_document_key):
                    raise ValidationFailed("A prescription is required for some items", code="prescription_required",
                                           details=[l["offer_id"] for l in lines if l["product"].get("requires_prescription")])
            else:
                if body.lab_collection is None:
                    raise ValidationFailed("Choose a sample collection date and slot", code="lab_collection_required")
                fulfilment = body.lab_collection.type
                if fulfilment == "home_collection" and body.delivery_address is None:
                    raise ValidationFailed("An address is required for home sample collection", code="address_required")
                fee = settings.lab_home_collection_fee if fulfilment == "home_collection" else ZERO
                schedule = body.lab_collection.model_dump(mode="json")
            sub = SubOrder(id=uuid7(), seq=seq, partner_id=uuid.UUID(partner_id), partner_name=lines[0]["partner_name"],
                           kind=kind, fulfilment=fulfilment, status="pending_payment", subtotal=subtotal,
                           delivery_fee=money(fee), total=money(subtotal + fee), requires_prescription=requires_rx,
                           prescription_id=prescription_id, prescription_document_key=body.prescription_document_key,
                           schedule=schedule, items=[])
            for l in lines:
                sub.items.append(OrderItem(
                    offer_id=l["offer_id"], product_id=l["product_id"], sku=l["product"].get("sku"),
                    name=l["product"]["name"], product_type=l["product"]["type"], qty=l["qty"],
                    unit_price=money(l["unit_price"]), line_total=money(l["line_total"]),
                    requires_prescription=bool(l["product"].get("requires_prescription"))))
            order.sub_orders.append(sub)

    holds: list[str] = []
    for item in consult_items:
        seq += 1
        hold = await app.state.bridge.post("/internal/consult/holds", json={
            **item.consultation, "user_id": str(principal.user_id), "tenant_id": str(tenant.id),
            "reference": str(order_id)})
        holds.append(hold["hold_id"])
        fee = money(hold["fee"])
        sub = SubOrder(id=uuid7(), seq=seq, partner_id=uuid.UUID(hold["partner_id"]) if hold.get("partner_id") else None,
                       partner_name=hold["doctor_name"], kind="consultation", fulfilment="teleconsult",
                       status="pending_payment", subtotal=fee, delivery_fee=ZERO, total=fee,
                       schedule={"slot_start": hold["slot_start"], "slot_end": hold["slot_end"], "hold_id": hold["hold_id"],
                                 "doctor_id": item.consultation["doctor_id"], "slot_id": item.consultation["slot_id"]},
                       items=[OrderItem(name=f"Consultation · {hold['doctor_name']}", product_type="consultation",
                                        qty=1, unit_price=fee, line_total=fee,
                                        meta={"specialty": hold.get("specialty"), "doctor_id": item.consultation["doctor_id"]})])
        order.sub_orders.append(sub)

    order.subtotal = money(sum(s.subtotal for s in order.sub_orders))
    order.delivery_fee = money(sum(s.delivery_fee for s in order.sub_orders))
    order.total = money(order.subtotal + order.delivery_fee)
    session.add(order)
    _history(session, order, None, None, "pending_payment", f"user:{principal.user_id}")
    emit(session, "order.placed", key=order.id, tenant_id=tenant.id, data=order_payload(order))
    await session.commit()

    # Saga: reserve stock, then open the payment. Compensate on failure.
    try:
        if product_items:
            await app.state.catalog.post("/internal/catalog/reservations", json={
                "reservation_id": str(order.id),
                "items": [{"offer_id": i.offer_id, "qty": i.qty} for s in order.sub_orders for i in s.items if i.offer_id]})
        payment = await app.state.payments.post("/internal/payments", json={
            "purpose": "order", "reference_id": str(order.id), "amount": str(order.total), "currency": "MYR",
            "payer_type": "user", "payer_id": str(principal.user_id), "tenant_id": str(tenant.id),
            "method": body.payment_method, "return_url": body.return_url,
            "description": f"Order {order.number}", "idempotency_key": f"order:{order.id}",
            "expires_in_minutes": settings.payment_window_minutes})
    except AppError:
        await _abort_checkout(app, session, order, holds, "checkout_failed")
        raise

    order.payment_id = payment["id"]
    ordered_offer_ids = {i.offer_id for i in product_items}
    for item in list(cart.items):
        if item.kind == "consultation" or item.offer_id in ordered_offer_ids:
            cart.items.remove(item)
    await session.commit()
    return {"order": order, "payment": payment}


async def _abort_checkout(app: FastAPI, session: AsyncSession, order: Order, holds: list[str], reason: str) -> None:
    try:
        await app.state.catalog.post(f"/internal/catalog/reservations/{order.id}/release")
    except AppError:
        pass
    for hold_id in holds:
        try:
            await app.state.bridge.post(f"/internal/consult/holds/{hold_id}/release")
        except AppError:
            pass
    await set_order_status(session, order, "cancelled", "system", reason)
    order.cancelled_at = _now()
    order.cancel_reason = reason
    emit(session, "order.cancelled", key=order.id, tenant_id=order.tenant_id, data={**order_payload(order), "reason": reason})
    await session.commit()


async def set_order_status(session: AsyncSession, order: Order, new: str, actor: str, note: str | None = None) -> None:
    for sub in order.sub_orders:
        if sub.status not in TERMINAL:
            _history(session, order, sub, sub.status, new, actor, note)
            sub.status = new
    _history(session, order, None, order.status, new, actor, note)
    order.status = new


# ---------------------------------------------------------------- payment outcomes
async def on_payment_succeeded(app: FastAPI, session: AsyncSession, data: dict) -> None:
    order = await session.get(Order, uuid.UUID(data["reference_id"]), with_for_update=True)
    if order is None:
        log.error("payment_for_unknown_order", reference_id=data["reference_id"])
        return
    if order.status != "pending_payment":
        if order.status in ("expired", "cancelled"):
            # Paid after the window closed: give the money back.
            await app.state.payments.post(f"/internal/payments/{data['payment_id']}/refunds", json={
                "amount": data["amount"], "reason": "Order expired before payment completed",
                "reference": f"late-payment:{order.id}"})
        return
    order.status = "paid"
    order.paid_at = _now()
    order.payment_id = data["payment_id"]
    for sub in order.sub_orders:
        _history(session, order, sub, sub.status, "placed", "payment")
        sub.status = "placed"
    _history(session, order, None, "pending_payment", "paid", "payment")
    if any(i.offer_id for s in order.sub_orders for i in s.items):
        await app.state.catalog.post(f"/internal/catalog/reservations/{order.id}/commit")
    rx_ids = {s.prescription_id for s in order.sub_orders if s.prescription_id}
    for rx in rx_ids:
        prescription = await session.get(Prescription, rx)
        if prescription:
            prescription.status = "ordered"
    emit(session, "order.paid", key=order.id, tenant_id=order.tenant_id, data=order_payload(order))


async def on_payment_failed(app: FastAPI, session: AsyncSession, data: dict) -> None:
    order = await session.get(Order, uuid.UUID(data["reference_id"]), with_for_update=True)
    if order is None or order.status != "pending_payment":
        return
    await release_resources(app, order)
    await set_order_status(session, order, "payment_failed", "payment", data.get("reason"))
    emit(session, "order.payment_failed", key=order.id, tenant_id=order.tenant_id, data=order_payload(order))


async def release_resources(app: FastAPI, order: Order) -> None:
    if any(i.offer_id for s in order.sub_orders for i in s.items):
        await app.state.catalog.post(f"/internal/catalog/reservations/{order.id}/release")
    for s in order.sub_orders:
        if s.kind == "consultation" and s.schedule and s.schedule.get("hold_id"):
            await app.state.bridge.post(f"/internal/consult/holds/{s.schedule['hold_id']}/release")


# ---------------------------------------------------------------- sub-order transitions
async def apply_sub_order_status(app: FastAPI, session: AsyncSession, sub_order_id: uuid.UUID, new: str, actor: str,
                                 *, note: str | None = None, tracking: dict | None = None,
                                 schedule: dict | None = None, external_ref: str | None = None) -> SubOrder:
    sub = await session.get(SubOrder, sub_order_id, with_for_update=True)
    if sub is None:
        raise NotFound("Sub-order not found")
    order = await session.get(Order, sub.order_id, with_for_update=True)
    if sub.status == new:
        return sub
    if not can_transition(sub.kind, sub.status, new):
        raise Conflict(f"Cannot move {sub.kind} order from {sub.status} to {new}", code="invalid_transition")
    old = sub.status
    sub.status = new
    if tracking:
        sub.tracking = {**(sub.tracking or {}), **tracking}
    if schedule:
        sub.schedule = {**(sub.schedule or {}), **schedule}
    if external_ref:
        sub.external_ref = external_ref
    _history(session, order, sub, old, new, actor, note)
    emit(session, "order.sub_order.status_changed", key=order.id, tenant_id=order.tenant_id, data={
        "order_id": order.id, "number": order.number, "user_id": order.user_id, "channel": order.channel,
        "old_status": old, **sub_payload(sub), "note": note})

    if new in ("rejected", "cancelled", "booking_failed") and old != "pending_payment":
        await refund_sub_order(app, session, order, sub, note or new)
        if sub.kind == "pharmacy" and old in ("placed", "accepted", "rx_verified"):
            for item in sub.items:  # put the stock back
                try:
                    await app.state.catalog.post("/internal/catalog/stock-adjust", params={
                        "partner_id": str(sub.partner_id), "product_id": item.product_id, "delta": item.qty})
                except AppError:
                    pass

    await _roll_up(session, order)
    return sub


async def refund_sub_order(app: FastAPI, session: AsyncSession, order: Order, sub: SubOrder, reason: str) -> None:
    if not order.payment_id or sub.total <= 0:
        return
    await app.state.payments.post(f"/internal/payments/{order.payment_id}/refunds", json={
        "amount": str(sub.total), "reason": reason, "reference": f"sub-order:{sub.id}"})
    order.refunded_amount = money(order.refunded_amount + sub.total)
    emit(session, "order.sub_order.refunded", key=order.id, tenant_id=order.tenant_id, data={
        "order_id": order.id, "sub_order_id": sub.id, "partner_id": sub.partner_id, "amount": sub.total,
        "channel": order.channel, "reason": reason})


async def _roll_up(session: AsyncSession, order: Order) -> None:
    statuses = {s.status for s in order.sub_orders}
    if not statuses <= TERMINAL:
        return
    done = {"completed"} & statuses
    new = ("completed" if statuses == {"completed"} else "partially_completed") if done else "cancelled"
    if order.status != new:
        _history(session, order, None, order.status, new, "system")
        order.status = new
        emit(session, f"order.{new}", key=order.id, tenant_id=order.tenant_id, data=order_payload(order))


async def customer_cancel(app: FastAPI, session: AsyncSession, order: Order, reason: str) -> Order:
    if any(s.status not in CUSTOMER_CANCELLABLE for s in order.sub_orders):
        raise Conflict("Your order is already being prepared and can no longer be cancelled here",
                       code="not_cancellable")
    was_paid = order.status == "paid"
    await release_resources(app, order)
    if was_paid:
        for sub in order.sub_orders:
            await refund_sub_order(app, session, order, sub, reason)
    elif order.payment_id:
        await app.state.payments.post(f"/internal/payments/{order.payment_id}/cancel")
    await set_order_status(session, order, "cancelled", f"user:{order.user_id}", reason)
    order.cancelled_at = _now()
    order.cancel_reason = reason
    emit(session, "order.cancelled", key=order.id, tenant_id=order.tenant_id,
         data={**order_payload(order), "reason": reason, "refunded": was_paid})
    return order


# ---------------------------------------------------------------- prescriptions from consultations
async def on_prescription_issued(app: FastAPI, session: AsyncSession, data: dict) -> None:
    if await session.scalar(select(Prescription.id).where(Prescription.legacy_consult_id == data["legacy_consult_id"])):
        return
    user_id, tenant_id = uuid.UUID(data["user_id"]), uuid.UUID(data["tenant_id"])
    rx = Prescription(id=uuid7(), user_id=user_id, tenant_id=tenant_id, legacy_consult_id=data["legacy_consult_id"],
                      doctor_name=data.get("doctor_name"), items=data["items"], document_url=data.get("document_url"),
                      issued_at=datetime.fromisoformat(data["issued_at"]))
    session.add(rx)
    match = await app.state.catalog.post("/internal/catalog/prescription-match", json={
        "tenant_id": str(tenant_id),
        "items": [{"kind": i["kind"], "sku": i.get("sku"), "name": i.get("name"), "qty": i.get("qty", 1)} for i in data["items"]]})
    cart = await get_cart(session, user_id, tenant_id)
    existing = {i.offer_id: i for i in cart.items}
    for m in match["matched"]:
        offer_id, qty = m["offer"]["id"], m["item"]["qty"]
        if offer_id in existing:
            existing[offer_id].qty = max(existing[offer_id].qty, qty)
            existing[offer_id].prescription_id = rx.id
        else:
            cart.items.append(CartItem(kind="product", offer_id=offer_id, qty=qty, source="prescription", prescription_id=rx.id))
    rx.status = "carted" if match["matched"] else "received"
    emit(session, "order.cart.prescription_added", key=user_id, tenant_id=tenant_id, data={
        "user_id": user_id, "tenant_id": tenant_id, "prescription_id": rx.id, "doctor_name": rx.doctor_name,
        "matched": len(match["matched"]), "unmatched": match["unmatched"]})
