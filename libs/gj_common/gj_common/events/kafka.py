import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from gj_common.db import Database
from gj_common.events.envelope import EventEnvelope, topic_for
from gj_common.events.models import OutboxEvent, ProcessedEvent
from gj_common.events.retry import is_transient
from gj_common.logging import get_logger
from gj_common.metrics import (
    CONSUMER_BLOCKED,
    EVENTS_DEAD_LETTERED,
    EVENTS_PROCESSED,
    EVENTS_RETRIED,
    OUTBOX_OLDEST_SECONDS,
    OUTBOX_PENDING,
)

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
    """Moves committed outbox rows to Kafka, oldest first.

    Only one replica publishes at a time (a Postgres advisory lock per batch), so events for the same
    order or partner reach Kafka in the order they were committed. Rows are marked published only after
    Kafka acknowledged them (acks=all); a crash in between re-sends them, and consumers drop the duplicates.
    """

    LOCK_ID = 727_001

    def __init__(self, db: Database, bus: EventBus, *, batch_size: int = 200, idle_sleep: float = 0.25) -> None:
        self.db = db
        self.bus = bus
        self.batch_size = batch_size
        self.idle_sleep = idle_sleep

    async def drain_once(self) -> int:
        async with self.db.sessionmaker() as session, session.begin():
            if not await session.scalar(select(func.pg_try_advisory_xact_lock(self.LOCK_ID))):
                return 0  # another replica is publishing
            rows = (
                await session.scalars(
                    select(OutboxEvent)
                    .where(OutboxEvent.published_at.is_(None))
                    .order_by(OutboxEvent.created_at, OutboxEvent.id)
                    .limit(self.batch_size)
                )
            ).all()
            if not rows:
                return 0
            await self.bus.publish_many([(r.topic, r.key, r.payload) for r in rows])
            now = datetime.now(timezone.utc)
            for row in rows:
                row.published_at = now
            return len(rows)

    async def update_gauges(self) -> None:
        async with self.db.sessionmaker() as session:
            pending, oldest = (await session.execute(
                select(func.count(), func.min(OutboxEvent.created_at)).where(OutboxEvent.published_at.is_(None))
            )).one()
        OUTBOX_PENDING.set(pending)
        OUTBOX_OLDEST_SECONDS.set((datetime.now(timezone.utc) - oldest).total_seconds() if oldest else 0)

    async def run(self) -> None:
        loops = 0
        while True:
            try:
                published = await self.drain_once()
                loops += 1
                if published == 0 or loops % 20 == 0:
                    await self.update_gauges()
                if published == 0:
                    await asyncio.sleep(self.idle_sleep)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # Kafka or Postgres down: nothing is lost, rows stay unpublished
                log.warning("outbox_relay_error", error=str(exc))
                await asyncio.sleep(2)


class EventConsumer:
    """Consumes the topics needed by `handlers` (keyed by event type), with at-least-once delivery and
    exactly-once effect.

    * With a database, each handler runs in one transaction together with a `processed_events` insert,
      so a redelivered event is skipped. Without one (Mongo-backed catalog) handlers are idempotent.
    * A **transient** failure (a service, database or broker unreachable, HTTP 5xx, `RetryLater`) is
      retried with capped backoff for as long as it takes: the offset is not committed, so the event
      is never skipped and later events of the same partition wait behind it (order is kept).
    * A **permanent** failure (a bug, invalid data) is retried `max_attempts` times, then parked in
      `<topic>.dlq` with the error, and the partition moves on. `scripts/replay_dlq.py` re-publishes it.
    * Offsets are committed only after the handler succeeded or the event was safely parked.
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
        max_backoff: float = 30.0,
    ) -> None:
        self.group_id = group_id
        self.bootstrap_servers = bootstrap_servers
        self.handlers = handlers
        self.db = db
        self.bus = bus
        self.max_attempts = max_attempts
        self.max_backoff = max_backoff
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
                max_poll_interval_ms=600_000,
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

    async def _dead_letter(self, topic: str, key: str, value: object, error: str, partition: int, offset: int) -> None:
        await self.bus.publish(f"{topic}.dlq", key or "unknown", {
            "consumer": self.group_id, "source_topic": topic, "partition": partition, "offset": offset,
            "error": error[:2000], "failed_at": datetime.now(timezone.utc).isoformat(), "envelope": value,
        })

    async def process(self, topic: str, partition: int, offset: int, key: str, raw: bytes, attempt: int) -> str:
        """Returns "done" (commit and move on) or "retry" (seek back and try again later)."""
        try:
            value = json.loads(raw)
            envelope = EventEnvelope.model_validate(value)
        except Exception as exc:
            log.error("event_unparseable", topic=topic, offset=offset, error=str(exc))
            await self._dead_letter(topic, key, raw.decode(errors="replace"), f"unparseable: {exc}", partition, offset)
            return "done"
        handler = self.handlers.get(envelope.type)
        if handler is None:
            return "done"
        try:
            await self._dispatch(envelope, handler)
        except Exception as exc:
            transient = is_transient(exc)
            EVENTS_RETRIED.labels(self.group_id, envelope.type, str(transient).lower()).inc()
            log.warning("event_handler_failed", type=envelope.type, id=str(envelope.id), attempt=attempt,
                        transient=transient, error=f"{type(exc).__name__}: {exc}")
            if transient or attempt < self.max_attempts:
                return "retry"
            log.error("event_dead_lettered", type=envelope.type, id=str(envelope.id))
            await self._dead_letter(topic, envelope.key, value, f"{type(exc).__name__}: {exc}", partition, offset)
            EVENTS_DEAD_LETTERED.labels(self.group_id, envelope.type).inc()
            return "done"
        EVENTS_PROCESSED.labels(self.group_id, envelope.type).inc()
        return "done"

    async def run(self) -> None:
        """Runs forever: if the broker connection breaks, the consumer is rebuilt and resumes from the last
        committed offset (anything not committed is redelivered and de-duplicated)."""
        while True:
            try:
                await self._consume()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("kafka_consumer_restart", error=f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(3)

    async def _consume(self) -> None:
        consumer = await self._start()
        attempts: dict[tuple[str, int, int], int] = {}
        blocked_at: dict[TopicPartition, int] = {}  # partition -> offset of the event being retried
        blocked = CONSUMER_BLOCKED.labels(self.group_id)
        try:
            while True:
                msg = await consumer.getone()
                tp = TopicPartition(msg.topic, msg.partition)
                if tp in blocked_at and msg.offset > blocked_at[tp]:
                    consumer.seek(tp, blocked_at[tp])  # never run past an event that has not succeeded
                    continue
                slot = (msg.topic, msg.partition, msg.offset)
                attempt = attempts.get(slot, 0) + 1
                try:
                    outcome = await self.process(msg.topic, msg.partition, msg.offset,
                                                 (msg.key or b"").decode(), msg.value, attempt)
                except Exception as exc:  # e.g. the DLQ publish itself failed: try again later
                    log.warning("event_processing_error", error=str(exc))
                    outcome = "retry"
                if outcome == "retry":
                    attempts[slot] = attempt
                    blocked_at[tp] = msg.offset
                    blocked.set(1)
                    consumer.seek(tp, msg.offset)  # re-read this event; nothing after it in the partition is skipped
                    await asyncio.sleep(min(0.5 * 2 ** min(attempt, 8), self.max_backoff))
                    continue
                attempts.pop(slot, None)
                blocked_at.pop(tp, None)
                blocked.set(1 if blocked_at else 0)
                await consumer.commit({tp: msg.offset + 1})
        finally:
            await consumer.stop()
