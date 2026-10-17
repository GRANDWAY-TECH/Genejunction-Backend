import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app import settlement
from app.invoicing import add_month
from app.models import CommissionRule, SettlementEntry, Subscription

D = Decimal


def rule(model, scope, value, rate):
    return CommissionRule(model=model, scope=scope, scope_value=value, rate_pct=D(rate))


async def test_most_specific_commission_rule_wins(session):
    special = uuid.uuid4()
    session.add_all([rule("B2C", "default", None, "10"), rule("B2C", "partner_type", "lab", "15"),
                     rule("B2C", "partner", str(special), "7")])
    await session.flush()
    assert await settlement.resolve_gj_rate(session, "B2C", "pharmacy", None, uuid.uuid4()) == D("10")
    assert await settlement.resolve_gj_rate(session, "B2C", "lab", None, uuid.uuid4()) == D("15")
    assert await settlement.resolve_gj_rate(session, "B2C", "lab", None, special) == D("7")


async def test_white_label_plan_fee_applies_unless_partner_specific(session):
    tenant, partner = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(timezone.utc)
    session.add_all([rule("B2B2C", "default", None, "3"),
                     Subscription(subscriber_type="tenant", subscriber_id=tenant, plan_code="wl",
                                  current_period_start=now, current_period_end=add_month(now))])
    await session.flush()
    assert await settlement.resolve_gj_rate(session, "B2B2C", "pharmacy", tenant, partner) == D("2.00")
    session.add(rule("B2B2C", "partner", str(partner), "1"))
    await session.flush()
    assert await settlement.resolve_gj_rate(session, "B2B2C", "pharmacy", tenant, partner) == D("1")


async def test_accrue_split_then_clawback_after_payout(session):
    session.add(rule("B2C", "default", None, "10"))
    partner, sub_id = uuid.uuid4(), uuid.uuid4()
    order = {"order_id": str(uuid.uuid4()), "tenant_id": str(uuid.uuid4()), "channel": "B2C"}
    sub = {"sub_order_id": str(sub_id), "partner_id": str(partner), "kind": "pharmacy",
           "subtotal": "38.00", "total": "43.00"}
    entry = await settlement.accrue_sub_order(session, order, sub)
    assert await settlement.accrue_sub_order(session, order, sub) is None  # idempotent
    assert (entry.gj_commission, entry.partner_net) == (D("3.80"), D("39.20"))

    await session.flush()
    await settlement.mark_earned(session, sub_id)
    payouts = await settlement.build_payouts(session)
    assert [p.amount for p in payouts] == [D("39.20")]

    await settlement.reverse(session, sub_id)  # refunded after it was already paid out
    await session.flush()
    rows = (await session.scalars(select(SettlementEntry).where(SettlementEntry.source_id == sub_id))).all()
    reversal = next(r for r in rows if r.entry_kind == "reversal")
    assert reversal.partner_net == D("-39.20") and reversal.status == "earned" and reversal.payout_id is None


def test_add_month_clamps_to_month_end():
    assert add_month(datetime(2026, 1, 31)).date().isoformat() == "2026-02-28"
    assert add_month(datetime(2026, 12, 15)).date().isoformat() == "2027-01-15"
