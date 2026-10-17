import logging
import sys
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def configure_logging(service_name: str, level: str = "INFO") -> None:
    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    logging.basicConfig(stream=sys.stdout, level=numeric_level, format="%(message)s")

    def add_service(_, __, event_dict):
        event_dict.setdefault("service", service_name)
        return event_dict

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            add_service,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None):
    return structlog.get_logger(name)


def current_request_id() -> str | None:
    return structlog.contextvars.get_contextvars().get("request_id")


class RequestContextMiddleware:
    """Binds a request id (taken from X-Request-ID or generated) to every log line and echoes it back."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        request_id = headers.get(b"x-request-id", b"").decode() or uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"].append((b"x-request-id", request_id.encode()))
            await send(message)

        await self.app(scope, receive, send_wrapper)
