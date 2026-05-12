from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from m365_mcp_scanner.auth import AppOnlyTokenProvider, DelegatedTokenProvider
from m365_mcp_scanner.clients.api_recorder import ApiCallRecorder
from m365_mcp_scanner.clients.graph import GraphClient
from m365_mcp_scanner.clients.power_platform_admin import PowerPlatformAdminClient
from m365_mcp_scanner.config import Settings
from m365_mcp_scanner.discovery import (
    CopilotStudioDiscoverer,
    CustomConnectorsDiscoverer,
    DeclarativeAgentsPackagesDiscoverer,
    DeclarativeAgentsTeamsAppDiscoverer,
    DiscoveryContext,
    Discoverer,
    FirstPartyMcpDiscoverer,
    SyncedCopilotConnectorsDiscoverer,
)
from m365_mcp_scanner.models import (
    FindingsBySeverity,
    ScanDocument,
    ScanError,
    ScanStatus,
    ScanSummary,
    StageStatus,
)
from m365_mcp_scanner.models.ids import compute_scan_id

logger = logging.getLogger(__name__)

KNOWN_SCOPES = {
    "synced_copilot_connectors",
    "first_party_mcp",
    "copilot_studio",
    "declarative_agents_packages",
    "declarative_agents_teamsapp",
    "custom_connectors",
    "federated_copilot_connectors",
}

DELEGATED_SCOPES: frozenset[str] = frozenset(
    {"declarative_agents_packages", "declarative_agents_teamsapp"}
)

IMPLEMENTED_SCOPES: frozenset[str] = frozenset(
    {
        "synced_copilot_connectors",
        "first_party_mcp",
        "custom_connectors",
        "copilot_studio",
        "declarative_agents_packages",
        "declarative_agents_teamsapp",
    }
)

DEFAULT_SCOPES: tuple[str, ...] = (
    "synced_copilot_connectors",
    "first_party_mcp",
    "custom_connectors",
    "copilot_studio",
    "declarative_agents_packages",
    "declarative_agents_teamsapp",
)

