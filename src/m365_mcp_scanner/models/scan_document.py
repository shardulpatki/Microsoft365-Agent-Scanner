from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from m365_mcp_scanner.models.activity import ActivityRecord
from m365_mcp_scanner.models.agent import NormalizedAgent
from m365_mcp_scanner.models.consent import Consent
from m365_mcp_scanner.models.consumption import ConsumptionEdge
from m365_mcp_scanner.models.enums import ScanStatus
from m365_mcp_scanner.models.finding import Finding
from m365_mcp_scanner.models.mcp_server import NormalizedMcpServer


class StageStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    skipped: bool = False
    reason: str | None = None
    errors: list[dict[str, object]] = Field(default_factory=list)


class FindingsBySeverity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0
    info: int = 0


class ScanSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agents_total: int = 0
    agents_with_mcp: int = 0
    mcp_servers_total: int = 0
    mcp_servers_first_party: int = 0
    mcp_servers_external: int = 0
    findings_total: int = 0
    findings_by_severity: FindingsBySeverity = Field(default_factory=FindingsBySeverity)


class ScanError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    surface: str | None = None
    message: str
    timestamp: datetime


class ScanDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    scan_id: str
    tenant_id: str
    started_at: datetime
    finished_at: datetime | None = None
    status: ScanStatus
    scope: list[str]
    config_snapshot: dict[str, object] = Field(default_factory=dict)
    stages: dict[str, StageStatus] = Field(default_factory=dict)
    summary: ScanSummary = Field(default_factory=ScanSummary)
    agents: list[NormalizedAgent] = Field(default_factory=list)
    mcp_servers: list[NormalizedMcpServer] = Field(default_factory=list)
    consumption_edges: list[ConsumptionEdge] = Field(default_factory=list)
    consents: list[Consent] = Field(default_factory=list)
    activity: list[ActivityRecord] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    errors: list[ScanError] = Field(default_factory=list)
