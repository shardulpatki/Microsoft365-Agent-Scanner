from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from m365_mcp_scanner.auth.token_provider import TokenProvider
from m365_mcp_scanner.clients.graph import GRAPH_BETA, GraphClient
from m365_mcp_scanner.discovery import (
    DeclarativeAgentsPackagesDiscoverer,
    DiscoveryContext,
)

FIXTURES = Path(__file__).parent / "fixtures"
PACKAGES = FIXTURES / "copilot_packages_response.json"
MANIFEST = FIXTURES / "declarative_agent_manifest.json"


class StaticTokenProvider:
    def __init__(self, token: str = "test") -> None:
        self._token = token

    async def get_token(self, scope: str) -> str:  # noqa: ARG002
        return self._token


@pytest.mark.asyncio
async def test_packages_happy_path_discovers_one_agent_two_servers() -> None:
    packages = json.loads(PACKAGES.read_text())
    manifest = json.loads(MANIFEST.read_text())
    full_pkg = {"id": "pkg-001", "manifest": manifest}

    with respx.mock(base_url=GRAPH_BETA, assert_all_called=False) as router:
        router.get("/copilot/admin/catalog/packages").mock(
            return_value=httpx.Response(200, json=packages)
        )
        router.get("/copilot/admin/catalog/packages/pkg-001").mock(
            return_value=httpx.Response(200, json=full_pkg)
        )
        provider: TokenProvider = StaticTokenProvider()
        delegated_graph = GraphClient(provider)
        try:
            ctx = DiscoveryContext(
                graph=delegated_graph,  # not used by this discoverer
                tenant_id="t",
                delegated_graph=delegated_graph,
            )
            result = await DeclarativeAgentsPackagesDiscoverer().discover(ctx)
        finally:
            await delegated_graph.aclose()

    assert result.errors == []
    assert len(result.agents) == 1
    assert len(result.mcp_servers) == 2
    assert len(result.consumption_edges) == 2
    assert all(s.discovered_via == "declarative_agents_copilot_package" for s in result.mcp_servers)


@pytest.mark.asyncio
async def test_packages_403_frontier_records_tenant_not_eligible() -> None:
    body = {
        "error": {
            "code": "Forbidden",
            "message": "This API is not eligible for the current tenant (Frontier preview).",
        }
    }
    with respx.mock(base_url=GRAPH_BETA, assert_all_called=False) as router:
        router.get("/copilot/admin/catalog/packages").mock(
            return_value=httpx.Response(403, json=body)
        )
        provider: TokenProvider = StaticTokenProvider()
        delegated_graph = GraphClient(provider)
        try:
            ctx = DiscoveryContext(
                graph=delegated_graph,
                tenant_id="t",
                delegated_graph=delegated_graph,
            )
            result = await DeclarativeAgentsPackagesDiscoverer().discover(ctx)
        finally:
            await delegated_graph.aclose()

    assert result.mcp_servers == []
    assert result.agents == []
    assert len(result.errors) == 1
    err = result.errors[0]
    assert err.code == "tenant_not_eligible"
    assert err.surface == "declarative_agents_packages"


@pytest.mark.asyncio
async def test_packages_403_agent365_license_records_tenant_not_eligible() -> None:
    body = {
        "error": {
            "code": "Forbidden",
            "message": "Customer must be a licensed for Agent 365 in order to use Agent 365 Graph APIs",
        }
    }
    with respx.mock(base_url=GRAPH_BETA, assert_all_called=False) as router:
        router.get("/copilot/admin/catalog/packages").mock(
            return_value=httpx.Response(403, json=body)
        )
        provider: TokenProvider = StaticTokenProvider()
        delegated_graph = GraphClient(provider)
        try:
            ctx = DiscoveryContext(
                graph=delegated_graph,
                tenant_id="t",
                delegated_graph=delegated_graph,
            )
            result = await DeclarativeAgentsPackagesDiscoverer().discover(ctx)
        finally:
            await delegated_graph.aclose()

    assert len(result.errors) == 1
    err = result.errors[0]
    assert err.code == "tenant_not_eligible"
    assert err.message == (
        "tenant not eligible for Copilot Packages API: "
        "Customer must be a licensed for Agent 365 in order to use Agent 365 Graph APIs"
    )


@pytest.mark.asyncio
async def test_packages_403_insufficient_privileges_records_permission_missing() -> None:
    body = {
        "error": {
            "code": "Forbidden",
            "message": "Insufficient privileges to complete the operation",
        }
    }
    with respx.mock(base_url=GRAPH_BETA, assert_all_called=False) as router:
        router.get("/copilot/admin/catalog/packages").mock(
            return_value=httpx.Response(403, json=body)
        )
        provider: TokenProvider = StaticTokenProvider()
        delegated_graph = GraphClient(provider)
        try:
            ctx = DiscoveryContext(
                graph=delegated_graph,
                tenant_id="t",
                delegated_graph=delegated_graph,
            )
            result = await DeclarativeAgentsPackagesDiscoverer().discover(ctx)
        finally:
            await delegated_graph.aclose()

    assert len(result.errors) == 1
    assert result.errors[0].code == "permission_missing"


@pytest.mark.asyncio
async def test_packages_403_unmatched_message_records_forbidden() -> None:
    body = {
        "error": {
            "code": "Forbidden",
            "message": "Application is not authorized",
        }
    }
    with respx.mock(base_url=GRAPH_BETA, assert_all_called=False) as router:
        router.get("/copilot/admin/catalog/packages").mock(
            return_value=httpx.Response(403, json=body)
        )
        provider: TokenProvider = StaticTokenProvider()
        delegated_graph = GraphClient(provider)
        try:
            ctx = DiscoveryContext(
                graph=delegated_graph,
                tenant_id="t",
                delegated_graph=delegated_graph,
            )
            result = await DeclarativeAgentsPackagesDiscoverer().discover(ctx)
        finally:
            await delegated_graph.aclose()

    assert len(result.errors) == 1
    assert result.errors[0].code == "forbidden"


@pytest.mark.asyncio
async def test_packages_403_malformed_body_records_forbidden_without_crash() -> None:
    with respx.mock(base_url=GRAPH_BETA, assert_all_called=False) as router:
        router.get("/copilot/admin/catalog/packages").mock(
            return_value=httpx.Response(403, text="upstream error")
        )
        provider: TokenProvider = StaticTokenProvider()
        delegated_graph = GraphClient(provider)
        try:
            ctx = DiscoveryContext(
                graph=delegated_graph,
                tenant_id="t",
                delegated_graph=delegated_graph,
            )
            result = await DeclarativeAgentsPackagesDiscoverer().discover(ctx)
        finally:
            await delegated_graph.aclose()

    assert len(result.errors) == 1
    assert result.errors[0].code == "forbidden"
    assert "upstream error" in result.errors[0].message


@pytest.mark.asyncio
async def test_packages_skipped_when_no_delegated_session() -> None:
    ctx = DiscoveryContext(
        graph=None,  # type: ignore[arg-type]
        tenant_id="t",
        delegated_graph=None,
    )
    result = await DeclarativeAgentsPackagesDiscoverer().discover(ctx)
    assert result.mcp_servers == []
    assert len(result.errors) == 1
    assert result.errors[0].code == "delegated_session_required"
