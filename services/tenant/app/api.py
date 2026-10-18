import secrets
import uuid

from fastapi import APIRouter, Query, Request
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DEFAULT_BRANDING, NetworkMember, Tenant, TenantDomain
from app.resolver import invalidate_tenant_cache, resolve_tenant, tenant_context
from app.schemas import (
    AdminInvite,
    Branding,
    DomainCreate,
    DomainOut,
    MemberCreate,
    MemberOut,
    MemberPatch,
    PublicTenant,
    TenantCreate,
    TenantOut,
    TenantPatch,
)
from gj_common.auth import GJ_ADMIN, GJ_OPS, TENANT_ADMIN, CurrentUser, InternalOnly, require_roles, staff_only
from gj_common.contracts import RoleGranted
from gj_common.db import SessionDep
from gj_common.errors import Conflict, NotFound, ValidationFailed
from gj_common.events import emit
from gj_common.pagination import Page, PageDep, paginate
from gj_common.tenancy import CurrentTenant

public = APIRouter(prefix="/api/v1/tenants", tags=["tenants"])
internal = APIRouter(prefix="/internal", tags=["internal"], dependencies=[InternalOnly])


async def _tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound("Tenant not found")
    return tenant


def _member_event(session: AsyncSession, member: NetworkMember, event_type: str = "tenant.network.member_upserted") -> None:
    emit(
        session,
        event_type,
        key=member.partner_id or member.legacy_doctor_id,
        tenant_id=member.tenant_id,
        data={
            "member_id": member.id,
            "tenant_id": member.tenant_id,
            "member_type": member.member_type,
            "partner_id": member.partner_id,
            "legacy_doctor_id": member.legacy_doctor_id,
            "listed_on_marketplace": member.listed_on_marketplace,
            "commission_pct": member.commission_pct,
            "status": member.status,
        },
    )


@public.get("/current", response_model=PublicTenant)
async def current_tenant(tenant: CurrentTenant, session: SessionDep):
    """Brand configuration the website and mobile app load at start-up (name, colours, logo, enabled features).
    No login needed. The brand comes from the domain or the `X-Tenant` header."""
    row = await _tenant(session, tenant.id)
    return PublicTenant(
        id=row.id, slug=row.slug, name=row.name, type=row.type, channel=tenant.channel,
        branding=row.branding, features=row.features,
    )


@public.get("", response_model=Page[TenantOut], dependencies=[staff_only()])
async def list_tenants(session: SessionDep, page: PageDep, type: str | None = None):
    stmt = select(Tenant).order_by(Tenant.created_at.desc())
    if type:
        stmt = stmt.where(Tenant.type == type)
    return await paginate(session, stmt, page)


@public.post("", response_model=TenantOut, status_code=201, dependencies=[require_roles(GJ_ADMIN, GJ_OPS)])
async def create_tenant(body: TenantCreate, request: Request, session: SessionDep):
    """GJ ops onboards a clinic group onto white label (agreement and plan already signed)."""
    if await session.scalar(select(Tenant.id).where(Tenant.slug == body.slug)):
        raise Conflict("Slug already in use", code="slug_taken")
    branding = {**DEFAULT_BRANDING, "app_name": body.name, **(body.branding.model_dump(exclude_none=True) if body.branding else {})}
    tenant = Tenant(
        slug=body.slug, name=body.name, type="white_label", status="onboarding",
        plan_code=body.plan_code, clinic_group_partner_id=body.clinic_group_partner_id, branding=branding,
    )
    session.add(tenant)
    await session.flush()
    # Default sub-domain on the platform until the custom domain is verified.
    session.add(TenantDomain(tenant_id=tenant.id, domain=f"{body.slug}.genejunction.com", verified=True,
                             verification_token=secrets.token_hex(16), is_primary=True))
    identity = request.app.state.identity
    admin = await identity.post("/internal/users/lookup", json={"email": body.admin_email, "full_name": body.admin_name})
    await identity.post(f"/internal/users/{admin['id']}/roles",
                        json={"role": TENANT_ADMIN, "scope_type": "tenant", "scope_id": str(tenant.id)})
    emit(session, "tenant.created", key=tenant.id, tenant_id=tenant.id, data={
        "tenant_id": tenant.id, "slug": tenant.slug, "name": tenant.name, "plan_code": tenant.plan_code,
        "clinic_group_partner_id": tenant.clinic_group_partner_id, "admin_user_id": admin["id"],
    })
    await session.commit()
    await session.refresh(tenant, ["domains"])
    return tenant


@public.get("/{tenant_id}", response_model=TenantOut)
async def get_tenant(tenant_id: uuid.UUID, principal: CurrentUser, session: SessionDep):
    principal.ensure_tenant_admin(tenant_id)
    return await _tenant(session, tenant_id)


