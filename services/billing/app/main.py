import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import credits, invoicing, settlement
from app.api import create_topup, finance, internal, router
from app.config import settings
from app.models import CommissionRule, CreditPack, CreditWallet, Invoice, Plan, RateCardItem, TenantPartnerRate, TopUp
from gj_common.app_factory import ServiceSpec, create_service_app
from gj_common.events import EventEnvelope, emit
from gj_common.http import ServiceClient
from gj_common.jobs import PeriodicJob
from gj_common.logging import get_logger

log = get_logger(__name__)
state: dict = {}

RATE_CARD = [
    ("ai_report_summary", "AI lab report summary", "report", "10", ["B2C", "B2B2C"], "Lab or clinic group"),
    ("alert_message", "SMS, WhatsApp, email alerts", "message", "0.2", ["B2B", "B2B2C"], "Any business"),
    ("featured_listing_week", "Featured listing in search", "week", "300", ["B2C"], "Pharmacy, lab, doctor"),
    ("patient_campaign", "Patient campaign (push + SMS)", "campaign", "200", ["B2B2C"], "Clinic group"),
    ("extra_admin_user", "Extra admin user", "user / month", "50", ["B2B", "B2B2C"], "Any business"),
]
PACKS = [("starter", "Starter", "1000", "1000", "0"), ("growth", "Growth", "5000", "5000", "250"),
         ("enterprise", "Enterprise", "20000", "20000", "2000")]
PLANS = [
    ("white_label_standard", "White label · Standard", "white_label", "2500", "15000", "2.00"),
    ("white_label_enterprise", "White label · Enterprise", "white_label", "6000", "30000", "1.50"),
    ("partner_basic", "Partner · Basic", "partner", "0", "0", None),
    ("partner_pro", "Partner · Pro", "partner", "199", "0", None),
]
DEFAULT_RULES = [("B2C", "default", None, "10"), ("B2C", "partner_type", "lab", "15"),
                 ("B2C", "partner_type", "doctor", "20"), ("B2B2C", "default", None, "2"), ("B2B", "default", None, "3")]


async def seed(app: FastAPI) -> None:
    state["app"] = app
    app.state.payments = ServiceClient(settings.payment_service_url, settings.internal_api_key, timeout=15)
    app.state.partners = ServiceClient(settings.partner_service_url, settings.internal_api_key)
    app.state.catalog = ServiceClient(settings.catalog_service_url, settings.internal_api_key)
    async with app.state.db.sessionmaker() as session, session.begin():
        for code, name, unit, rate, models, to in RATE_CARD:
            await session.execute(pg_insert(RateCardItem).values(
                action_code=code, name=name, unit=unit, credits_per_unit=Decimal(rate), models=models, charged_to=to)
                .on_conflict_do_nothing())
        for code, name, price, cr, bonus in PACKS:
            await session.execute(pg_insert(CreditPack).values(
                code=code, name=name, price=Decimal(price), credits=Decimal(cr), bonus_credits=Decimal(bonus))
                .on_conflict_do_nothing())
        for code, name, audience, monthly, setup, fee in PLANS:
            await session.execute(pg_insert(Plan).values(
                code=code, name=name, audience=audience, monthly_fee=Decimal(monthly), setup_fee=Decimal(setup),
                transaction_fee_pct=Decimal(fee) if fee else None).on_conflict_do_nothing())
        if not await session.scalar(select(CommissionRule.id).limit(1)):
            for model, scope, value, rate in DEFAULT_RULES:
                session.add(CommissionRule(model=model, scope=scope, scope_value=value, rate_pct=Decimal(rate),
                                           note="default"))


async def shutdown(app: FastAPI) -> None:
    for c in (app.state.payments, app.state.partners, app.state.catalog):
        await c.aclose()


# ---------------------------------------------------------------- event handlers
async def on_order_paid(event: EventEnvelope, session) -> None:
    for sub in event.data["sub_orders"]:
        await settlement.accrue_sub_order(session, event.data, sub)


async def on_sub_order_status(event: EventEnvelope, session) -> None:
    if event.data["status"] == "completed":
        await settlement.mark_earned(session, uuid.UUID(event.data["sub_order_id"]))


async def on_sub_order_refunded(event: EventEnvelope, session) -> None:
    await settlement.reverse(session, uuid.UUID(event.data["sub_order_id"]))


async def on_po_received(event: EventEnvelope, session) -> None:
    await settlement.accrue_purchase_order(session, event.data)


