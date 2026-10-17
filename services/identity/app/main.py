import uuid

from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api import router
from app.config import settings
from app.models import RoleAssignment, User
from app.tokens import keystore
from gj_common.app_factory import ServiceSpec, create_service_app
from gj_common.auth import GJ_ADMIN
from gj_common.events import EventEnvelope


async def load_keys(app: FastAPI) -> None:
    await keystore.load(app.state.db)
    app.state.jwks.set_local_keys(keystore.jwks)


async def bootstrap_admin(app: FastAPI) -> None:
    if not (settings.bootstrap_admin_email and settings.bootstrap_admin_password):
        return
    email = settings.bootstrap_admin_email.lower()
    async with app.state.db.sessionmaker() as session, session.begin():
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(email=email, full_name="GeneJunction Admin", password_hash=PasswordHasher().hash(settings.bootstrap_admin_password))
            session.add(user)
            await session.flush()
        await session.execute(
            pg_insert(RoleAssignment)
            .values(user_id=user.id, role=GJ_ADMIN, scope_type="platform", scope_id=None)
            .on_conflict_do_nothing()
        )


async def on_patient_linked(event: EventEnvelope, session) -> None:
    user = await session.get(User, uuid.UUID(event.data["user_id"]))
    if user is not None:
        user.legacy_patient_id = event.data["legacy_patient_id"]


app = create_service_app(
    ServiceSpec(
        settings=settings,
        title="GeneJunction Identity Service",
        description="Phone-OTP and password login, JWT/JWKS issuing, roles and tenant membership.",
        routers=[router],
        on_startup=[load_keys, bootstrap_admin],
        event_handlers={"legacy.patient.linked": on_patient_linked},
    )
)
