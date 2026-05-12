from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any, Self

import httpx

from m365_mcp_scanner.auth.token_provider import TokenProvider
from m365_mcp_scanner.clients.api_recorder import ApiCallRecorder

logger = logging.getLogger(__name__)


class TokenBucket:
    """Simple async token bucket. rate = tokens/sec, capacity = burst size."""

    def __init__(self, rate: float, capacity: float) -> None:
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._last = asyncio.get_event_loop().time() if False else 0.0
        self._lock = asyncio.Lock()

    async def acquire(self, n: float = 1.0) -> None:
        async with self._lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            if self._last == 0.0:
                self._last = now
            self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens >= n:
                self._tokens -= n
                return
            wait = (n - self._tokens) / self._rate
        await asyncio.sleep(wait)
        async with self._lock:
            self._tokens = max(0.0, self._tokens - n)
            self._last = asyncio.get_event_loop().time()


class BaseAsyncClient:
    """httpx-based async client with retry, rate limiting, and pagination."""

    def __init__(
        self,
        token_provider: TokenProvider,
        scope: str,
        *,
        base_url: str = "",
        rate: float = 10.0,
        burst: float = 20.0,
        max_retries: int = 5,
        timeout: float = 30.0,
        recorder: ApiCallRecorder | None = None,
        client_name: str = "",
    ) -> None:
        self._token_provider = token_provider
        self._scope = scope
        self._bucket = TokenBucket(rate=rate, capacity=burst)
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._base_url = base_url
        self._recorder = recorder
        self._client_name = client_name

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _auth_headers(self) -> dict[str, str]:
        token = await self._token_provider.get_token(self._scope)
        return {"Authorization": f"Bearer {token}"}

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        await self._bucket.acquire()
        headers = await self._auth_headers()
        attempt = 0
        t0 = time.perf_counter()
        full_url = url if url.startswith("http") else f"{self._base_url}{url}"
        while True:
            attempt += 1
            try:
                response = await self._client.request(
                    method, url, params=params, json=json, headers=headers
                )
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    self._record(
                        method=method,
                        url=full_url,
                        status=None,
                        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
                        attempts=attempt,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    raise
                delay = self._backoff(attempt)
                logger.warning("transport error %s on %s; retrying in %.1fs", exc, url, delay)
                await asyncio.sleep(delay)
                continue

            if response.status_code in (429, 503) and attempt < self._max_retries:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else self._backoff(attempt)
                logger.warning(
                    "rate-limited (%s) on %s; retrying in %.1fs",
                    response.status_code,
                    url,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            err: str | None = None
            if response.status_code >= 400:
                try:
                    err = response.text[:2048]
                except Exception:  # noqa: BLE001
                    err = None
            self._record(
                method=method,
                url=str(response.request.url),
                status=response.status_code,
                elapsed_ms=(time.perf_counter() - t0) * 1000.0,
                attempts=attempt,
                error=err,
            )
            return response

    def _record(
        self,
        *,
        method: str,
        url: str,
        status: int | None,
        elapsed_ms: float,
        attempts: int,
        error: str | None,
    ) -> None:
        if self._recorder is None:
            return
        self._recorder.record(
            client=self._client_name,
            method=method,
            url=url,
            status=status,
            elapsed_ms=elapsed_ms,
            attempts=attempts,
            error=error,
        )

    @staticmethod
    def _backoff(attempt: int) -> float:
        return float(min(30.0, (2 ** (attempt - 1)) + random.uniform(0, 0.5)))

    async def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await self.request("GET", url, params=params)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def paginate(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield each row across paginated responses.

        Supports both Graph-style ``@odata.nextLink`` and the bare ``nextLink``
        used by Power Platform / Power Apps admin APIs.
        """
        next_url: str | None = url
        next_params = params
        while next_url:
            payload = await self.get_json(next_url, params=next_params)
            for item in payload.get("value", []):
                yield item
            next_link = payload.get("@odata.nextLink") or payload.get("nextLink")
            if not next_link:
                return
            next_url = next_link
            next_params = None  # nextLink already encodes params