async def on_payment_succeeded(event: EventEnvelope, session) -> None:
    data = event.data
    if data["purpose"] == "credit_topup":
        topup = await session.get(TopUp, uuid.UUID(data["reference_id"]), with_for_update=True)
        if topup is None or topup.status != "pending":
            return
        pack = await session.get(CreditPack, topup.pack_code)
        wallet = await session.get(CreditWallet, topup.wallet_id, with_for_update=True)
        await credits.add_credits(session, wallet, topup.credits, source="topup", reference=f"topup:{topup.id}",
                                  description=f"Top-up · {pack.name} pack (RM {topup.price})")
        await credits.add_credits(session, wallet, topup.bonus_credits, source="bonus",
                                  reference=f"topup-bonus:{topup.id}", description=f"{pack.name} pack bonus")
        wallet.last_topup_credits = topup.credits + topup.bonus_credits
        wallet.low_alert_sent = False
        invoice = await invoicing.issue_invoice(
            session, customer_type=wallet.owner_type, customer_id=wallet.owner_id, kind="credit_topup",
            lines=[{"description": f"{pack.name} credit pack — {topup.credits + topup.bonus_credits} credits",
                    "unit_price": topup.price}], paid=True, payment_id=data["payment_id"])
        topup.status = "paid"
        topup.invoice_id = invoice.id
        emit(session, "billing.topup.completed", key=wallet.owner_id, data={
            "owner_type": wallet.owner_type, "owner_id": wallet.owner_id, "credits": wallet.last_topup_credits,
            "balance": wallet.balance, "invoice_number": invoice.number})
    elif data["purpose"] == "subscription":
        invoice = await session.get(Invoice, uuid.UUID(data["reference_id"]), with_for_update=True)
        if invoice and invoice.status == "issued":
            invoice.status = "paid"
            invoice.paid_at = datetime.now(timezone.utc)
            invoice.payment_id = data["payment_id"]


async def on_payment_failed(event: EventEnvelope, session) -> None:
    if event.data["purpose"] == "credit_topup":
        topup = await session.get(TopUp, uuid.UUID(event.data["reference_id"]), with_for_update=True)
        if topup and topup.status == "pending":
            topup.status = "failed"


async def on_low_balance(event: EventEnvelope, session) -> None:
    pack = event.data.get("auto_topup_pack")
    if not pack:
        return
    wallet = await session.get(CreditWallet, uuid.UUID(event.data["wallet_id"]))
    # With a tokenised card on file the gateway charges this without customer action.
    await create_topup(state["app"].state.payments, session, wallet, pack, "card", None, None, automatic=True)


async def on_tenant_created(event: EventEnvelope, session) -> None:
    if event.data.get("plan_code"):
        await invoicing.start_subscription(session, "tenant", uuid.UUID(event.data["tenant_id"]), event.data["plan_code"])


async def on_network_member(event: EventEnvelope, session) -> None:
    data = event.data
    if not data.get("partner_id"):
        return
    pct = Decimal(data["commission_pct"]) if data.get("commission_pct") is not None else None
    await session.execute(pg_insert(TenantPartnerRate).values(
        tenant_id=uuid.UUID(data["tenant_id"]), partner_id=uuid.UUID(data["partner_id"]), commission_pct=pct)
        .on_conflict_do_update(index_elements=["tenant_id", "partner_id"], set_={"commission_pct": pct}))


# ---------------------------------------------------------------- jobs
async def job_payouts(app: FastAPI) -> None:
    async with app.state.db.sessionmaker() as session, session.begin():
        created = await settlement.build_payouts(session)
    log.info("payouts_built", count=len(created))


async def job_credit_housekeeping(app: FastAPI) -> None:
    async with app.state.db.sessionmaker() as session, session.begin():
        expired = await credits.expire_lots(session)
        paused = await credits.pause_expired_grace(session)
        renewed = await invoicing.renew_due(session)
    log.info("billing_housekeeping", expired_lots=expired, paused_wallets=paused, renewed_subscriptions=renewed)


app = create_service_app(
    ServiceSpec(
        settings=settings,
        title="GeneJunction Billing Service",
        description="Commission & settlement, credit wallets and rate card, payouts, subscriptions and SST invoices.",
        routers=[router, finance, internal],
        on_startup=[seed],
        on_shutdown=[shutdown],
        event_handlers={
            "order.paid": on_order_paid,
            "order.sub_order.status_changed": on_sub_order_status,
            "order.sub_order.refunded": on_sub_order_refunded,
            "wholesale.po.received": on_po_received,
            "payment.succeeded": on_payment_succeeded,
            "payment.failed": on_payment_failed,
            "billing.wallet.low_balance": on_low_balance,
            "tenant.created": on_tenant_created,
            "tenant.network.member_upserted": on_network_member,
        },
        jobs=[PeriodicJob("weekly-payouts", settings.payout_interval_seconds, job_payouts),
              PeriodicJob("credit-housekeeping", 3600, job_credit_housekeeping)],
    )
)
