import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from gj_common.contracts import PaymentRef


class RateCardOut(BaseModel):
    action_code: str = Field(examples=["ai_report_summary"])
    name: str
    unit: str
    credits_per_unit: Decimal
    models: list[str]
    charged_to: str


class CreditPackOut(BaseModel):
    code: str
    name: str
    price: Decimal = Field(description="RM, before SST")
    credits: Decimal
    bonus_credits: Decimal
    total_credits: Decimal


class PlanOut(BaseModel):
    code: str
    name: str
    audience: str
    monthly_fee: Decimal
    setup_fee: Decimal
    transaction_fee_pct: Decimal | None


class WalletView(BaseModel):
    id: uuid.UUID
    owner_type: str
    owner_id: uuid.UUID
    balance: Decimal = Field(description="Credits (1 credit = RM 1). Negative only during the grace period")
    status: str = Field(description="active | grace (out of credits, extras still work for 7 days) | paused")
    grace_started_at: datetime | None
    last_topup_credits: Decimal
    auto_topup_pack: str | None
    low_balance_threshold: Decimal = Field(description="Alert is sent when the balance reaches this (20% of last top-up)")
    grace_ends_at: datetime | None


class LedgerPage(BaseModel):
    items: list["LedgerItem"]
    total: int
    limit: int
    offset: int


class LedgerItem(BaseModel):
    id: uuid.UUID
    entry_type: str = Field(description="topup | bonus | debit | refund | expiry | adjustment")
    amount: Decimal = Field(description="Positive adds credits, negative uses them")
    balance_after: Decimal
    action_code: str | None
    quantity: Decimal | None
    unit_credits: Decimal | None
    description: str
    created_at: datetime


class TopUpResponse(BaseModel):
    topup_id: uuid.UUID
    pack: str
    price: Decimal
    sst: Decimal
    total: Decimal = Field(description="Amount charged: price + SST")
    credits: Decimal = Field(description="Credits added after payment, including bonus")
    payment: PaymentRef


class ChargeResult(BaseModel):
    charged: bool
    credits: Decimal
    balance: Decimal
    status: str
    ledger_id: uuid.UUID
    duplicate: bool


class FeaturedResult(ChargeResult):
    featured_until: datetime


class SettlementOut(BaseModel):
    id: uuid.UUID
    source_type: str = Field(description="sub_order | purchase_order")
    source_id: uuid.UUID
    entry_kind: str = Field(description="sale | reversal (claw-back of a refunded sale already paid out)")
    order_id: uuid.UUID | None
    channel: str = Field(description="B2C | B2B2C | B2B")
    gross: Decimal
    gj_rate_pct: Decimal
    gj_commission: Decimal
    tenant_commission: Decimal = Field(description="Clinic group's share (B2B2C)")
    partner_net: Decimal = Field(description="Paid out to the partner")
    status: str = Field(description="accrued (paid, not delivered) | earned (payable) | reversed")
    payout_id: uuid.UUID | None
    created_at: datetime


class SettlementSummary(BaseModel):
    gross: Decimal
    gj_commission: Decimal
    tenant_commission: Decimal
    partner_net: Decimal


class SettlementPage(BaseModel):
    items: list[SettlementOut]
    total: int
    limit: int
    offset: int
    unpaid_summary: SettlementSummary


class SettlementList(BaseModel):
    items: list[SettlementOut]
    total: int
    limit: int
    offset: int


class PayoutOut(BaseModel):
    id: uuid.UUID
    payee_type: str = Field(description="partner | tenant (clinic group commission)")
    payee_id: uuid.UUID
    amount: Decimal
    entry_count: int
    status: str = Field(description="pending_approval | approved | paid | failed")
    period_end: datetime
    paid_at: datetime | None
    bank_reference: str | None
    bank: dict | None = Field(description="Bank snapshot at approval (only last 4 digits)")


class PayoutPage(BaseModel):
    items: list[PayoutOut]
    total: int
    limit: int
    offset: int


class InvoiceLine(BaseModel):
    description: str
    qty: Decimal
    unit_price: Decimal
    amount: Decimal


class InvoiceOut(BaseModel):
    id: uuid.UUID
    number: str = Field(examples=["GJ-INV-2026-0000009"])
    kind: str = Field(description="credit_topup | subscription")
    subtotal: Decimal
    sst_rate: Decimal
    sst_amount: Decimal
    total: Decimal
    lines: list[InvoiceLine]
    status: str = Field(description="issued | paid | void")
    issued_at: datetime
    due_at: datetime | None
    paid_at: datetime | None


class InvoicePage(BaseModel):
    items: list[InvoiceOut]
    total: int
    limit: int
    offset: int


class InvoicePayment(BaseModel):
    invoice: InvoiceOut
    payment: PaymentRef


class ChannelRevenue(BaseModel):
    channel: str
    gmv: Decimal
    gj_earnings: Decimal


class RevenueReport(BaseModel):
    commission_and_fees: list[ChannelRevenue]
    credits_consumed: Decimal
    subscription_revenue: Decimal


class SubscriptionOut(BaseModel):
    id: uuid.UUID
    plan_code: str
    current_period_end: datetime


LedgerPage.model_rebuild()
