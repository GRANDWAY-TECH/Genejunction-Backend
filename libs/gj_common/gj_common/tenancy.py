"""Tenant resolution: which brand (GeneJunction or a white-label clinic group) a request belongs to.

Web requests are resolved from the host (care.sejahtera.com.my), mobile apps send their
tenant slug in `X-Tenant`. Results are cached in-process and in Redis.
"""

import json
import time
import uuid
from typing import Annotated, Literal

from fastapi import Depends, Header, Request
from pydantic import BaseModel
from redis.asyncio import Redis

from gj_common.http import ServiceClient

PLATFORM = "platform"
WHITE_LABEL = "white_label"


class TenantContext(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    type: Literal["platform", "white_label"]
    status: str = "active"

    @property
    def channel(self) -> str:
        """Sales channel used for tagging orders and picking commission rules."""
        return "B2C" if self.type == PLATFORM else "B2B2C"

    @property
    def is_platform(self) -> bool:
        return self.type == PLATFORM


class TenantResolver:
    LOCAL_TTL = 30
    REDIS_TTL = 300

    def __init__(self, client: ServiceClient, redis: Redis, default_slug: str) -> None:
        self.client = client
        self.redis = redis
        self.default_slug = default_slug
        self._local: dict[str, tuple[float, TenantContext]] = {}

    async def _lookup(self, slug: str | None, host: str | None) -> TenantContext:
        data = await self.client.get("/internal/tenants/resolve", params={"slug": slug or "", "host": host or ""})
        return TenantContext.model_validate(data)

    async def resolve(self, *, slug: str | None = None, host: str | None = None) -> TenantContext:
        cache_key = f"tenant:resolve:{slug or ''}:{host or ''}"
        hit = self._local.get(cache_key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        cached = await self.redis.get(cache_key)
        if cached:
            ctx = TenantContext.model_validate_json(cached)
        else:
            ctx = await self._lookup(slug, host)
            await self.redis.set(cache_key, ctx.model_dump_json(), ex=self.REDIS_TTL)
        self._local[cache_key] = (time.monotonic() + self.LOCAL_TTL, ctx)
        return ctx

    async def get_by_id(self, tenant_id: uuid.UUID) -> TenantContext:
        cache_key = f"tenant:id:{tenant_id}"
        cached = await self.redis.get(cache_key)
        if cached:
            return TenantContext.model_validate(json.loads(cached))
        data = await self.client.get(f"/internal/tenants/{tenant_id}")
        ctx = TenantContext.model_validate(data)
        await self.redis.set(cache_key, ctx.model_dump_json(), ex=self.REDIS_TTL)
        return ctx


def request_tenant_hints(request: Request) -> tuple[str | None, str | None]:
    slug = request.headers.get("x-tenant")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if host:
        host = host.split(":")[0].lower()
    return slug, host


async def get_tenant(
    request: Request,
    x_tenant: str | None = Header(
        None,
        description="Tenant (brand) slug, e.g. `sejahtera`. Sent by white-label mobile builds. "
        "Web apps can omit it: the brand is taken from the domain. Omitted on genejunction.com = GeneJunction marketplace.",
    ),
) -> TenantContext:
    slug, host = request_tenant_hints(request)
    return await request.app.state.tenant_resolver.resolve(slug=slug, host=host)


CurrentTenant = Annotated[TenantContext, Depends(get_tenant)]
