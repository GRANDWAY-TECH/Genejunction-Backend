import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Payment, Refund, WebhookEvent
from app.providers import WebhookResult, get_provider, sign
from gj_common.auth import CurrentUser, InternalOnly, require_roles, GJ_ADMIN, GJ_FINANCE, GJ_OPS
from gj_common.db import SessionDep
from gj_common.errors import Conflict, NotFound, ValidationFailed
from gj_common.events import emit
from gj_common.money import money
from gj_common.pagination import Page, PageDep, paginate

public = APIRouter(prefix="/api/v1/payments", tags=["payments"])
internal = APIRouter(prefix="/internal/payments", tags=["internal"], dependencies=[InternalOnly])


class PaymentCreate(BaseModel):
    purpose: Literal["order", "credit_topup", "subscription", "wholesale"]
    reference_id: str
    amount: Decimal = Field(gt=0, decimal_places=2)
    currency: Literal["MYR"] = "MYR"
    method: Literal["fpx", "card", "ewallet"]
    payer_type: Literal["user", "partner", "tenant"]
    payer_id: str
    tenant_id: uuid.UUID | None = None
    description: str
    return_url: str | None = None
    idempotency_key: str
    expires_in_minutes: int = Field(30, ge=5, le=1440)


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    purpose: str
    reference_id: str
    amount: Decimal
    currency: str
    status: str
    method: str
    provider: str
    checkout_url: str | None
    return_url: str | None
    payer_type: str
    payer_id: str
    description: str
    expires_at: datetime
    paid_at: datetime | None
    refunded_amount: Decimal
    failure_reason: str | None
    created_at: datetime


class RefundCreate(BaseModel):
    amount: Decimal = Field(gt=0, decimal_places=2)
    reason: str
    reference: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(session: AsyncSession, event_type: str, p: Payment, **extra) -> None:
    emit(session, event_type, key=p.reference_id, tenant_id=p.tenant_id, data={
        "payment_id": p.id, "purpose": p.purpose, "reference_id": p.reference_id, "amount": p.amount,
        "currency": p.currency, "method": p.method, "status": p.status, "payer_type": p.payer_type,
        "payer_id": p.payer_id, **extra})


# ---------------------------------------------------------------- internal
@internal.post("", response_model=PaymentOut, status_code=201)
async def create_payment(body: PaymentCreate, session: SessionDep):
    existing = await session.scalar(select(Payment).where(Payment.idempotency_key == body.idempotency_key))
    if existing is not None:
        return existing
    provider = get_provider()
    payment = Payment(**body.model_dump(exclude={"expires_in_minutes"}), provider=provider.name,
                      expires_at=_now() + timedelta(minutes=body.expires_in_minutes))
    session.add(payment)
    await session.flush()
    payment.provider_ref, payment.checkout_url = await provider.create_checkout(payment)
    await session.commit()
    return payment


@internal.get("/{payment_id}", response_model=PaymentOut)
async def internal_get(payment_id: uuid.UUID, session: SessionDep):
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise NotFound("Payment not found")
    return payment


@internal.post("/{payment_id}/cancel", response_model=PaymentOut)
async def cancel(payment_id: uuid.UUID, session: SessionDep):
    payment = await session.get(Payment, payment_id, with_for_update=True)
    if payment is None:
        raise NotFound("Payment not found")
    if payment.status == "requires_action":
        payment.status = "cancelled"
        await session.commit()
    return payment


@internal.post("/{payment_id}/refunds", status_code=201)
async def refund(payment_id: uuid.UUID, body: RefundCreate, session: SessionDep):
    existing = await session.scalar(select(Refund).where(Refund.reference == body.reference))
    if existing is not None:
        return {"refund_id": existing.id, "status": existing.status, "amount": existing.amount}
    payment = await session.get(Payment, payment_id, with_for_update=True)
    if payment is None:
        raise NotFound("Payment not found")
    if payment.status not in ("succeeded", "partially_refunded"):
        raise Conflict(f"Cannot refund a payment that is {payment.status}", code="not_refundable")
    amount = money(body.amount)
    if amount > payment.amount - payment.refunded_amount:
        raise ValidationFailed("Refund exceeds the remaining paid amount", code="refund_too_large")
    row = Refund(payment_id=payment.id, amount=amount, reason=body.reason, reference=body.reference)
    session.add(row)
    row.provider_ref = await get_provider(payment.provider).refund(payment, amount, body.reference)
    row.status = "succeeded"
    payment.refunded_amount = money(payment.refunded_amount + amount)
    payment.status = "refunded" if payment.refunded_amount >= payment.amount else "partially_refunded"
    _event(session, "payment.refunded", payment, refund_id=row.id, refund_amount=amount,
           refund_reference=body.reference, reason=body.reason)
    await session.commit()
    return {"refund_id": row.id, "status": row.status, "amount": amount}


