"""Event-pipeline metrics, exported on every service's /metrics."""

from prometheus_client import Counter, Gauge

EVENTS_PROCESSED = Counter("gj_events_processed_total", "Events handled successfully", ["consumer", "type"])
EVENTS_RETRIED = Counter("gj_events_retried_total", "Handler attempts that will be retried",
                         ["consumer", "type", "transient"])
EVENTS_DEAD_LETTERED = Counter("gj_events_dead_lettered_total", "Events parked in a DLQ", ["consumer", "type"])
CONSUMER_BLOCKED = Gauge("gj_consumer_blocked", "1 while the consumer is retrying an event", ["consumer"])
OUTBOX_PENDING = Gauge("gj_outbox_pending", "Committed events not yet published to Kafka")
OUTBOX_OLDEST_SECONDS = Gauge("gj_outbox_oldest_pending_seconds", "Age of the oldest unpublished event")
