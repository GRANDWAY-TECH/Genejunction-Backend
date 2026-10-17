import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from gj_common.db import Database
from gj_common.events.envelope import EventEnvelope, topic_for
from gj_common.events.models import OutboxEvent, ProcessedEvent
from gj_common.logging import get_logger

log = get_logger(__name__)

Handler = Callable[[EventEnvelope, AsyncSession | None], Awaitable[None]]


class EventBus:
    """Thin Kafka producer wrapper. Starts lazily and retries until the broker is reachable."""

    def __init__(self, bootstrap_servers: str) -> None:
        self.bootstrap_servers = bootstrap_servers
        self._producer: AIOKafkaProducer | None = None
        self._lock = asyncio.Lock()

    async def _ensure_started(self) -> AIOKafkaProducer:
        if self._producer is not None:
            return self._producer
        async with self._lock:
            if self._producer is None:
                delay = 1.0
                while True:
                    producer = AIOKafkaProducer(
                        bootstrap_servers=self.bootstrap_servers,
                        acks="all",
                        enable_idempotence=True,
                        linger_ms=5,
                        value_serializer=lambda v: json.dumps(v).encode(),
                        key_serializer=lambda k: k.encode(),
                    )
                    try:
                        await producer.start()
                        self._producer = producer
                        log.info("kafka_producer_started")
                        break
                    except Exception as exc:  # broker not up yet
                        await producer.stop()
                        log.warning("kafka_producer_retry", error=str(exc), delay=delay)
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 15)
        return self._producer

    async def publish(self, topic: str, key: str, payload: dict) -> None:
        producer = await self._ensure_started()
        await producer.send_and_wait(topic, payload, key=key)

    async def publish_many(self, messages: list[tuple[str, str, dict]]) -> None:
        producer = await self._ensure_started()
        futures = [await producer.send(topic, payload, key=key) for topic, key, payload in messages]
        await asyncio.gather(*futures)

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None


class OutboxRelay:
    """Moves committed outbox rows to Kafka. Safe to run on many replicas (SKIP LOCKED)."""

    def __init__(self, db: Database, bus: EventBus, *, batch_size: int = 200, idle_sleep: float = 0.25) -> None:
        self.db = db
        self.bus = bus
        self.batch_size = batch_size
        self.idle_sleep = idle_sleep

    async def drain_once(self) -> int:
        async with self.db.sessionmaker() as session, session.begin():
            rows = (
                await session.scalars(
                    select(OutboxEvent)
                    .where(OutboxEvent.published_at.is_(None))
                    .order_by(OutboxEvent.created_at)
                    .limit(self.batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            if not rows:
                return 0
            await self.bus.publish_many([(r.topic, r.key, r.payload) for r in rows])
            now = datetime.now(timezone.utc)
            for row in rows:
                row.published_at = now
            return len(rows)

    async def run(self) -> None:
        while True:
            try:
                published = await self.drain_once()
                if published == 0:
                    await asyncio.sleep(self.idle_sleep)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("outbox_relay_error", error=str(exc))
                await asyncio.sleep(2)


class EventConsumer:
    """Consumes the topics needed by `handlers` (keyed by event type).

    With a database, every handler runs inside one transaction together with a
    ProcessedEvent insert, so redelivered events are skipped. Without a database
    (Mongo-backed services) handlers must be idempotent on their own.
    Messages that keep failing are sent to `<topic>.dlq`.
    """

    def __init__(
        self,
        *,
        group_id: str,
        bootstrap_servers: str,
        handlers: dict[str, Handler],
        db: Database | None,
        bus: EventBus,
        max_attempts: int = 5,
    ) -> None:
        self.group_id = group_id
        self.bootstrap_servers = bootstrap_servers
        self.handlers = handlers
        self.db = db
        self.bus = bus
        self.max_attempts = max_attempts
        self.topics = sorted({topic_for(t) for t in handlers})

    async def _start(self) -> AIOKafkaConsumer:
        delay = 1.0
        while True:
            consumer = AIOKafkaConsumer(
                *self.topics,
                bootstrap_servers=self.bootstrap_servers,
                group_id=self.group_id,
                enable_auto_commit=False,
                auto_offset_reset="earliest",
                value_deserializer=lambda v: json.loads(v.decode()),
            )
            try:
                await consumer.start()
                log.info("kafka_consumer_started", topics=self.topics, group=self.group_id)
                return consumer
            except Exception as exc:
                await consumer.stop()
                log.warning("kafka_consumer_retry", error=str(exc), delay=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15)

    async def _dispatch(self, envelope: EventEnvelope, handler: Handler) -> None:
        if self.db is None:
            await handler(envelope, None)
            return
        async with self.db.sessionmaker() as session, session.begin():
            inserted = await session.execute(
                pg_insert(ProcessedEvent)
                .values(event_id=envelope.id, consumer=self.group_id)
                .on_conflict_do_nothing()
                .returning(ProcessedEvent.event_id)
            )
            if inserted.first() is None:
                return  # already handled
            await handler(envelope, session)

    async def handle_message(self, topic: str, value: dict) -> None:
        envelope = EventEnvelope.model_validate(value)
        handler = self.handlers.get(envelope.type)
        if handler is None:
            return
        for attempt in range(1, self.max_attempts + 1):
            try:
                await self._dispatch(envelope, handler)
                return
            except Exception as exc:
                log.warning("event_handler_failed", type=envelope.type, id=str(envelope.id), attempt=attempt, error=str(exc))
                await asyncio.sleep(min(0.5 * 2**attempt, 10))
        log.error("event_sent_to_dlq", type=envelope.type, id=str(envelope.id))
        await self.bus.publish(f"{topic}.dlq", envelope.key, value)

    async def run(self) -> None:
        consumer = await self._start()
        try:
            async for message in consumer:
                try:
                    await self.handle_message(message.topic, message.value)
                except Exception as exc:  # malformed envelope etc.
                    log.error("event_unprocessable", error=str(exc), topic=message.topic)
                await consumer.commit()
        finally:
            await consumer.stop()
