from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msal

from m365_mcp_scanner.auth import file_cache

logger = logging.getLogger(__name__)

GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
POWER_PLATFORM_DEFAULT_SCOPE = "https://service.powerapps.com/.default"


def dataverse_scope(org_url: str) -> str:
    """Build the AAD scope string for a Dataverse org's Web API.

    Example: ``https://contoso.crm.dynamics.com`` -> ``https://contoso.crm.dynamics.com/.default``.
    """
    return f"{org_url.rstrip('/')}/.default"

# Delegated scopes used during device-code login. ``.default`` reflects whatever
# permissions the Entra app has been admin-consented for, so adding more
# permissions later does not require a code change here.
DELEGATED_LOGIN_SCOPES: tuple[str, ...] = (
    "https://graph.microsoft.com/.default",
)

# AAD error codes that frequently appear during the ~10-30s after a fresh
# Entra app+secret is provisioned, while MSAL's v2.0 token endpoint converges.
# A retry within ~60s typically succeeds; anything else is a real failure.
_RETRYABLE_AAD_CODES: tuple[str, ...] = ("AADSTS7000215", "AADSTS700016")
_RETRY_DELAYS_SECONDS: tuple[int, ...] = (5, 10, 15, 20, 10)
_RETRY_DEADLINE_SECONDS: float = 60.0


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
        deadline = time.monotonic() + _RETRY_DEADLINE_SECONDS
        delays = iter(_RETRY_DELAYS_SECONDS)
        while True:
            result: dict[str, Any] = self._app.acquire_token_for_client(scopes=[scope])
            if "access_token" in result:
                access = str(result["access_token"])
                expires_in = int(result.get("expires_in", 3600))
                self._cache[scope] = _CachedToken(
                    value=access, expires_at=time.time() + expires_in
                )
                return access
            err = (
                result.get("error_description")
                or result.get("error")
                or "unknown error"
            )
            if not any(code in err for code in _RETRYABLE_AAD_CODES):
                raise AuthError(
                    f"failed to acquire app-only token for {scope}: {err}"
                )
            delay = next(delays, None)
            if delay is None or time.monotonic() + delay > deadline:
                raise AuthError(
                    f"failed to acquire app-only token for {scope} after retries: {err}"
                )
            logger.info(
                "transient AAD error acquiring %s token; retrying in %ss: %s",
                scope, delay, err,
            )
            time.sleep(delay)


DeviceFlowCallback = Callable[[dict[str, Any]], None]


