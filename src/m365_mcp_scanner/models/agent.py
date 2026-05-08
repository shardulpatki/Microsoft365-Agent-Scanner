from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from m365_mcp_scanner.models.enums import AgentPath


class NormalizedAgent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    path: AgentPath
    display_name: str
    owner_id: str | None = None
    environment_id: str | None = None
    published: bool = False
    source_ref: dict[str, object] = Field(default_factory=dict)
