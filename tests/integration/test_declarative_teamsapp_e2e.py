from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from m365_mcp_scanner.auth.token_provider import TokenProvider
from m365_mcp_scanner.clients.graph import GRAPH_V1, GraphClient
from m365_mcp_scanner.discovery import (
    DeclarativeAgentsTeamsAppDiscoverer,
    DiscoveryContext,
)

FIXTURES = Path(__file__).parent / "fixtures"
APPS = FIXTURES / "teams_apps_response.json"
MANIFEST = FIXTURES / "declarative_agent_manifest.json"


class StaticTokenProvider:
    def __init__(self, token: str = "test") -> None:
        self._token = token

    async def get_token(self, scope: str) -> str:  # noqa: ARG002
        return self._token


@pytest.mark.asyncio
async def test_teamsapp_happy_path_discovers_one_agent_two_servers() -> None:
    apps = json.loads(APPS.read_text())
    manifest_bytes = MANIFEST.read_bytes()

    with respx.mock(base_url=GRAPH_V1, assert_all_called=False) as router:
        router.get("/appCatalogs/teamsApps").mock(
            return_value=httpx.Response(200, json=apps)
        )
        router.get(
            "/appCatalogs/teamsApps/ta-001/appDefinitions/def-001/manifest"
        ).mock(return_value=httpx.Response(200, content=manifest_bytes))
        provider: TokenProvider = StaticTokenProvider()
        delegated_graph = GraphClient(provider)
        try:
            ctx = DiscoveryContext(
                graph=delegated_graph,
                tenant_id="t",
                delegated_graph=delegated_graph,
            )
            result = await DeclarativeAgentsTeamsAppDiscoverer().discover(ctx)
        finally:
            await delegated_graph.aclose()

    assert result.errors == []
    assert len(result.agents) == 1
    assert len(result.mcp_servers) == 2
    assert len(result.consumption_edges) == 2
    urls = sorted(s.url for s in result.mcp_servers)
    assert urls == [
        "https://mcp.example.com/search",
        "https://mcp.example.com/weather",
    ]
    agent = result.agents[0]
    assert agent.path.value == "declarative"
    assert agent.source_ref["kind"] == "teams_app"
    assert agent.source_ref["source_id"] == "ta-001"


@pytest.mark.asyncio
async def test_teamsapp_empty_catalog_yields_nothing_no_errors() -> None:
    with respx.mock(base_url=GRAPH_V1, assert_all_called=False) as router:
        router.get("/appCatalogs/teamsApps").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        provider: TokenProvider = StaticTokenProvider()
        delegated_graph = GraphClient(provider)
        try:
            ctx = DiscoveryContext(
                graph=delegated_graph,
                tenant_id="t",
                delegated_graph=delegated_graph,
            )
            result = await DeclarativeAgentsTeamsAppDiscoverer().discover(ctx)
        finally:
            await delegated_graph.aclose()

    assert result.mcp_servers == []
    assert result.agents == []
    assert result.errors == []


@pytest.mark.asyncio
async def test_teamsapp_skipped_when_no_delegated_session() -> None:
    ctx = DiscoveryContext(
        graph=None,  # type: ignore[arg-type]
        tenant_id="t",
        delegated_graph=None,
    )
    result = await DeclarativeAgentsTeamsAppDiscoverer().discover(ctx)
    assert result.mcp_servers == []
    assert len(result.errors) == 1
    assert result.errors[0].code == "delegated_session_required"


@pytest.mark.asyncio
async def test_teamsapp_manifest_400_emits_agent_shell_no_servers() -> None:
    """Microsoft Graph returns 400 for declarative-agent-only Teams apps.

    The scanner must still emit an agent (from catalog metadata) and record
    an informational error with code=manifest_endpoint_unavailable. No MCP
    server or consumption-edge entries should be emitted.
    """
    apps = json.loads(APPS.read_text())
    error_body = (
        '{"error":{"code":"BadRequest",'
        '"message":"Resource not found for the segment \'manifest\'."}}'
    )
    with respx.mock(base_url=GRAPH_V1, assert_all_called=False) as router:
        router.get("/appCatalogs/teamsApps").mock(
            return_value=httpx.Response(200, json=apps)
        )
        router.get(
            "/appCatalogs/teamsApps/ta-001/appDefinitions/def-001/manifest"
        ).mock(
            return_value=httpx.Response(
                400,
                content=error_body,
                headers={"content-type": "application/json"},
            )
        )
        provider: TokenProvider = StaticTokenProvider()
        delegated_graph = GraphClient(provider)
        try:
            ctx = DiscoveryContext(
                graph=delegated_graph,
                tenant_id="t",
                delegated_graph=delegated_graph,
            )
            result = await DeclarativeAgentsTeamsAppDiscoverer().discover(ctx)
        finally:
            await delegated_graph.aclose()

    assert len(result.agents) == 1
    assert result.mcp_servers == []
    assert result.consumption_edges == []
    assert len(result.errors) == 1
    assert result.errors[0].code == "manifest_endpoint_unavailable"
    agent = result.agents[0]
    assert agent.path.value == "declarative"
    assert agent.display_name == "Hello MCP Agent"
    assert agent.published is True
    assert agent.source_ref["manifest_fetch_status"] == "unavailable"
    assert agent.source_ref["app_definition_id"] == "def-001"


@pytest.mark.asyncio
async def test_teamsapp_skips_apps_without_copilot_extension() -> None:
    apps = {
        "value": [
            {
                "id": "ta-plain",
                "displayName": "Plain Teams App",
                "distributionMethod": "organization",
                "appDefinitions": [
                    {"id": "def-plain", "publishingState": "published"}
                ],
            }
        ]
    }
    plain_manifest = b'{"name": "Plain", "manifestVersion": "1.0"}'
    with respx.mock(base_url=GRAPH_V1, assert_all_called=False) as router:
        router.get("/appCatalogs/teamsApps").mock(
            return_value=httpx.Response(200, json=apps)
        )
        router.get(
            "/appCatalogs/teamsApps/ta-plain/appDefinitions/def-plain/manifest"
        ).mock(return_value=httpx.Response(200, content=plain_manifest))
        provider: TokenProvider = StaticTokenProvider()
        delegated_graph = GraphClient(provider)
        try:
            ctx = DiscoveryContext(
                graph=delegated_graph,
                tenant_id="t",
                delegated_graph=delegated_graph,
            )
            result = await DeclarativeAgentsTeamsAppDiscoverer().discover(ctx)
        finally:
            await delegated_graph.aclose()

    assert result.mcp_servers == []
    assert result.agents == []
    assert result.errors == []
