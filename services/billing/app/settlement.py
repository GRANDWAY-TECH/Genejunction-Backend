"""Commission per sub-order, and weekly payouts to partners and clinic groups."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import CommissionRule, Payout, Plan, SettlementEntry, Subscription, TenantPartnerRate
from gj_common.events import emit
from gj_common.money import ZERO, money, pct_of

SPECIFICITY = {"partner": 4, "tenant": 3, "partner_type": 2, "default": 1}


async def resolve_gj_rate(session: AsyncSession, model: str, partner_type: str, tenant_id: uuid.UUID | None,
                          partner_id: uuid.UUID) -> Decimal:
    if model == "B2B2C" and tenant_id:
        # The white-label plan's per-order fee applies unless a more specific rule exists.
        plan_fee = await session.scalar(
            select(Plan.transaction_fee_pct).join(Subscription, Subscription.plan_code == Plan.code)
            .where(Subscription.subscriber_type == "tenant", Subscription.subscriber_id == tenant_id))
    else:
        plan_fee = None
    rules = (await session.scalars(select(CommissionRule).where(
        CommissionRule.model == model, CommissionRule.active.is_(True),
        or_(CommissionRule.scope == "default",
            (CommissionRule.scope == "partner_type") & (CommissionRule.scope_value == partner_type),
            (CommissionRule.scope == "tenant") & (CommissionRule.scope_value == str(tenant_id)),
            (CommissionRule.scope == "partner") & (CommissionRule.scope_value == str(partner_id)))))).all()
    best = max(rules, key=lambda r: SPECIFICITY[r.scope], default=None)
    if plan_fee is not None and (best is None or SPECIFICITY[best.scope] <= SPECIFICITY["partner_type"]):
        return plan_fee
    return best.rate_pct if best else Decimal("0")


async def accrue_sub_order(session: AsyncSession, order: dict, sub: dict) -> SettlementEntry | None:
    if not sub.get("partner_id"):
        return None  # GeneJunction-run service, nothing to settle
    exists = await session.scalar(select(SettlementEntry.id).where(
        SettlementEntry.source_type == "sub_order", SettlementEntry.source_id == uuid.UUID(sub["sub_order_id"]),
        SettlementEntry.entry_kind == "sale"))
    if exists:
        return None
    partner_id = uuid.UUID(sub["partner_id"])
    tenant_id = uuid.UUID(order["tenant_id"])
    channel = order["channel"]
    partner_type = sub["kind"] if sub["kind"] != "consultation" else "doctor"
    base = money(sub["subtotal"])
    gross = money(sub["total"])
    gj_rate = await resolve_gj_rate(session, channel, partner_type, tenant_id, partner_id)
    tenant_rate = Decimal("0")
    if channel == "B2B2C":
        rate = await session.get(TenantPartnerRate, (tenant_id, partner_id))
        tenant_rate = rate.commission_pct if rate and rate.commission_pct else Decimal("0")
    gj_commission = pct_of(base, gj_rate)
    tenant_commission = pct_of(base, tenant_rate)
    entry = SettlementEntry(
        source_type="sub_order", source_id=uuid.UUID(sub["sub_order_id"]), order_id=uuid.UUID(order["order_id"]),
        partner_id=partner_id, tenant_id=tenant_id, channel=channel, gross=gross, commission_base=base,
        gj_rate_pct=gj_rate, gj_commission=gj_commission, tenant_rate_pct=tenant_rate,
        tenant_commission=tenant_commission, partner_net=money(gross - gj_commission - tenant_commission))
    session.add(entry)
    return entry


async def accrue_purchase_order(session: AsyncSession, po: dict) -> None:
    po_id = uuid.UUID(po["po_id"])
    if await session.scalar(select(SettlementEntry.id).where(SettlementEntry.source_type == "purchase_order",
                                                             SettlementEntry.source_id == po_id)):
        return
    stockist = uuid.UUID(po["stockist_partner_id"])
    gross = money(po["total"])
    rate = await resolve_gj_rate(session, "B2B", "stockist", None, stockist)
    fee = pct_of(gross, rate)
    session.add(SettlementEntry(
        source_type="purchase_order", source_id=po_id, partner_id=stockist, channel="B2B", gross=gross,
        commission_base=gross, gj_rate_pct=rate, gj_commission=fee, partner_net=money(gross - fee),
        status="earned", earned_at=datetime.now(timezone.utc)))


async def mark_earned(session: AsyncSession, sub_order_id: uuid.UUID) -> None:
    entry = await session.scalar(select(SettlementEntry).where(
        SettlementEntry.source_type == "sub_order", SettlementEntry.source_id == sub_order_id,
        SettlementEntry.entry_kind == "sale"))
    if entry and entry.status == "accrued":
        entry.status = "earned"
        entry.earned_at = datetime.now(timezone.utc)


async def reverse(session: AsyncSession, sub_order_id: uuid.UUID) -> None:
    """Refunded sub-order: drop it from settlement, or claw it back if it was already paid out."""
    entry = await session.scalar(select(SettlementEntry).where(
        SettlementEntry.source_type == "sub_order", SettlementEntry.source_id == sub_order_id,
        SettlementEntry.entry_kind == "sale"))
    if entry is None or entry.status == "reversed":
        return
    if entry.payout_id is None and entry.tenant_payout_id is None:
        entry.status = "reversed"
        return
    exists = await session.scalar(select(SettlementEntry.id).where(
        SettlementEntry.source_type == "sub_order", SettlementEntry.source_id == sub_order_id,
        SettlementEntry.entry_kind == "reversal"))
    if not exists:
        session.add(SettlementEntry(
            source_type="sub_order", source_id=sub_order_id, entry_kind="reversal", order_id=entry.order_id,
            partner_id=entry.partner_id, tenant_id=entry.tenant_id, channel=entry.channel, gross=-entry.gross,
            commission_base=-entry.commission_base, gj_rate_pct=entry.gj_rate_pct, gj_commission=-entry.gj_commission,
            tenant_rate_pct=entry.tenant_rate_pct, tenant_commission=-entry.tenant_commission,
            partner_net=-entry.partner_net, status="earned", earned_at=datetime.now(timezone.utc)))


async def build_payouts(session: AsyncSession) -> list[Payout]:
    """Weekly batch: one payout per partner (their net) and per clinic group (their commission share)."""
    now = datetime.now(timezone.utc)
    created: list[Payout] = []
    partner_totals = (await session.execute(
        select(SettlementEntry.partner_id, func.sum(SettlementEntry.partner_net), func.count())
        .where(SettlementEntry.status == "earned", SettlementEntry.payout_id.is_(None))
        .group_by(SettlementEntry.partner_id))).all()
    for partner_id, amount, count in partner_totals:
        if money(amount) < settings.min_payout_amount:
            continue
        payout = Payout(payee_type="partner", payee_id=partner_id, period_end=now, amount=money(amount), entry_count=count)
        session.add(payout)
        await session.flush()
        entries = (await session.scalars(select(SettlementEntry).where(
            SettlementEntry.partner_id == partner_id, SettlementEntry.status == "earned",
            SettlementEntry.payout_id.is_(None)).with_for_update())).all()
        for e in entries:
            e.payout_id = payout.id
        created.append(payout)

    tenant_totals = (await session.execute(
        select(SettlementEntry.tenant_id, func.sum(SettlementEntry.tenant_commission), func.count())
        .where(SettlementEntry.status == "earned", SettlementEntry.tenant_payout_id.is_(None),
               SettlementEntry.tenant_commission != 0, SettlementEntry.channel == "B2B2C")
        .group_by(SettlementEntry.tenant_id))).all()
    for tenant_id, amount, count in tenant_totals:
        if money(amount) < settings.min_payout_amount:
            continue
        payout = Payout(payee_type="tenant", payee_id=tenant_id, period_end=now, amount=money(amount), entry_count=count)
        session.add(payout)
        await session.flush()
        entries = (await session.scalars(select(SettlementEntry).where(
            SettlementEntry.tenant_id == tenant_id, SettlementEntry.status == "earned",
            SettlementEntry.tenant_payout_id.is_(None), SettlementEntry.tenant_commission != 0)
            .with_for_update())).all()
        for e in entries:
            e.tenant_payout_id = payout.id
        created.append(payout)

    for p in created:
        emit(session, "billing.payout.created", key=p.payee_id, data={
            "payout_id": p.id, "payee_type": p.payee_type, "payee_id": p.payee_id, "amount": p.amount,
            "entry_count": p.entry_count})
    return created


def summarize(entries: list[SettlementEntry]) -> dict:
    return {
        "gross": str(money(sum((e.gross for e in entries), ZERO))),
        "gj_commission": str(money(sum((e.gj_commission for e in entries), ZERO))),
        "tenant_commission": str(money(sum((e.tenant_commission for e in entries), ZERO))),
        "partner_net": str(money(sum((e.partner_net for e in entries), ZERO))),
    }
