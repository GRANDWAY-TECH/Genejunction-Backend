import uuid

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Tenant, TenantDomain
from gj_common.db import Database
from gj_common.errors import Forbidden, NotFound
from gj_common.tenancy import TenantContext


def tenant_context(t: Tenant) -> dict:
    return {"id": str(t.id), "slug": t.slug, "name": t.name, "type": t.type, "status": t.status}


async def resolve_tenant(session: AsyncSession, slug: str | None, host: str | None) -> dict:
    tenant = None
    if slug:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
        if tenant is None:
            raise NotFound("Unknown tenant", code="unknown_tenant")
    elif host and host not in settings.platform_domains:
        tenant = await session.scalar(
            select(Tenant).join(TenantDomain).where(TenantDomain.domain == host, TenantDomain.verified.is_(True))
        )
    if tenant is None:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == settings.default_tenant_slug))
        if tenant is None:
            raise NotFound("Platform tenant missing", code="unknown_tenant")
    if tenant.status == "suspended":
        raise Forbidden("This service is currently unavailable", code="tenant_suspended")
    return tenant_context(tenant)


async def invalidate_tenant_cache(redis: Redis, tenant_id: uuid.UUID) -> None:
    keys = [k async for k in redis.scan_iter("tenant:resolve:*", count=500)]
    keys.append(f"tenant:id:{tenant_id}")
    await redis.delete(*keys)


class LocalTenantResolver:
    """The tenant service resolves from its own database instead of calling itself."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def resolve(self, *, slug: str | None = None, host: str | None = None) -> TenantContext:
        async with self.db.sessionmaker() as session:
            return TenantContext.model_validate(await resolve_tenant(session, slug, host))

    async def get_by_id(self, tenant_id: uuid.UUID) -> TenantContext:
        async with self.db.sessionmaker() as session:
            tenant = await session.get(Tenant, tenant_id)
            if tenant is None:
                raise NotFound("Tenant not found")
            return TenantContext.model_validate(tenant_context(tenant))
