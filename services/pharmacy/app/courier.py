"""Courier adapters (Lalamove, J&T, Pos Laju ... behind one interface)."""

import hashlib
import hmac
import secrets
from typing import Protocol

from app.config import settings


class Courier(Protocol):
    name: str

    async def create_shipment(self, *, reference: str, pickup: dict, dropoff: dict, contact: dict) -> dict:
        """Returns {tracking_no, tracking_url}."""


class MockCourier:
    name = "mock"

    async def create_shipment(self, *, reference: str, pickup: dict, dropoff: dict, contact: dict) -> dict:
        tracking = f"GJX{secrets.token_hex(5).upper()}"
        return {"tracking_no": tracking, "tracking_url": f"https://track.example.com/{tracking}"}


def verify_signature(body: bytes, signature: str) -> bool:
    expected = hmac.new(settings.courier_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


COURIERS: dict[str, Courier] = {"mock": MockCourier()}


def get_courier() -> Courier:
    return COURIERS[settings.courier_provider]
