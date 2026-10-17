import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, Sequence, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from gj_common.db import Base, Timestamps, UUIDPk
from gj_common.events import OutboxEvent, ProcessedEvent  # noqa: F401

# ---------------------------------------------------------------- commission & settlement


class CommissionRule(UUIDPk, Timestamps, Base):
    """GJ's take per channel. Most specific match wins: partner > tenant > partner_type > default."""

    __tablename__ = "commission_rules"

    model: Mapped[str] = mapped_column(String(10))  # B2C | B2B2C | B2B
    scope: Mapped[str] = mapped_column(String(20))  # default | partner_type | tenant | partner
    scope_value: Mapped[str | None] = mapped_column(String(64))
    rate_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    active: Mapped[bool] = mapped_column(default=True)
    note: Mapped[str | None]


class TenantPartnerRate(Base):
    """A clinic group's own commission on providers in its network (from the business console)."""

    __tablename__ = "tenant_partner_rates"

    tenant_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    partner_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    commission_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))


class SettlementEntry(UUIDPk, Timestamps, Base):
    """Money split for one sub-order or wholesale purchase order."""

    __tablename__ = "settlement_entries"
    __table_args__ = (
        UniqueConstraint("source_type", "source_id", "entry_kind"),
        Index("ix_settlement_payable", "partner_id", "status", postgresql_where=text("payout_id IS NULL")),
    )

    source_type: Mapped[str] = mapped_column(String(20))  # sub_order | purchase_order
    source_id: Mapped[uuid.UUID]
    entry_kind: Mapped[str] = mapped_column(String(20), default="sale")  # sale | reversal
    order_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    partner_id: Mapped[uuid.UUID] = mapped_column(index=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    channel: Mapped[str] = mapped_column(String(10))
    gross: Mapped[Decimal]
    commission_base: Mapped[Decimal]
    gj_rate_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    gj_commission: Mapped[Decimal]
    tenant_rate_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    tenant_commission: Mapped[Decimal] = mapped_column(default=Decimal("0.00"))
    partner_net: Mapped[Decimal]
    status: Mapped[str] = mapped_column(String(20), default="accrued")  # accrued | earned | reversed
    earned_at: Mapped[datetime | None]
    payout_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    tenant_payout_id: Mapped[uuid.UUID | None] = mapped_column(index=True)


class Payout(UUIDPk, Timestamps, Base):
    __tablename__ = "payouts"

    payee_type: Mapped[str] = mapped_column(String(10))  # partner | tenant
    payee_id: Mapped[uuid.UUID] = mapped_column(index=True)
    period_end: Mapped[datetime]
    amount: Mapped[Decimal]
    entry_count: Mapped[int]
    # pending_approval -> approved -> paid | failed
    status: Mapped[str] = mapped_column(String(20), default="pending_approval", index=True)
    bank_snapshot: Mapped[dict | None]
    approved_by: Mapped[uuid.UUID | None]
    paid_at: Mapped[datetime | None]
    bank_reference: Mapped[str | None] = mapped_column(String(100))


# ---------------------------------------------------------------- credits


class CreditWallet(UUIDPk, Timestamps, Base):
    __tablename__ = "credit_wallets"
    __table_args__ = (UniqueConstraint("owner_type", "owner_id"),)

    owner_type: Mapped[str] = mapped_column(String(10))  # tenant | partner
    owner_id: Mapped[uuid.UUID]
    balance: Mapped[Decimal] = mapped_column(default=Decimal("0.00"))
    status: Mapped[str] = mapped_column(String(10), default="active")  # active | grace | paused
    grace_started_at: Mapped[datetime | None]
    last_topup_credits: Mapped[Decimal] = mapped_column(default=Decimal("0.00"))
    low_alert_sent: Mapped[bool] = mapped_column(default=False)
    auto_topup_pack: Mapped[str | None] = mapped_column(String(20))


class CreditLot(UUIDPk, Base):
    """Credits expire 12 months after purchase; lots are consumed oldest-expiry first."""

    __tablename__ = "credit_lots"
    __table_args__ = (Index("ix_credit_lots_open", "wallet_id", "expires_at", postgresql_where=text("remaining > 0")),)

    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("credit_wallets.id"))
    source: Mapped[str] = mapped_column(String(20))  # topup | bonus | refund | adjustment
    credits: Mapped[Decimal]
    remaining: Mapped[Decimal]
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CreditLedger(UUIDPk, Base):
    """Append-only ledger; balance_after makes every statement line self-checking."""

    __tablename__ = "credit_ledger"
    __table_args__ = (UniqueConstraint("wallet_id", "reference"), Index("ix_credit_ledger_wallet_time", "wallet_id", "created_at"))

    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("credit_wallets.id"))
    entry_type: Mapped[str] = mapped_column(String(20))  # topup | bonus | debit | refund | expiry | adjustment
    amount: Mapped[Decimal]
    balance_after: Mapped[Decimal]
    action_code: Mapped[str | None] = mapped_column(String(40))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    unit_credits: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    reference: Mapped[str] = mapped_column(String(160))
    description: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RateCardItem(Base):
    __tablename__ = "rate_card"

    action_code: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str]
    unit: Mapped[str] = mapped_column(String(30))
    credits_per_unit: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    models: Mapped[list]
    charged_to: Mapped[str]
    active: Mapped[bool] = mapped_column(default=True)