# ---------------------------------------------------------------- webhooks
async def apply_webhook(session: AsyncSession, provider_name: str, result: WebhookResult, raw: dict) -> str:
    inserted = await session.execute(
        pg_insert(WebhookEvent).values(provider=provider_name, provider_event_id=result.event_id, payload=raw)
        .on_conflict_do_nothing().returning(WebhookEvent.id))
    if inserted.first() is None:
        return "duplicate"
    payment = await session.scalar(select(Payment).where(Payment.provider_ref == result.provider_ref).with_for_update())
    if payment is None:
        raise NotFound("Unknown payment reference")
    if payment.status != "requires_action":
        await session.commit()
        return "ignored"
    if result.outcome == "succeeded":
        payment.status = "succeeded"
        payment.paid_at = _now()
        _event(session, "payment.succeeded", payment)
    else:
        payment.status = "failed"
        payment.failure_reason = result.reason or "declined"
        _event(session, "payment.failed", payment, reason=payment.failure_reason)
    await session.commit()
    return payment.status


@public.post("/webhooks/{provider_name}", include_in_schema=False)
async def webhook(provider_name: str, request: Request, session: SessionDep):
    body = await request.body()
    result = get_provider(provider_name).parse_webhook({k.lower(): v for k, v in request.headers.items()}, body)
    return {"result": await apply_webhook(session, provider_name, result, json.loads(body))}


@public.post("/mock/{payment_id}/complete", tags=["dev"])
async def mock_complete(payment_id: uuid.UUID, session: SessionDep, outcome: Literal["succeeded", "failed"] = "succeeded"):
    """Dev only: behaves like the gateway calling our webhook after the customer pays."""
    if not settings.enable_mock_gateway:
        raise NotFound()
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise NotFound("Payment not found")
    raw = {"event_id": f"evt_{secrets.token_hex(8)}", "provider_ref": payment.provider_ref, "outcome": outcome,
           "reason": None if outcome == "succeeded" else "Declined by bank"}
    body = json.dumps(raw).encode()
    result = get_provider("mock").parse_webhook({"x-signature": sign(body)}, body)
    status = await apply_webhook(session, "mock", result, raw)
    return {"payment_id": payment_id, "status": status, "return_url": payment.return_url}


# ---------------------------------------------------------------- payer / finance views
@public.get("/{payment_id}", response_model=PaymentOut)
async def get_payment(payment_id: uuid.UUID, principal: CurrentUser, session: SessionDep):
    payment = await session.get(Payment, payment_id)
    allowed = payment is not None and (
        principal.is_staff
        or (payment.payer_type == "user" and payment.payer_id == str(principal.user_id))
        or (payment.payer_type == "partner" and payment.payer_id in {str(p) for p in principal.partner_ids})
        or (payment.payer_type == "tenant" and payment.payer_id == str(principal.tenant_id))
    )
    if not allowed:
        raise NotFound("Payment not found")
    return payment


@public.get("", response_model=Page[PaymentOut], dependencies=[require_roles(GJ_ADMIN, GJ_FINANCE, GJ_OPS)])
async def list_payments(session: SessionDep, page: PageDep, status: str | None = None, purpose: str | None = None,
                        reference_id: str | None = None):
    stmt = select(Payment).order_by(Payment.created_at.desc())
    if status:
        stmt = stmt.where(Payment.status == status)
    if purpose:
        stmt = stmt.where(Payment.purpose == purpose)
    if reference_id:
        stmt = stmt.where(Payment.reference_id == reference_id)
    return await paginate(session, stmt, page)
