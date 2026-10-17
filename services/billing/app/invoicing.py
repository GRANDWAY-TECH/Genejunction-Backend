import calendar
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Invoice, Plan, Subscription, invoice_number_seq
from gj_common.errors import NotFound
from gj_common.events import emit
from gj_common.money import ZERO, money, pct_of


def _now() -> datetime:
    return datetime.now(timezone.utc)


def add_month(dt: datetime) -> datetime:
    month = dt.month % 12 + 1
    year = dt.year + (dt.month == 12)
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


async def issue_invoice(session: AsyncSession, *, customer_type: str, customer_id: uuid.UUID, kind: str,
                        lines: list[dict], due_days: int = 14, paid: bool = False, payment_id: str | None = None) -> Invoice:
    """lines: [{description, qty, unit_price}] — amounts exclusive of SST."""
    normalized = []
    for line in lines:
        amount = money(Decimal(str(line["unit_price"])) * Decimal(str(line.get("qty", 1))))
        normalized.append({"description": line["description"], "qty": str(line.get("qty", 1)),
                           "unit_price": str(money(line["unit_price"])), "amount": str(amount)})
    subtotal = money(sum((Decimal(l["amount"]) for l in normalized), ZERO))
    sst = pct_of(subtotal, settings.sst_rate)
    seq = await session.scalar(select(invoice_number_seq.next_value()))
    invoice = Invoice(number=f"GJ-INV-{_now():%Y}-{seq:07d}", customer_type=customer_type, customer_id=customer_id,
                      kind=kind, subtotal=subtotal, sst_rate=settings.sst_rate, sst_amount=sst,
                      total=money(subtotal + sst), lines=normalized, status="paid" if paid else "issued",
                      due_at=_now() + timedelta(days=due_days), paid_at=_now() if paid else None, payment_id=payment_id)
    session.add(invoice)
    await session.flush()
    emit(session, "billing.invoice.issued", key=customer_id, data={
        "invoice_id": invoice.id, "number": invoice.number, "customer_type": customer_type, "customer_id": customer_id,
        "kind": kind, "total": invoice.total, "status": invoice.status})
    return invoice


async def start_subscription(session: AsyncSession, subscriber_type: str, subscriber_id: uuid.UUID, plan_code: str) -> Subscription:
    plan = await session.get(Plan, plan_code)
    if plan is None:
        raise NotFound(f"Unknown plan {plan_code}")
    existing = await session.scalar(select(Subscription).where(Subscription.subscriber_type == subscriber_type,
                                                               Subscription.subscriber_id == subscriber_id))
    if existing:
        existing.plan_code = plan_code
        return existing
    start = _now()
    sub = Subscription(subscriber_type=subscriber_type, subscriber_id=subscriber_id, plan_code=plan_code,
                       current_period_start=start, current_period_end=add_month(start))
    session.add(sub)
    lines = [{"description": f"{plan.name} — monthly subscription", "unit_price": plan.monthly_fee}]
    if plan.setup_fee > 0:
        lines.insert(0, {"description": f"{plan.name} — one-time setup", "unit_price": plan.setup_fee})
    await issue_invoice(session, customer_type=subscriber_type, customer_id=subscriber_id, kind="subscription", lines=lines)
    return sub


async def renew_due(session: AsyncSession) -> int:
    subs = (await session.scalars(select(Subscription).where(Subscription.status == "active",
                                                             Subscription.current_period_end <= _now())
                                  .limit(200).with_for_update(skip_locked=True))).all()
    for sub in subs:
        plan = await session.get(Plan, sub.plan_code)
        sub.current_period_start = sub.current_period_end
        sub.current_period_end = add_month(sub.current_period_end)
        await issue_invoice(session, customer_type=sub.subscriber_type, customer_id=sub.subscriber_id,
                            kind="subscription",
                            lines=[{"description": f"{plan.name} — {sub.current_period_start:%b %Y}",
                                    "unit_price": plan.monthly_fee}])
    return len(subs)
