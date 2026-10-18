import asyncio
from datetime import datetime, timedelta, timezone
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal

from fastapi import APIRouter, FastAPI, encoders
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from redis.asyncio import Redis
from sqlalchemy import delete, func, select

from gj_common.auth import InternalOnly, JWKSCache
from gj_common.db import Database
from gj_common.errors import ERROR_RESPONSES, install_error_handlers
from gj_common.events import OutboxEvent, ProcessedEvent, set_event_source, topic_for
from gj_common.events.kafka import EventBus, EventConsumer, Handler, OutboxRelay
from gj_common.http import ServiceClient
from gj_common.jobs import PeriodicJob
from gj_common.logging import RequestContextMiddleware, configure_logging, get_logger
from gj_common.settings import ServiceSettings
from gj_common.tenancy import TenantResolver

log = get_logger(__name__)

# Money is Decimal end to end; serialise it as an exact string ("38.00"), never a float.
encoders.ENCODERS_BY_TYPE[Decimal] = str

Hook = Callable[[FastAPI], Awaitable[None]]


@dataclass
class ServiceSpec:
    settings: ServiceSettings
    title: str
    description: str = ""
    routers: list[APIRouter] = field(default_factory=list)
    event_handlers: dict[str, Handler] = field(default_factory=dict)
    jobs: list[PeriodicJob] = field(default_factory=list)
    on_startup: list[Hook] = field(default_factory=list)
    on_shutdown: list[Hook] = field(default_factory=list)
    uses_postgres: bool = True


async def _event_housekeeping(app: FastAPI) -> None:
    """Dedupe rows are kept 30 days (far beyond Kafka redelivery); published outbox rows 7 days."""
    now = datetime.now(timezone.utc)
    async with app.state.db.sessionmaker() as session, session.begin():
        await session.execute(delete(ProcessedEvent).where(ProcessedEvent.processed_at < now - timedelta(days=30)))
        await session.execute(delete(OutboxEvent).where(OutboxEvent.published_at < now - timedelta(days=7)))


def create_service_app(spec: ServiceSpec) -> FastAPI:
    settings = spec.settings
    configure_logging(settings.service_name, settings.log_level)
    set_event_source(settings.service_name)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
        app.state.db = None
        if spec.uses_postgres and settings.database_url:
            app.state.db = Database(
                settings.database_url, pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow
            )
        app.state.bus = EventBus(settings.kafka_bootstrap_servers)
        app.state.jwks = JWKSCache(settings.jwks_url)
        app.state.tenant_client = ServiceClient(settings.tenant_service_url, settings.internal_api_key)
        app.state.tenant_resolver = TenantResolver(
            app.state.tenant_client, app.state.redis, settings.default_tenant_slug
        )

        for hook in spec.on_startup:
            await hook(app)

        tasks: list[asyncio.Task] = []
        if settings.kafka_enabled:
            if app.state.db is not None and settings.run_outbox_relay:
                tasks.append(asyncio.create_task(OutboxRelay(app.state.db, app.state.bus).run(), name="outbox"))
            if spec.event_handlers and settings.run_consumers:
                consumer = EventConsumer(
                    group_id=f"{settings.service_name}-service",
                    bootstrap_servers=settings.kafka_bootstrap_servers,
                    handlers=spec.event_handlers,
                    db=app.state.db,
                    bus=app.state.bus,
                )
                tasks.append(asyncio.create_task(consumer.run(), name="consumer"))
        if settings.run_jobs:
            jobs = list(spec.jobs)
            if app.state.db is not None:
                jobs.append(PeriodicJob(f"{settings.service_name}-event-housekeeping", 3600, _event_housekeeping))
            tasks.extend(asyncio.create_task(job.run_forever(app), name=job.name) for job in jobs)
        log.info("service_started", tasks=[t.get_name() for t in tasks])

        yield

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for hook in spec.on_shutdown:
            await hook(app)
        await app.state.bus.stop()
        await app.state.tenant_client.aclose()
        if app.state.db is not None:
            await app.state.db.dispose()
        await app.state.redis.aclose()

    app = FastAPI(
        title=spec.title,
        description=spec.description,
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)

    @app.get("/health", include_in_schema=False)
    async def health():
        return {"status": "ok", "service": settings.service_name}

    @app.get("/ready", include_in_schema=False)
    async def ready():
        checks = {}
        try:
            await app.state.redis.ping()
            checks["redis"] = "ok"
        except Exception as exc:
            checks["redis"] = str(exc)
        if app.state.db is not None:
            try:
                await app.state.db.ping()
                checks["postgres"] = "ok"
            except Exception as exc:
                checks["postgres"] = str(exc)
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)

    @app.get("/internal/events/status", include_in_schema=False, dependencies=[InternalOnly])
    async def events_status():
        """Event-pipeline health for scripts/event_audit.py: outbox backlog and handled counts."""
        consumer = ({"group": f"{settings.service_name}-service", "topics": sorted({topic_for(t) for t in spec.event_handlers})}
                    if spec.event_handlers else None)
        if app.state.db is None:
            return {"service": settings.service_name, "outbox": None, "consumer": consumer}
        async with app.state.db.sessionmaker() as session:
            pending, oldest = (await session.execute(
                select(func.count(), func.min(OutboxEvent.created_at)).where(OutboxEvent.published_at.is_(None))
            )).one()
            total = await session.scalar(select(func.count()).select_from(OutboxEvent))
            processed = await session.scalar(select(func.count()).select_from(ProcessedEvent))
        return {"service": settings.service_name,
                "outbox": {"pending": pending, "oldest_pending": oldest, "total": total},
                "processed_events": processed, "consumer": consumer}

    for router in spec.routers:
        app.include_router(router, responses=ERROR_RESPONSES)

    Instrumentator(excluded_handlers=["/health", "/ready", "/metrics"]).instrument(app).expose(
        app, endpoint="/metrics", include_in_schema=False
    )
    return app
