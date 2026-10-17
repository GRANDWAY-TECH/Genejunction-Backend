"""Adapter for the existing GeneJunction (PHP) system. The marketplace only talks to it through these calls.

`HttpLegacyClient` maps to the PHP REST API (endpoint contract in docs/LEGACY_CONTRACT.md).
`MockLegacyClient` is a Redis-backed stand-in so the full journey runs locally and in CI.
"""

import json
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol

import httpx
from redis.asyncio import Redis

from gj_common.errors import NotFound, UpstreamError

MYT = timezone(timedelta(hours=8))


class LegacyClient(Protocol):
    async def list_doctors(self, specialty: str | None, q: str | None) -> list[dict]: ...
    async def get_doctor(self, doctor_id: str) -> dict: ...
    async def get_slots(self, doctor_id: str, day: date) -> list[dict]: ...
    async def find_or_create_patient(self, phone: str | None, name: str | None, email: str | None) -> str: ...
    async def book(self, *, doctor_id: str, slot_id: str, patient_id: str, external_ref: str, amount: Decimal) -> dict: ...
    async def folio(self, patient_id: str) -> list[dict]: ...
    async def add_folio_record(self, patient_id: str, record: dict) -> None: ...
    async def push_order(self, patient_id: str, order: dict) -> None: ...


class HttpLegacyClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=10, headers={"X-Api-Key": api_key})

    async def _call(self, method: str, path: str, **kw):
        try:
            resp = await self._http.request(method, path, **kw)
        except httpx.TransportError as exc:
            raise UpstreamError("GeneJunction core system unreachable") from exc
        if resp.status_code == 404:
            raise NotFound("Not found in GeneJunction core system")
        if resp.status_code >= 400:
            raise UpstreamError(f"GeneJunction core system error {resp.status_code}")
        return resp.json() if resp.content else None

    async def list_doctors(self, specialty, q):
        return await self._call("GET", "/api/doctors", params={k: v for k, v in {"specialty": specialty, "q": q}.items() if v})

    async def get_doctor(self, doctor_id):
        return await self._call("GET", f"/api/doctors/{doctor_id}")

    async def get_slots(self, doctor_id, day):
        return await self._call("GET", f"/api/doctors/{doctor_id}/slots", params={"date": day.isoformat()})

    async def find_or_create_patient(self, phone, name, email):
        return (await self._call("POST", "/api/patients/find-or-create",
                                 json={"phone": phone, "name": name, "email": email}))["patient_id"]

    async def book(self, *, doctor_id, slot_id, patient_id, external_ref, amount):
        return await self._call("POST", "/api/bookings", json={
            "doctor_id": doctor_id, "slot_id": slot_id, "patient_id": patient_id,
            "external_ref": external_ref, "paid_amount": str(amount)})

    async def folio(self, patient_id):
        return await self._call("GET", f"/api/patients/{patient_id}/folio")

    async def add_folio_record(self, patient_id, record):
        await self._call("POST", f"/api/patients/{patient_id}/folio", json=record)

    async def push_order(self, patient_id, order):
        await self._call("POST", f"/api/patients/{patient_id}/orders", json=order)


MOCK_DOCTORS = [
    {"id": "doc-1001", "name": "Dr. Ravi Kumar", "specialty": "Cardiology", "clinic": "Mediva", "fee": "120.00"},
    {"id": "doc-1002", "name": "Dr. Lim Wei Ling", "specialty": "General Practice", "clinic": "Sejahtera", "fee": "40.00"},
    {"id": "doc-1003", "name": "Dr. Aisyah Rahman", "specialty": "Dermatology", "clinic": "Mediva", "fee": "90.00"},
    {"id": "doc-1004", "name": "Dr. Tan Mei Hua", "specialty": "Paediatrics", "clinic": "Sejahtera", "fee": "80.00"},
]


class MockLegacyClient:
    def __init__(self, redis: Redis) -> None:
        self.r = redis

    async def list_doctors(self, specialty, q):
        docs = MOCK_DOCTORS
        if specialty:
            docs = [d for d in docs if d["specialty"].lower() == specialty.lower()]
        if q:
            docs = [d for d in docs if q.lower() in (d["name"] + d["specialty"]).lower()]
        return docs

    async def get_doctor(self, doctor_id):
        for d in MOCK_DOCTORS:
            if d["id"] == doctor_id:
                return d
        raise NotFound("Doctor not found")

    async def get_slots(self, doctor_id, day):
        await self.get_doctor(doctor_id)
        taken = await self.r.smembers(f"legacy-mock:booked:{doctor_id}")
        slots = []
        for hour in range(9, 17):
            start = datetime(day.year, day.month, day.day, hour, tzinfo=MYT)
            slot_id = f"{doctor_id}@{start:%Y-%m-%dT%H:%M}"
            if slot_id not in taken and start > datetime.now(MYT):
                slots.append({"slot_id": slot_id, "start": start.isoformat(),
                              "end": (start + timedelta(minutes=30)).isoformat()})
        return slots

    async def find_or_create_patient(self, phone, name, email):
        key = phone or email
        existing = await self.r.hget("legacy-mock:patients", key)
        if existing:
            return existing
        pid = f"pt-{secrets.token_hex(4)}"
        await self.r.hset("legacy-mock:patients", key, pid)
        return pid

    async def book(self, *, doctor_id, slot_id, patient_id, external_ref, amount):
        if not await self.r.sadd(f"legacy-mock:booked:{doctor_id}", slot_id):
            raise UpstreamError("Slot already booked in core system")
        booking_id = f"bk-{secrets.token_hex(4)}"
        await self.r.hset("legacy-mock:bookings", booking_id, json.dumps(
            {"doctor_id": doctor_id, "slot_id": slot_id, "patient_id": patient_id, "external_ref": external_ref}))
        return {"booking_id": booking_id, "video_url": f"https://consult.genejunction.com/room/{booking_id}"}

    async def folio(self, patient_id):
        return [json.loads(x) for x in await self.r.lrange(f"legacy-mock:folio:{patient_id}", 0, 200)]

    async def add_folio_record(self, patient_id, record):
        await self.r.lpush(f"legacy-mock:folio:{patient_id}", json.dumps(
            {"id": f"rec-{secrets.token_hex(4)}", **record}))

    async def push_order(self, patient_id, order):
        await self.r.lpush(f"legacy-mock:orders:{patient_id}", json.dumps(order))