@public.patch("/{tenant_id}", response_model=TenantOut)
async def patch_tenant(tenant_id: uuid.UUID, body: TenantPatch, principal: CurrentUser, request: Request, session: SessionDep):
    principal.ensure_tenant_admin(tenant_id)
    tenant = await _tenant(session, tenant_id)
    if body.plan_code is not None and not principal.is_staff:
        raise ValidationFailed("Only GeneJunction can change the plan")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(tenant, field, {**getattr(tenant, field), **value} if isinstance(value, dict) else value)
    emit(session, "tenant.updated", key=tenant.id, tenant_id=tenant.id, data={"tenant_id": tenant.id, "fields": list(body.model_fields_set)})
    await session.commit()
    await invalidate_tenant_cache(request.app.state.redis, tenant.id)
    return tenant


@public.put("/{tenant_id}/branding", response_model=TenantOut)
async def update_branding(tenant_id: uuid.UUID, body: Branding, principal: CurrentUser, request: Request, session: SessionDep):
    principal.ensure_tenant_admin(tenant_id)
    tenant = await _tenant(session, tenant_id)
    tenant.branding = {**tenant.branding, **body.model_dump(exclude_none=True)}
    emit(session, "tenant.updated", key=tenant.id, tenant_id=tenant.id, data={"tenant_id": tenant.id, "fields": ["branding"]})
    await session.commit()
    await invalidate_tenant_cache(request.app.state.redis, tenant.id)
    return tenant


async def _set_status(tenant_id: uuid.UUID, status: str, request: Request, session: AsyncSession) -> Tenant:
    tenant = await _tenant(session, tenant_id)
    tenant.status = status
    emit(session, f"tenant.{status}", key=tenant.id, tenant_id=tenant.id, data={"tenant_id": tenant.id})
    await session.commit()
    await invalidate_tenant_cache(request.app.state.redis, tenant.id)
    return tenant


@public.post("/{tenant_id}/activate", response_model=TenantOut, dependencies=[require_roles(GJ_ADMIN, GJ_OPS)])
async def activate_tenant(tenant_id: uuid.UUID, request: Request, session: SessionDep):
    return await _set_status(tenant_id, "active", request, session)


@public.post("/{tenant_id}/suspend", response_model=TenantOut, dependencies=[require_roles(GJ_ADMIN, GJ_OPS)])
async def suspend_tenant(tenant_id: uuid.UUID, request: Request, session: SessionDep):
    return await _set_status(tenant_id, "suspended", request, session)


# ---------------------------------------------------------------- domains
@public.post("/{tenant_id}/domains", response_model=DomainOut, status_code=201)
async def add_domain(tenant_id: uuid.UUID, body: DomainCreate, principal: CurrentUser, session: SessionDep):
    principal.ensure_tenant_admin(tenant_id)
    await _tenant(session, tenant_id)
    if await session.scalar(select(TenantDomain.id).where(TenantDomain.domain == body.domain)):
        raise Conflict("Domain already registered", code="domain_taken")
    domain = TenantDomain(tenant_id=tenant_id, domain=body.domain, is_primary=body.is_primary,
                          verification_token=secrets.token_hex(16))
    session.add(domain)
    await session.commit()
    return domain


@public.post("/{tenant_id}/domains/{domain_id}/verify", response_model=DomainOut, dependencies=[require_roles(GJ_ADMIN, GJ_OPS)])
async def verify_domain(tenant_id: uuid.UUID, domain_id: uuid.UUID, request: Request, session: SessionDep):
    """Marks the domain verified once ops has confirmed the TXT record
    `_genejunction.<domain> = <verification_token>` and issued the CloudFront certificate."""
    domain = await session.get(TenantDomain, domain_id)
    if domain is None or domain.tenant_id != tenant_id:
        raise NotFound("Domain not found")
    domain.verified = True
    await session.commit()
    await invalidate_tenant_cache(request.app.state.redis, tenant_id)
    return domain


@public.delete("/{tenant_id}/domains/{domain_id}", status_code=204)
async def delete_domain(tenant_id: uuid.UUID, domain_id: uuid.UUID, principal: CurrentUser, request: Request, session: SessionDep):
    principal.ensure_tenant_admin(tenant_id)
    domain = await session.get(TenantDomain, domain_id)
    if domain is None or domain.tenant_id != tenant_id:
        raise NotFound("Domain not found")
    await session.delete(domain)
    await session.commit()
    await invalidate_tenant_cache(request.app.state.redis, tenant_id)


@public.post("/{tenant_id}/admins", status_code=201, response_model=RoleGranted)
async def invite_admin(tenant_id: uuid.UUID, body: AdminInvite, principal: CurrentUser, request: Request, session: SessionDep):
    principal.ensure_tenant_admin(tenant_id)
    await _tenant(session, tenant_id)
    identity = request.app.state.identity
    user = await identity.post("/internal/users/lookup", json={"email": body.email, "full_name": body.full_name})
    await identity.post(f"/internal/users/{user['id']}/roles",
                        json={"role": TENANT_ADMIN, "scope_type": "tenant", "scope_id": str(tenant_id)})
    emit(session, "tenant.admin_added", key=tenant_id, tenant_id=tenant_id, data={"tenant_id": tenant_id, "user_id": user["id"]})
    await session.commit()
    return {"user_id": user["id"], "role": TENANT_ADMIN}


