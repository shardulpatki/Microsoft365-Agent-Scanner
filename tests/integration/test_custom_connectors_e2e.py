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
from m365_mcp_scanner.discovery import CustomConnectorsDiscoverer, DiscoveryContext
from m365_mcp_scanner.models import ScanStatus
from m365_mcp_scanner.models.enums import Transport
from m365_mcp_scanner.orchestrator import run_pipeline

FIXTURES = Path(__file__).parent / "fixtures"
ENVS = FIXTURES / "environments_response.json"
CONNECTORS = FIXTURES / "connectors_list_response.json"
TOP_LEVEL_MCP = FIXTURES / "connector_openapi_top_level_mcp.json"


class StaticTokenProvider:
    def __init__(self, token: str = "test-token") -> None:
        self._token = token

    async def get_token(self, scope: str) -> str:  # noqa: ARG002
        return self._token


@pytest.fixture
def envs_response() -> dict:
    return json.loads(ENVS.read_text())


@pytest.fixture
def connectors_response() -> dict:
    return json.loads(CONNECTORS.read_text())


@pytest.fixture
def top_level_mcp_swagger() -> dict:
    return json.loads(TOP_LEVEL_MCP.read_text())


def _wire_pp_routes(
    envs_response: dict[str, Any],
    connectors_response: dict[str, Any],
    legacy_swagger: dict[str, Any],
) -> tuple[respx.MockRouter, respx.MockRouter, respx.MockRouter]:
    """Build three respx routers (BAP + PAPI + Blob). Caller is responsible for entering them."""
    bap = respx.mock(base_url="https://api.bap.microsoft.com", assert_all_called=False)
    papi = respx.mock(base_url="https://api.powerapps.com", assert_all_called=False)
    blob = respx.mock(base_url="https://blob.example.com", assert_all_called=False)
    env_id = "a787c566-03fd-e1c6-972d-9549daaad71c"
    bap.get(
        "/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments"
    ).mock(return_value=httpx.Response(200, json=envs_response))
    papi.get(
        f"/providers/Microsoft.PowerApps/scopes/admin/environments/{env_id}/apis"
    ).mock(return_value=httpx.Response(200, json=connectors_response))
    # Legacy connector swagger lives at a (mock) pre-signed Azure Blob URL.
    blob.get("/swagger/legacy.json").mock(
        return_value=httpx.Response(200, json=legacy_swagger)
    )
    return bap, papi, blob


@pytest.mark.asyncio
async def test_discoverer_finds_only_mcp_shaped_connectors(
    envs_response: dict,
    connectors_response: dict,
    top_level_mcp_swagger: dict,
) -> None:
    bap, papi, blob = _wire_pp_routes(envs_response, connectors_response, top_level_mcp_swagger)
    with bap, papi, blob:
        provider: TokenProvider = StaticTokenProvider()
        pp = PowerPlatformAdminClient(token_provider=provider)
        try:
            ctx = DiscoveryContext(graph=None, tenant_id="t", power_platform=pp)  # type: ignore[arg-type]
            result = await CustomConnectorsDiscoverer().discover(ctx)
        finally:
            await pp.aclose()

    assert result.errors == []
    assert len(result.mcp_servers) == 2
    urls = sorted(s.url for s in result.mcp_servers)
    assert urls == ["https://example.com/mcp", "https://legacy.example.com/api/run"]
    assert all(s.transport is Transport.custom_connector for s in result.mcp_servers)
    assert all(s.discovered_via == "custom_connectors" for s in result.mcp_servers)
    # The example.com host is external, the SharePoint one would have been Microsoft (filtered out).
    test_mcp = next(s for s in result.mcp_servers if s.url == "https://example.com/mcp")
    assert test_mcp.external_domain is True
    assert test_mcp.is_first_party is False
    assert test_mcp.evidence["connector_id"] == "shared_test_mcp_1"
    assert test_mcp.evidence["environment_id"] == "a787c566-03fd-e1c6-972d-9549daaad71c"
    assert test_mcp.evidence["environment_display_name"] == "mcp-scanner-test"
    assert test_mcp.evidence["path"] == "/mcp"
    assert test_mcp.evidence["method"] == "POST"


@pytest.mark.asyncio
async def test_discoverer_handles_missing_inline_swagger_via_fallback_fetch(
    envs_response: dict,
    connectors_response: dict,
    top_level_mcp_swagger: dict,
) -> None:
    bap, papi, blob = _wire_pp_routes(envs_response, connectors_response, top_level_mcp_swagger)
    with bap, papi, blob:
        provider: TokenProvider = StaticTokenProvider()
        pp = PowerPlatformAdminClient(token_provider=provider)
        try:
            ctx = DiscoveryContext(graph=None, tenant_id="t", power_platform=pp)  # type: ignore[arg-type]
            result = await CustomConnectorsDiscoverer().discover(ctx)
        finally:
            await pp.aclose()

        legacy_route = next(r for r in blob.routes if "legacy" in str(r.pattern))
        assert legacy_route.call_count == 1, (
            "blob fetch should have been issued exactly once for the connector "
            "with no inline swagger"
        )

    legacy = next(s for s in result.mcp_servers if "legacy.example.com" in s.url)
    assert legacy.url == "https://legacy.example.com/api/run"


@pytest.mark.asyncio
async def test_pipeline_with_custom_connectors_scope_e2e(
    tmp_path: Path,
    envs_response: dict,
    connectors_response: dict,
    top_level_mcp_swagger: dict,
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

    bap, papi, blob = _wire_pp_routes(envs_response, connectors_response, top_level_mcp_swagger)
    with bap, papi, blob:
        doc = await run_pipeline(["custom_connectors"], settings)

    assert doc.status is ScanStatus.completed
    assert "custom_connectors" in doc.scope
    assert doc.summary.mcp_servers_total == 2
    assert doc.summary.mcp_servers_external == 2
    assert doc.summary.mcp_servers_first_party == 0
    assert all(s.discovered_via == "custom_connectors" for s in doc.mcp_servers)


@pytest.mark.asyncio
async def test_discoverer_records_error_when_pp_client_missing() -> None:
    ctx = DiscoveryContext(graph=None, tenant_id="t", power_platform=None)  # type: ignore[arg-type]
    result = await CustomConnectorsDiscoverer().discover(ctx)
    assert result.mcp_servers == []
    assert len(result.errors) == 1
    assert "power_platform" in result.errors[0].message
