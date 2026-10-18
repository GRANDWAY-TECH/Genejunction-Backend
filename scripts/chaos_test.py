"""Failure-injection test: stop services, the broker and dependencies mid-journey, then prove every update
still lands exactly once when they come back.

    python scripts/chaos_test.py        # needs the docker compose stack running

Scenarios
  1. Five consuming services down while orders are paid.
  2. Kafka (the broker) down while an order is paid.
  3. A dependency (catalog) down while a consumer handles the payment.
  4. The existing PHP system down when a paid consultation must be booked.
  5. The same events delivered again (duplicates).
Then: the audit shows no event waiting anywhere and nothing parked.
"""

import json
import subprocess
import sys
import time
import traceback
import uuid
from datetime import date, timedelta
from pathlib import Path

import asyncio

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

sys.path.insert(0, str(Path(__file__).parent))
from e2e_journey import ADMIN, RUN, Api, business_user, onboard_partner, step, wait_for  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOMORROW = (date.today() + timedelta(days=1)).isoformat()


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True)


def redis(*args: str) -> None:
    compose("exec", "-T", "redis", "redis-cli", *args)


def ready(port: int) -> bool:
    import httpx
    try:
        return httpx.get(f"http://localhost:{port}/ready", timeout=2).status_code == 200
    except Exception:
        return False


def start(*services: str) -> None:
    compose("start", *services)


