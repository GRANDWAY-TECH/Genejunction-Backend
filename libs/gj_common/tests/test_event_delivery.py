import json
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import exc as sa_exc

from gj_common.errors import Conflict, NotFound, UpstreamError
from gj_common.events import RetryLater, is_transient
from gj_common.events.kafka import EventConsumer


class FakeBus:
    def __init__(self):
        self.published = []

    async def publish(self, topic, key, payload):
        self.published.append((topic, key, payload))


def raw_event(event_type="order.paid"):
    return json.dumps({"id": str(uuid.uuid4()), "type": event_type, "occurred_at": datetime.now(timezone.utc).isoformat(),
                       "source": "order", "key": "k1", "data": {"x": 1}}).encode()


def consumer(handler, bus):
    return EventConsumer(group_id="test", bootstrap_servers="unused", handlers={"order.paid": handler}, db=None,
                         bus=bus, max_attempts=5)


def test_outages_are_transient_bugs_are_not():
    assert is_transient(httpx.ConnectError("refused"))
    assert is_transient(UpstreamError("catalog returned 503"))
    assert is_transient(RetryLater("patient not linked yet"))
    assert is_transient(sa_exc.OperationalError("SELECT 1", {}, Exception("server closed the connection")))
    assert is_transient(ConnectionRefusedError())
    wrapped = RuntimeError("handler failed")
    wrapped.__cause__ = httpx.ReadTimeout("slow")
    assert is_transient(wrapped)
    assert not is_transient(KeyError("sub_order_id"))
    assert not is_transient(Conflict("invalid transition"))
    assert not is_transient(NotFound())


async def test_transient_failure_is_retried_forever_and_never_dead_lettered():
    bus = FakeBus()

    async def down(event, session):
        raise httpx.ConnectError("catalog unreachable")

    c = consumer(down, bus)
    outcomes = [await c.process("gj.order", 0, 7, "k1", raw_event(), attempt) for attempt in range(1, 51)]
    assert set(outcomes) == {"retry"}  # 50 attempts, still waiting: the offset is never committed
    assert bus.published == []


async def test_bug_is_dead_lettered_after_max_attempts_with_context():
    bus = FakeBus()

    async def bug(event, session):
        raise KeyError("sub_order_id")

    c = consumer(bug, bus)
    raw = raw_event()
    assert [await c.process("gj.order", 2, 41, "k1", raw, a) for a in range(1, 5)] == ["retry"] * 4
    assert await c.process("gj.order", 2, 41, "k1", raw, 5) == "done"
    topic, key, payload = bus.published[0]
    assert topic == "gj.order.dlq" and key == "k1"
    assert payload["envelope"]["id"] == json.loads(raw)["id"]
    assert payload["consumer"] == "test" and payload["offset"] == 41 and "KeyError" in payload["error"]


async def test_recovers_when_dependency_comes_back():
    bus, calls = FakeBus(), []

    async def flaky(event, session):
        calls.append(1)
        if len(calls) < 4:
            raise UpstreamError("payment service down")

    c = consumer(flaky, bus)
    outcomes = [await c.process("gj.order", 0, 1, "k1", raw_event(), a) for a in range(1, 5)]
    assert outcomes == ["retry", "retry", "retry", "done"] and bus.published == []


async def test_unparseable_message_is_parked_not_dropped():
    bus = FakeBus()
    c = consumer(None, bus)
    assert await c.process("gj.order", 0, 3, "k1", b"{not json", 1) == "done"
    assert bus.published[0][0] == "gj.order.dlq" and bus.published[0][2]["envelope"] == "{not json"


async def test_unknown_event_types_are_ignored():
    bus = FakeBus()
    c = consumer(None, bus)
    assert await c.process("gj.order", 0, 3, "k1", raw_event("order.placed"), 1) == "done"
    assert bus.published == []
