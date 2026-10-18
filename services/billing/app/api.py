import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import credits, invoicing, settlement
from app.config import settings
from app.models import (
    CommissionRule,
    CreditLedger,
    CreditPack,
    CreditWallet,
    Invoice,
    Payout,
    Plan,
    RateCardItem,
    RateOverride,
    SettlementEntry,
    TopUp,
)
from app.responses import (
    CreditPackOut,
    FeaturedResult,
    InvoicePage,
    InvoicePayment,
    LedgerPage,
    PayoutOut,
    PayoutPage,
    PlanOut,
    RateCardOut,
    RevenueReport,
    SettlementList,
    SettlementPage,
    SubscriptionOut,
    TopUpResponse,
    WalletView,
)
from gj_common.auth import GJ_ADMIN, GJ_FINANCE, CurrentUser, InternalOnly, Principal, require_roles, staff_only
from gj_common.db import SessionDep
from gj_common.errors import Conflict, NotFound
from gj_common.events import emit
from gj_common.money import money, pct_of
from gj_common.pagination import PageDep, paginate

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])
finance = APIRouter(prefix="/api/v1/billing", tags=["finance"], dependencies=[require_roles(GJ_ADMIN, GJ_FINANCE)])
internal = APIRouter(prefix="/internal", tags=["internal"], dependencies=[InternalOnly])

OwnerType = Literal["tenant", "partner"]


def ensure_owner(principal: Principal, owner_type: str, owner_id: uuid.UUID) -> None:
    if owner_type == "partner":
        principal.ensure_partner(owner_id)
    else:
        principal.ensure_tenant_admin(owner_id)


# ---------------------------------------------------------------- schemas
class WalletOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_type: str
    owner_id: uuid.UUID
    balance: Decimal
    status: str
    grace_started_at: datetime | None
    last_topup_credits: Decimal
    auto_topup_pack: str | None


class LedgerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entry_type: str
    amount: Decimal
    balance_after: Decimal
    action_code: str | None
    quantity: Decimal | None
    unit_credits: Decimal | None
    description: str
    created_at: datetime


class TopUpRequest(BaseModel):
    pack_code: str
    method: Literal["fpx", "card", "ewallet"] = "fpx"
    return_url: str | None = None


class PayRequest(BaseModel):
    method: Literal["fpx", "card", "ewallet"] = "fpx"
    return_url: str | None = None


class AutoTopUp(BaseModel):
    pack_code: str | None


class FeatureRequest(BaseModel):
    weeks: int = Field(1, ge=1, le=12)


class ChargeRequest(BaseModel):
    owner_type: OwnerType
    owner_id: uuid.UUID
    action_code: str
    quantity: Decimal = Field(Decimal("1"), gt=0)
    reference: str = Field(max_length=150)
    description: str | None = None


class ChargeRefund(BaseModel):
    owner_type: OwnerType
    owner_id: uuid.UUID
    reference: str
    reason: str


class RuleIn(BaseModel):
    model: Literal["B2C", "B2B2C", "B2B"]
    scope: Literal["default", "partner_type", "tenant", "partner"]
    scope_value: str | None = None
    rate_pct: Decimal = Field(ge=0, le=100)
    note: str | None = None


