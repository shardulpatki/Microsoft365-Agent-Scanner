"""Per-environment async client for Dataverse Web API.

Each Power Platform environment with Dataverse has its own org URL like
``https://contoso.crm.dynamics.com`` and requires an audience-scoped AAD
token (``<org_url>/.default``). This client lazily constructs one
:class:`BaseAsyncClient` per org URL it sees, sharing the same underlying
:class:`TokenProvider` (which caches tokens per scope).

The scanner SP must be added as an **application user** with a sufficient
security role to each env's Dataverse. Envs where it is not added return
401/403; we translate those to :class:`DataverseAccessDeniedError` so the
discoverer can record a per-env scan error and continue.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import httpx

from m365_mcp_scanner.auth.msal_broker import dataverse_scope
from m365_mcp_scanner.auth.token_provider import TokenProvider
from m365_mcp_scanner.clients.api_recorder import ApiCallRecorder
from m365_mcp_scanner.clients.base import BaseAsyncClient
from m365_mcp_scanner.clients.exceptions import DataverseAccessDeniedError

logger = logging.getLogger(__name__)

API_PREFIX = "/api/data/v9.2"

BOT_SELECT = (
    "botid,name,createdon,modifiedon,_ownerid_value,"
    "authenticationmode,ismanaged,configuration"
)
BOTCOMPONENT_SELECT = (
    "botcomponentid,name,componenttype,description,content,data"
)


class DataverseClient:
    """Coordinator that owns one :class:`BaseAsyncClient` per org URL."""

    def __init__(
        self,
        token_provider: TokenProvider,
        *,
        recorder: ApiCallRecorder | None = None,
        rate: float = 10.0,
        burst: float = 20.0,
    ) -> None:
        self._token_provider = token_provider
        self._recorder = recorder
        self._rate = rate
        self._burst = burst
        self._by_org: dict[str, BaseAsyncClient] = {}

    def _client_for(self, org_url: str) -> BaseAsyncClient:
        key = org_url.rstrip("/")
        client = self._by_org.get(key)
        if client is None:
            host = urlparse(key).hostname or key
            client = BaseAsyncClient(
                token_provider=self._token_provider,
                scope=dataverse_scope(key),
                base_url=key,
                rate=self._rate,
                burst=self._burst,
                recorder=self._recorder,
                client_name=f"dataverse[{host}]",
            )
            self._by_org[key] = client
        return client

    async def aclose(self) -> None:
        for client in self._by_org.values():
            await client.aclose()
        self._by_org.clear()

    async def _paginate(
        self,
        org_url: str,
        env_id: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        client = self._client_for(org_url)
        next_url: str | None = path
        next_params = params
        while next_url:
            resp = await client.request("GET", next_url, params=next_params)
            if resp.status_code in (401, 403):
                raise DataverseAccessDeniedError(
                    env_id=env_id, org_url=org_url, status_code=resp.status_code
                )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"Dataverse {resp.status_code} on {next_url}: {resp.text[:300]}"
                ) from exc
            payload = resp.json()
            for item in payload.get("value", []):
                yield item
            nxt = payload.get("@odata.nextLink")
            if not nxt:
                return
            next_url = nxt
            next_params = None

    async def list_bots(
        self, org_url: str, env_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield each row from the ``bots`` table (one row per Copilot Studio agent)."""
        async for row in self._paginate(
            org_url,
            env_id,
            f"{API_PREFIX}/bots",
            params={"$select": BOT_SELECT},
        ):
            yield row

    async def list_botcomponents_for_bot(
        self, org_url: str, env_id: str, botid: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield each ``botcomponents`` row whose ``_parentbotid_value`` matches."""
        async for row in self._paginate(
            org_url,
            env_id,
            f"{API_PREFIX}/botcomponents",
            params={
                "$select": BOTCOMPONENT_SELECT,
                "$filter": f"_parentbotid_value eq {botid}",
            },
        ):
            yield row

    async def get_connection_reference(
        self, org_url: str, env_id: str, logical_name: str
    ) -> dict[str, Any] | None:
        """Look up a connection reference by its logical name. Returns first match or None."""
        # OData string literal — wrap the value in single quotes and escape any.
        escaped = logical_name.replace("'", "''")
        client = self._client_for(org_url)
        resp = await client.request(
            "GET",
            f"{API_PREFIX}/connectionreferences",
            params={
                "$filter": f"connectionreferencelogicalname eq '{escaped}'",
                "$select": "connectionreferenceid,connectionreferencelogicalname,connectorid",
            },
        )
        if resp.status_code in (401, 403):
            raise DataverseAccessDeniedError(
                env_id=env_id, org_url=org_url, status_code=resp.status_code
            )
        if resp.status_code >= 400:
            logger.warning(
                "dataverse: get_connection_reference status=%s body=%s",
                resp.status_code,
                resp.text[:300],
            )
            return None
        rows = resp.json().get("value", [])
        if not rows:
            return None
        first = rows[0]
        return first if isinstance(first, dict) else None
