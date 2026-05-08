from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from m365_mcp_scanner.auth.msal_broker import GRAPH_DEFAULT_SCOPE
from m365_mcp_scanner.auth.token_provider import TokenProvider
from m365_mcp_scanner.clients.base import BaseAsyncClient

GRAPH_V1 = "https://graph.microsoft.com/v1.0"
GRAPH_BETA = "https://graph.microsoft.com/beta"


class GraphClient(BaseAsyncClient):
    def __init__(self, token_provider: TokenProvider) -> None:
        super().__init__(
            token_provider=token_provider,
            scope=GRAPH_DEFAULT_SCOPE,
            base_url=GRAPH_V1,
            rate=10.0,
            burst=20.0,
        )

    async def list_external_connections(self) -> AsyncIterator[dict[str, Any]]:
        async for item in self.paginate("/external/connections"):
            yield item

    async def get_external_connection(self, connection_id: str) -> dict[str, Any]:
        return await self.get_json(f"/external/connections/{connection_id}")

    async def get_external_connection_schema(self, connection_id: str) -> dict[str, Any] | None:
        resp = await self.request("GET", f"/external/connections/{connection_id}/schema")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def get_service_principal_by_app_id(self, app_id: str) -> dict[str, Any] | None:
        """Return the servicePrincipal whose appId matches, or None if absent.

        Requires Graph application permission Application.Read.All.
        """
        resp = await self.request(
            "GET",
            "/servicePrincipals",
            params={"$filter": f"appId eq '{app_id}'", "$top": 1},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("value") or []
        if not rows:
            return None
        first: dict[str, Any] = rows[0]
        return first

    # --- Phase 2+ stubs --------------------------------------------------

    async def list_copilot_packages(self) -> AsyncIterator[dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError("Copilot packages discovery lands in Phase 3")
        yield {}  # pragma: no cover - for type checker

    async def get_copilot_package(self, package_id: str) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError("Copilot packages resolve lands in Phase 3")

    async def list_oauth2_permission_grants(
        self, *, client_id: str
    ) -> AsyncIterator[dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError("consent enrichment lands in Phase 4")
        yield {}  # pragma: no cover - for type checker

    async def doctor_ping(self) -> tuple[bool, str]:
        """Quick reachability + permission check used by `mcp-scan doctor`."""
        try:
            resp = await self.request(
                "GET", "/external/connections", params={"$top": 1}
            )
        except httpx.HTTPError as exc:
            return False, f"transport error: {exc}"
        if resp.status_code == 200:
            count = len(resp.json().get("value", []))
            return True, f"Graph reachable; /external/connections returned 200 ({count} sample row(s))"
        if resp.status_code == 403:
            return False, "403 Forbidden — missing ExternalConnection.Read.All app permission?"
        if resp.status_code == 401:
            return False, "401 Unauthorized — check tenant_id / client_id / client_secret"
        return False, f"unexpected status {resp.status_code}: {resp.text[:200]}"
