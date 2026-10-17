"""Delivery channels. Console implementations log; swap in SES / SNS / WhatsApp Business / FCM adapters."""

from typing import Protocol

from gj_common.logging import get_logger

log = get_logger(__name__)


class Channel(Protocol):
    name: str

    async def send(self, *, address: str, title: str, body: str, sender: str) -> None: ...


class ConsoleChannel:
    def __init__(self, name: str) -> None:
        self.name = name

    async def send(self, *, address: str, title: str, body: str, sender: str) -> None:
        masked = address[:3] + "***" + address[-3:] if len(address) > 6 else "***"
        log.info("notification_delivered", channel=self.name, to=masked, sender=sender, title=title)


CHANNELS: dict[str, Channel] = {name: ConsoleChannel(name) for name in ("sms", "whatsapp", "email", "push")}
PAID_CHANNELS = {"sms", "whatsapp"}
