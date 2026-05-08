from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from m365_mcp_scanner.models.enums import Severity


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str
    rule_id: str
    severity: Severity
    agent_id: str | None = None
    server_id: str | None = None
    evidence: dict[str, object] = Field(default_factory=dict)
    remediation: str | None = None