# Surface aliases — accept legacy/shorthand names without breaking the contract.
SCOPE_ALIASES: dict[str, list[str]] = {
    "copilot_connectors": ["synced_copilot_connectors"],
    # "declarative" shorthand expands to both Phase 3 surfaces.
    "declarative": ["declarative_agents_packages", "declarative_agents_teamsapp"],
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _expand_aliases(raw: list[str]) -> list[str]:
    """Expand alias scope names to their canonical forms; preserve order, dedupe."""
    out: list[str] = []
    seen: set[str] = set()
    for name in raw:
        canonical = SCOPE_ALIASES.get(name, [name])
        for c in canonical:
            if c not in seen:
                out.append(c)
                seen.add(c)
    return out


async def run_pipeline(
    scope: list[str],
    settings: Settings,
    *,
    recorder: ApiCallRecorder | None = None,
) -> ScanDocument:
    started = _utcnow()
    scan_id = compute_scan_id()
    raw = list(scope) if scope else list(DEFAULT_SCOPES)
    requested = _expand_aliases(raw)

    doc = ScanDocument(
        scan_id=scan_id,
        tenant_id=settings.tenant_id,
        started_at=started,
        status=ScanStatus.running,
        scope=requested,
        config_snapshot=settings.snapshot(),
        stages={
            "discover": StageStatus(),
        },
        summary=ScanSummary(),
    )

    unknown = [s for s in requested if s not in KNOWN_SCOPES]
    for s in unknown:
        doc.errors.append(
            ScanError(
                stage="discover",
                surface=s,
                code="unknown_scope",
                message=f"unknown scope '{s}' — ignored",
                timestamp=_utcnow(),
            )
        )
    deferred = [s for s in requested if s in KNOWN_SCOPES and s not in IMPLEMENTED_SCOPES]
    for s in deferred:
        doc.errors.append(
            ScanError(
                stage="discover",
                surface=s,
                code="not_implemented",
                message=f"surface '{s}' is not yet implemented; skipping",
                timestamp=_utcnow(),
            )
        )

    discover = doc.stages["discover"]
    discover.started_at = _utcnow()
    t0 = time.monotonic()

    discoverers: list[Discoverer] = []
    if "synced_copilot_connectors" in requested:
        discoverers.append(SyncedCopilotConnectorsDiscoverer())
    if "first_party_mcp" in requested:
        discoverers.append(FirstPartyMcpDiscoverer())
    if "custom_connectors" in requested:
        discoverers.append(CustomConnectorsDiscoverer())
    if "copilot_studio" in requested:
        discoverers.append(CopilotStudioDiscoverer())
    if "declarative_agents_packages" in requested:
        discoverers.append(DeclarativeAgentsPackagesDiscoverer())
    if "declarative_agents_teamsapp" in requested:
        discoverers.append(DeclarativeAgentsTeamsAppDiscoverer())

    if discoverers:
        token_provider = AppOnlyTokenProvider(
            tenant_id=settings.tenant_id,
            client_id=settings.client_id,
            client_secret=settings.client_secret.get_secret_value(),
        )
        pp_client: PowerPlatformAdminClient | None = None
        if "custom_connectors" in requested or "copilot_studio" in requested:
            pp_client = PowerPlatformAdminClient(
                token_provider=token_provider, recorder=recorder
            )

        delegated_provider: DelegatedTokenProvider | None = None
        delegated_graph: GraphClient | None = None
        wants_delegated = any(s in DELEGATED_SCOPES for s in requested)
        if wants_delegated:
            try:
                delegated_provider = DelegatedTokenProvider(
                    tenant_id=settings.tenant_id,
                    client_id=settings.client_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("delegated provider init failed: %s", exc)
                delegated_provider = None
            if delegated_provider is not None and delegated_provider.is_logged_in():
                delegated_graph = GraphClient(
                    delegated_provider,
                    recorder=recorder,
                    client_name="graph_delegated",
                )

        try:
            async with GraphClient(
                token_provider, recorder=recorder, client_name="graph"
            ) as graph:
                ctx = DiscoveryContext(
                    graph=graph,
                    tenant_id=settings.tenant_id,
                    power_platform=pp_client,
                    delegated_graph=delegated_graph,
                    token_provider=token_provider,
                    recorder=recorder,
                )
                for d in discoverers:
                    try:
                        result = await d.discover(ctx)
                    except Exception as exc:  # noqa: BLE001 - surface isolation
                        logger.exception("discoverer %s crashed", d.surface)
                        doc.errors.append(
                            ScanError(
                                stage="discover",
                                surface=d.surface,
                                message=f"{type(exc).__name__}: {exc}",
                                timestamp=_utcnow(),
                            )
                        )
                        continue
                    doc.mcp_servers.extend(result.mcp_servers)
                    doc.agents.extend(result.agents)
                    doc.consumption_edges.extend(result.consumption_edges)
                    doc.errors.extend(result.errors)
                    discover.errors.extend(
                        {
                            "surface": e.surface,
                            "code": e.code or "unknown",
                            "message": e.message,
                        }
                        for e in result.errors
                    )
        finally:
            if pp_client is not None:
                await pp_client.aclose()
            if delegated_graph is not None:
                await delegated_graph.aclose()

    discover.finished_at = _utcnow()
    discover.duration_ms = int((time.monotonic() - t0) * 1000)

    doc.summary = _build_summary(doc)
    doc.finished_at = _utcnow()
    doc.status = ScanStatus.failed if any(e.stage == "fatal" for e in doc.errors) else ScanStatus.completed
    return doc


def _build_summary(doc: ScanDocument) -> ScanSummary:
    servers = doc.mcp_servers
    sev = FindingsBySeverity()
    for f in doc.findings:
        setattr(sev, f.severity.value, getattr(sev, f.severity.value) + 1)
    return ScanSummary(
        agents_total=len(doc.agents),
        agents_with_mcp=len({e.agent_id for e in doc.consumption_edges}),
        mcp_servers_total=len(servers),
        mcp_servers_first_party=sum(1 for s in servers if s.is_first_party),
        mcp_servers_external=sum(1 for s in servers if s.external_domain),
        findings_total=len(doc.findings),
        findings_by_severity=sev,
    )
