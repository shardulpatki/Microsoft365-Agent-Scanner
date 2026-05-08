from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent tests from picking up the developer's real M365 credentials."""
    for var in (
        "M365_MCP_TENANT_ID",
        "M365_MCP_CLIENT_ID",
        "M365_MCP_CLIENT_SECRET",
        "M365_MCP_DATA_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
