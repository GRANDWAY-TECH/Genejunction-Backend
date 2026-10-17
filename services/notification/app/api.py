import asyncio
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.channels import CHANNELS
from app.models import Campaign, DeviceToken, Notification
from gj_common.auth import CurrentUser
from gj_common.db import SessionDep
from gj_common.errors import NotFound
from gj_common.logging import get_logger
from gj_common.pagination import PageDep, paginate
from gj_common.tenancy import CurrentTenant

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel: str
    template: str
    title: str
    body: str
    data: dict
    read_at: datetime | None
    created_at: datetime


class DeviceIn(BaseModel):
    platform: Literal["ios", "android", "web"]
    token: str = Field(max_length=512)


class CampaignIn(BaseModel):
    tenant_id: uuid.UUID
    title: str = Field(max_length=200)
    message: str = Field(max_length=640)
    channels: list[Literal["push", "sms", "email"]] = ["push", "sms"]


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    title: str
    message: str
    channels: list
    status: str
    recipients: int
    created_at: datetime


@router.get("/me")
async def my_inbox(principal: CurrentUser, tenant: CurrentTenant, session: SessionDep, page: PageDep):
    stmt = select(Notification).where(Notification.recipient_type == "user", Notification.recipient_id == principal.user_id,
                                      Notification.tenant_id == tenant.id, Notification.channel == "in_app") \
        .order_by(Notification.created_at.desc())
    return await paginate(session, stmt, page, NotificationOut.model_validate)


@router.get("/business")
async def business_inbox(owner_type: Literal["partner", "tenant"], owner_id: uuid.UUID, principal: CurrentUser,
                         session: SessionDep, page: PageDep):
    if owner_type == "partner":
        principal.ensure_partner(owner_id)
    else:
        principal.ensure_tenant_admin(owner_id)
    stmt = select(Notification).where(Notification.recipient_type == owner_type, Notification.recipient_id == owner_id,
                                      Notification.channel == "in_app").order_by(Notification.created_at.desc())
    return await paginate(session, stmt, page, NotificationOut.model_validate)


@router.post("/{notification_id}/read", status_code=204)
async def mark_read(notification_id: uuid.UUID, principal: CurrentUser, session: SessionDep):
    n = await session.get(Notification, notification_id)
    if n is None:
        raise NotFound()
    allowed = (n.recipient_type == "user" and n.recipient_id == principal.user_id) or \
              (n.recipient_type == "partner" and principal.can_act_for_partner(n.recipient_id)) or \
              (n.recipient_type == "tenant" and principal.tenant_id == n.recipient_id)
    if not allowed:
        raise NotFound()
    n.read_at = datetime.now(timezone.utc)
    await session.commit()


@router.post("/devices", status_code=201)
async def register_device(body: DeviceIn, principal: CurrentUser, tenant: CurrentTenant, session: SessionDep):
    existing = await session.scalar(select(DeviceToken).where(DeviceToken.token == body.token))
    if existing:
        existing.user_id, existing.tenant_id, existing.platform = principal.user_id, tenant.id, body.platform
    else:
        session.add(DeviceToken(user_id=principal.user_id, tenant_id=tenant.id, platform=body.platform, token=body.token))
    await session.commit()
    return {"registered": True}


@router.post("/campaigns", response_model=CampaignOut, status_code=201)
async def create_campaign(body: CampaignIn, principal: CurrentUser, request: Request, session: SessionDep):
    """Clinic-group patient campaign (push + SMS): 200 credits per campaign."""
    principal.ensure_tenant_admin(body.tenant_id)
    campaign = Campaign(id=uuid.uuid4(), tenant_id=body.tenant_id, title=body.title, message=body.message,
                        channels=body.channels, created_by=principal.user_id)
    await request.app.state.billing.post("/internal/credits/charge", json={
        "owner_type": "tenant", "owner_id": str(body.tenant_id), "action_code": "patient_campaign", "quantity": "1",
        "reference": f"campaign:{campaign.id}", "description": f"Campaign · {body.title}"})
    session.add(campaign)
    await session.commit()
    task = asyncio.create_task(send_campaign(request.app, campaign.id))
    request.app.state.background.add(task)
    task.add_done_callback(request.app.state.background.discard)
    return campaign


@router.get("/campaigns")
async def list_campaigns(tenant_id: uuid.UUID, principal: CurrentUser, session: SessionDep, page: PageDep):
    principal.ensure_tenant_admin(tenant_id)
    stmt = select(Campaign).where(Campaign.tenant_id == tenant_id).order_by(Campaign.created_at.desc())
    return await paginate(session, stmt, page, CampaignOut.model_validate)


async def send_campaign(app: FastAPI, campaign_id: uuid.UUID) -> None:
    db = app.state.db
    async with db.sessionmaker() as session:
        campaign = await session.get(Campaign, campaign_id)
        campaign.status = "sending"
        await session.commit()
        tenant = await app.state.tenant_resolver.get_by_id(campaign.tenant_id)
        after, sent = None, 0
        try:
            while True:
                page = await app.state.identity.get(f"/internal/tenants/{campaign.tenant_id}/members",
                                                    params={"after": after, "limit": 500} if after else {"limit": 500})
                for member in page["items"]:
                    uid = uuid.UUID(member["id"])
                    session.add(Notification(tenant_id=campaign.tenant_id, recipient_type="user", recipient_id=uid,
                                             channel="in_app", template="campaign", title=campaign.title,
                                             body=campaign.message, data={"campaign_id": str(campaign.id)}, status="sent"))
                    for ch in campaign.channels:
                        address = member.get("phone") if ch == "sms" else member.get("email") if ch == "email" else None
                        if ch == "push":
                            tokens = (await session.scalars(select(DeviceToken.token).where(
                                DeviceToken.user_id == uid, DeviceToken.tenant_id == campaign.tenant_id))).all()
                            for t in tokens:
                                await CHANNELS["push"].send(address=t, title=campaign.title, body=campaign.message,
                                                            sender=tenant.name)
                        elif address:
                            await CHANNELS[ch].send(address=address, title=campaign.title, body=campaign.message,
                                                    sender=tenant.name)
                    sent += 1
                await session.commit()
                after = page["next"]
                if not after:
                    break
            campaign.status = "sent"
        except Exception as exc:
            log.exception("campaign_failed", campaign=str(campaign_id), error=str(exc))
            campaign.status = "failed"
        campaign.recipients = sent
        await session.commit()
