import uuid
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from fastapi import APIRouter, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Partner, PartnerDocument, PartnerUser, ReviewLog
from app.schemas import (
    BankAccount,
    DocumentOut,
    DocumentUpload,
    PartnerCreate,
    PartnerOut,
    PartnerPatch,
    ReviewDecision,
    StaffInvite,
)
from gj_common.auth import GJ_ADMIN, GJ_OPS, PARTNER_ADMIN, TENANT_ADMIN, CurrentUser, InternalOnly, require_roles, staff_only
from gj_common.db import SessionDep
from gj_common.errors import Conflict, Forbidden, NotFound, ValidationFailed
from gj_common.events import emit
from gj_common.pagination import Page, PageDep, paginate

router = APIRouter(prefix="/api/v1/partners", tags=["partners"])
internal = APIRouter(prefix="/internal", tags=["internal"], dependencies=[InternalOnly])

fernet = Fernet(settings.fernet_key)


async def _partner(session: AsyncSession, partner_id: uuid.UUID) -> Partner:
    partner = await session.get(Partner, partner_id)
    if partner is None:
        raise NotFound("Partner not found")
    return partner


def _partner_event(session: AsyncSession, event_type: str, p: Partner, **extra) -> None:
    emit(session, event_type, key=p.id, tenant_id=p.owner_tenant_id, data={
        "partner_id": p.id, "type": p.type, "display_name": p.display_name, "status": p.status,
        "owner_tenant_id": p.owner_tenant_id, "legacy_doctor_id": p.legacy_doctor_id,
        "latitude": p.latitude, "longitude": p.longitude, "service_radius_km": p.service_radius_km,
        "address": p.address, "contact_email": p.contact_email, "contact_phone": p.contact_phone, **extra,
    })


def _log(session: AsyncSession, partner: Partner, action: str, actor: uuid.UUID, note: str | None = None) -> None:
    session.add(ReviewLog(partner_id=partner.id, action=action, actor_id=actor, note=note))


@router.post("", response_model=PartnerOut, status_code=201)
async def register_partner(body: PartnerCreate, principal: CurrentUser, request: Request, session: SessionDep):
    """Any signed-in business user can register a partner; they become its partner_admin.
    Refresh the access token afterwards to receive the new partner_ids claim."""
    if body.owner_tenant_id:
        principal.ensure_tenant_admin(body.owner_tenant_id)
        if body.type in ("clinic_group",):
            raise ValidationFailed("A clinic group cannot be owned by a tenant")
    if body.type == "doctor" and not body.legacy_doctor_id:
        raise ValidationFailed("Doctors must reference their consultant record in the existing system")
    duplicate = await session.scalar(select(Partner.id).where(
        Partner.registration_no == body.registration_no, Partner.type == body.type))
    if duplicate:
        raise Conflict("A partner with this registration number already exists", code="duplicate_partner")
    partner = Partner(**body.model_dump(exclude={"address"}), address=body.address.model_dump(), created_by=principal.user_id)
    session.add(partner)
    await session.flush()
    session.add(PartnerUser(partner_id=partner.id, user_id=principal.user_id, role=PARTNER_ADMIN))
    _log(session, partner, "registered", principal.user_id)
    _partner_event(session, "partner.registered", partner)
    await request.app.state.identity.post(
        f"/internal/users/{principal.user_id}/roles",
        json={"role": PARTNER_ADMIN, "scope_type": "partner", "scope_id": str(partner.id)},
    )
    await session.commit()
    return partner


@router.get("/mine", response_model=list[PartnerOut])
async def my_partners(principal: CurrentUser, session: SessionDep):
    stmt = select(Partner).join(PartnerUser).where(PartnerUser.user_id == principal.user_id)
    return (await session.scalars(stmt)).all()


@router.get("", response_model=Page[PartnerOut], dependencies=[staff_only()])
async def list_partners(session: SessionDep, page: PageDep, status: str | None = None, type: str | None = None,
                        q: str | None = None):
    stmt = select(Partner).order_by(Partner.created_at.desc())
    if status:
        stmt = stmt.where(Partner.status == status)
    if type:
        stmt = stmt.where(Partner.type == type)
    if q:
        stmt = stmt.where(Partner.display_name.ilike(f"%{q}%") | Partner.registration_no.ilike(f"%{q}%"))
    return await paginate(session, stmt, page)


