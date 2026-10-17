from datetime import datetime, timezone

from fastapi import FastAPI
from sqlalchemy import select

from app.api import _event, internal, public
from app.config import settings
from app.models import Payment
from gj_common.app_factory import ServiceSpec, create_service_app
from gj_common.jobs import PeriodicJob


async def expire_payments(app: FastAPI) -> None:
    async with app.state.db.sessionmaker() as session, session.begin():
        rows = (await session.scalars(
            select(Payment).where(Payment.status == "requires_action", Payment.expires_at < datetime.now(timezone.utc))
            .limit(500).with_for_update(skip_locked=True))).all()
        for payment in rows:
            payment.status = "expired"
            payment.failure_reason = "expired"
            _event(session, "payment.failed", payment, reason="expired")


app = create_service_app(
    ServiceSpec(
        settings=settings,
        title="GeneJunction Payment Service",
        description="Payment intents (FPX, cards, e-wallets), gateway webhooks, refunds.",
        routers=[public, internal],
        jobs=[PeriodicJob("expire-payments", 60, expire_payments)],
    )
)