# ---------------------------------------------------------------- network
@public.get("/{tenant_id}/network", response_model=Page[MemberOut])
async def list_network(tenant_id: uuid.UUID, principal: CurrentUser, session: SessionDep, page: PageDep,
                       member_type: str | None = None):
    principal.ensure_tenant_admin(tenant_id)
    stmt = select(NetworkMember).where(NetworkMember.tenant_id == tenant_id).order_by(NetworkMember.display_name)
    if member_type:
        stmt = stmt.where(NetworkMember.member_type == member_type)
    return await paginate(session, stmt, page)


@public.post("/{tenant_id}/network", response_model=MemberOut, status_code=201)
async def add_member(tenant_id: uuid.UUID, body: MemberCreate, principal: CurrentUser, request: Request, session: SessionDep):
    principal.ensure_tenant_admin(tenant_id)
    tenant = await _tenant(session, tenant_id)
    if body.member_type == "doctor":
        if not body.legacy_doctor_id:
            raise ValidationFailed("legacy_doctor_id is required for doctors (set up in the existing system)")
    else:
        if not body.partner_id:
            raise ValidationFailed("partner_id is required")
        partner = await request.app.state.partners.get(f"/internal/partners/{body.partner_id}")
        if partner["type"] != body.member_type:
            raise ValidationFailed(f"Partner is a {partner['type']}, not a {body.member_type}")
    if tenant.type == "platform":
        raise ValidationFailed("The marketplace tenant has no private network")
    member = NetworkMember(tenant_id=tenant_id, **body.model_dump())
    session.add(member)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict("Already in network") from exc
    _member_event(session, member)
    await session.commit()
    return member


@public.patch("/{tenant_id}/network/{member_id}", response_model=MemberOut)
async def patch_member(tenant_id: uuid.UUID, member_id: uuid.UUID, body: MemberPatch, principal: CurrentUser, session: SessionDep):
    principal.ensure_tenant_admin(tenant_id)
    member = await session.get(NetworkMember, member_id)
    if member is None or member.tenant_id != tenant_id:
        raise NotFound("Member not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(member, field, value)
    _member_event(session, member)
    await session.commit()
    return member


@public.delete("/{tenant_id}/network/{member_id}", status_code=204)
async def remove_member(tenant_id: uuid.UUID, member_id: uuid.UUID, principal: CurrentUser, session: SessionDep):
    principal.ensure_tenant_admin(tenant_id)
    member = await session.get(NetworkMember, member_id)
    if member is None or member.tenant_id != tenant_id:
        raise NotFound("Member not found")
    _member_event(session, member, "tenant.network.member_removed")
    await session.delete(member)
    await session.commit()


# ---------------------------------------------------------------- internal
@internal.get("/tenants/resolve")
async def internal_resolve(session: SessionDep, slug: str = "", host: str = ""):
    return await resolve_tenant(session, slug or None, host or None)


@internal.get("/tenants/{tenant_id}")
async def internal_get(tenant_id: uuid.UUID, session: SessionDep):
    return tenant_context(await _tenant(session, tenant_id))


@internal.get("/tenants/{tenant_id}/doctor-filter")
async def doctor_filter(tenant_id: uuid.UUID, session: SessionDep):
    """Which legacy doctors a tenant's patients may see.
    White label: only its own network. Marketplace: everyone except doctors kept private by a clinic group."""
    tenant = await _tenant(session, tenant_id)
    if tenant.type == "white_label":
        ids = await session.scalars(select(NetworkMember.legacy_doctor_id).where(
            NetworkMember.tenant_id == tenant_id, NetworkMember.member_type == "doctor", NetworkMember.status == "active"))
        return {"mode": "include", "doctor_ids": sorted(ids)}
    listed = select(NetworkMember.legacy_doctor_id).where(
        NetworkMember.member_type == "doctor", NetworkMember.listed_on_marketplace.is_(True),
        NetworkMember.status == "active", NetworkMember.legacy_doctor_id.is_not(None))
    private = await session.scalars(select(NetworkMember.legacy_doctor_id).where(
        NetworkMember.member_type == "doctor", NetworkMember.legacy_doctor_id.not_in(listed)))
    return {"mode": "exclude", "doctor_ids": sorted(set(private))}


@internal.get("/partners/{partner_id}/memberships")
async def partner_memberships(partner_id: uuid.UUID, session: SessionDep):
    rows = (await session.scalars(select(NetworkMember).where(NetworkMember.partner_id == partner_id))).all()
    return [MemberOut.model_validate(r).model_dump(mode="json") for r in rows]


@internal.get("/tenants")
async def internal_list(session: SessionDep, type: str | None = Query(None)):
    stmt = select(Tenant).where(or_(Tenant.status == "active", Tenant.status == "onboarding"))
    if type:
        stmt = stmt.where(Tenant.type == type)
    return [tenant_context(t) for t in (await session.scalars(stmt)).all()]
