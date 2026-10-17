import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import FastAPI

from gj_common.logging import get_logger

log = get_logger(__name__)


@dataclass
class PeriodicJob:
    """A background job that runs on one replica at a time (Redis lock) every `interval` seconds."""

    name: str
    interval: int
    func: Callable[[FastAPI], Awaitable[None]]

    async def run_forever(self, app: FastAPI) -> None:
        redis = app.state.redis
        await asyncio.sleep(5)
        while True:
            try:
                if await redis.set(f"job-lock:{self.name}", "1", nx=True, ex=max(self.interval - 1, 1)):
                    await self.func(app)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("job_failed", job=self.name, error=str(exc))
            await asyncio.sleep(self.interval)