class RuleOut(RuleIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    active: bool


class RateOverrideIn(BaseModel):
    owner_type: OwnerType
    owner_id: uuid.UUID
    action_code: str
    credits_per_unit: Decimal = Field(ge=0)


class Adjustment(BaseModel):
    credits: Decimal
    reason: str


class MarkPaid(BaseModel):
    bank_reference: str


class SubscriptionIn(BaseModel):
    subscriber_type: OwnerType
    subscriber_id: uuid.UUID
    plan_code: str


# ---------------------------------------------------------------- catalogue of paid extras
@router.get("/rate-card", response_model=list[RateCardOut])
async def rate_card(session: SessionDep):
    rows = (await session.scalars(select(RateCardItem).where(RateCardItem.active.is_(True)))).all()
    return [{"action_code": r.action_code, "name": r.name, "unit": r.unit, "credits_per_unit": r.credits_per_unit,
             "models": r.models, "charged_to": r.charged_to} for r in rows]


@router.get("/credit-packs", response_model=list[CreditPackOut])
async def credit_packs(session: SessionDep):
    rows = (await session.scalars(select(CreditPack).where(CreditPack.active.is_(True)).order_by(CreditPack.price))).all()
    return [{"code": p.code, "name": p.name, "price": p.price, "credits": p.credits, "bonus_credits": p.bonus_credits,
             "total_credits": p.credits + p.bonus_credits} for p in rows]


@router.get("/plans", response_model=list[PlanOut])
async def plans(session: SessionDep):
    rows = (await session.scalars(select(Plan).where(Plan.active.is_(True)))).all()
    return [{"code": p.code, "name": p.name, "audience": p.audience, "monthly_fee": p.monthly_fee,
             "setup_fee": p.setup_fee, "transaction_fee_pct": p.transaction_fee_pct} for p in rows]


# ---------------------------------------------------------------- wallets
@router.get("/wallets/{owner_type}/{owner_id}", response_model=WalletView)
async def wallet(owner_type: OwnerType, owner_id: uuid.UUID, principal: CurrentUser, session: SessionDep):
    ensure_owner(principal, owner_type, owner_id)
    w = await credits.get_wallet(session, owner_type, owner_id)
    await session.commit()
    threshold = money(w.last_topup_credits * settings.low_balance_pct / 100)
    grace_ends = w.grace_started_at + timedelta(days=settings.grace_period_days) if w.grace_started_at else None
    return {**WalletOut.model_validate(w).model_dump(), "low_balance_threshold": threshold, "grace_ends_at": grace_ends}


@router.get("/wallets/{owner_type}/{owner_id}/ledger", response_model=LedgerPage)
async def ledger(owner_type: OwnerType, owner_id: uuid.UUID, principal: CurrentUser, session: SessionDep, page: PageDep):
    ensure_owner(principal, owner_type, owner_id)
    w = await credits.get_wallet(session, owner_type, owner_id)
    stmt = select(CreditLedger).where(CreditLedger.wallet_id == w.id).order_by(CreditLedger.created_at.desc())
    return await paginate(session, stmt, page, LedgerOut.model_validate)


async def create_topup(payments, session: AsyncSession, w: CreditWallet, pack_code: str, method: str,
                       return_url: str | None, requested_by: uuid.UUID | None, automatic: bool = False) -> dict:
    pack = await session.get(CreditPack, pack_code)
    if pack is None or not pack.active:
        raise NotFound("Unknown credit pack")
    sst = pct_of(pack.price, settings.sst_rate)
    topup = TopUp(id=uuid.uuid4(), wallet_id=w.id, pack_code=pack.code, price=pack.price, sst_amount=sst,
                  total=money(pack.price + sst), credits=pack.credits, bonus_credits=pack.bonus_credits,
                  requested_by=requested_by, automatic=automatic)
    session.add(topup)
    await session.flush()
    payment = await payments.post("/internal/payments", json={
        "purpose": "credit_topup", "reference_id": str(topup.id), "amount": str(topup.total), "currency": "MYR",
        "method": method, "payer_type": w.owner_type, "payer_id": str(w.owner_id),
        "description": f"{pack.name} credit pack", "return_url": return_url,
        "idempotency_key": f"topup:{topup.id}", "expires_in_minutes": 60})
    topup.payment_id = payment["id"]
    return {"topup_id": topup.id, "pack": pack.code, "price": pack.price, "sst": sst, "total": topup.total,
            "credits": pack.credits + pack.bonus_credits, "payment": payment}


@router.post("/wallets/{owner_type}/{owner_id}/topups", status_code=201, response_model=TopUpResponse)
async def topup(owner_type: OwnerType, owner_id: uuid.UUID, body: TopUpRequest, principal: CurrentUser,
                request: Request, session: SessionDep):
    """Buy a credit pack (see `/billing/credit-packs`). Pay at `payment.checkout_url`; credits (with bonus) are
    added when payment succeeds and an SST invoice is issued. 1 credit = RM 1, valid 12 months."""
    ensure_owner(principal, owner_type, owner_id)
    w = await credits.get_wallet(session, owner_type, owner_id)
    result = await create_topup(request.app.state.payments, session, w, body.pack_code, body.method, body.return_url, principal.user_id)
    await session.commit()
    return result


@router.put("/wallets/{owner_type}/{owner_id}/auto-topup", response_model=WalletOut)
async def auto_topup(owner_type: OwnerType, owner_id: uuid.UUID, body: AutoTopUp, principal: CurrentUser,
                     session: SessionDep):
    ensure_owner(principal, owner_type, owner_id)
    if body.pack_code and await session.get(CreditPack, body.pack_code) is None:
        raise NotFound("Unknown credit pack")
    w = await credits.get_wallet(session, owner_type, owner_id, lock=True)
    w.auto_topup_pack = body.pack_code
    await session.commit()
    return WalletOut.model_validate(w)


@router.post("/partners/{partner_id}/featured", response_model=FeaturedResult)
async def buy_featured(partner_id: uuid.UUID, body: FeatureRequest, principal: CurrentUser, request: Request,
                       session: SessionDep):
    """Featured listing in search: charged per week from the partner's credits."""
    principal.ensure_partner(partner_id)
    week = datetime.now(timezone.utc).strftime("%G-W%V")
    result = await credits.charge(session, "partner", partner_id, "featured_listing_week", Decimal(body.weeks),
                                  reference=f"featured:{partner_id}:{week}:{body.weeks}",
                                  description=f"Featured listing × {body.weeks} week(s)")
    until = datetime.now(timezone.utc) + timedelta(weeks=body.weeks)
    await request.app.state.catalog.post("/internal/catalog/featured", json={"partner_id": str(partner_id),
                                                                             "until": until.isoformat()})
    await session.commit()
    return {**result, "featured_until": until}


# ---------------------------------------------------------------- settlements, payouts, invoices (business views)
@router.get("/partners/{partner_id}/settlements", response_model=SettlementPage)
async def partner_settlements(partner_id: uuid.UUID, principal: CurrentUser, session: SessionDep, page: PageDep,
                              status: str | None = None):
    principal.ensure_partner(partner_id)
    stmt = select(SettlementEntry).where(SettlementEntry.partner_id == partner_id).order_by(SettlementEntry.created_at.desc())
    if status:
        stmt = stmt.where(SettlementEntry.status == status)
    unpaid = (await session.scalars(select(SettlementEntry).where(
        SettlementEntry.partner_id == partner_id, SettlementEntry.payout_id.is_(None),
        SettlementEntry.status.in_(["accrued", "earned"])))).all()
    page_data = await paginate(session, stmt, page, _entry)
    return {**page_data, "unpaid_summary": settlement.summarize(unpaid)}


def _entry(e: SettlementEntry) -> dict:
    return {"id": e.id, "source_type": e.source_type, "source_id": e.source_id, "entry_kind": e.entry_kind,
            "order_id": e.order_id, "channel": e.channel, "gross": e.gross, "gj_rate_pct": e.gj_rate_pct,
            "gj_commission": e.gj_commission, "tenant_commission": e.tenant_commission, "partner_net": e.partner_net,
            "status": e.status, "payout_id": e.payout_id, "created_at": e.created_at}


def _payout(p: Payout) -> dict:
    return {"id": p.id, "payee_type": p.payee_type, "payee_id": p.payee_id, "amount": p.amount,
            "entry_count": p.entry_count, "status": p.status, "period_end": p.period_end, "paid_at": p.paid_at,
            "bank_reference": p.bank_reference, "bank": p.bank_snapshot}


@router.get("/payees/{payee_type}/{payee_id}/payouts", response_model=PayoutPage)
async def payee_payouts(payee_type: OwnerType, payee_id: uuid.UUID, principal: CurrentUser, session: SessionDep,
                        page: PageDep):
    ensure_owner(principal, payee_type, payee_id)
    stmt = select(Payout).where(Payout.payee_type == payee_type, Payout.payee_id == payee_id).order_by(Payout.created_at.desc())
    return await paginate(session, stmt, page, _payout)


@router.get("/tenants/{tenant_id}/earnings", response_model=SettlementList)
async def tenant_earnings(tenant_id: uuid.UUID, principal: CurrentUser, session: SessionDep, page: PageDep):
    """The clinic group's commission on orders in its white-label app."""
    principal.ensure_tenant_admin(tenant_id)
    stmt = select(SettlementEntry).where(SettlementEntry.tenant_id == tenant_id, SettlementEntry.channel == "B2B2C") \
        .order_by(SettlementEntry.created_at.desc())
    return await paginate(session, stmt, page, _entry)


@router.get("/invoices", response_model=InvoicePage)
async def invoices(customer_type: OwnerType, customer_id: uuid.UUID, principal: CurrentUser, session: SessionDep,
                   page: PageDep):
    ensure_owner(principal, customer_type, customer_id)
    stmt = select(Invoice).where(Invoice.customer_type == customer_type, Invoice.customer_id == customer_id) \
        .order_by(Invoice.issued_at.desc())
    return await paginate(session, stmt, page, _invoice)


def _invoice(i: Invoice) -> dict:
    return {"id": i.id, "number": i.number, "kind": i.kind, "subtotal": i.subtotal, "sst_rate": i.sst_rate,
            "sst_amount": i.sst_amount, "total": i.total, "lines": i.lines, "status": i.status,
            "issued_at": i.issued_at, "due_at": i.due_at, "paid_at": i.paid_at}


@router.post("/invoices/{invoice_id}/pay", status_code=201, response_model=InvoicePayment)
async def pay_invoice(invoice_id: uuid.UUID, body: PayRequest, principal: CurrentUser, request: Request,
                      session: SessionDep):
    invoice = await session.get(Invoice, invoice_id)
    if invoice is None:
        raise NotFound("Invoice not found")
    ensure_owner(principal, invoice.customer_type, invoice.customer_id)
    if invoice.status != "issued":
        raise Conflict(f"Invoice is {invoice.status}")
    payment = await request.app.state.payments.post("/internal/payments", json={
        "purpose": "subscription", "reference_id": str(invoice.id), "amount": str(invoice.total), "currency": "MYR",
        "method": body.method, "payer_type": invoice.customer_type,
        "payer_id": str(invoice.customer_id), "description": f"Invoice {invoice.number}",
        "return_url": body.return_url, "idempotency_key": f"invoice:{invoice.id}",
        "expires_in_minutes": 1440})
    return {"invoice": _invoice(invoice), "payment": payment}


# ---------------------------------------------------------------- finance console
@finance.get("/commission-rules", response_model=list[RuleOut])
async def list_rules(session: SessionDep):
    return (await session.scalars(select(CommissionRule).order_by(CommissionRule.model, CommissionRule.scope))).all()


@finance.post("/commission-rules", response_model=RuleOut, status_code=201)
async def create_rule(body: RuleIn, session: SessionDep):
    rule = CommissionRule(**body.model_dump())
    session.add(rule)
    await session.commit()
    return rule


@finance.post("/commission-rules/{rule_id}/deactivate", response_model=RuleOut)
async def deactivate_rule(rule_id: uuid.UUID, session: SessionDep):
    rule = await session.get(CommissionRule, rule_id)
    if rule is None:
        raise NotFound()
    rule.active = False
    await session.commit()
    return rule


@finance.put("/rate-overrides", status_code=200, response_model=RateOverrideIn)
async def put_override(body: RateOverrideIn, session: SessionDep):
    if await session.get(RateCardItem, body.action_code) is None:
        raise NotFound("Unknown action")
    row = await session.get(RateOverride, (body.owner_type, body.owner_id, body.action_code))
    if row:
        row.credits_per_unit = body.credits_per_unit
    else:
        session.add(RateOverride(**body.model_dump()))
    await session.commit()
    return body


@finance.post("/wallets/{owner_type}/{owner_id}/adjust", response_model=WalletOut)
async def adjust(owner_type: OwnerType, owner_id: uuid.UUID, body: Adjustment, principal: CurrentUser, session: SessionDep):
    w = await credits.get_wallet(session, owner_type, owner_id, lock=True)
    ref = f"adjust:{uuid.uuid4()}"
    if body.credits > 0:
        await credits.add_credits(session, w, money(body.credits), source="adjustment", reference=ref,
                                  description=f"Adjustment by {principal.user_id}: {body.reason}")
    else:
        w.balance = money(w.balance + body.credits)
        session.add(CreditLedger(wallet_id=w.id, entry_type="adjustment", amount=money(body.credits),
                                 balance_after=w.balance, reference=ref, description=body.reason))
    await session.commit()
    return WalletOut.model_validate(w)


@finance.get("/payouts", response_model=PayoutPage)
async def list_payouts(session: SessionDep, page: PageDep, status: str | None = None):
    stmt = select(Payout).order_by(Payout.created_at.desc())
    if status:
        stmt = stmt.where(Payout.status == status)
    return await paginate(session, stmt, page, _payout)


@finance.post("/payouts/run", response_model=list[PayoutOut])
async def run_payouts(session: SessionDep):
    created = await settlement.build_payouts(session)
    await session.commit()
    return [_payout(p) for p in created]


@finance.post("/payouts/{payout_id}/approve", response_model=PayoutOut)
async def approve_payout(payout_id: uuid.UUID, principal: CurrentUser, request: Request, session: SessionDep):
    payout = await session.get(Payout, payout_id, with_for_update=True)
    if payout is None:
        raise NotFound()
    if payout.status != "pending_approval":
        raise Conflict(f"Payout is {payout.status}")
    if payout.payee_type == "partner":
        account = await request.app.state.partners.get(f"/internal/partners/{payout.payee_id}/payout-account")
        payout.bank_snapshot = {"bank_name": account["bank_name"], "account_name": account["account_name"],
                                "account_last4": account["account_no"][-4:]}
    payout.status = "approved"
    payout.approved_by = principal.user_id
    await session.commit()
    return _payout(payout)


@finance.post("/payouts/{payout_id}/mark-paid", response_model=PayoutOut)
async def mark_paid(payout_id: uuid.UUID, body: MarkPaid, session: SessionDep):
    payout = await session.get(Payout, payout_id, with_for_update=True)
    if payout is None:
        raise NotFound()
    if payout.status != "approved":
        raise Conflict("Approve the payout first")
    payout.status = "paid"
    payout.paid_at = datetime.now(timezone.utc)
    payout.bank_reference = body.bank_reference
    emit(session, "billing.payout.paid", key=payout.payee_id, data=_payout(payout))
    await session.commit()
    return _payout(payout)


@finance.post("/subscriptions", status_code=201, response_model=SubscriptionOut)
async def create_subscription(body: SubscriptionIn, session: SessionDep):
    sub = await invoicing.start_subscription(session, body.subscriber_type, body.subscriber_id, body.plan_code)
    await session.commit()
    return {"id": sub.id, "plan_code": sub.plan_code, "current_period_end": sub.current_period_end}


@router.get("/reports/revenue", response_model=RevenueReport, dependencies=[staff_only()])
async def revenue(session: SessionDep, start: datetime, end: datetime):
    """GJ earnings by stream: commission / transaction fees, credits consumed, subscriptions."""
    by_channel = (await session.execute(
        select(SettlementEntry.channel, func.sum(SettlementEntry.gross), func.sum(SettlementEntry.gj_commission))
        .where(SettlementEntry.status != "reversed", SettlementEntry.created_at.between(start, end))
        .group_by(SettlementEntry.channel))).all()
    credits_used = await session.scalar(select(func.coalesce(func.sum(-CreditLedger.amount), 0)).where(
        CreditLedger.entry_type == "debit", CreditLedger.created_at.between(start, end)))
    subs = await session.scalar(select(func.coalesce(func.sum(Invoice.subtotal), 0)).where(
        Invoice.kind == "subscription", Invoice.status == "paid", Invoice.paid_at.between(start, end)))
    return {
        "commission_and_fees": [{"channel": c, "gmv": g, "gj_earnings": e} for c, g, e in by_channel],
        "credits_consumed": credits_used,
        "subscription_revenue": subs,
    }


# ---------------------------------------------------------------- internal (other services charge credits)
@internal.post("/credits/charge")
async def internal_charge(body: ChargeRequest, session: SessionDep):
    result = await credits.charge(session, body.owner_type, body.owner_id, body.action_code, body.quantity,
                                  body.reference, body.description)
    await session.commit()
    return result


@internal.post("/credits/refund")
async def internal_refund(body: ChargeRefund, session: SessionDep):
    result = await credits.refund_charge(session, body.owner_type, body.owner_id, body.reference, body.reason)
    await session.commit()
    return result


@internal.get("/wallets/{owner_type}/{owner_id}")
async def internal_wallet(owner_type: OwnerType, owner_id: uuid.UUID, session: SessionDep):
    w = await credits.get_wallet(session, owner_type, owner_id)
    await session.commit()
    return WalletOut.model_validate(w)
