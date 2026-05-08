from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from m365_mcp_scanner.models.enums import AuthType, Transport


class NormalizedMcpServer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_id: str
    url: str
    transport: Transport
    auth_type: AuthType
    is_first_party: bool
    external_domain: bool
    advertised_tools: list[str] | None = None
    discovered_via: str
    discovered_at: datetime
    evidence: dict[str, object] = Field(default_factory=dict)
