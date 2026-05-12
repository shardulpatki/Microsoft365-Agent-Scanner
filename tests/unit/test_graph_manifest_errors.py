from __future__ import annotations

import httpx
import pytest
import respx

from m365_mcp_scanner.auth.token_provider import TokenProvider
from m365_mcp_scanner.clients.exceptions import (
    ManifestNotAvailableError,
)
from m365_mcp_scanner.clients.graph import GRAPH_V1, GraphClient


class StaticTokenProvider:
    def __init__(self, token: str = "test") -> None:
        self._token = token

    async def get_token(self, scope: str) -> str:  # noqa: ARG002
        return self._token


MANIFEST_PATH = "/appCatalogs/teamsApps/app-1/appDefinitions/def-1/manifest"


@pytest.mark.asyncio
async def test_manifest_400_specific_body_raises_manifest_not_available() -> None:
    body = (
        '{"error":{"code":"BadRequest",'
        '"message":"Resource not found for the segment \'manifest\'."}}'
    )
    with respx.mock(base_url=GRAPH_V1, assert_all_called=False) as router:
        router.get(MANIFEST_PATH).mock(
            return_value=httpx.Response(
                400, content=body, headers={"content-type": "application/json"}
            )
        )
        provider: TokenProvider = StaticTokenProvider()
        client = GraphClient(provider)
        try:
            with pytest.raises(ManifestNotAvailableError) as ei:
                await client.get_teams_app_manifest("app-1", "def-1")
        finally:
            await client.aclose()

    assert ei.value.code == "manifest_endpoint_unavailable"
    assert ei.value.app_id == "app-1"
    assert ei.value.def_id == "def-1"


@pytest.mark.asyncio
async def test_manifest_400_other_body_does_not_raise_manifest_not_available() -> None:
    body = '{"error":{"code":"BadRequest","message":"something else"}}'
    with respx.mock(base_url=GRAPH_V1, assert_all_called=False) as router:
        router.get(MANIFEST_PATH).mock(
            return_value=httpx.Response(
                400, content=body, headers={"content-type": "application/json"}
            )
        )
        provider: TokenProvider = StaticTokenProvider()
        client = GraphClient(provider)
        try:
            with pytest.raises(Exception) as ei:
                await client.get_teams_app_manifest("app-1", "def-1")
        finally:
            await client.aclose()

    assert not isinstance(ei.value, ManifestNotAvailableError)


@pytest.mark.asyncio
async def test_manifest_200_returns_content() -> None:
    payload = b'{"name":"X","manifestVersion":"1.0"}'
    with respx.mock(base_url=GRAPH_V1, assert_all_called=False) as router:
        router.get(MANIFEST_PATH).mock(
            return_value=httpx.Response(200, content=payload)
        )
        provider: TokenProvider = StaticTokenProvider()
        client = GraphClient(provider)
        try:
            got = await client.get_teams_app_manifest("app-1", "def-1")
        finally:
            await client.aclose()

    assert got == payload