class RateOverride(Base):
    """Per-agreement price for an action."""

    __tablename__ = "rate_overrides"

    owner_type: Mapped[str] = mapped_column(String(10), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    action_code: Mapped[str] = mapped_column(ForeignKey("rate_card.action_code"), primary_key=True)
    credits_per_unit: Mapped[Decimal] = mapped_column(Numeric(10, 2))


class CreditPack(Base):
    __tablename__ = "credit_packs"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str]
    price: Mapped[Decimal]
    credits: Mapped[Decimal]
    bonus_credits: Mapped[Decimal]
    active: Mapped[bool] = mapped_column(default=True)


class TopUp(UUIDPk, Timestamps, Base):
    __tablename__ = "credit_topups"

    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("credit_wallets.id"), index=True)
    pack_code: Mapped[str] = mapped_column(String(20))
    price: Mapped[Decimal]
    sst_amount: Mapped[Decimal]
    total: Mapped[Decimal]
    credits: Mapped[Decimal]
    bonus_credits: Mapped[Decimal]
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending | paid | failed
    payment_id: Mapped[str | None] = mapped_column(String(64))
    invoice_id: Mapped[uuid.UUID | None]
    requested_by: Mapped[uuid.UUID | None]
    automatic: Mapped[bool] = mapped_column(default=False)


# ---------------------------------------------------------------- subscriptions & invoices


class Plan(Base):
    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str]
    audience: Mapped[str] = mapped_column(String(20))  # white_label | partner
    monthly_fee: Mapped[Decimal]
    setup_fee: Mapped[Decimal]
    transaction_fee_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    included_admin_users: Mapped[int] = mapped_column(default=3)
    active: Mapped[bool] = mapped_column(default=True)


class Subscription(UUIDPk, Timestamps, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("subscriber_type", "subscriber_id"),)

    subscriber_type: Mapped[str] = mapped_column(String(10))  # tenant | partner
    subscriber_id: Mapped[uuid.UUID]
    plan_code: Mapped[str] = mapped_column(ForeignKey("plans.code"))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | past_due | cancelled
    current_period_start: Mapped[datetime]
    current_period_end: Mapped[datetime] = mapped_column(index=True)


invoice_number_seq = Sequence("invoice_number_seq", metadata=Base.metadata)


class Invoice(UUIDPk, Timestamps, Base):
    __tablename__ = "invoices"
    __table_args__ = (Index("ix_invoices_customer", "customer_type", "customer_id", "issued_at"),)

    number: Mapped[str] = mapped_column(String(30), unique=True)
    customer_type: Mapped[str] = mapped_column(String(10))
    customer_id: Mapped[uuid.UUID]
    kind: Mapped[str] = mapped_column(String(20))  # credit_topup | subscription | setup_fee
    currency: Mapped[str] = mapped_column(String(3), default="MYR")
    subtotal: Mapped[Decimal]
    sst_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    sst_amount: Mapped[Decimal]
    total: Mapped[Decimal]
    lines: Mapped[list]
    status: Mapped[str] = mapped_column(String(10), default="issued")  # issued | paid | void
    issued_at: Mapped[datetime] = mapped_column(server_default=func.now())
    due_at: Mapped[datetime | None]
    paid_at: Mapped[datetime | None]
    payment_id: Mapped[str | None] = mapped_column(String(64))
