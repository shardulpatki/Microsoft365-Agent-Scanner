from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from m365_mcp_scanner.discovery.base import DiscoveryContext, DiscoveryResult
from m365_mcp_scanner.models import NormalizedMcpServer, ScanError
from m365_mcp_scanner.models.enums import AuthType, Transport
from m365_mcp_scanner.models.ids import compute_server_id

logger = logging.getLogger(__name__)


class SyncedCopilotConnectorsDiscoverer:
    """Maps Graph /external/connections rows to NormalizedMcpServer entries.

    These are the *synced* Copilot Connectors — Microsoft Graph connectors that
    index external content into the tenant's search graph. They differ from the
    *federated* MCP-backed connectors visible in the admin center (LSEG, Moody's,
    etc.), which currently have no public Graph API and are tracked as a Phase 6
    TODO blocked on Microsoft.

    The /external/connections resource does not surface a remote URL on the row,
    so we use a synthetic m365:// identity so server_id stays stable across scans.
    """

    surface = "synced_copilot_connectors"

    async def discover(self, ctx: DiscoveryContext) -> DiscoveryResult:
        result = DiscoveryResult(surface=self.surface)
        try:
            async for row in ctx.graph.list_external_connections():
                server = self._row_to_server(row)
                if server is not None:
                    result.mcp_servers.append(server)
        except Exception as exc:  # noqa: BLE001 - surface-level isolation
            logger.exception("synced_copilot_connectors discovery failed")
            result.errors.append(
                ScanError(
                    stage="discover",
                    surface=self.surface,
                    message=f"{type(exc).__name__}: {exc}",
                    timestamp=_utcnow(),
                )
            )
        return result

    @staticmethod
    def _row_to_server(row: dict[str, Any]) -> NormalizedMcpServer | None:
        connection_id = row.get("id")
        if not isinstance(connection_id, str) or not connection_id:
            logger.warning("skipping external_connection row with missing id: %r", row)
            return None
        display_name = row.get("name") or connection_id
        state = row.get("state")
        configuration = row.get("configuration") or {}
        auth_type = AuthType.managed
        url = f"m365://external-connections/{connection_id}"
        evidence: dict[str, object] = {
            "connection_id": connection_id,
            "display_name": display_name,
            "connection_state": state,
            "microsoft_enabled": True,
        }
        if isinstance(configuration, dict):
            authorized_apps = configuration.get("authorizedAppIds")
            if authorized_apps is not None:
                evidence["authorized_app_ids"] = authorized_apps
        return NormalizedMcpServer(
            server_id=compute_server_id(url, auth_type.value),
            url=url,
            transport=Transport.copilot_connector,
            auth_type=auth_type,
            is_first_party=True,
            external_domain=False,
            advertised_tools=None,
            discovered_via="synced_copilot_connectors",
            discovered_at=_utcnow(),
            evidence=evidence,
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
