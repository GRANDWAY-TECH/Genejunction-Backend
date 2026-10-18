"""Which failures are worth retrying forever (something is down) and which are not (a bug or bad data)."""

import asyncio

import httpx
from sqlalchemy import exc as sa_exc

from gj_common.errors import AppError


class RetryLater(Exception):
    """Raise from a handler when the event cannot be applied *yet* (e.g. waiting on another event)."""


_TRANSIENT_DRIVER_ERRORS = {
    # asyncpg / Postgres: connection trouble, failover, contention
    "ConnectionDoesNotExistError", "ConnectionFailureError", "CannotConnectNowError", "TooManyConnectionsError",
    "PostgresConnectionError", "InterfaceError", "DeadlockDetectedError", "SerializationError",
    "AdminShutdownError", "CrashShutdownError", "QueryCanceledError",
    # pymongo
    "AutoReconnect", "NetworkTimeout", "ServerSelectionTimeoutError", "NotPrimaryError", "ConnectionFailure",
    # redis / kafka
    "ConnectionError", "TimeoutError", "KafkaConnectionError", "KafkaTimeoutError", "NodeNotReadyError",
    "RequestTimedOutError", "NotLeaderForPartitionError",
}


def is_transient(exc: BaseException) -> bool:
    seen = 0
    while exc is not None and seen < 5:
        if isinstance(exc, RetryLater):
            return True
        if isinstance(exc, AppError):
            return exc.status_code >= 500 or exc.status_code == 429
        if isinstance(exc, (httpx.TransportError, asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
            return True
        if isinstance(exc, (sa_exc.OperationalError, sa_exc.TimeoutError, sa_exc.InterfaceError)):
            return True
        if isinstance(exc, sa_exc.DBAPIError):
            if exc.connection_invalidated:
                return True
            exc = exc.orig
            seen += 1
            continue
        if type(exc).__name__ in _TRANSIENT_DRIVER_ERRORS:
            return True
        exc = exc.__cause__
        seen += 1
    return False
