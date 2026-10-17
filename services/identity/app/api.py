import uuid
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Query, Request
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import otp
from app.config import settings
from app.models import RefreshToken, RoleAssignment, User, UserTenant
from app.schemas import (
    BusinessSignup,
    LegacyLink,
    OtpRequest,
    OtpRequested,
    OtpVerify,
    PasswordLogin,
    RefreshRequest,
    RoleGrant,
    UserLookup,
    UserOut,
    UserUpdate,
)
from app.tokens import hash_token, issue_tokens, keystore
from gj_common.auth import GJ_ADMIN, CurrentUser, InternalOnly, require_roles, staff_only
from gj_common.db import SessionDep
from gj_common.errors import Conflict, Forbidden, NotFound, Unauthorized
from gj_common.events import emit
from gj_common.pagination import Page, PageDep, paginate
from gj_common.tenancy import CurrentTenant

router = APIRouter()
auth = APIRouter(prefix="/api/v1/auth", tags=["auth"])
users = APIRouter(prefix="/api/v1/users", tags=["users"])
internal = APIRouter(prefix="/internal", tags=["internal"], dependencies=[InternalOnly])

hasher = PasswordHasher()


async def _ensure_membership(session: AsyncSession, user_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    await session.execute(
        pg_insert(UserTenant).values(user_id=user_id, tenant_id=tenant_id).on_conflict_do_nothing()
    )


def _registered(session: AsyncSession, user: User, tenant_id: uuid.UUID) -> None:
    emit(
        session,
        "identity.user.registered",
        key=user.id,
        tenant_id=tenant_id,
        data={"user_id": user.id, "phone": user.phone, "email": user.email, "full_name": user.full_name},
    )


@router.get("/.well-known/jwks.json", tags=["auth"])
async def jwks():
    return keystore.jwks


@auth.post("/otp/request", response_model=OtpRequested)
async def request_otp(body: OtpRequest, request: Request, tenant: CurrentTenant):
    phone = otp.normalize_phone(body.phone)
    code = await otp.request_otp(request.app.state.redis, phone, tenant.name)
    return OtpRequested(expires_in=settings.otp_ttl, debug_otp=code if settings.otp_debug_return else None)


@auth.post("/otp/verify")
async def verify_otp(body: OtpVerify, request: Request, session: SessionDep, tenant: CurrentTenant):
    phone = otp.normalize_phone(body.phone)
    await otp.verify_otp(request.app.state.redis, phone, body.code)
    user = await session.scalar(select(User).where(User.phone == phone))
    if user is None:
        user = User(phone=phone, full_name=body.full_name)
        session.add(user)
        await session.flush()
        _registered(session, user, tenant.id)
    if user.status != "active":
        raise Forbidden("Account disabled")
    user.last_login_at = datetime.now(timezone.utc)
    await _ensure_membership(session, user.id, tenant.id)
    tokens = await issue_tokens(session, user, tenant.id)
    await session.commit()
    return tokens


@auth.post("/signup", status_code=201)
async def business_signup(body: BusinessSignup, session: SessionDep, tenant: CurrentTenant):
    """Email + password accounts for partner portal / business console users."""
    email = body.email.lower()
    if await session.scalar(select(User.id).where(User.email == email)):
        raise Conflict("Email already registered", code="email_taken")
    phone = otp.normalize_phone(body.phone) if body.phone else None
    if phone and await session.scalar(select(User.id).where(User.phone == phone)):
        raise Conflict("Phone already registered", code="phone_taken")
    user = User(email=email, phone=phone, full_name=body.full_name, password_hash=hasher.hash(body.password))
    session.add(user)
    await session.flush()
    _registered(session, user, tenant.id)
    await _ensure_membership(session, user.id, tenant.id)
    tokens = await issue_tokens(session, user, tenant.id)
    await session.commit()
    return tokens


@auth.post("/login")
async def password_login(body: PasswordLogin, session: SessionDep, tenant: CurrentTenant):
    user = await session.scalar(select(User).where(User.email == body.email.lower()))
    if user is None or not user.password_hash:
        raise Unauthorized("Invalid email or password", code="invalid_credentials")
    try:
        hasher.verify(user.password_hash, body.password)
    except VerifyMismatchError as exc:
        raise Unauthorized("Invalid email or password", code="invalid_credentials") from exc
    if user.status != "active":
        raise Forbidden("Account disabled")
    if hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hasher.hash(body.password)
    user.last_login_at = datetime.now(timezone.utc)
    await _ensure_membership(session, user.id, tenant.id)
    tokens = await issue_tokens(session, user, tenant.id)
    await session.commit()
    return tokens


@auth.post("/refresh")
async def refresh(body: RefreshRequest, session: SessionDep):
    row = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(body.refresh_token)).with_for_update()
    )
    now = datetime.now(timezone.utc)
    if row is None:
        raise Unauthorized("Invalid refresh token", code="invalid_token")
    if row.revoked_at is not None:
        # A rotated token was replayed: assume theft and revoke the whole family.
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await session.commit()
        raise Unauthorized("Refresh token reuse detected", code="token_reused")
    if row.expires_at < now:
        raise Unauthorized("Refresh token expired", code="token_expired")
    row.revoked_at = now
    user = await session.get(User, row.user_id)
    if user is None or user.status != "active":
        raise Unauthorized()
    tokens = await issue_tokens(session, user, row.tenant_id, family_id=row.family_id)
    await session.commit()
    return tokens


