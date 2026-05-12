from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from m365_mcp_scanner.auth.token_provider import TokenProvider
from m365_mcp_scanner.clients.power_platform_admin import PowerPlatformAdminClient
from m365_mcp_scanner.config import Settings
from m365_mcp_scanner.discovery import CopilotStudioDiscoverer, DiscoveryContext
from m365_mcp_scanner.models import ScanStatus
from m365_mcp_scanner.models.enums import AgentPath, Transport, WiredVia
from m365_mcp_scanner.orchestrator import run_pipeline

FIXTURES = Path(__file__).parent / "fixtures"

ENV_ID = "a787c566-03fd-e1c6-972d-9549daaad71c"
ORG_URL = "https://orgtest.crm.dynamics.com"
DV = "https://orgtest.crm.dynamics.com"
BOT_ID = "b7faf513-8e4d-f111-bec5-70a8a59be151"
CONN_LOGICAL = (
    "cra93_tavilyWebSearchAgent.shared_tavilymcp.f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1"
)


class StaticTokenProvider:
    async def get_token(self, scope: str) -> str:  # noqa: ARG002
        return "test-token"


@pytest.fixture
def envs_with_dv() -> dict[str, Any]:
    return json.loads((FIXTURES / "environments_with_dataverse_response.json").read_text())


@pytest.fixture
def bots_response() -> dict[str, Any]:
    return json.loads((FIXTURES / "bots_response.json").read_text())


@pytest.fixture
def connref_response() -> dict[str, Any]:
    return json.loads((FIXTURES / "connectionreference_mcp.json").read_text())


def _component(comp_id: str, fixture_name: str) -> dict[str, Any]:
    return {
        "botcomponentid": comp_id,
        "name": fixture_name.rsplit(".", 1)[0],
        "componenttype": 9,
        "description": "",
        "content": "",
        "data": (FIXTURES / fixture_name).read_text(),
    }


def _botcomponents_response(*components: dict[str, Any]) -> dict[str, Any]:
    return {
        "@odata.context": f"{DV}/api/data/v9.2/$metadata#botcomponents",
        "value": list(components),
    }


def _wire(
    envs: dict[str, Any],
    *,
    bots: dict[str, Any] | None,
    botcomponents: dict[str, Any] | None,
    connref: dict[str, Any] | None,
    dv_status: int = 200,
    extra_dataverses: dict[str, dict[str, int | dict[str, Any]]] | None = None,
) -> tuple[respx.MockRouter, respx.MockRouter]:
    bap = respx.mock(base_url="https://api.bap.microsoft.com", assert_all_called=False)
    dataverse = respx.mock(base_url=DV, assert_all_called=False)

    bap.get(
        "/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments"
    ).mock(return_value=httpx.Response(200, json=envs))

    if bots is not None:
        dataverse.get("/api/data/v9.2/bots").mock(
            return_value=httpx.Response(dv_status, json=bots if dv_status == 200 else {})
        )
    if botcomponents is not None:
        dataverse.get("/api/data/v9.2/botcomponents").mock(
            return_value=httpx.Response(200, json=botcomponents)
        )
    if connref is not None:
        dataverse.get("/api/data/v9.2/connectionreferences").mock(
            return_value=httpx.Response(200, json=connref)
        )
    return bap, dataverse


@pytest.mark.asyncio
async def test_full_pipeline_tavily(
    envs_with_dv: dict[str, Any],
    bots_response: dict[str, Any],
    connref_response: dict[str, Any],
) -> None:
    components = _botcomponents_response(
        _component("sys-topic-1", "botcomponent_topic_system.yaml"),
        _component("mcp-1", "botcomponent_mcp_tavily.yaml"),
    )
    bap, dv = _wire(
        envs_with_dv,
        bots=bots_response,
        botcomponents=components,
        connref=connref_response,
    )
    with bap, dv:
        provider: TokenProvider = StaticTokenProvider()
        pp = PowerPlatformAdminClient(token_provider=provider)
        try:
            ctx = DiscoveryContext(
                graph=None,  # type: ignore[arg-type]
                tenant_id="t",
                power_platform=pp,
                token_provider=provider,
            )
            result = await CopilotStudioDiscoverer().discover(ctx)
        finally:
            await pp.aclose()

    assert result.errors == []
    assert len(result.agents) == 1
    assert len(result.mcp_servers) == 1
    assert len(result.consumption_edges) == 1

    agent = result.agents[0]
    assert agent.path is AgentPath.copilot_studio
    assert agent.display_name == "Tavily Web Search Agent"
    assert agent.environment_id == ENV_ID

    server = result.mcp_servers[0]
    assert server.transport is Transport.custom_connector
    assert server.discovered_via == "copilot_studio"
    assert (
        server.evidence["connector_id"]
        == "/providers/Microsoft.PowerApps/apis/shared_tavilymcp"
    )
    assert server.evidence["bot_id"] == BOT_ID
    assert server.evidence["environment_id"] == ENV_ID
    assert server.evidence["operation_id"] == "InvokeServer"

    edge = result.consumption_edges[0]
    assert edge.agent_id == agent.agent_id
    assert edge.server_id == server.server_id
    assert edge.wired_via is WiredVia.native_mcp_tool
    assert edge.config_evidence["connection_reference_logical_name"] == CONN_LOGICAL


