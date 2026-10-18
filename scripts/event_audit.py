"""Is every event delivered? Checks each place an event can be waiting.

    python scripts/event_audit.py            # one report; exit 1 if anything is pending
    python scripts/event_audit.py --wait 120 # poll until everything has drained (or time out)

1. Outbox: committed events not yet published to Kafka (each Postgres service + the catalog's Mongo outbox).
2. Consumer lag: events on Kafka that a service has not processed yet (per consumer group, per topic).
3. Dead-letter queues: events parked after a permanent failure (fix the cause, then scripts/replay_dlq.py).
"""

import argparse
import asyncio
import os
import sys
import time

import httpx
from aiokafka import AIOKafkaConsumer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient
from pymongo import MongoClient

KAFKA = os.environ.get("KAFKA", "localhost:19092")
MONGO = os.environ.get("MONGO", "mongodb://localhost:27017")
KEY = os.environ.get("INTERNAL_API_KEY", "dev-internal-key-change-me")
SERVICES = {name: 8100 + i for i, name in enumerate(
    ["identity", "tenant", "partner", "catalog", "order", "payment", "billing", "pharmacy", "lab", "wholesale",
     "notification", "legacy-bridge"], start=1)}


def service_status() -> tuple[dict, list[str]]:
    status, errors = {}, []
    with httpx.Client(timeout=5, headers={"x-internal-key": KEY}) as http:
        for name, port in SERVICES.items():
            try:
                r = http.get(f"http://localhost:{port}/internal/events/status")
                r.raise_for_status()
                status[name] = r.json()
            except Exception as exc:
                errors.append(f"{name}: unreachable ({type(exc).__name__})")
    return status, errors


async def kafka_state(groups: dict[str, list[str]]) -> tuple[dict, dict]:
    consumer = AIOKafkaConsumer(bootstrap_servers=KAFKA)
    admin = AIOKafkaAdminClient(bootstrap_servers=KAFKA)
    await consumer.start()
    await admin.start()
    try:
        topics = await consumer.topics()
        parts = {t: [TopicPartition(t, p) for p in (consumer.partitions_for_topic(t) or [])] for t in topics}
        every = [tp for tps in parts.values() for tp in tps]
        end = await consumer.end_offsets(every)
        start = await consumer.beginning_offsets(every)
        lag: dict[str, dict[str, int]] = {}
        for group, group_topics in groups.items():
            committed = await admin.list_consumer_group_offsets(group)
            for topic in group_topics:
                total = 0
                for tp in parts.get(topic, []):
                    meta = committed.get(tp)
                    position = meta.offset if meta and meta.offset >= 0 else start[tp]
                    total += max(end[tp] - position, 0)
                lag.setdefault(group, {})[topic] = total
        dlq = {t: sum(end[tp] - start[tp] for tp in parts[t]) for t in topics if t.endswith(".dlq")}
        return lag, dlq
    finally:
        await consumer.stop()
        await admin.close()


def catalog_outbox() -> int:
    with MongoClient(MONGO, serverSelectionTimeoutMS=3000) as client:
        return client["gj_catalog"]["outbox"].count_documents({"published": False})


async def audit() -> tuple[bool, list[str]]:
    lines, ok = [], True
    status, errors = service_status()
    for e in errors:
        lines.append(f"  !! {e}")
        ok = False
    lines.append("Outbox (committed, not yet on Kafka)")
    for name, s in status.items():
        if s.get("outbox"):
            pending = s["outbox"]["pending"]
            ok &= pending == 0
            lines.append(f"  {'ok' if pending == 0 else '..'} {name:14} pending {pending:>5}   published {s['outbox']['total'] - pending}")
    mongo_pending = catalog_outbox()
    ok &= mongo_pending == 0
    lines.append(f"  {'ok' if mongo_pending == 0 else '..'} {'catalog':14} pending {mongo_pending:>5}   (Mongo outbox)")

    groups = {s["consumer"]["group"]: s["consumer"]["topics"] for s in status.values() if s.get("consumer")}
    lag, dlq = await kafka_state(groups)
    lines.append("Consumer lag (on Kafka, not yet processed)")
    for group, per_topic in sorted(lag.items()):
        total = sum(per_topic.values())
        ok &= total == 0
        detail = ", ".join(f"{t}={n}" for t, n in per_topic.items() if n) or "all caught up"
        lines.append(f"  {'ok' if total == 0 else '..'} {group:24} lag {total:>5}   {detail}")
    lines.append("Dead-letter queues (parked after a permanent failure)")
    parked = {t: n for t, n in dlq.items() if n}
    ok &= not parked
    lines.append("  ok none" if not parked else "\n".join(f"  !! {t}: {n}" for t, n in parked.items()))
    return ok, lines


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", type=int, default=0, help="seconds to wait for everything to drain")
    args = parser.parse_args()
    deadline = time.time() + args.wait
    while True:
        ok, lines = asyncio.run(audit())
        if ok or time.time() >= deadline:
            break
        time.sleep(3)
    print("\n".join(lines))
    print("\nALL EVENTS DELIVERED" if ok else "\nEVENTS STILL PENDING OR PARKED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
