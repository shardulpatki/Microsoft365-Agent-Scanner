from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx
from pydantic import SecretStr

from m365_mcp_scanner.auth import doctor as doctor_module
from m365_mcp_scanner.auth.doctor import (
    CheckResult,
    check_dataverse,
    check_delegated_session,
    check_graph,
    check_power_platform,
    run_all,
)


def _make_settings(**overrides: Any) -> Any:
    """Lightweight Settings stand-in that doesn't read from environment."""
    s = MagicMock()
    s.tenant_id = overrides.get("tenant_id", "tid")
    s.client_id = overrides.get("client_id", "cid")
    s.client_secret = SecretStr(overrides.get("client_secret", "secret"))
    return s


class _StaticTokenProvider:
    async def get_token(self, scope: str = "") -> str:  # noqa: ARG002
        return "test-token"


def _patch_app_only_provider() -> Any:
    """Patch ``AppOnlyTokenProvider`` in doctor module to return a stub."""
    return patch.object(
        doctor_module, "AppOnlyTokenProvider", return_value=_StaticTokenProvider()
    )


def test_check_result_is_frozen() -> None:
    r = CheckResult(name="x", audience="graph", status="pass", detail="d")
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        r.name = "y"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_check_graph_pass() -> None:
    with respx.mock(base_url="https://graph.microsoft.com") as router:
        router.get("/v1.0/applications").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _patch_app_only_provider():
            result = await check_graph(_make_settings())
    assert result.audience == "graph"
    assert result.status == "pass"
    assert "Graph reachable" in result.detail


@pytest.mark.asyncio
async def test_check_graph_fail_403() -> None:
    with respx.mock(base_url="https://graph.microsoft.com") as router:
        router.get("/v1.0/applications").mock(
            return_value=httpx.Response(403, text="forbidden")
        )
        with _patch_app_only_provider():
            result = await check_graph(_make_settings())
    assert result.status == "fail"
    assert "403" in result.detail


@pytest.mark.asyncio
async def test_check_graph_fail_403_hints_app_read() -> None:
    with respx.mock(base_url="https://graph.microsoft.com") as router:
        router.get("/v1.0/applications").mock(
            return_value=httpx.Response(403, text="forbidden")
        )
        with _patch_app_only_provider():
            result = await check_graph(_make_settings())
    assert result.status == "fail"
    assert "Application.Read.All" in result.detail
    assert "tenant_id" not in result.detail


@pytest.mark.asyncio
async def test_check_graph_auth_misconfigured() -> None:
    from m365_mcp_scanner.auth.msal_broker import AuthError

    with patch.object(
        doctor_module, "AppOnlyTokenProvider", side_effect=AuthError("missing tenant")
    ):
        result = await check_graph(_make_settings())
    assert result.status == "fail"
    assert result.detail.startswith("auth misconfigured: ")
    assert "missing tenant" in result.detail


@pytest.mark.asyncio
async def test_check_power_platform_pass() -> None:
    with respx.mock(base_url="https://api.bap.microsoft.com") as router:
        router.get(
            "/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments"
        ).mock(return_value=httpx.Response(200, json={"value": [{"id": "e1"}]}))
        with _patch_app_only_provider():
            result = await check_power_platform(_make_settings())
    assert result.audience == "power_platform"
    assert result.status == "pass"
    assert "Power Platform admin reachable" in result.detail


@pytest.mark.asyncio
async def test_check_power_platform_fail_403() -> None:
    with respx.mock(base_url="https://api.bap.microsoft.com") as router:
        router.get(
            "/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments"
        ).mock(return_value=httpx.Response(403, text="forbidden"))
        with _patch_app_only_provider():
            result = await check_power_platform(_make_settings())
    assert result.status == "fail"
    assert "403" in result.detail


def test_check_delegated_session_pass() -> None:
    fake = MagicMock()
    fake.is_logged_in.return_value = True
    fake.account_username.return_value = "alice@example.com"
    with patch.object(doctor_module, "DelegatedTokenProvider", return_value=fake):
        result = check_delegated_session(_make_settings())
    assert result.audience == "delegated"
    assert result.status == "pass"
    assert result.detail == "alice@example.com"


def test_check_delegated_session_not_logged_in() -> None:
    fake = MagicMock()
    fake.is_logged_in.return_value = False
    with patch.object(doctor_module, "DelegatedTokenProvider", return_value=fake):
        result = check_delegated_session(_make_settings())
    assert result.status == "fail"
    assert result.detail.startswith("not logged in")


def test_check_delegated_session_misconfigured() -> None:
    from m365_mcp_scanner.auth.msal_broker import AuthError

    with patch.object(
        doctor_module, "DelegatedTokenProvider", side_effect=AuthError("bad cfg")
    ):
        result = check_delegated_session(_make_settings())
    assert result.status == "fail"
    assert result.detail.startswith("not available")
    assert "bad cfg" in result.detail


@pytest.mark.asyncio
async def test_check_dataverse_pass() -> None:
    org = "https://contoso.crm.dynamics.com"
    env = {
        "name": "env-1",
        "properties": {
            "displayName": "Contoso Prod",
            "linkedEnvironmentMetadata": {"instanceApiUrl": org},
        },
    }
    with respx.mock(base_url=org) as router:
        router.get("/api/data/v9.2/bots").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _patch_app_only_provider():
            result = await check_dataverse(_make_settings(), env)
    assert result.audience == "dataverse"
    assert result.status == "pass"
    assert result.name == "Contoso Prod"


@pytest.mark.asyncio
async def test_check_dataverse_fail_401() -> None:
    org = "https://contoso.crm.dynamics.com"
    env = {
        "name": "env-1",
        "properties": {
            "displayName": "Contoso Prod",
            "linkedEnvironmentMetadata": {"instanceApiUrl": org},
        },
    }
    with respx.mock(base_url=org) as router:
        router.get("/api/data/v9.2/bots").mock(
            return_value=httpx.Response(401, text="unauthorized")
        )
        with _patch_app_only_provider():
            result = await check_dataverse(_make_settings(), env)
    assert result.status == "fail"
    assert "401" in result.detail


@pytest.mark.asyncio
async def test_check_dataverse_no_linked_env() -> None:
    env = {"name": "env-without-dv", "properties": {"displayName": "Dev"}}
    result = await check_dataverse(_make_settings(), env)
    assert result.status == "fail"
    assert result.audience == "dataverse"
    assert "no linked Dataverse" in result.detail


@pytest.mark.asyncio
async def test_run_all_returns_three_results_in_order() -> None:
    fake_delegated = MagicMock()
    fake_delegated.is_logged_in.return_value = False
    with respx.mock(assert_all_called=False) as router:
        router.get(
            "https://graph.microsoft.com/v1.0/applications"
        ).mock(return_value=httpx.Response(200, json={"value": []}))
        router.get(
            "https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments"
        ).mock(return_value=httpx.Response(200, json={"value": []}))
        with _patch_app_only_provider(), patch.object(
            doctor_module, "DelegatedTokenProvider", return_value=fake_delegated
        ):
            results = await run_all(_make_settings())
    assert len(results) == 3
    assert [r.audience for r in results] == ["graph", "power_platform", "delegated"]
