from __future__ import annotations

import httpx
import pytest
import respx

from m365_mcp_scanner.clients.power_platform_admin import PowerPlatformAdminClient


class StaticTokenProvider:
    async def get_token(self, scope: str) -> str:  # noqa: ARG002
        return "test-token"


@pytest.mark.asyncio
async def test_fetch_swagger_url_returns_parsed_json() -> None:
    spec_payload = {"swagger": "2.0", "host": "example.com", "paths": {}}
    with respx.mock(base_url="https://blob.example.com") as router:
        router.get("/swagger/x.json").mock(
            return_value=httpx.Response(200, json=spec_payload)
        )
        pp = PowerPlatformAdminClient(token_provider=StaticTokenProvider())
        try:
            spec = await pp.fetch_swagger_url("https://blob.example.com/swagger/x.json")
        finally:
            await pp.aclose()
    assert spec == spec_payload


@pytest.mark.asyncio
async def test_fetch_swagger_url_returns_none_on_error() -> None:
    with respx.mock(base_url="https://blob.example.com") as router:
        router.get("/swagger/broken.json").mock(
            return_value=httpx.Response(403, text="forbidden")
        )
        pp = PowerPlatformAdminClient(token_provider=StaticTokenProvider())
        try:
            spec = await pp.fetch_swagger_url(
                "https://blob.example.com/swagger/broken.json"
            )
        finally:
            await pp.aclose()
    assert spec is None


@pytest.mark.asyncio
async def test_pagination_follows_nextlink() -> None:
    page1 = {
        "value": [{"name": "env-1"}],
        "nextLink": "https://api.bap.microsoft.com/page2",
    }
    page2 = {"value": [{"name": "env-2"}]}
    with respx.mock(base_url="https://api.bap.microsoft.com") as router:
        router.get(
            "/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments"
        ).mock(return_value=httpx.Response(200, json=page1))
        router.get("/page2").mock(return_value=httpx.Response(200, json=page2))
        pp = PowerPlatformAdminClient(token_provider=StaticTokenProvider())
        try:
            envs = [e async for e in pp.list_environments()]
        finally:
            await pp.aclose()
    assert [e["name"] for e in envs] == ["env-1", "env-2"]


@pytest.mark.asyncio
async def test_doctor_ping_handles_403() -> None:
    with respx.mock(base_url="https://api.bap.microsoft.com") as router:
        router.get(
            "/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments"
        ).mock(return_value=httpx.Response(403, text="Forbidden"))
        pp = PowerPlatformAdminClient(token_provider=StaticTokenProvider())
        try:
            ok, msg = await pp.doctor_ping()
        finally:
            await pp.aclose()
    assert ok is False
    assert "403" in msg
    assert "New-PowerAppManagementApp" in msg
