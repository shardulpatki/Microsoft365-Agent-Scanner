from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from m365_mcp_scanner.auth.msal_broker import GRAPH_DEFAULT_SCOPE
from m365_mcp_scanner.auth.token_provider import TokenProvider
from m365_mcp_scanner.clients.api_recorder import ApiCallRecorder
from m365_mcp_scanner.clients.base import BaseAsyncClient
from m365_mcp_scanner.clients.exceptions import (
    ForbiddenError,
    ManifestNotAvailableError,
    PermissionMissingError,
    ReauthRequiredError,
    TenantNotEligibleError,
)

GRAPH_V1 = "https://graph.microsoft.com/v1.0"
GRAPH_BETA = "https://graph.microsoft.com/beta"

# Substrings that distinguish a Frontier/eligibility 403 from a generic
# permission-missing 403. The exact wording has shifted over preview releases,
# so we look for any of these markers (case-insensitive).
_TENANT_INELIGIBLE_MARKERS: tuple[str, ...] = (
    "frontier",
    "not eligible",
    "is not enabled for this tenant",
    "tenant is not enrolled",
    "preview is not available",
)

_LICENSE_MARKERS: tuple[str, ...] = (
    "licensed for agent 365",
    "agent 365 license",
    "frontier preview",
    "early access preview",
    "not eligible",
    "not enrolled in",
    "preview program",
    "frontier",
    "is not enabled for this tenant",
    "tenant is not enrolled",
    "preview is not available",
)

_PERMISSION_MARKERS: tuple[str, ...] = (
    "insufficient privileges",
    "permission",
    "scope",
    "consent",
    "authorization_requestdenied",
)


def _categorize_graph_error(resp: httpx.Response) -> Exception:
    """Map a non-2xx Graph response to a typed exception."""
    body = ""
    try:
        body = resp.text
    except Exception:  # noqa: BLE001 - body decode best-effort only
        body = ""
    snippet = body[:500]
    if resp.status_code == 401:
        return ReauthRequiredError(
            f"401 Unauthorized from {resp.request.url.path}: {snippet}"
        )
    if resp.status_code == 403:
        lowered = body.lower()
        if any(marker in lowered for marker in _TENANT_INELIGIBLE_MARKERS):
            return TenantNotEligibleError(
                f"403 Forbidden (tenant not eligible) from {resp.request.url.path}: {snippet}"
            )
        return PermissionMissingError(
            f"403 Forbidden (permission missing) from {resp.request.url.path}: {snippet}"
        )
    return httpx.HTTPStatusError(
        f"{resp.status_code} from {resp.request.url.path}: {snippet}",
        request=resp.request,
        response=resp,
    )


def _categorize_phase3_403(resp: httpx.Response, *, surface_label: str) -> Exception:
    """Phase 3 admin-endpoint categorizer.

    Differentiates 403s into license/permission/forbidden by inspecting
    ``error.message`` from the JSON body when present, falling back to the
    raw text for substring matching. Non-403 statuses delegate to the
    shared categorizer.
    """
    if resp.status_code != 403:
        return _categorize_graph_error(resp)

    body = ""
    try:
        body = resp.text
    except Exception:  # noqa: BLE001 - body decode best-effort only
        body = ""

    error_message = ""
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, str):
                error_message = msg

    match_text = (error_message or body).lower()
    path = resp.request.url.path

    if any(marker in match_text for marker in _LICENSE_MARKERS):
        detail = error_message or body
        return TenantNotEligibleError(
            f"tenant not eligible for {surface_label}: {detail}"
        )
    if any(marker in match_text for marker in _PERMISSION_MARKERS):
        return PermissionMissingError(
            f"403 Forbidden (permission missing) from {path}: {body}"
        )
    return ForbiddenError(f"403 Forbidden from {path}: {body}")


