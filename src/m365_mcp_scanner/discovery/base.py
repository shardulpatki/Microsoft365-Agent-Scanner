from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from m365_mcp_scanner.clients.graph import GraphClient
from m365_mcp_scanner.models import NormalizedAgent, NormalizedMcpServer, ScanError


@dataclass
class DiscoveryContext:
    graph: GraphClient
    tenant_id: str


@dataclass
class DiscoveryResult:
    surface: str
    mcp_servers: list[NormalizedMcpServer] = field(default_factory=list)
    agents: list[NormalizedAgent] = field(default_factory=list)
    errors: list[ScanError] = field(default_factory=list)


class Discoverer(Protocol):
    surface: str

    async def discover(self, ctx: DiscoveryContext) -> DiscoveryResult: ...
