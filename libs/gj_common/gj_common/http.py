from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from gj_common.errors import AppError, UpstreamError
from gj_common.logging import current_request_id

_PASSTHROUGH = {400, 402, 403, 404, 409, 422, 429}


class ServiceClient:
    """HTTP client for internal service-to-service calls (adds the internal key and request id)."""

    def __init__(self, base_url: str, internal_key: str, *, timeout: float = 5.0) -> None:
        self.base_url = base_url
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={"x-internal-key": internal_key},
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )

    async def request(self, method: str, path: str, *, json: Any = None, params: dict | None = None) -> Any:
        headers = {}
        if rid := current_request_id():
            headers["x-request-id"] = rid
        # Only network-level failures are retried; idempotency on writes is the caller's job.
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(httpx.TransportError),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.2, max=2),
            reraise=True,
        ):
            with attempt:
                resp = await self._client.request(method, path, json=json, params=params, headers=headers)
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {})
            except ValueError:
                err = {}
            message = err.get("message") or f"{self.base_url}{path} returned {resp.status_code}"
            if resp.status_code in _PASSTHROUGH:
                raise AppError(message, code=err.get("code"), status_code=resp.status_code, details=err.get("details"))
            raise UpstreamError(message)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def get(self, path: str, **kw) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw) -> Any:
        return await self.request("POST", path, **kw)

    async def patch(self, path: str, **kw) -> Any:
        return await self.request("PATCH", path, **kw)

    async def aclose(self) -> None:
        await self._client.aclose()