@pytest.mark.asyncio
async def test_per_env_isolation(
    bots_response: dict[str, Any],
    connref_response: dict[str, Any],
) -> None:
    # Two envs: A returns 401 on bots; B succeeds.
    envs = {
        "value": [
            {
                "id": "/.../envs/env-a",
                "name": "env-a",
                "properties": {
                    "displayName": "env-a",
                    "linkedEnvironmentMetadata": {
                        "instanceApiUrl": "https://orga.crm.dynamics.com"
                    },
                },
            },
            {
                "id": "/.../envs/env-b",
                "name": "env-b",
                "properties": {
                    "displayName": "env-b",
                    "linkedEnvironmentMetadata": {
                        "instanceApiUrl": "https://orgb.crm.dynamics.com"
                    },
                },
            },
        ]
    }
    components = _botcomponents_response(
        _component("mcp-1", "botcomponent_mcp_tavily.yaml")
    )

    bap = respx.mock(base_url="https://api.bap.microsoft.com", assert_all_called=False)
    bap.get(
        "/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments"
    ).mock(return_value=httpx.Response(200, json=envs))

    orga = respx.mock(base_url="https://orga.crm.dynamics.com", assert_all_called=False)
    orga.get("/api/data/v9.2/bots").mock(return_value=httpx.Response(401, json={}))

    orgb = respx.mock(base_url="https://orgb.crm.dynamics.com", assert_all_called=False)
    orgb.get("/api/data/v9.2/bots").mock(
        return_value=httpx.Response(200, json=bots_response)
    )
    orgb.get("/api/data/v9.2/botcomponents").mock(
        return_value=httpx.Response(200, json=components)
    )
    orgb.get("/api/data/v9.2/connectionreferences").mock(
        return_value=httpx.Response(200, json=connref_response)
    )

    with bap, orga, orgb:
        provider: TokenProvider = StaticTokenProvider()
        pp = PowerPlatformAdminClient(token_provider=provider)
        try:
            ctx = DiscoveryContext(
                graph=None,  # type: ignore[arg-type]
                tenant_id="t",
                power_platform=pp,
                token_provider=provider,
            )
            result = await CopilotStudioDiscoverer().discover(ctx)
        finally:
            await pp.aclose()

    assert len(result.mcp_servers) == 1
    assert len(result.agents) == 1
    assert any(e.code == "no_dataverse_access" for e in result.errors)


@pytest.mark.asyncio
async def test_no_org_url_records_org_url_not_resolved() -> None:
    envs = {
        "value": [
            {
                "id": "/.../envs/env-x",
                "name": "env-x",
                "properties": {"displayName": "env-x"},
            }
        ]
    }
    bap = respx.mock(base_url="https://api.bap.microsoft.com", assert_all_called=False)
    bap.get(
        "/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments"
    ).mock(return_value=httpx.Response(200, json=envs))

    with bap:
        provider: TokenProvider = StaticTokenProvider()
        pp = PowerPlatformAdminClient(token_provider=provider)
        try:
            ctx = DiscoveryContext(
                graph=None,  # type: ignore[arg-type]
                tenant_id="t",
                power_platform=pp,
                token_provider=provider,
            )
            result = await CopilotStudioDiscoverer().discover(ctx)
        finally:
            await pp.aclose()

    assert result.mcp_servers == [] and result.agents == []
    assert len(result.errors) == 1
    assert result.errors[0].code == "org_url_not_resolved"


@pytest.mark.asyncio
async def test_no_mcp_components_yields_nothing(
    envs_with_dv: dict[str, Any],
    bots_response: dict[str, Any],
) -> None:
    components = _botcomponents_response(
        _component("sys-topic-1", "botcomponent_topic_system.yaml"),
        _component("gpt-1", "botcomponent_gpt.yaml"),
    )
    bap, dv = _wire(
        envs_with_dv,
        bots=bots_response,
        botcomponents=components,
        connref=None,
    )
    with bap, dv:
        provider: TokenProvider = StaticTokenProvider()
        pp = PowerPlatformAdminClient(token_provider=provider)
        try:
            ctx = DiscoveryContext(
                graph=None,  # type: ignore[arg-type]
                tenant_id="t",
                power_platform=pp,
                token_provider=provider,
            )
            result = await CopilotStudioDiscoverer().discover(ctx)
        finally:
            await pp.aclose()

    assert result.mcp_servers == []
    assert result.agents == []
    assert result.consumption_edges == []
    assert result.errors == []


@pytest.mark.asyncio
async def test_pipeline_with_copilot_studio_scope_e2e(
    tmp_path: Path,
    envs_with_dv: dict[str, Any],
    bots_response: dict[str, Any],
    connref_response: dict[str, Any],
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

    components = _botcomponents_response(
        _component("mcp-1", "botcomponent_mcp_tavily.yaml")
    )
    bap, dv = _wire(
        envs_with_dv,
        bots=bots_response,
        botcomponents=components,
        connref=connref_response,
    )
    with bap, dv:
        doc = await run_pipeline(["copilot_studio"], settings)

    assert doc.status is ScanStatus.completed
    assert "copilot_studio" in doc.scope
    assert doc.summary.mcp_servers_total == 1
    assert doc.summary.agents_with_mcp == 1
    assert all(s.discovered_via == "copilot_studio" for s in doc.mcp_servers)
