"""Event -> message copy. All patient-facing copy is sent in the tenant's own brand."""

import uuid

from app.dispatcher import Dispatcher
from gj_common.events import EventEnvelope

dispatcher: Dispatcher | None = None

# status -> (title, body, send_sms)
STATUS_COPY = {
    "accepted": ("Order accepted", "{partner} has accepted your order {number}.", False),
    "dispatched": ("On the way", "Your order {number} from {partner} is out for delivery.", True),
    "ready_for_pickup": ("Ready for pickup", "Your order {number} is ready to collect at {partner}.", True),
    "delivered": ("Delivered", "Your order {number} from {partner} has been delivered.", False),
    "rejected": ("Part of your order was cancelled",
                 "{partner} couldn't fulfil part of order {number}. RM {total} will be refunded to you.", True),
    "scheduled": ("Sample collection booked", "Sample collection for order {number} is on {date} at {slot}.", True),
    "sample_collected": ("Sample collected", "{partner} has collected your sample for order {number}.", False),
    "report_ready": ("Your lab report is ready", "Your report from {partner} is ready in your health folio.", True),
    "booked": ("Consultation confirmed", "Your consultation with {partner} is confirmed.", True),
    "booking_failed": ("Consultation not booked",
                       "We couldn't book your consultation with {partner}. RM {total} will be refunded.", True),
}


def _uuid(v) -> uuid.UUID:
    return uuid.UUID(str(v))


async def order_paid(event: EventEnvelope, session) -> None:
    d = event.data
    await dispatcher.to_user(session, event_id=event.id, user_id=_uuid(d["user_id"]), tenant_id=_uuid(d["tenant_id"]),
                             template="order_confirmed", title="Order confirmed",
                             body=f"Thank you! Order {d['number']} (RM {d['total']}) is confirmed.",
                             data={"order_id": d["order_id"]}, sms=True)
    for sub in d["sub_orders"]:
        if sub.get("partner_id"):
            await dispatcher.to_business(session, owner_type="partner", owner_id=_uuid(sub["partner_id"]),
                                         template="new_order", title=f"New order {d['number']}",
                                         body=f"{len(sub['items'])} item(s), RM {sub['total']} · via {d['channel']}",
                                         data={"sub_order_id": sub["sub_order_id"], "channel": d["channel"]})


async def sub_order_status(event: EventEnvelope, session) -> None:
    d = event.data
    copy = STATUS_COPY.get(d["status"])
    if copy is None:
        return
    title, body, sms = copy
    schedule = d.get("schedule") or {}
    text = body.format(partner=d.get("partner_name") or "Your provider", number=d["number"], total=d["total"],
                       date=schedule.get("date", ""), slot=schedule.get("slot", ""))
    await dispatcher.to_user(session, event_id=event.id, user_id=_uuid(d["user_id"]), tenant_id=_uuid(event.tenant_id),
                             template=f"order_{d['status']}", title=title, body=text,
                             data={"order_id": d["order_id"], "sub_order_id": d["sub_order_id"]}, sms=sms)


async def order_closed(event: EventEnvelope, session) -> None:
    d = event.data
    reason = {"order.expired": "was not paid in time", "order.payment_failed": "payment did not go through"}[event.type]
    await dispatcher.to_user(session, event_id=event.id, user_id=_uuid(d["user_id"]), tenant_id=_uuid(d["tenant_id"]),
                             template=event.type.replace(".", "_"), title="Order not completed",
                             body=f"Order {d['number']} {reason}. Your cart is still saved.", data={"order_id": d["order_id"]})


async def prescription_carted(event: EventEnvelope, session) -> None:
    d = event.data
    await dispatcher.to_user(session, event_id=event.id, user_id=_uuid(d["user_id"]), tenant_id=_uuid(d["tenant_id"]),
                             template="prescription_cart", title="Your cart is ready",
                             body=f"{d.get('doctor_name') or 'Your doctor'}'s prescription has been added to your cart.",
                             data={"prescription_id": d["prescription_id"]}, sms=True)


async def partner_review(event: EventEnvelope, session) -> None:
    d = event.data
    if event.type == "partner.verified":
        title, body = "You're verified", f"{d['display_name']} is approved. You can now add products and take orders."
    else:
        title, body = "Verification needs attention", f"Reason: {d.get('reason')}. Please update and resubmit."
    await dispatcher.to_business(session, owner_type="partner", owner_id=_uuid(d["partner_id"]),
                                 template=event.type.replace(".", "_"), title=title, body=body,
                                 email=d.get("contact_email"))


WALLET_COPY = {
    "billing.wallet.low_balance": ("Credits running low", "Your balance is {balance} credits. Top up to keep paid extras running."),
    "billing.wallet.grace_started": ("Out of credits — 7-day grace started", "Paid extras keep working for 7 days. Top up to avoid a pause."),
    "billing.wallet.paused": ("Paid extras paused", "Top up credits to resume. Orders, consultations and payments are unaffected."),
    "billing.wallet.reactivated": ("Paid extras resumed", "Thanks for topping up. Your balance is {balance} credits."),
    "billing.topup.completed": ("Credits added", "{credits} credits added. Balance: {balance}. Invoice {invoice_number}."),
}


async def wallet_event(event: EventEnvelope, session) -> None:
    d = event.data
    title, body = WALLET_COPY[event.type]
    await dispatcher.to_business(session, owner_type=d["owner_type"], owner_id=_uuid(d["owner_id"]),
                                 template=event.type.replace(".", "_"), title=title, body=body.format(**d), data=d)


async def payout_paid(event: EventEnvelope, session) -> None:
    d = event.data
    await dispatcher.to_business(session, owner_type=d["payee_type"], owner_id=_uuid(d["payee_id"]), template="payout_paid",
                                 title="Payout sent", body=f"RM {d['amount']} has been paid out (ref {d['bank_reference']}).")


async def stock_low(event: EventEnvelope, session) -> None:
    d = event.data
    await dispatcher.to_business(session, owner_type="partner", owner_id=_uuid(d["partner_id"]), template="stock_low",
                                 title="Low stock", body=f"{d['product_name']}: {d['stock']} left. Restock from a stockist.",
                                 data=d)


async def wholesale_event(event: EventEnvelope, session) -> None:
    d = event.data
    target, title = {
        "wholesale.po.submitted": ("stockist_partner_id", f"New purchase order {d['number']}"),
        "wholesale.po.accepted": ("buyer_partner_id", f"Purchase order {d['number']} accepted"),
        "wholesale.po.shipped": ("buyer_partner_id", f"Purchase order {d['number']} shipped"),
        "wholesale.po.rejected": ("buyer_partner_id", f"Purchase order {d['number']} rejected"),
    }[event.type]
    await dispatcher.to_business(session, owner_type="partner", owner_id=_uuid(d[target]),
                                 template=event.type.replace(".", "_"), title=title, body=f"Total RM {d['total']}", data=d)


HANDLERS = {
    "order.paid": order_paid,
    "order.sub_order.status_changed": sub_order_status,
    "order.expired": order_closed,
    "order.payment_failed": order_closed,
    "order.cart.prescription_added": prescription_carted,
    "partner.verified": partner_review,
    "partner.rejected": partner_review,
    **{t: wallet_event for t in WALLET_COPY},
    "billing.payout.paid": payout_paid,
    "catalog.stock.low": stock_low,
    "wholesale.po.submitted": wholesale_event,
    "wholesale.po.accepted": wholesale_event,
    "wholesale.po.shipped": wholesale_event,
    "wholesale.po.rejected": wholesale_event,
}
