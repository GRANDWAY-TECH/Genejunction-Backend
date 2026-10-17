"""DB-backed tests run against a throwaway database on the compose Postgres (skipped if unreachable)."""

import os
from decimal import Decimal
from urllib.parse import urlparse

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models
from gj_common.db import Base

URL = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://gj:gj@127.0.0.1:55432/gj_billing_test")


async def _ensure_database() -> None:
    u = urlparse(URL.replace("+asyncpg", ""))
    try:
        conn = await asyncpg.connect(user=u.username, password=u.password, host=u.hostname, port=u.port,
                                     database="postgres", timeout=3)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Postgres not reachable for DB tests: {exc}")
    db = u.path.lstrip("/")
    if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db):
        await conn.execute(f'CREATE DATABASE "{db}"')
    await conn.close()


@pytest.fixture
async def session():
    await _ensure_database()
    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        s.add_all([
            models.RateCardItem(action_code="ai_report_summary", name="AI summary", unit="report",
                                credits_per_unit=Decimal("10"), models=["B2C"], charged_to="Lab"),
            models.RateCardItem(action_code="alert_message", name="Alert", unit="message",
                                credits_per_unit=Decimal("0.2"), models=["B2B"], charged_to="Any"),
            models.Plan(code="wl", name="White label", audience="white_label", monthly_fee=Decimal("2500"),
                        setup_fee=Decimal("15000"), transaction_fee_pct=Decimal("2.00")),
        ])
        await s.commit()
        yield s
    await engine.dispose()
