from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from m365_mcp_scanner.auth.token_provider import TokenProvider
from m365_mcp_scanner.clients.graph import GraphClient
from m365_mcp_scanner.config import Settings
from m365_mcp_scanner.discovery import (
    DiscoveryContext,
    FirstPartyMcpDiscoverer,
    SyncedCopilotConnectorsDiscoverer,
)
from m365_mcp_scanner.models import ScanDocument, ScanStatus
from m365_mcp_scanner.models.enums import AuthType, Transport
from m365_mcp_scanner.orchestrator import run_pipeline
from m365_mcp_scanner.storage import (
    ensure_data_dir,
    load_scan,
    scan_dir,
    scan_filename,
    update_latest_pointer,
    write_scan_document,
)
from m365_mcp_scanner.storage.json_store import resolve_latest

FIXTURES = Path(__file__).parent / "fixtures"
EXTERNAL_CONNECTIONS = FIXTURES / "external_connections_response.json"
SERVICE_PRINCIPAL = FIXTURES / "service_principal_response.json"


class StaticTokenProvider:
    def __init__(self, token: str = "test-token") -> None:
        self._token = token

    async def get_token(self, scope: str) -> str:  # noqa: ARG002
        return self._token


@pytest.fixture
def graph_response() -> dict:
    return json.loads(EXTERNAL_CONNECTIONS.read_text())


@pytest.fixture
def sp_response() -> dict:
    return json.loads(SERVICE_PRINCIPAL.read_text())


@pytest.mark.asyncio
async def test_synced_discoverer_maps_connections(graph_response: dict) -> None:
    with respx.mock(base_url="https://graph.microsoft.com/v1.0") as router:
        router.get("/external/connections").mock(
            return_value=httpx.Response(200, json=graph_response)
        )
        provider: TokenProvider = StaticTokenProvider()
        async with GraphClient(provider) as client:
            ctx = DiscoveryContext(graph=client, tenant_id="tenant")
            result = await SyncedCopilotConnectorsDiscoverer().discover(ctx)

    assert len(result.mcp_servers) == 3
    assert all(s.transport is Transport.copilot_connector for s in result.mcp_servers)
    assert all(s.discovered_via == "synced_copilot_connectors" for s in result.mcp_servers)


@pytest.mark.asyncio
async def test_first_party_discoverer_finds_known_app(sp_response: dict) -> None:
    with respx.mock(base_url="https://graph.microsoft.com/v1.0") as router:
        router.get("/servicePrincipals").mock(
            return_value=httpx.Response(200, json=sp_response)
        )
        provider: TokenProvider = StaticTokenProvider()
        async with GraphClient(provider) as client:
            ctx = DiscoveryContext(graph=client, tenant_id="tenant")
            result = await FirstPartyMcpDiscoverer().discover(ctx)

    assert len(result.mcp_servers) == 1
    server = result.mcp_servers[0]
    assert server.is_first_party is True
    assert server.transport is Transport.streamable_http
    assert server.auth_type is AuthType.oauth2_static
    assert server.evidence["app_id"] == "e8c77dc2-69b3-43f4-bc51-3213c9d915b4"


@pytest.mark.asyncio
async def test_first_party_discoverer_handles_absent_app() -> None:
    empty: dict = {"value": []}
    with respx.mock(base_url="https://graph.microsoft.com/v1.0") as router:
        router.get("/servicePrincipals").mock(
            return_value=httpx.Response(200, json=empty)
        )
        provider: TokenProvider = StaticTokenProvider()
        async with GraphClient(provider) as client:
            ctx = DiscoveryContext(graph=client, tenant_id="tenant")
            result = await FirstPartyMcpDiscoverer().discover(ctx)

    assert result.mcp_servers == []
    assert result.errors == []


@pytest.mark.asyncio
async def test_pipeline_runs_both_phase1_discoverers(
    tmp_path: Path,
    graph_response: dict,
    sp_response: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("M365_MCP_TENANT_ID", "tenant-id")
    monkeypatch.setenv("M365_MCP_CLIENT_ID", "client-id")
    monkeypatch.setenv("M365_MCP_CLIENT_SECRET", "secret")
    monkeypatch.setenv("M365_MCP_DATA_DIR", str(tmp_path))

    settings = Settings(data_dir=tmp_path)

    from m365_mcp_scanner.orchestrator import pipeline as pipeline_mod

    monkeypatch.setattr(
        pipeline_mod, "AppOnlyTokenProvider", lambda **_: StaticTokenProvider()
    )

    with respx.mock(base_url="https://graph.microsoft.com/v1.0") as router:
        router.get("/external/connections").mock(
            return_value=httpx.Response(200, json=graph_response)
        )
        router.get("/servicePrincipals").mock(
            return_value=httpx.Response(200, json=sp_response)
        )
        doc = await run_pipeline(
            ["synced_copilot_connectors", "first_party_mcp"], settings
        )

    assert doc.status is ScanStatus.completed
    assert doc.summary.mcp_servers_total == 4
    assert doc.summary.mcp_servers_first_party == 4
    assert doc.stages["resolve"].skipped is True

    ensure_data_dir(tmp_path)
    target = scan_dir(tmp_path) / scan_filename(doc.started_at, doc.scan_id)
    write_scan_document(doc, target)
    update_latest_pointer(target, tmp_path)
    loaded = load_scan(target)
    assert isinstance(loaded, ScanDocument)
    assert resolve_latest(tmp_path) is not None


@pytest.mark.asyncio
async def test_pipeline_accepts_legacy_alias(
    tmp_path: Path,
    graph_response: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("M365_MCP_TENANT_ID", "tenant-id")
    monkeypatch.setenv("M365_MCP_CLIENT_ID", "client-id")
    monkeypatch.setenv("M365_MCP_CLIENT_SECRET", "secret")
    monkeypatch.setenv("M365_MCP_DATA_DIR", str(tmp_path))
    settings = Settings(data_dir=tmp_path)

    from m365_mcp_scanner.orchestrator import pipeline as pipeline_mod

    monkeypatch.setattr(
        pipeline_mod, "AppOnlyTokenProvider", lambda **_: StaticTokenProvider()
    )

    with respx.mock(base_url="https://graph.microsoft.com/v1.0") as router:
        router.get("/external/connections").mock(
            return_value=httpx.Response(200, json=graph_response)
        )
        doc = await run_pipeline(["copilot_connectors"], settings)

    assert "synced_copilot_connectors" in doc.scope
    assert doc.summary.mcp_servers_total == 3
