import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app import credits
from app.models import CreditLedger, CreditLot, CreditWallet, RateOverride
from gj_common.errors import PaymentRequired
from gj_common.events import OutboxEvent

D = Decimal


async def invariants(session, wallet: CreditWallet) -> None:
    ledger_sum = await session.scalar(select(func.coalesce(func.sum(CreditLedger.amount), 0))
                                      .where(CreditLedger.wallet_id == wallet.id))
    lots_sum = await session.scalar(select(func.coalesce(func.sum(CreditLot.remaining), 0))
                                    .where(CreditLot.wallet_id == wallet.id))
    assert wallet.balance == ledger_sum, "balance must equal the ledger"
    assert lots_sum == max(wallet.balance, D("0")), "open lots must cover a positive balance exactly"


async def funded(session, credits_amount: str) -> CreditWallet:
    w = await credits.get_wallet(session, "partner", uuid.uuid4(), lock=True)
    await credits.add_credits(session, w, D(credits_amount), source="topup", reference=f"t:{uuid.uuid4()}",
                              description="top-up")
    w.last_topup_credits = D(credits_amount)
    await session.flush()
    return w


async def test_charges_consume_soonest_expiring_lot_first(session):
    w = await funded(session, "100")
    await credits.add_credits(session, w, D("50"), source="bonus", reference="bonus", description="bonus")
    lots = (await session.scalars(select(CreditLot).where(CreditLot.wallet_id == w.id).order_by(CreditLot.credits))).all()
    lots[0].expires_at = datetime.now(timezone.utc) + timedelta(days=5)  # the 50-credit lot expires first
    await session.flush()
    await credits.charge(session, "partner", w.owner_id, "ai_report_summary", D("6"), reference="r1")
    await session.refresh(lots[0])
    await session.refresh(lots[1])
    assert (lots[0].remaining, lots[1].remaining) == (D("0.00"), D("90.00"))
    assert w.balance == D("90.00")
    await invariants(session, w)


async def test_fractional_rate_and_idempotent_reference(session):
    w = await funded(session, "10")
    first = await credits.charge(session, "partner", w.owner_id, "alert_message", D("3"), reference="sms-1")
    again = await credits.charge(session, "partner", w.owner_id, "alert_message", D("3"), reference="sms-1")
    assert first["credits"] == D("0.60") and again["duplicate"] is True
    assert w.balance == D("9.40")
    await invariants(session, w)


async def test_agreement_override_beats_rate_card(session):
    w = await funded(session, "100")
    session.add(RateOverride(owner_type="partner", owner_id=w.owner_id, action_code="ai_report_summary",
                             credits_per_unit=D("6")))
    await session.flush()
    result = await credits.charge(session, "partner", w.owner_id, "ai_report_summary", D("1"), reference="x")
    assert result["credits"] == D("6.00")


async def test_running_out_starts_grace_then_pauses_then_topup_reactivates(session):
    w = await funded(session, "5")
    result = await credits.charge(session, "partner", w.owner_id, "ai_report_summary", D("1"), reference="a")
    assert result["status"] == "grace" and w.balance == D("-5.00")
    await invariants(session, w)

    w.grace_started_at = datetime.now(timezone.utc) - timedelta(days=8)
    await session.commit()
    with pytest.raises(PaymentRequired):
        await credits.charge(session, "partner", w.owner_id, "ai_report_summary", D("1"), reference="b")
    await session.refresh(w)
    assert w.status == "paused"

    await credits.add_credits(session, w, D("100"), source="topup", reference="top", description="top-up")
    assert w.status == "active" and w.balance == D("95.00")
    await invariants(session, w)  # the 5-credit debt was paid from the new lot


async def test_low_balance_alert_fires_once_at_20_percent(session):
    w = await funded(session, "100")
    await credits.charge(session, "partner", w.owner_id, "ai_report_summary", D("7"), reference="c1")  # 30 left
    await credits.charge(session, "partner", w.owner_id, "ai_report_summary", D("1"), reference="c2")  # 20 left
    await credits.charge(session, "partner", w.owner_id, "ai_report_summary", D("1"), reference="c3")  # 10 left
    alerts = await session.scalar(select(func.count()).select_from(OutboxEvent)
                                  .where(OutboxEvent.event_type == "billing.wallet.low_balance"))
    assert alerts == 1


async def test_failed_action_refund_returns_credits_once(session):
    w = await funded(session, "50")
    await credits.charge(session, "partner", w.owner_id, "ai_report_summary", D("1"), reference="sum-1")
    await credits.refund_charge(session, "partner", w.owner_id, "sum-1", "summary failed")
    second = await credits.refund_charge(session, "partner", w.owner_id, "sum-1", "summary failed")
    assert second["refunded"] is False and w.balance == D("50.00")
    await invariants(session, w)


async def test_unused_credits_expire_after_12_months(session):
    w = await funded(session, "40")
    lot = await session.scalar(select(CreditLot).where(CreditLot.wallet_id == w.id))
    lot.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.flush()
    assert await credits.expire_lots(session) == 1
    await session.flush()
    await session.refresh(w)
    assert w.balance == D("0.00")
    await invariants(session, w)
