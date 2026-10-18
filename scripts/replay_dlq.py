"""Re-deliver events parked in dead-letter queues, after the cause has been fixed and deployed.

    python scripts/replay_dlq.py --list                  # show what is parked and why
    python scripts/replay_dlq.py gj.order.dlq            # replay everything parked on that DLQ
    python scripts/replay_dlq.py gj.order.dlq --event <event id>

Each parked event is published back to its original topic with its original id. Consumers that already
handled it skip it (processed_events), so only the service that failed applies it. Progress is kept in the
`dlq-replayer` consumer group, so an event is replayed once.
"""

import argparse
import asyncio
import json
import os

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

KAFKA = os.environ.get("KAFKA", "localhost:19092")


async def parked(topic: str, group: str | None):
    consumer = AIOKafkaConsumer(topic, bootstrap_servers=KAFKA, group_id=group, enable_auto_commit=False,
                                auto_offset_reset="earliest")
    await consumer.start()
    try:
        batch = await consumer.getmany(timeout_ms=3000)
        while batch:
            for _tp, messages in batch.items():
                for m in messages:
                    yield consumer, m
            batch = await consumer.getmany(timeout_ms=1500)
    finally:
        await consumer.stop()


async def list_all() -> None:
    probe = AIOKafkaConsumer(bootstrap_servers=KAFKA)
    await probe.start()
    topics = sorted(t for t in await probe.topics() if t.endswith(".dlq"))
    await probe.stop()
    for topic in topics:
        async for _c, m in parked(topic, None):
            w = json.loads(m.value)
            env = w.get("envelope") or {}
            print(f"{topic} offset {m.offset}: {env.get('type', '?')} {env.get('id', '?')} "
                  f"consumer={w.get('consumer')} error={w.get('error')}")


async def replay(topic: str, event_id: str | None) -> None:
    producer = AIOKafkaProducer(bootstrap_servers=KAFKA, acks="all", enable_idempotence=True)
    await producer.start()
    count = 0
    try:
        async for consumer, m in parked(topic, "dlq-replayer"):
            wrapper = json.loads(m.value)
            env = wrapper.get("envelope")
            if isinstance(env, dict) and (event_id is None or env.get("id") == event_id):
                await producer.send_and_wait(wrapper["source_topic"], json.dumps(env).encode(), key=m.key)
                count += 1
                print(f"replayed {env['type']} {env['id']} -> {wrapper['source_topic']}")
            elif not isinstance(env, dict):
                print(f"skipped unparseable message at offset {m.offset} (inspect it with --list)")
            if event_id is None:
                await consumer.commit()  # a targeted replay leaves the rest of the queue untouched
    finally:
        await producer.stop()
    print(f"{count} event(s) replayed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("topic", nargs="?")
    parser.add_argument("--event")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list or not args.topic:
        asyncio.run(list_all())
    else:
        asyncio.run(replay(args.topic, args.event))


if __name__ == "__main__":
    main()
