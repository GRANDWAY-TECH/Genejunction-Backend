import hashlib
import hmac
import json
import re
import secrets

from redis.asyncio import Redis

from app.config import settings
from gj_common.errors import AppError, TooManyRequests, ValidationFailed
from gj_common.logging import get_logger

log = get_logger(__name__)

_PHONE = re.compile(r"^\+\d{8,15}$")


def normalize_phone(raw: str) -> str:
    phone = re.sub(r"[\s\-()]", "", raw)
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    elif phone.startswith("0"):
        phone = "+60" + phone[1:]  # Malaysian local format
    elif phone.startswith("60"):
        phone = "+" + phone
    if not _PHONE.match(phone):
        raise ValidationFailed("Invalid phone number", code="invalid_phone")
    return phone


def _hash(phone: str, code: str) -> str:
    return hmac.new(settings.internal_api_key.encode(), f"{phone}:{code}".encode(), hashlib.sha256).hexdigest()


class SmsSender:
    """Swap for an SNS / SMS-gateway adapter in production."""

    async def send(self, phone: str, message: str) -> None:
        log.info("sms_sent", phone=phone[:-4] + "****", message_len=len(message))


sms = SmsSender()


async def request_otp(redis: Redis, phone: str, app_name: str) -> str:
    if await redis.exists(f"otp:cooldown:{phone}"):
        raise TooManyRequests("Please wait before requesting another code")
    hourly_key = f"otp:hourly:{phone}"
    count = await redis.incr(hourly_key)
    if count == 1:
        await redis.expire(hourly_key, 3600)
    if count > settings.otp_requests_per_hour:
        raise TooManyRequests("Too many codes requested, try again later")

    code = f"{secrets.randbelow(1_000_000):06d}"
    await redis.set(f"otp:{phone}", json.dumps({"h": _hash(phone, code), "a": 0}), ex=settings.otp_ttl)
    await redis.set(f"otp:cooldown:{phone}", "1", ex=settings.otp_resend_cooldown)
    await sms.send(phone, f"{app_name}: your verification code is {code}. It expires in 5 minutes.")
    return code


async def verify_otp(redis: Redis, phone: str, code: str) -> None:
    key = f"otp:{phone}"
    raw = await redis.get(key)
    if raw is None:
        raise AppError("Code expired or not requested", code="otp_expired", status_code=400)
    state = json.loads(raw)
    if state["a"] >= settings.otp_max_attempts:
        await redis.delete(key)
        raise TooManyRequests("Too many wrong attempts")
    if not hmac.compare_digest(state["h"], _hash(phone, code)):
        state["a"] += 1
        ttl = await redis.ttl(key)
        await redis.set(key, json.dumps(state), ex=max(ttl, 1))
        raise AppError("Incorrect code", code="otp_invalid", status_code=400)
    await redis.delete(key)
