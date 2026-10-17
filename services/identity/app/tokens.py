import base64
import hashlib
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import RefreshToken, RoleAssignment, SigningKey, User
from gj_common.auth import PARTNER_ADMIN, PATIENT
from gj_common.db import Database


def _b64(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _new_key() -> SigningKey:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private.public_key().public_numbers()
    jwk = {"kty": "RSA", "alg": "RS256", "use": "sig", "n": _b64(numbers.n), "e": _b64(numbers.e)}
    jwk["kid"] = hashlib.sha256(f"{jwk['n']}.{jwk['e']}".encode()).hexdigest()[:16]
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return SigningKey(kid=jwk["kid"], private_pem=pem, public_jwk=jwk, active=True)


class KeyStore:
    """RSA signing keys live in Postgres so every identity replica signs with the same key."""

    def __init__(self) -> None:
        self.active: SigningKey | None = None
        self.jwks: dict = {"keys": []}

    async def load(self, db: Database) -> None:
        async with db.sessionmaker() as session, session.begin():
            await session.execute(text("SELECT pg_advisory_xact_lock(814201)"))
            keys = (await session.scalars(select(SigningKey).order_by(SigningKey.created_at.desc()))).all()
            if not any(k.active for k in keys):
                key = _new_key()
                session.add(key)
                keys = [key, *keys]
        self.active = next(k for k in keys if k.active)
        self.jwks = {"keys": [k.public_jwk for k in keys[:3]]}


keystore = KeyStore()


async def build_claims(session: AsyncSession, user: User, tenant_id: uuid.UUID) -> dict:
    assignments = (await session.scalars(select(RoleAssignment).where(RoleAssignment.user_id == user.id))).all()
    roles = {PATIENT}
    partner_ids: list[str] = []
    partner_admin_ids: list[str] = []
    for a in assignments:
        if a.scope_type == "platform":
            roles.add(a.role)
        elif a.scope_type == "tenant" and a.scope_id == tenant_id:
            roles.add(a.role)
        elif a.scope_type == "partner":
            roles.add(a.role)
            partner_ids.append(str(a.scope_id))
            if a.role == PARTNER_ADMIN:
                partner_admin_ids.append(str(a.scope_id))
    return {
        "sub": str(user.id),
        "uid": str(user.id),
        "tenant_id": str(tenant_id),
        "roles": sorted(roles),
        "partner_ids": sorted(set(partner_ids)),
        "partner_admin_ids": sorted(set(partner_admin_ids)),
        "phone_number": user.phone,
        "email": user.email,
        "name": user.full_name,
        "legacy_patient_id": user.legacy_patient_id,
    }


def sign_access_token(claims: dict) -> str:
    now = int(time.time())
    payload = {
        **claims,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + settings.access_token_ttl,
        "jti": secrets.token_hex(8),
        "token_use": "access",
    }
    key = keystore.active
    return jwt.encode(payload, key.private_pem, algorithm="RS256", headers={"kid": key.kid})


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_refresh_token(user_id: uuid.UUID, tenant_id: uuid.UUID, family_id: uuid.UUID | None = None) -> tuple[str, RefreshToken]:
    raw = secrets.token_urlsafe(48)
    row = RefreshToken(
        user_id=user_id,
        tenant_id=tenant_id,
        family_id=family_id or uuid.uuid4(),
        token_hash=hash_token(raw),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=settings.refresh_token_ttl),
    )
    return raw, row


async def issue_tokens(session: AsyncSession, user: User, tenant_id: uuid.UUID, family_id: uuid.UUID | None = None) -> dict:
    claims = await build_claims(session, user, tenant_id)
    raw_refresh, row = new_refresh_token(user.id, tenant_id, family_id)
    session.add(row)
    return {
        "access_token": sign_access_token(claims),
        "refresh_token": raw_refresh,
        "token_type": "Bearer",
        "expires_in": settings.access_token_ttl,
        "user": {
            "id": str(user.id),
            "full_name": user.full_name,
            "phone": user.phone,
            "email": user.email,
            "roles": claims["roles"],
            "partner_ids": claims["partner_ids"],
            "tenant_id": str(tenant_id),
        },
    }
