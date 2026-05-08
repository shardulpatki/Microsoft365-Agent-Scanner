from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from m365_mcp_scanner.auth import AppOnlyTokenProvider
from m365_mcp_scanner.clients.graph import GraphClient
from m365_mcp_scanner.config import Settings
from m365_mcp_scanner.discovery import (
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
    "declarative",
    "custom_connectors",
    "federated_copilot_connectors",
}

PHASE_1_SCOPES = {"synced_copilot_connectors", "first_party_mcp"}

# Surface aliases — accept legacy/shorthand names without breaking the contract.
SCOPE_ALIASES: dict[str, str] = {
    "copilot_connectors": "synced_copilot_connectors",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def run_pipeline(scope: list[str], settings: Settings) -> ScanDocument:
    started = _utcnow()
    scan_id = compute_scan_id()
    raw = list(scope) if scope else ["synced_copilot_connectors", "first_party_mcp"]
    requested = [SCOPE_ALIASES.get(s, s) for s in raw]

    doc = ScanDocument(
        scan_id=scan_id,
        tenant_id=settings.tenant_id,
        started_at=started,
        status=ScanStatus.running,
        scope=requested,
        config_snapshot=settings.snapshot(),
        stages={
            "discover": StageStatus(),
            "resolve": StageStatus(skipped=True, reason="not implemented in Phase 1"),
            "enrich": StageStatus(skipped=True, reason="not implemented in Phase 1"),
            "score": StageStatus(skipped=True, reason="not implemented in Phase 1"),
        },
        summary=ScanSummary(),
    )

    unknown = [s for s in requested if s not in KNOWN_SCOPES]
    for s in unknown:
        doc.errors.append(
            ScanError(
                stage="discover",
                surface=s,
                message=f"unknown scope '{s}' — ignored",
                timestamp=_utcnow(),
            )
        )

    deferred = [s for s in requested if s in KNOWN_SCOPES and s not in PHASE_1_SCOPES]
    for s in deferred:
        doc.errors.append(
            ScanError(
                stage="discover",
                surface=s,
                message=f"surface '{s}' is not implemented in Phase 1; skipping",
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

    if discoverers:
        token_provider = AppOnlyTokenProvider(
            tenant_id=settings.tenant_id,
            client_id=settings.client_id,
            client_secret=settings.client_secret.get_secret_value(),
        )
        async with GraphClient(token_provider) as graph:
            ctx = DiscoveryContext(graph=graph, tenant_id=settings.tenant_id)
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
                doc.errors.extend(result.errors)
                discover.errors.extend(
                    {"surface": e.surface, "message": e.message} for e in result.errors
                )

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
