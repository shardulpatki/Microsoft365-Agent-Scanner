from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Consent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_id: str
    server_id: str
    client_id: str
    principal_id: str | None = None
    scopes: list[str]
    consent_type: str
