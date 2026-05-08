from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ActivityRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_id: str
    agent_id: str | None = None
    timestamp: datetime
    user_id: str | None = None
    operation: str
    success: bool
