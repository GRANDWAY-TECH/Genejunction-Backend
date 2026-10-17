"""Prepaid credit wallets: 1 credit = RM 1. Businesses pay for paid extras from here.

Invariants (checked in tests):
  * wallet.balance == sum of ledger amounts
  * sum(open lot remaining) == max(balance, 0)
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import CreditLedger, CreditLot, CreditWallet, RateCardItem, RateOverride
from gj_common.errors import NotFound, PaymentRequired
from gj_common.events import emit
from gj_common.money import ZERO, money


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_wallet(session: AsyncSession, owner_type: str, owner_id: uuid.UUID, *, lock: bool = False) -> CreditWallet:
    await session.execute(pg_insert(CreditWallet).values(id=uuid.uuid4(), owner_type=owner_type, owner_id=owner_id)
                          .on_conflict_do_nothing(index_elements=["owner_type", "owner_id"]))
    stmt = select(CreditWallet).where(CreditWallet.owner_type == owner_type, CreditWallet.owner_id == owner_id)
    if lock:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def _existing(session: AsyncSession, wallet: CreditWallet, reference: str) -> CreditLedger | None:
    return await session.scalar(select(CreditLedger).where(CreditLedger.wallet_id == wallet.id,
                                                           CreditLedger.reference == reference))


def _wallet_event(session: AsyncSession, event_type: str, wallet: CreditWallet, **extra) -> None:
    emit(session, event_type, key=wallet.owner_id, data={
        "wallet_id": wallet.id, "owner_type": wallet.owner_type, "owner_id": wallet.owner_id,
        "balance": wallet.balance, "status": wallet.status, **extra})


async def add_credits(session: AsyncSession, wallet: CreditWallet, credits: Decimal, *, source: str, reference: str,
                      description: str, entry_type: str | None = None) -> CreditLedger | None:
    """Add a lot. Any negative balance (used during grace) is paid off first."""
    if credits <= 0 or await _existing(session, wallet, reference):
        return None
    debt = max(-wallet.balance, ZERO)
    session.add(CreditLot(wallet_id=wallet.id, source=source, credits=credits, remaining=max(credits - debt, ZERO),
                          expires_at=_now() + timedelta(days=settings.credit_validity_days)))
    wallet.balance = money(wallet.balance + credits)
    entry = CreditLedger(wallet_id=wallet.id, entry_type=entry_type or source, amount=credits,
                         balance_after=wallet.balance, reference=reference, description=description)
    session.add(entry)
    if wallet.balance > 0 and wallet.status != "active":
        wallet.status = "active"
        wallet.grace_started_at = None
        _wallet_event(session, "billing.wallet.reactivated", wallet)
    return entry


async def resolve_rate(session: AsyncSession, wallet: CreditWallet, action_code: str) -> tuple[RateCardItem, Decimal]:
    item = await session.get(RateCardItem, action_code)
    if item is None or not item.active:
        raise NotFound(f"Unknown paid action {action_code}", code="unknown_action")
    override = await session.get(RateOverride, (wallet.owner_type, wallet.owner_id, action_code))
    return item, (override.credits_per_unit if override else item.credits_per_unit)


async def charge(session: AsyncSession, owner_type: str, owner_id: uuid.UUID, action_code: str, quantity: Decimal,
                 reference: str, description: str | None = None) -> dict:
    """Deduct credits for a paid extra. Idempotent on `reference`."""
    wallet = await get_wallet(session, owner_type, owner_id, lock=True)
    if (done := await _existing(session, wallet, reference)) is not None:
        return {"charged": True, "credits": -done.amount, "balance": wallet.balance, "status": wallet.status,
                "ledger_id": done.id, "duplicate": True}
    item, unit = await resolve_rate(session, wallet, action_code)
    amount = money(unit * quantity)
    now = _now()

    if wallet.status == "grace" and wallet.grace_started_at and now - wallet.grace_started_at > timedelta(days=settings.grace_period_days):
        wallet.status = "paused"
        _wallet_event(session, "billing.wallet.paused", wallet)
    if wallet.status == "paused":
        await session.commit()  # persist the pause even though the charge is refused
        raise PaymentRequired("Paid extras are paused until credits are topped up",
                              details={"balance": str(wallet.balance), "required": str(amount)})
    if wallet.balance < amount and wallet.status == "active":
        wallet.status = "grace"
        wallet.grace_started_at = now
        _wallet_event(session, "billing.wallet.grace_started", wallet,
                      grace_ends_at=now + timedelta(days=settings.grace_period_days))

    # Consume lots, soonest expiry first. Anything not covered drives the balance negative (grace).
    left = amount
    lots = (await session.scalars(select(CreditLot).where(CreditLot.wallet_id == wallet.id, CreditLot.remaining > 0,
                                                          CreditLot.expires_at > now)
                                  .order_by(CreditLot.expires_at).with_for_update())).all()
    for lot in lots:
        if left <= 0:
            break
        take = min(lot.remaining, left)
        lot.remaining = money(lot.remaining - take)
        left -= take
    wallet.balance = money(wallet.balance - amount)
    entry = CreditLedger(wallet_id=wallet.id, entry_type="debit", amount=-amount, balance_after=wallet.balance,
                         action_code=action_code, quantity=quantity, unit_credits=unit, reference=reference,
                         description=description or f"{item.name} × {quantity}")
    session.add(entry)

    threshold = money(wallet.last_topup_credits * settings.low_balance_pct / 100)
    if wallet.balance <= threshold and not wallet.low_alert_sent and wallet.last_topup_credits > 0:
        wallet.low_alert_sent = True
        _wallet_event(session, "billing.wallet.low_balance", wallet, threshold=threshold,
                      auto_topup_pack=wallet.auto_topup_pack)
    await session.flush()
    return {"charged": True, "credits": amount, "balance": wallet.balance, "status": wallet.status,
            "ledger_id": entry.id, "duplicate": False}


async def refund_charge(session: AsyncSession, owner_type: str, owner_id: uuid.UUID, reference: str, reason: str) -> dict:
    """Credits for a failed or cancelled action are returned."""
    wallet = await get_wallet(session, owner_type, owner_id, lock=True)
    debit = await _existing(session, wallet, reference)
    if debit is None or debit.entry_type != "debit":
        raise NotFound("No charge with that reference")
    entry = await add_credits(session, wallet, -debit.amount, source="refund", reference=f"refund:{reference}",
                              description=f"Refund: {reason}")
    return {"refunded": entry is not None, "balance": wallet.balance}


async def expire_lots(session: AsyncSession) -> int:
    now = _now()
    lots = (await session.scalars(select(CreditLot).where(CreditLot.remaining > 0, CreditLot.expires_at <= now)
                                  .limit(500).with_for_update(skip_locked=True))).all()
    for lot in lots:
        wallet = await session.get(CreditWallet, lot.wallet_id, with_for_update=True)
        wallet.balance = money(wallet.balance - lot.remaining)
        session.add(CreditLedger(wallet_id=wallet.id, entry_type="expiry", amount=-lot.remaining,
                                 balance_after=wallet.balance, reference=f"expiry:{lot.id}",
                                 description="Credits expired (12 months after purchase)"))
        lot.remaining = ZERO
    return len(lots)


async def pause_expired_grace(session: AsyncSession) -> int:
    cutoff = _now() - timedelta(days=settings.grace_period_days)
    wallets = (await session.scalars(select(CreditWallet).where(CreditWallet.status == "grace",
                                                                CreditWallet.grace_started_at < cutoff)
                                     .with_for_update(skip_locked=True))).all()
    for wallet in wallets:
        wallet.status = "paused"
        _wallet_event(session, "billing.wallet.paused", wallet)
    return len(wallets)