@router.get("/{partner_id}", response_model=PartnerOut)
async def get_partner(partner_id: uuid.UUID, principal: CurrentUser, session: SessionDep):
    partner = await _partner(session, partner_id)
    if not (principal.can_act_for_partner(partner_id) or
            (partner.owner_tenant_id and TENANT_ADMIN in principal.roles and principal.tenant_id == partner.owner_tenant_id)):
        raise Forbidden()
    return partner


@router.patch("/{partner_id}", response_model=PartnerOut)
async def update_partner(partner_id: uuid.UUID, body: PartnerPatch, principal: CurrentUser, session: SessionDep):
    principal.ensure_partner(partner_id)
    partner = await _partner(session, partner_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(partner, field, value)
    _partner_event(session, "partner.updated", partner)
    await session.commit()
    return partner


@router.put("/{partner_id}/bank-account", response_model=PartnerOut)
async def set_bank_account(partner_id: uuid.UUID, body: BankAccount, principal: CurrentUser, session: SessionDep):
    principal.ensure_partner_admin(partner_id)
    partner = await _partner(session, partner_id)
    partner.bank_name = body.bank_name
    partner.bank_account_name = body.account_name
    partner.bank_account_no_enc = fernet.encrypt(body.account_no.encode()).decode()
    partner.bank_account_last4 = body.account_no[-4:]
    _log(session, partner, "bank_account_changed", principal.user_id)
    await session.commit()
    return partner


@router.post("/{partner_id}/documents", status_code=201)
async def create_document_upload(partner_id: uuid.UUID, body: DocumentUpload, principal: CurrentUser,
                                 request: Request, session: SessionDep):
    principal.ensure_partner(partner_id)
    await _partner(session, partner_id)
    doc_id = uuid.uuid4()
    key = f"partners/{partner_id}/documents/{doc_id}/{body.file_name}"
    doc = PartnerDocument(id=doc_id, partner_id=partner_id, doc_type=body.doc_type, file_name=body.file_name,
                          content_type=body.content_type, s3_key=key)
    session.add(doc)
    await session.commit()
    upload_url = request.app.state.storage.presign_upload(key, body.content_type)
    return {"document_id": doc_id, "upload_url": upload_url, "method": "PUT", "headers": {"Content-Type": body.content_type}}


@router.get("/{partner_id}/documents", response_model=list[DocumentOut])
async def list_documents(partner_id: uuid.UUID, principal: CurrentUser, request: Request, session: SessionDep):
    principal.ensure_partner(partner_id)
    docs = (await session.scalars(select(PartnerDocument).where(PartnerDocument.partner_id == partner_id)
                                  .order_by(PartnerDocument.created_at))).all()
    storage = request.app.state.storage
    return [DocumentOut.model_validate(d).model_copy(update={"download_url": storage.presign_download(d.s3_key)}) for d in docs]


@router.post("/{partner_id}/submit", response_model=PartnerOut)
async def submit_for_review(partner_id: uuid.UUID, principal: CurrentUser, session: SessionDep):
    principal.ensure_partner(partner_id)
    partner = await _partner(session, partner_id)
    if partner.status not in ("pending_documents", "rejected"):
        raise Conflict(f"Cannot submit from status {partner.status}")
    docs = await session.scalar(select(func.count()).select_from(PartnerDocument).where(PartnerDocument.partner_id == partner_id))
    if not docs:
        raise ValidationFailed("Upload at least one licence or registration document first")
    if partner.type != "clinic_group" and not partner.bank_account_no_enc:
        raise ValidationFailed("Add bank details for payouts first")
    partner.status = "under_review"
    partner.rejection_reason = None
    _log(session, partner, "submitted", principal.user_id)
    _partner_event(session, "partner.submitted", partner)
    await session.commit()
    return partner


@router.post("/{partner_id}/approve", response_model=PartnerOut, dependencies=[require_roles(GJ_ADMIN, GJ_OPS)])
async def approve(partner_id: uuid.UUID, body: ReviewDecision, principal: CurrentUser, session: SessionDep):
    partner = await _partner(session, partner_id)
    if partner.status != "under_review":
        raise Conflict(f"Cannot approve from status {partner.status}")
    partner.status = "verified"
    partner.verified_at = datetime.now(timezone.utc)
    partner.verified_by = principal.user_id
    for doc in (await session.scalars(select(PartnerDocument).where(PartnerDocument.partner_id == partner_id))).all():
        doc.status = "accepted"
    _log(session, partner, "approved", principal.user_id, body.note)
    _partner_event(session, "partner.verified", partner)
    await session.commit()
    return partner


@router.post("/{partner_id}/reject", response_model=PartnerOut, dependencies=[require_roles(GJ_ADMIN, GJ_OPS)])
async def reject(partner_id: uuid.UUID, body: ReviewDecision, principal: CurrentUser, session: SessionDep):
    partner = await _partner(session, partner_id)
    if partner.status != "under_review":
        raise Conflict(f"Cannot reject from status {partner.status}")
    if not body.note:
        raise ValidationFailed("A reason is required")
    partner.status = "rejected"
    partner.rejection_reason = body.note
    _log(session, partner, "rejected", principal.user_id, body.note)
    _partner_event(session, "partner.rejected", partner, reason=body.note)
    await session.commit()
    return partner


@router.post("/{partner_id}/suspend", response_model=PartnerOut, dependencies=[require_roles(GJ_ADMIN, GJ_OPS)])
async def suspend(partner_id: uuid.UUID, body: ReviewDecision, principal: CurrentUser, session: SessionDep):
    partner = await _partner(session, partner_id)
    partner.status = "suspended"
    _log(session, partner, "suspended", principal.user_id, body.note)
    _partner_event(session, "partner.suspended", partner, reason=body.note)
    await session.commit()
    return partner


@router.get("/{partner_id}/review-log", dependencies=[staff_only()])
async def review_log(partner_id: uuid.UUID, session: SessionDep):
    rows = (await session.scalars(select(ReviewLog).where(ReviewLog.partner_id == partner_id).order_by(ReviewLog.created_at))).all()
    return [{"action": r.action, "actor_id": r.actor_id, "note": r.note, "at": r.created_at} for r in rows]


@router.post("/{partner_id}/staff", status_code=201)
async def invite_staff(partner_id: uuid.UUID, body: StaffInvite, principal: CurrentUser, request: Request, session: SessionDep):
    principal.ensure_partner_admin(partner_id)
    await _partner(session, partner_id)
    identity = request.app.state.identity
    user = await identity.post("/internal/users/lookup", json={"email": body.email, "full_name": body.full_name})
    await identity.post(f"/internal/users/{user['id']}/roles",
                        json={"role": body.role, "scope_type": "partner", "scope_id": str(partner_id)})
    existing = await session.get(PartnerUser, (partner_id, uuid.UUID(user["id"])))
    if existing is None:
        session.add(PartnerUser(partner_id=partner_id, user_id=uuid.UUID(user["id"]), role=body.role))
    await session.commit()
    return {"user_id": user["id"], "role": body.role}


# ---------------------------------------------------------------- internal
@internal.get("/partners/{partner_id}")
async def internal_get(partner_id: uuid.UUID, session: SessionDep):
    return PartnerOut.model_validate(await _partner(session, partner_id)).model_dump(mode="json")


@internal.post("/partners/batch")
async def internal_batch(ids: list[uuid.UUID], session: SessionDep):
    rows = (await session.scalars(select(Partner).where(Partner.id.in_(ids[:500])))).all()
    return [PartnerOut.model_validate(r).model_dump(mode="json") for r in rows]


@internal.get("/partners/{partner_id}/payout-account")
async def payout_account(partner_id: uuid.UUID, session: SessionDep):
    partner = await _partner(session, partner_id)
    if not partner.bank_account_no_enc:
        raise NotFound("No bank account on file")
    return {
        "partner_id": partner.id,
        "bank_name": partner.bank_name,
        "account_name": partner.bank_account_name,
        "account_no": fernet.decrypt(partner.bank_account_no_enc.encode()).decode(),
    }
