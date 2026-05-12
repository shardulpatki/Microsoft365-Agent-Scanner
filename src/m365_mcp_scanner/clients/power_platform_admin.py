"""Async client for Power Platform admin APIs.

Two host bases are involved:

- ``api.bap.microsoft.com`` — Business Application Platform admin: environments
- ``api.powerapps.com`` — Power Apps admin: connectors (custom connector defs)

Both accept the same Power Platform admin token
(``https://service.powerapps.com/.default``) when the scanner's Entra app
service principal has been registered as a Power Platform management app via
``New-PowerAppManagementApp -ApplicationId <client-id>``.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from m365_mcp_scanner.auth.msal_broker import POWER_PLATFORM_DEFAULT_SCOPE
from m365_mcp_scanner.auth.token_provider import TokenProvider
from m365_mcp_scanner.clients.api_recorder import ApiCallRecorder
from m365_mcp_scanner.clients.base import BaseAsyncClient

logger = logging.getLogger(__name__)

BAP_HOST = "https://api.bap.microsoft.com"
PAPI_HOST = "https://api.powerapps.com"

ENV_API_VERSION = "2020-10-01"
PAPI_API_VERSION = "2016-11-01"


class PowerPlatformAdminClient:
    """Wraps two BaseAsyncClient instances (one per host) sharing one token scope."""

    def __init__(
        self,
        token_provider: TokenProvider,
        *,
        rate: float = 10.0,
        burst: float = 20.0,
        recorder: ApiCallRecorder | None = None,
    ) -> None:
        self._bap = BaseAsyncClient(
            token_provider=token_provider,
            scope=POWER_PLATFORM_DEFAULT_SCOPE,
            base_url=BAP_HOST,
            rate=rate,
            burst=burst,
            recorder=recorder,
            client_name="power_platform_bap",
        )
        self._papi = BaseAsyncClient(
            token_provider=token_provider,
            scope=POWER_PLATFORM_DEFAULT_SCOPE,
            base_url=PAPI_HOST,
            rate=rate,
            burst=burst,
            recorder=recorder,
            client_name="power_platform_papi",
        )

    async def aclose(self) -> None:
        await self._bap.aclose()
        await self._papi.aclose()

    async def list_environments(self) -> AsyncIterator[dict[str, Any]]:
        """Yield each Power Platform environment visible to the admin app.

        Expands ``linkedEnvironmentMetadata`` so callers can read the
        Dataverse org URL (``properties.linkedEnvironmentMetadata.instanceApiUrl``)
        in the same call. Envs without a linked Dataverse omit that block.
        """
        async for row in self._bap.paginate(
            "/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments",
            params={
                "api-version": ENV_API_VERSION,
                "$expand": "properties/linkedEnvironmentMetadata",
            },
        ):
            yield row

    async def list_connectors(self, environment_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield **custom** connector definitions in a given environment.

        Uses the env-scoped admin API with ``isCustomApi eq 'True'`` filter so
        we don't iterate the thousands of built-in/standard connectors that
        every env inherits — those are pre-baked OAuth integrations (Zoho,
        SharePoint, …), not tenant-defined MCP servers.

        The OpenAPI spec for each connector is surfaced as a pre-signed Azure
        Blob URL on ``properties.apiDefinitions.originalSwaggerUrl`` — the
        admin API does not support per-connector GET with ``$expand=swagger``
        (returns 405), so the discoverer fetches the signed URL directly.
        """
        async for row in self._papi.paginate(
            f"/providers/Microsoft.PowerApps/scopes/admin/environments/{environment_id}/apis",
            params={
                "api-version": PAPI_API_VERSION,
                "$filter": "isCustomApi eq 'True'",
            },
        ):
            yield row

    async def fetch_swagger_url(self, signed_url: str) -> dict[str, Any] | None:
        """Fetch a Swagger spec from a pre-signed Azure Blob URL.

        Power Apps stores connector OpenAPI specs in Azure Blob Storage and
        surfaces them on the listing as ``apiDefinitions.originalSwaggerUrl``
        — a SAS-signed URL with no auth requirement. We do an unauthenticated
        GET (no Authorization header) and parse the JSON body. Returns
        ``None`` on any error rather than raising.
        """
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(signed_url)
        except httpx.HTTPError as exc:
            logger.warning("failed_to_fetch_swagger_url err=%s", exc)
            return None
        if resp.status_code != 200:
            logger.warning(
                "failed_to_fetch_swagger_url status=%s body=%s",
                resp.status_code,
                resp.text[:200],
            )
            return None
        try:
            payload = resp.json()
        except ValueError as exc:
            logger.warning("swagger url returned non-json: %s", exc)
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    async def doctor_ping(self) -> tuple[bool, str]:
        """Reachability + permissions check used by ``mcp-scan doctor``."""
        try:
            resp = await self._bap.request(
                "GET",
                "/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments",
                params={"api-version": ENV_API_VERSION, "$top": 1},
            )
        except httpx.HTTPError as exc:
            return False, f"transport error: {exc}"
        if resp.status_code == 200:
            count = len(resp.json().get("value", []))
            return (
                True,
                f"Power Platform admin reachable; environments returned 200 ({count} sample row(s))",
            )
        if resp.status_code == 403:
            return (
                False,
                "403 Forbidden — Entra app likely missing Power Platform admin role. "
                "Run: New-PowerAppManagementApp -ApplicationId <client-id>",
            )
        if resp.status_code == 401:
            return False, "401 Unauthorized — Power Platform token rejected"
        return False, f"unexpected status {resp.status_code}: {resp.text[:200]}"
