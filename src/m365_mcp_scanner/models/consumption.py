from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from m365_mcp_scanner.models.enums import WiredVia


class ConsumptionEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    server_id: str
    wired_via: WiredVia
    config_evidence: dict[str, object] = Field(default_factory=dict)
