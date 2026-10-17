import secrets
import uuid

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api import internal, public
from app.config import settings
from app.models import DEFAULT_BRANDING, NetworkMember, Tenant, TenantDomain
from app.resolver import LocalTenantResolver
from gj_common.app_factory import ServiceSpec, create_service_app
from gj_common.events import EventEnvelope, emit
from gj_common.http import ServiceClient


async def startup(app: FastAPI) -> None:
    app.state.tenant_resolver = LocalTenantResolver(app.state.db)
    app.state.identity = ServiceClient(settings.identity_service_url, settings.internal_api_key)
    app.state.partners = ServiceClient(settings.partner_service_url, settings.internal_api_key)
    async with app.state.db.sessionmaker() as session, session.begin():
        exists = await session.scalar(select(Tenant.id).where(Tenant.slug == settings.default_tenant_slug))
        if not exists:
            tenant = Tenant(slug=settings.default_tenant_slug, name="GeneJunction", type="platform",
                            status="active", branding=dict(DEFAULT_BRANDING))
            session.add(tenant)
            await session.flush()
            for domain in ("genejunction.com", "www.genejunction.com"):
                session.add(TenantDomain(tenant_id=tenant.id, domain=domain, verified=True,
                                         verification_token=secrets.token_hex(16), is_primary=domain == "genejunction.com"))


async def shutdown(app: FastAPI) -> None:
    await app.state.identity.aclose()
    await app.state.partners.aclose()


async def on_partner_registered(event: EventEnvelope, session) -> None:
    """A clinic group's own pharmacy/lab joins its network automatically (private by default)."""
    data = event.data
    owner = data.get("owner_tenant_id")
    if not owner or data["type"] not in ("pharmacy", "lab", "stockist"):
        return
    stmt = (
        pg_insert(NetworkMember)
        .values(tenant_id=uuid.UUID(owner), member_type=data["type"], partner_id=uuid.UUID(data["partner_id"]),
                display_name=data["display_name"], listed_on_marketplace=False)
        .on_conflict_do_nothing(constraint="uq_network_partner")
        .returning(NetworkMember)
    )
    member = (await session.execute(stmt)).scalar_one_or_none()
    if member is not None:
        emit(session, "tenant.network.member_upserted", key=member.partner_id, tenant_id=member.tenant_id, data={
            "member_id": member.id, "tenant_id": member.tenant_id, "member_type": member.member_type,
            "partner_id": member.partner_id, "legacy_doctor_id": None,
            "listed_on_marketplace": False, "commission_pct": None, "status": member.status,
        })


app = create_service_app(
    ServiceSpec(
        settings=settings,
        title="GeneJunction Tenant Service",
        description="Brands (marketplace + white-label tenants), domains, branding and provider networks.",
        routers=[public, internal],
        on_startup=[startup],
        on_shutdown=[shutdown],
        event_handlers={"partner.registered": on_partner_registered},
    )
)
