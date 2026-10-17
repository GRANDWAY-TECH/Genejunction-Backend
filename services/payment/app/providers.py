"""Payment gateway adapters.

Every gateway (FPX via e.g. Billplz/iPay88, cards, e-wallets) implements the same small
interface. The mock gateway is for local development and automated tests only.
"""

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.config import settings
from app.models import Payment
from gj_common.errors import Unauthorized


@dataclass
class WebhookResult:
    event_id: str
    provider_ref: str
    outcome: str  # succeeded | failed
    reason: str | None = None


class PaymentProvider(Protocol):
    name: str

    async def create_checkout(self, payment: Payment) -> tuple[str, str]:
        """Returns (provider_ref, checkout_url)."""

    async def refund(self, payment: Payment, amount: Decimal, reference: str) -> str:
        """Returns the provider's refund reference. Raise to signal failure."""

    def parse_webhook(self, headers: dict, body: bytes) -> WebhookResult:
        """Verify the signature and normalise the gateway's payload."""


def sign(body: bytes) -> str:
    return hmac.new(settings.webhook_secret.encode(), body, hashlib.sha256).hexdigest()


class MockProvider:
    name = "mock"

    async def create_checkout(self, payment: Payment) -> tuple[str, str]:
        ref = f"mock_{secrets.token_hex(8)}"
        return ref, f"{settings.public_base_url}/api/v1/payments/mock/{payment.id}/complete"

    async def refund(self, payment: Payment, amount: Decimal, reference: str) -> str:
        return f"mock_rf_{secrets.token_hex(6)}"

    def parse_webhook(self, headers: dict, body: bytes) -> WebhookResult:
        signature = headers.get("x-signature", "")
        if not hmac.compare_digest(signature, sign(body)):
            raise Unauthorized("Bad webhook signature", code="bad_signature")
        data = json.loads(body)
        return WebhookResult(event_id=data["event_id"], provider_ref=data["provider_ref"],
                             outcome=data["outcome"], reason=data.get("reason"))


PROVIDERS: dict[str, PaymentProvider] = {"mock": MockProvider()}


def get_provider(name: str | None = None) -> PaymentProvider:
    return PROVIDERS[name or settings.provider]
