"""Discover first-party MCP service principals in the tenant.

Microsoft ships a small set of first-party MCP servers (e.g. "MCP Server for
Enterprise"). When the tenant has consented to one of these apps, a
servicePrincipal exists in the directory with a known appId. We enumerate by
appId rather than scanning all SPs — far cheaper and immune to displayName
drift.

Required Graph permission (app-only): Application.Read.All
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from m365_mcp_scanner.discovery.base import DiscoveryContext, DiscoveryResult
from m365_mcp_scanner.models import NormalizedMcpServer, ScanError
from m365_mcp_scanner.models.enums import AuthType, Transport
from m365_mcp_scanner.models.ids import compute_server_id

logger = logging.getLogger(__name__)


# (appId, friendly label). Add new IDs here as Microsoft ships more first-party
# MCP servers. Keep IDs lowercase — Graph normalises but our lookup compares strings.
KNOWN_FIRST_PARTY_MCP_APPS: tuple[tuple[str, str], ...] = (
    ("e8c77dc2-69b3-43f4-bc51-3213c9d915b4", "MCP Server for Enterprise"),
)


class FirstPartyMcpDiscoverer:
    surface = "first_party_mcp"

    async def discover(self, ctx: DiscoveryContext) -> DiscoveryResult:
        result = DiscoveryResult(surface=self.surface)
        for app_id, label in KNOWN_FIRST_PARTY_MCP_APPS:
            try:
                sp = await ctx.graph.get_service_principal_by_app_id(app_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("first_party_mcp lookup failed for %s", app_id)
                result.errors.append(
                    ScanError(
                        stage="discover",
                        surface=self.surface,
                        message=f"{type(exc).__name__} on appId {app_id}: {exc}",
                        timestamp=_utcnow(),
                    )
                )
                continue
            if sp is None:
                logger.info("first-party MCP app %s (%s) not present in tenant", app_id, label)
                continue
            server = self._sp_to_server(sp, label=label)
            if server is not None:
                result.mcp_servers.append(server)
        return result

    @staticmethod
    def _sp_to_server(sp: dict[str, Any], *, label: str) -> NormalizedMcpServer | None:
        app_id = sp.get("appId")
        if not isinstance(app_id, str) or not app_id:
            logger.warning("skipping servicePrincipal row with missing appId: %r", sp)
            return None
        display_name = sp.get("displayName") or label
        url = f"m365://service-principals/{app_id}"
        auth_type = AuthType.oauth2_static
        evidence: dict[str, object] = {
            "app_id": app_id,
            "service_principal_id": sp.get("id"),
            "display_name": display_name,
            "service_principal_type": sp.get("servicePrincipalType"),
            "account_enabled": sp.get("accountEnabled"),
            "first_party_label": label,
        }
        return NormalizedMcpServer(
            server_id=compute_server_id(url, auth_type.value),
            url=url,
            transport=Transport.streamable_http,
            auth_type=auth_type,
            is_first_party=True,
            external_domain=False,
            advertised_tools=None,
            discovered_via="first_party_mcp",
            discovered_at=_utcnow(),
            evidence=evidence,
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