class World:
    def __init__(self, api: Api) -> None:
        self.api = api
        a = api
        self.admin = a.post("/api/v1/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})["access_token"]
        self.pharmacy_id, owner = onboard_partner(a, self.admin, business_user(a, "Chaos Pharmacy"), "pharmacy", "ChaosPharm")
        self.pt = owner["access_token"]
        self.lab_id, lab_owner = onboard_partner(a, self.admin, business_user(a, "Chaos Lab"), "lab", "ChaosLab")
        self.lt = lab_owner["access_token"]
        med = a.post("/api/v1/catalog/products", self.admin, json={
            "sku": f"CHAOS-MED-{RUN}", "type": "medicine", "name": f"Chaos Paracetamol {RUN}", "category": "Pain"})
        test = a.post("/api/v1/catalog/products", self.admin, json={
            "sku": f"CHAOS-LAB-{RUN}", "type": "lab_test", "name": f"Chaos Lipid Panel {RUN}", "category": "Blood tests"})
        self.med_offer = a.post(f"/api/v1/catalog/partners/{self.pharmacy_id}/offers", self.pt,
                                json={"product_id": med["id"], "price": "12.00", "stock": 500})["id"]
        self.lab_offer = a.post(f"/api/v1/catalog/partners/{self.lab_id}/offers", self.lt,
                                json={"product_id": test["id"], "price": "80.00"})["id"]
        wait_for(lambda: a.get("/api/v1/catalog/search", params={"q": RUN})["total"] >= 2, "offers searchable")
        phone = f"+6014{int(RUN, 16) % 10_000_000:07d}"
        code = a.post("/api/v1/auth/otp/request", json={"phone": phone})["debug_otp"]
        self.dt = a.post("/api/v1/auth/otp/verify", json={"phone": phone, "code": code, "full_name": "Chaos Patient"})["access_token"]
        self.address = {"recipient": "Chaos Patient", "phone": phone, "line1": "1 Jalan Test", "city": "Kuala Lumpur",
                        "state": "WP", "postcode": "50000"}

    def checkout(self, *, med: int = 0, lab: bool = False, consult: bool = False) -> tuple[dict, dict]:
        a = self.api
        if med:
            a.post("/api/v1/cart/items", self.dt, json={"offer_id": self.med_offer, "qty": med})
        if lab:
            a.post("/api/v1/cart/items", self.dt, json={"offer_id": self.lab_offer, "qty": 1})
        if consult:
            doctor = a.get("/api/v1/consult/doctors", params={"specialty": "Dermatology"})[0]
            slot = a.get(f"/api/v1/consult/doctors/{doctor['id']}/slots", params={"day": TOMORROW})[0]
            a.post("/api/v1/cart/consultation", self.dt, json={"doctor_id": doctor["id"], "slot_id": slot["slot_id"]})
        body = {"payment_method": "fpx", "delivery_address": self.address}
        if lab:
            body["lab_collection"] = {"type": "home_collection", "date": TOMORROW, "slot": "10:00-12:00"}
        r = a.post("/api/v1/orders/checkout", self.dt, headers={"Idempotency-Key": str(uuid.uuid4())}, json=body)
        return r["order"], r["payment"]

    def pay(self, payment: dict) -> None:
        self.api.post(f"/api/v1/payments/mock/{payment['id']}/complete")

    def order(self, order_id: str) -> dict:
        return self.api.get(f"/api/v1/orders/{order_id}", self.dt)

    def sub(self, order_id: str, kind: str) -> dict:
        return next(s for s in self.order(order_id)["sub_orders"] if s["kind"] == kind)

    def fulfilments(self, number: str) -> list:
        return [f for f in self.api.get(f"/api/v1/pharmacy/{self.pharmacy_id}/fulfilments", self.pt,
                                        params={"limit": 100})["items"] if f["order_number"] == number]

    def lab_bookings(self, number: str) -> list:
        return [b for b in self.api.get(f"/api/v1/lab/{self.lab_id}/bookings", self.lt,
                                        params={"limit": 100})["items"] if b["order_number"] == number]

    def settlements(self, order_id: str) -> list:
        return [e for e in self.api.get(f"/api/v1/billing/partners/{self.pharmacy_id}/settlements", self.pt,
                                        params={"limit": 100})["items"] if e["order_id"] == order_id]

    def confirmations(self, order_id: str) -> list:
        return [n for n in self.api.get("/api/v1/notifications/me", self.dt, params={"limit": 100})["items"]
                if n["title"] == "Order confirmed" and n["data"].get("order_id") == order_id]

    def stock(self) -> int:
        return self.api.get(f"/api/v1/catalog/offers/{self.med_offer}")["stock"]


async def find_events(event_type: str, order_ids: set[str]) -> list[tuple[bytes, bytes]]:
    consumer = AIOKafkaConsumer("gj.order", bootstrap_servers="localhost:19092", auto_offset_reset="earliest")
    await consumer.start()
    found = []
    try:
        batch = await consumer.getmany(timeout_ms=3000)
        while batch:
            for messages in batch.values():
                for m in messages:
                    env = json.loads(m.value)
                    if env["type"] == event_type and env["data"].get("order_id") in order_ids:
                        found.append((m.key, m.value))
            batch = await consumer.getmany(timeout_ms=1500)
    finally:
        await consumer.stop()
    return found


async def redeliver(messages: list[tuple[bytes, bytes]], times: int) -> None:
    producer = AIOKafkaProducer(bootstrap_servers="localhost:19092", acks="all")
    await producer.start()
    try:
        for _ in range(times):
            for key, value in messages:
                await producer.send_and_wait("gj.order", value, key=key)
    finally:
        await producer.stop()


def run() -> None:
    api = Api("http://localhost:8080")
    print(f"GeneJunction chaos test (run {RUN})")
    w = World(api)
    step("setup: pharmacy, lab, products, patient")
    stock0 = w.stock()

    # ---------------------------------------------------------------- 1. consumers down
    down = ["pharmacy", "lab", "billing", "notification", "legacy-bridge"]
    compose("stop", *down)
    o1, p1 = w.checkout(med=2, lab=True)
    w.pay(p1)
    wait_for(lambda: w.order(o1["id"])["status"] == "paid", "order paid while consumers are down")
    time.sleep(8)
    step(f"stopped {', '.join(down)}; order {o1['number']} paid; its events wait on Kafka")
    start(*down)
    wait_for(lambda: all(ready(p) for p in (8107, 8108, 8109, 8111, 8112)), "services back", 90)
    wait_for(lambda: len(w.fulfilments(o1["number"])) == 1, "pharmacy picked up the order", 90)
    wait_for(lambda: len(w.lab_bookings(o1["number"])) == 1, "lab picked up the order", 60)
    wait_for(lambda: len(w.settlements(o1["id"])) == 1, "billing recorded commission", 60)
    wait_for(lambda: len(w.confirmations(o1["id"])) == 1, "patient notified", 60)
    step("services restarted -> fulfilment, lab booking, commission and notification all caught up (once each)")

    # ---------------------------------------------------------------- 2. broker down
    compose("stop", "redpanda")
    o2, p2 = w.checkout(med=1)
    w.pay(p2)
    time.sleep(8)
    assert w.order(o2["id"])["status"] == "pending_payment", "nothing can flow without the broker"
    step(f"Kafka stopped; order {o2['number']} paid -> payment recorded, event held in the payment outbox")
    start("redpanda")
    wait_for(lambda: w.order(o2["id"])["status"] == "paid", "order paid after Kafka returns", 120)
    wait_for(lambda: len(w.fulfilments(o2["number"])) == 1, "pharmacy picked up the order", 90)
    wait_for(lambda: len(w.settlements(o2["id"])) == 1, "billing recorded commission", 60)
    step("Kafka restarted -> outbox published, order paid, pharmacy and billing caught up")

    # ---------------------------------------------------------------- 3. dependency down during a handler
    o3, p3 = w.checkout(med=3)
    compose("stop", "catalog")
    w.pay(p3)
    time.sleep(12)
    assert w.order(o3["id"])["status"] == "pending_payment", "order must wait for the catalog, not fail"
    step(f"catalog stopped while order {o3['number']} is paid -> order service keeps retrying (not failed, not parked)")
    start("catalog")
    wait_for(lambda: w.order(o3["id"])["status"] == "paid", "order paid after catalog returns", 120)
    wait_for(lambda: len(w.fulfilments(o3["number"])) == 1, "pharmacy picked up the order", 60)
    step("catalog restarted -> stock committed, order paid, fulfilment created")

    # ---------------------------------------------------------------- 4. existing PHP system down
    o4, p4 = w.checkout(consult=True)
    redis("SET", "legacy-mock:down", "1")
    try:
        w.pay(p4)
        wait_for(lambda: w.order(o4["id"])["status"] == "paid", "order paid", 60)
        time.sleep(12)
        consult = w.sub(o4["id"], "consultation")
        assert consult["status"] == "placed", f"outage must not fail the booking: {consult['status']}"
        assert w.order(o4["id"])["refunded_amount"] == "0.00"
        step(f"PHP system down for order {o4['number']} -> consultation waits as 'placed', no refund")
    finally:
        redis("DEL", "legacy-mock:down")
    wait_for(lambda: w.sub(o4["id"], "consultation")["status"] == "booked", "consult booked after PHP returns", 90)
    step("PHP system back -> consultation booked")

    # ---------------------------------------------------------------- 5. duplicates
    ids = {o1["id"], o2["id"], o3["id"]}
    paid_events = asyncio.run(find_events("order.paid", ids))
    assert len(paid_events) == 3, f"expected 3 order.paid events, found {len(paid_events)}"
    asyncio.run(redeliver(paid_events, times=2))
    time.sleep(15)
    for o in (o1, o2, o3):
        assert len(w.fulfilments(o["number"])) == 1, f"duplicate fulfilment for {o['number']}"
        assert len(w.settlements(o["id"])) == 1, f"duplicate commission for {o['number']}"
    assert len(w.lab_bookings(o1["number"])) == 1 and len(w.confirmations(o1["id"])) == 1
    step("3 order.paid events delivered twice more -> still one fulfilment, booking, commission, notice each")

    sold = 2 + 1 + 3
    assert w.stock() == stock0 - sold, f"stock {w.stock()} != {stock0} - {sold}"
    step(f"stock exact: {stock0} -> {w.stock()} after selling {sold}, through every outage")

    audit = subprocess.run([sys.executable, str(ROOT / "scripts" / "event_audit.py"), "--wait", "120"],
                           capture_output=True, text=True)
    print(audit.stdout)
    assert audit.returncode == 0, "events still pending or parked"
    print(f"CHAOS TEST PASSED ({RUN})")


if __name__ == "__main__":
    try:
        run()
    except AssertionError as exc:
        traceback.print_exc()
        print(f"\nFAILED: {exc}")
        sys.exit(1)
    finally:
        subprocess.run(["docker", "compose", "start"], cwd=ROOT, capture_output=True)
        subprocess.run(["docker", "compose", "exec", "-T", "redis", "redis-cli", "DEL", "legacy-mock:down"],
                       cwd=ROOT, capture_output=True)