class DelegatedTokenProvider:
    """Device-code flow with file-cached (encrypted) refresh tokens.

    ``get_token`` first tries silent acquisition from the cache. If no account
    is cached, raises :class:`AuthError` — callers must run
    :meth:`login` (interactive device flow) first. The interactive path is
    isolated from :meth:`get_token` so a scan run cannot accidentally trigger
    a browser prompt mid-execution.
    """

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        *,
        cache_dir: Path | None = None,
    ) -> None:
        if not (tenant_id and client_id):
            raise AuthError(
                "tenant_id and client_id are required for delegated auth"
            )
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._cache_dir = cache_dir
        self._cache = msal.SerializableTokenCache()
        self._load_cache()
        # MSAL's ``PublicClientApplication`` constructor performs an OIDC
        # discovery network call. Defer construction until first use so
        # cheap inspection (``is_logged_in``, ``clear_cache``) never makes a
        # network call and so unit tests can run offline.
        self._app: msal.PublicClientApplication | None = None
        self._token_cache: dict[str, _CachedToken] = {}
        self._lock = asyncio.Lock()

    def _get_app(self) -> msal.PublicClientApplication:
        if self._app is None:
            self._app = msal.PublicClientApplication(
                client_id=self._client_id,
                authority=f"https://login.microsoftonline.com/{self._tenant_id}",
                token_cache=self._cache,
            )
        return self._app

    # ---- cache plumbing -------------------------------------------------

    def _load_cache(self) -> None:
        blob = file_cache.load(
            self._tenant_id, self._client_id, cache_dir=self._cache_dir
        )
        if blob:
            try:
                self._cache.deserialize(blob)
            except Exception:  # noqa: BLE001 - corrupt cache should not crash
                logger.warning("delegated token cache was corrupt; discarding")

    def _persist_cache(self) -> None:
        if self._cache.has_state_changed:
            file_cache.save(
                self._tenant_id,
                self._client_id,
                self._cache.serialize(),
                cache_dir=self._cache_dir,
            )

    # ---- interface ------------------------------------------------------

    def _cached_accounts(self) -> list[dict[str, Any]]:
        """Read accounts straight from the serialized cache (no network)."""
        accounts = self._cache.find(msal.TokenCache.CredentialType.ACCOUNT)
        return list(accounts) if accounts else []

    def is_logged_in(self) -> bool:
        return bool(self._cached_accounts())

    def account_username(self) -> str | None:
        accounts = self._cached_accounts()
        if not accounts:
            return None
        return str(accounts[0].get("username") or "")

    def clear_cache(self) -> None:
        file_cache.clear(
            self._tenant_id, self._client_id, cache_dir=self._cache_dir
        )
        # Drop the in-memory MSAL state so subsequent calls reflect logout.
        # The MSAL app is rebuilt lazily on next use.
        self._cache = msal.SerializableTokenCache()
        self._app = None
        self._token_cache.clear()

    async def start_device_flow(
        self, scopes: list[str] | None = None
    ) -> dict[str, Any]:
        """Initiate device-code flow and return the MSAL flow dict.

        The returned dict contains ``user_code``, ``verification_uri``, and
        ``expires_in`` — the caller is responsible for surfacing the code to
        the user and then passing the same dict to :meth:`complete_device_flow`.
        """
        scopes = scopes or list(DELEGATED_LOGIN_SCOPES)
        app = self._get_app()
        flow: dict[str, Any] = await asyncio.to_thread(
            app.initiate_device_flow, scopes=scopes
        )
        if "user_code" not in flow:
            err = flow.get("error_description") or flow.get("error") or "unknown error"
            raise AuthError(f"device flow initiation failed: {err}")
        return flow

    async def complete_device_flow(self, flow: dict[str, Any]) -> dict[str, Any]:
        """Block until the user completes the flow (or it times out), persist
        the refresh token to the encrypted cache, and return the token result.
        """
        app = self._get_app()
        result: dict[str, Any] = await asyncio.to_thread(
            app.acquire_token_by_device_flow, flow
        )
        if "access_token" not in result:
            err = result.get("error_description") or result.get("error") or "unknown error"
            raise AuthError(f"device flow did not yield a token: {err}")
        self._persist_cache()
        return result

    async def login(
        self,
        *,
        scopes: list[str] | None = None,
        on_prompt: DeviceFlowCallback | None = None,
    ) -> dict[str, Any]:
        """Run device-code flow, persist refresh token to keyring, return token result."""
        flow = await self.start_device_flow(scopes=scopes)
        if on_prompt is not None:
            await asyncio.to_thread(on_prompt, flow)
        return await self.complete_device_flow(flow)

    async def get_token(self, scope: str = GRAPH_DEFAULT_SCOPE) -> str:
        async with self._lock:
            cached = self._token_cache.get(scope)
            if cached and cached.expires_at - 60 > time.time():
                return cached.value
            return await asyncio.to_thread(self._silent_blocking, scope)

    def _silent_blocking(self, scope: str) -> str:
        if not self._cached_accounts():
            raise AuthError(
                "no delegated session — run `mcp-scan login` first"
            )
        app = self._get_app()
        accounts = app.get_accounts()
        if not accounts:
            raise AuthError(
                "no delegated session — run `mcp-scan login` first"
            )
        result: dict[str, Any] | None = app.acquire_token_silent(
            scopes=[scope], account=accounts[0]
        )
        if not result or "access_token" not in result:
            err = (
                (result or {}).get("error_description")
                or (result or {}).get("error")
                or "silent token acquisition returned no token"
            )
            raise AuthError(
                f"delegated token refresh failed for {scope}: {err}; "
                "run `mcp-scan login` to re-authenticate"
            )
        access = str(result["access_token"])
        expires_in = int(result.get("expires_in", 3600))
        self._token_cache[scope] = _CachedToken(
            value=access, expires_at=time.time() + expires_in
        )
        self._persist_cache()
        return access


