"""Bearer-token verification shared by every service.

Tokens are RS256 JWTs. Locally the identity service signs them and publishes a JWKS;
in production the same code verifies Amazon Cognito tokens (set JWKS_URL / JWT_ISSUER
to the user pool, and roles come from `cognito:groups` when no `roles` claim exists).
"""

import asyncio
import hmac
import time
import uuid
from typing import Annotated, Any

import httpx
import jwt
from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from gj_common.errors import Forbidden, Unauthorized
from gj_common.logging import get_logger

log = get_logger(__name__)

# Platform roles
PATIENT = "patient"
PARTNER_ADMIN = "partner_admin"
PARTNER_STAFF = "partner_staff"
TENANT_ADMIN = "tenant_admin"
GJ_ADMIN = "gj_admin"
GJ_OPS = "gj_ops"
GJ_FINANCE = "gj_finance"
STAFF_ROLES = {GJ_ADMIN, GJ_OPS, GJ_FINANCE}


class Principal(BaseModel):
    user_id: uuid.UUID
    tenant_id: uuid.UUID | None = None
    roles: set[str] = Field(default_factory=set)
    partner_ids: list[uuid.UUID] = Field(default_factory=list)
    partner_admin_ids: list[uuid.UUID] = Field(default_factory=list)
    phone: str | None = None
    email: str | None = None
    name: str | None = None

    def has_any(self, *roles: str) -> bool:
        return bool(self.roles.intersection(roles))

    @property
    def is_staff(self) -> bool:
        return bool(self.roles & STAFF_ROLES)

    def can_act_for_partner(self, partner_id: uuid.UUID) -> bool:
        return self.is_staff or partner_id in self.partner_ids

    def ensure_partner(self, partner_id: uuid.UUID) -> None:
        if not self.can_act_for_partner(partner_id):
            raise Forbidden("You do not have access to this partner")

    def ensure_partner_admin(self, partner_id: uuid.UUID) -> None:
        if not (self.is_staff or partner_id in self.partner_admin_ids):
            raise Forbidden("Partner admin access required")

    def ensure_tenant_admin(self, tenant_id: uuid.UUID) -> None:
        if self.has_any(GJ_ADMIN, GJ_OPS):
            return
        if TENANT_ADMIN in self.roles and self.tenant_id == tenant_id:
            return
        raise Forbidden("Tenant admin access required")


class JWKSCache:
    def __init__(self, jwks_url: str, *, ttl: int = 600) -> None:
        self.jwks_url = jwks_url
        self.ttl = ttl
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()
        self._local = False

    def set_local_keys(self, jwks: dict) -> None:
        """Used by the identity service to verify its own tokens without an HTTP hop."""
        self._keys = {k["kid"]: jwt.PyJWK(k).key for k in jwks["keys"]}
        self._fetched_at = time.monotonic()
        self._local = True

    async def _refresh(self) -> None:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(self.jwks_url)
            resp.raise_for_status()
            self._keys = {k["kid"]: jwt.PyJWK(k).key for k in resp.json()["keys"]}
            self._fetched_at = time.monotonic()

    async def get_key(self, kid: str):
        if self._local:
            return self._keys.get(kid)
        stale = time.monotonic() - self._fetched_at > self.ttl
        if kid not in self._keys or stale:
            async with self._lock:
                if kid not in self._keys or time.monotonic() - self._fetched_at > self.ttl:
                    try:
                        await self._refresh()
                    except Exception as exc:
                        log.warning("jwks_refresh_failed", error=str(exc))
        return self._keys.get(kid)


def claims_to_principal(claims: dict) -> Principal:
    roles = claims.get("roles") or claims.get("cognito:groups") or []
    tenant_id = claims.get("tenant_id") or claims.get("custom:tenant_id")
    return Principal(
        user_id=uuid.UUID(claims.get("uid") or claims["sub"]),
        tenant_id=uuid.UUID(tenant_id) if tenant_id else None,
        roles=set(roles),
        partner_ids=[uuid.UUID(p) for p in claims.get("partner_ids", [])],
        partner_admin_ids=[uuid.UUID(p) for p in claims.get("partner_admin_ids", [])],
        phone=claims.get("phone_number"),
        email=claims.get("email"),
        name=claims.get("name"),
    )


_bearer = HTTPBearer(auto_error=False)


async def _decode(request: Request, token: str) -> Principal:
    settings = request.app.state.settings
    cache: JWKSCache = request.app.state.jwks
    try:
        header = jwt.get_unverified_header(token)
        key = await cache.get_key(header.get("kid", ""))
        if key is None:
            raise Unauthorized("Unknown signing key")
        options = {"verify_aud": settings.jwt_audience is not None}
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options=options,
            leeway=10,
        )
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("Token expired", code="token_expired") from exc
    except jwt.PyJWTError as exc:
        raise Unauthorized("Invalid token", code="invalid_token") from exc
    if claims.get("token_use", "access") != "access":
        raise Unauthorized("Access token required", code="invalid_token")
    return claims_to_principal(claims)


async def get_principal(
    request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer)
) -> Principal:
    if creds is None:
        raise Unauthorized()
    return await _decode(request, creds.credentials)


async def get_optional_principal(
    request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer)
) -> Principal | None:
    if creds is None:
        return None
    return await _decode(request, creds.credentials)


CurrentUser = Annotated[Principal, Depends(get_principal)]
OptionalUser = Annotated[Principal | None, Depends(get_optional_principal)]


def require_roles(*roles: str):
    async def dependency(principal: CurrentUser) -> Principal:
        if not principal.has_any(*roles):
            raise Forbidden(f"Requires one of roles: {', '.join(roles)}")
        return principal

    return Depends(dependency)


def staff_only():
    return require_roles(GJ_ADMIN, GJ_OPS, GJ_FINANCE)


async def require_internal(request: Request, x_internal_key: str = Header(default="")) -> None:
    expected = request.app.state.settings.internal_api_key
    if not hmac.compare_digest(x_internal_key, expected):
        raise Forbidden("Internal endpoint")


InternalOnly = Depends(require_internal)
