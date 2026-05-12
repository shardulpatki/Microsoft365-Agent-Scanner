from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from m365_mcp_scanner.clients.graph import GraphClient
from m365_mcp_scanner.models import NormalizedAgent, NormalizedMcpServer, ScanError
from m365_mcp_scanner.models.consumption import ConsumptionEdge

if TYPE_CHECKING:
    from m365_mcp_scanner.auth.token_provider import TokenProvider
    from m365_mcp_scanner.clients.api_recorder import ApiCallRecorder
    from m365_mcp_scanner.clients.power_platform_admin import PowerPlatformAdminClient


@dataclass
class DiscoveryContext:
    graph: GraphClient
    tenant_id: str
    power_platform: "PowerPlatformAdminClient | None" = None
    delegated_graph: GraphClient | None = None
    token_provider: "TokenProvider | None" = None
    recorder: "ApiCallRecorder | None" = None


@dataclass
class DiscoveryResult:
    surface: str
    mcp_servers: list[NormalizedMcpServer] = field(default_factory=list)
    agents: list[NormalizedAgent] = field(default_factory=list)
    consumption_edges: list[ConsumptionEdge] = field(default_factory=list)
    errors: list[ScanError] = field(default_factory=list)


class Discoverer(Protocol):
    surface: str

    async def discover(self, ctx: DiscoveryContext) -> DiscoveryResult: ...
