from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import msal

GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"


class AuthError(RuntimeError):
    pass


@dataclass
class _CachedToken:
    value: str
    expires_at: float  # epoch seconds


class AppOnlyTokenProvider:
    """Client-credentials flow against Entra ID. Caches tokens in-memory per scope."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        if not (tenant_id and client_id and client_secret):
            raise AuthError(
                "tenant_id, client_id, and client_secret are required for app-only auth"
            )
        self._app = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
        )
        self._cache: dict[str, _CachedToken] = {}
        self._lock = asyncio.Lock()

    async def get_token(self, scope: str = GRAPH_DEFAULT_SCOPE) -> str:
        async with self._lock:
            cached = self._cache.get(scope)
            if cached and cached.expires_at - 60 > time.time():
                return cached.value
            token = await asyncio.to_thread(self._acquire_blocking, scope)
            return token

    def _acquire_blocking(self, scope: str) -> str:
        result: dict[str, Any] = self._app.acquire_token_for_client(scopes=[scope])
        if "access_token" not in result:
            err = result.get("error_description") or result.get("error") or "unknown error"
            raise AuthError(f"failed to acquire app-only token for {scope}: {err}")
        access = str(result["access_token"])
        expires_in = int(result.get("expires_in", 3600))
        self._cache[scope] = _CachedToken(value=access, expires_at=time.time() + expires_in)
        return access


class DelegatedTokenProvider:
    """Phase 3 — device code flow with keyring-cached refresh tokens."""

    async def get_token(self, scope: str) -> str:  # pragma: no cover - phase 3
        raise NotImplementedError("delegated auth lands in Phase 3")


class DataverseTokenProvider:
    """Phase 2 — per-org Dataverse tokens (audience https://{org}.crm.dynamics.com)."""

    async def get_token(self, scope: str) -> str:  # pragma: no cover - phase 2
        raise NotImplementedError("Dataverse auth lands in Phase 2")