@auth.post("/logout", status_code=204)
async def logout(body: RefreshRequest, session: SessionDep):
    row = await session.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(body.refresh_token)))
    if row is not None:
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )
        await session.commit()


@users.get("/me", response_model=UserOut)
async def me(principal: CurrentUser, session: SessionDep):
    user = await session.get(User, principal.user_id)
    if user is None:
        raise NotFound("User not found")
    return user


@users.patch("/me", response_model=UserOut)
async def update_me(body: UserUpdate, principal: CurrentUser, session: SessionDep):
    user = await session.get(User, principal.user_id)
    if user is None:
        raise NotFound("User not found")
    if body.email and body.email.lower() != user.email:
        if await session.scalar(select(User.id).where(User.email == body.email.lower())):
            raise Conflict("Email already registered", code="email_taken")
        user.email = body.email.lower()
    if body.full_name is not None:
        user.full_name = body.full_name
    await session.commit()
    return user


@users.get("", response_model=Page[UserOut], dependencies=[staff_only()])
async def list_users(session: SessionDep, page: PageDep, q: str | None = Query(None)):
    stmt = select(User).order_by(User.created_at.desc())
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(User.phone.ilike(like), User.email.ilike(like), User.full_name.ilike(like)))
    return await paginate(session, stmt, page)


@users.post("/{user_id}/roles", status_code=201, dependencies=[require_roles(GJ_ADMIN)])
async def grant_role(user_id: uuid.UUID, body: RoleGrant, session: SessionDep):
    await _grant(session, user_id, body)
    await session.commit()
    return {"granted": True}


async def _grant(session: AsyncSession, user_id: uuid.UUID, body: RoleGrant) -> None:
    if await session.get(User, user_id) is None:
        raise NotFound("User not found")
    await session.execute(
        pg_insert(RoleAssignment)
        .values(user_id=user_id, role=body.role, scope_type=body.scope_type, scope_id=body.scope_id)
        .on_conflict_do_nothing()
    )


# ---------------------------------------------------------------- internal
@internal.get("/users/{user_id}", response_model=UserOut)
async def internal_get_user(user_id: uuid.UUID, session: SessionDep):
    user = await session.get(User, user_id)
    if user is None:
        raise NotFound("User not found")
    return user


@internal.post("/users/{user_id}/roles", status_code=201)
async def internal_grant_role(user_id: uuid.UUID, body: RoleGrant, session: SessionDep):
    await _grant(session, user_id, body)
    await session.commit()
    return {"granted": True}


@internal.post("/users/lookup", response_model=UserOut)
async def internal_lookup(body: UserLookup, session: SessionDep):
    """Find (or create) a user by email/phone, e.g. when a clinic group invites staff."""
    phone = otp.normalize_phone(body.phone) if body.phone else None
    email = body.email.lower() if body.email else None
    conds = [c for c in (User.email == email if email else None, User.phone == phone if phone else None) if c is not None]
    if not conds:
        raise NotFound("email or phone required")
    user = await session.scalar(select(User).where(or_(*conds)))
    if user is None:
        if not body.create:
            raise NotFound("User not found")
        user = User(email=email, phone=phone, full_name=body.full_name)
        session.add(user)
        await session.flush()
        emit(session, "identity.user.registered", key=user.id, data={"user_id": user.id, "phone": phone, "email": email})
        await session.commit()
    return user


@internal.post("/users/{user_id}/legacy-link", response_model=UserOut)
async def internal_legacy_link(user_id: uuid.UUID, body: LegacyLink, session: SessionDep):
    user = await session.get(User, user_id)
    if user is None:
        raise NotFound("User not found")
    user.legacy_patient_id = body.legacy_patient_id
    await session.commit()
    return user


@internal.get("/tenants/{tenant_id}/members")
async def internal_tenant_members(
    tenant_id: uuid.UUID,
    session: SessionDep,
    after: uuid.UUID | None = None,
    limit: int = Query(500, le=1000),
):
    """Keyset-paginated member list (used for patient campaigns)."""
    stmt = (
        select(User.id, User.phone, User.email, User.full_name)
        .join(UserTenant, UserTenant.user_id == User.id)
        .where(UserTenant.tenant_id == tenant_id, User.status == "active")
        .order_by(User.id)
        .limit(limit)
    )
    if after:
        stmt = stmt.where(User.id > after)
    rows = (await session.execute(stmt)).all()
    return {
        "items": [{"id": r.id, "phone": r.phone, "email": r.email, "full_name": r.full_name} for r in rows],
        "next": str(rows[-1].id) if len(rows) == limit else None,
    }


router.include_router(auth)
router.include_router(users)
router.include_router(internal)