class GraphClient(BaseAsyncClient):
    def __init__(
        self,
        token_provider: TokenProvider,
        *,
        recorder: ApiCallRecorder | None = None,
        client_name: str = "graph",
    ) -> None:
        super().__init__(
            token_provider=token_provider,
            scope=GRAPH_DEFAULT_SCOPE,
            base_url=GRAPH_V1,
            rate=10.0,
            burst=20.0,
            recorder=recorder,
            client_name=client_name,
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

    # --- Phase 3: declarative agents ------------------------------------

    async def list_copilot_packages(self) -> AsyncIterator[dict[str, Any]]:
        """Iterate Copilot Packages from the Frontier-gated admin catalog.

        Requires delegated ``CopilotPackages.Read.All``. Most tenants get 403
        regardless of consent because the API is not yet generally available.
        """
        url = f"{GRAPH_BETA}/copilot/admin/catalog/packages"
        resp = await self.request("GET", url)
        if resp.status_code != 200:
            raise _categorize_phase3_403(resp, surface_label="Copilot Packages API")
        payload = resp.json()
        for item in payload.get("value", []):
            yield item
        next_link: str | None = payload.get("@odata.nextLink")
        while next_link:
            resp = await self.request("GET", next_link)
            if resp.status_code != 200:
                raise _categorize_phase3_403(resp, surface_label="Copilot Packages API")
            payload = resp.json()
            for item in payload.get("value", []):
                yield item
            next_link = payload.get("@odata.nextLink")

    async def get_copilot_package(self, package_id: str) -> dict[str, Any]:
        url = f"{GRAPH_BETA}/copilot/admin/catalog/packages/{package_id}"
        resp = await self.request("GET", url)
        if resp.status_code != 200:
            raise _categorize_phase3_403(resp, surface_label="Copilot Packages API")
        data: dict[str, Any] = resp.json()
        return data

    async def list_teams_app_catalog(
        self, *, distribution_method: str = "organization"
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate Teams apps in the tenant catalog with their appDefinitions expanded.

        Requires delegated ``TeamsApp.Read.All`` or ``Directory.Read.All``.
        Returns the raw payload rows; each row carries ``appDefinitions`` so
        callers can identify the latest definition without a second round-trip.
        """
        params = {
            "$filter": f"distributionMethod eq '{distribution_method}'",
            "$expand": "appDefinitions",
        }
        url = "/appCatalogs/teamsApps"
        resp = await self.request("GET", url, params=params)
        if resp.status_code != 200:
            raise _categorize_phase3_403(resp, surface_label="Teams App Catalog")
        payload = resp.json()
        for item in payload.get("value", []):
            yield item
        next_link: str | None = payload.get("@odata.nextLink")
        while next_link:
            resp = await self.request("GET", next_link)
            if resp.status_code != 200:
                raise _categorize_phase3_403(resp, surface_label="Teams App Catalog")
            payload = resp.json()
            for item in payload.get("value", []):
                yield item
            next_link = payload.get("@odata.nextLink")

    async def get_teams_app_manifest(
        self, app_id: str, app_definition_id: str
    ) -> bytes:
        """Fetch the raw manifest for a specific Teams app definition.

        The endpoint returns either a zipped package or raw JSON depending on
        the app version; callers are expected to parse defensively.
        """
        url = f"/appCatalogs/teamsApps/{app_id}/appDefinitions/{app_definition_id}/manifest"
        resp = await self.request("GET", url)
        if resp.status_code == 400:
            body = ""
            try:
                body = resp.text
            except Exception:  # noqa: BLE001 - body decode best-effort only
                body = ""
            if "resource not found for the segment 'manifest'" in body.lower():
                raise ManifestNotAvailableError(app_id, app_definition_id, body)
        if resp.status_code != 200:
            raise _categorize_phase3_403(resp, surface_label="Teams App Catalog")
        return resp.content

    # --- Phase 4 stub ----------------------------------------------------

    async def list_oauth2_permission_grants(
        self, *, client_id: str
    ) -> AsyncIterator[dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError("consent enrichment lands in Phase 4")
        yield {}  # pragma: no cover - for type checker

    async def doctor_ping(self) -> tuple[bool, str]:
        """Quick reachability + permission check used by `mcp-scan doctor`.

        Pings ``/v1.0/applications?$top=1`` — the most representative Graph
        operation the scanner relies on, gated by ``Application.Read.All``
        which is one of the scanner's core granted permissions.
        """
        try:
            resp = await self.request(
                "GET", "/applications", params={"$top": 1}
            )
        except httpx.HTTPError as exc:
            return False, f"transport error: {exc}"
        if resp.status_code == 200:
            count = len(resp.json().get("value", []))
            return True, f"Graph reachable; /applications returned 200 ({count} sample row(s))"
        if resp.status_code == 403:
            return False, "403 Forbidden — Application.Read.All not granted to the scanner app"
        if resp.status_code == 401:
            return False, "401 Unauthorized — check tenant_id / client_id / client_secret"
        return False, f"unexpected status {resp.status_code}: {resp.text[:200]}"
