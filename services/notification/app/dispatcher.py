import json
import uuid

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels import CHANNELS, PAID_CHANNELS
from app.config import settings
from app.models import DeviceToken, Notification
from gj_common.errors import AppError
from gj_common.logging import get_logger

log = get_logger(__name__)


class Dispatcher:
    def __init__(self, app: FastAPI) -> None:
        self.app = app

    async def contact(self, user_id: uuid.UUID) -> dict:
        key = f"notif:contact:{user_id}"
        if cached := await self.app.state.redis.get(key):
            return json.loads(cached)
        user = await self.app.state.identity.get(f"/internal/users/{user_id}")
        data = {"phone": user.get("phone"), "email": user.get("email"), "name": user.get("full_name")}
        await self.app.state.redis.set(key, json.dumps(data), ex=3600)
        return data

    async def to_user(self, session: AsyncSession, *, event_id: uuid.UUID, user_id: uuid.UUID, tenant_id: uuid.UUID,
                      template: str, title: str, body: str, data: dict | None = None, sms: bool = False) -> None:
        """In-app + push + email always; SMS for important updates (paid by the clinic group in white label)."""
        tenant = await self.app.state.tenant_resolver.get_by_id(tenant_id)
        contact = await self.contact(user_id)
        base = dict(tenant_id=tenant_id, recipient_type="user", recipient_id=user_id, template=template,
                    title=title, body=body, data=data or {})
        session.add(Notification(channel="in_app", status="sent", **base))

        tokens = (await session.scalars(select(DeviceToken.token).where(
            DeviceToken.user_id == user_id, DeviceToken.tenant_id == tenant_id))).all()
        for token in tokens:
            await self._deliver(session, "push", token, tenant.name, base)
        if contact.get("email"):
            await self._deliver(session, "email", contact["email"], tenant.name, base)
        if sms and contact.get("phone"):
            charged_to = None
            if not tenant.is_platform:
                if not settings.white_label_sms_alerts:
                    return
                try:
                    await self.app.state.billing.post("/internal/credits/charge", json={
                        "owner_type": "tenant", "owner_id": str(tenant_id), "action_code": "alert_message",
                        "quantity": "1", "reference": f"notif:{event_id}:sms:{user_id}",
                        "description": f"SMS alert · {template}"})
                    charged_to = f"tenant:{tenant_id}"
                except AppError as exc:
                    if exc.status_code == 402:  # credits paused: fall back to free channels only
                        session.add(Notification(channel="sms", address=contact["phone"], status="skipped",
                                                 error="credits_paused", **base))
                        return
                    raise
            await self._deliver(session, "sms", contact["phone"], tenant.name, base, charged_to=charged_to)

    async def to_business(self, session: AsyncSession, *, owner_type: str, owner_id: uuid.UUID, template: str,
                          title: str, body: str, data: dict | None = None, email: str | None = None) -> None:
        base = dict(tenant_id=None, recipient_type=owner_type, recipient_id=owner_id, template=template,
                    title=title, body=body, data=data or {})
        session.add(Notification(channel="in_app", status="sent", **base))
        if email:
            await self._deliver(session, "email", email, "GeneJunction for Business", base)

    async def _deliver(self, session: AsyncSession, channel: str, address: str, sender: str, base: dict,
                       charged_to: str | None = None) -> None:
        try:
            await CHANNELS[channel].send(address=address, title=base["title"], body=base["body"], sender=sender)
            status, error = "sent", None
        except Exception as exc:  # provider outage: record and move on
            status, error = "failed", str(exc)
            log.warning("notification_failed", channel=channel, error=error)
        session.add(Notification(channel=channel, address=address, status=status, error=error,
                                 charged_to=charged_to if channel in PAID_CHANNELS else None, **base))
