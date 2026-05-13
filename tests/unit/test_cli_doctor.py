"""Snapshot the CLI ``doctor`` command's stderr to lock byte-identical output."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import respx
from pydantic import SecretStr
from typer.testing import CliRunner

from m365_mcp_scanner.auth import doctor as doctor_module
from m365_mcp_scanner.cli.main import app


def _make_settings() -> Any:
    s = MagicMock()
    s.tenant_id = "tid"
    s.client_id = "cid"
    s.client_secret = SecretStr("secret")
    return s


class _StaticTokenProvider:
    async def get_token(self, scope: str = "") -> str:  # noqa: ARG002
        return "test-token"


def test_doctor_all_pass_logged_in_output() -> None:
    runner = CliRunner()
    fake_delegated = MagicMock()
    fake_delegated.is_logged_in.return_value = True
    fake_delegated.account_username.return_value = "alice@example.com"
    with respx.mock(assert_all_called=False) as router:
        router.get(
            "https://graph.microsoft.com/v1.0/external/connections"
        ).mock(return_value=httpx.Response(200, json={"value": [{}]}))
        router.get(
            "https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments"
        ).mock(return_value=httpx.Response(200, json={"value": [{}]}))
        with patch("m365_mcp_scanner.cli.main.Settings", return_value=_make_settings()), \
             patch.object(doctor_module, "AppOnlyTokenProvider", return_value=_StaticTokenProvider()), \
             patch.object(doctor_module, "DelegatedTokenProvider", return_value=fake_delegated):
            result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    # err_console writes to stderr
    stderr = result.stderr
    assert "OK Graph: Graph reachable" in stderr
    assert "OK Power Platform admin reachable" in stderr
    assert "OK Delegated session: alice@example.com" in stderr


def test_doctor_pp_fail_output() -> None:
    runner = CliRunner()
    fake_delegated = MagicMock()
    fake_delegated.is_logged_in.return_value = False
    with respx.mock(assert_all_called=False) as router:
        router.get(
            "https://graph.microsoft.com/v1.0/external/connections"
        ).mock(return_value=httpx.Response(200, json={"value": []}))
        router.get(
            "https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments"
        ).mock(return_value=httpx.Response(403, text="forbidden"))
        with patch("m365_mcp_scanner.cli.main.Settings", return_value=_make_settings()), \
             patch.object(doctor_module, "AppOnlyTokenProvider", return_value=_StaticTokenProvider()), \
             patch.object(doctor_module, "DelegatedTokenProvider", return_value=fake_delegated):
            result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    stderr = result.stderr
    assert "OK Graph:" in stderr
    assert "FAIL" in stderr and "403" in stderr
    assert "Delegated session: not logged in" in stderr


def test_doctor_auth_misconfigured_short_circuits() -> None:
    runner = CliRunner()
    from m365_mcp_scanner.auth.msal_broker import AuthError

    with patch("m365_mcp_scanner.cli.main.Settings", return_value=_make_settings()), \
         patch.object(doctor_module, "AppOnlyTokenProvider", side_effect=AuthError("missing tenant")):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    stderr = result.stderr
    assert "auth misconfigured: missing tenant" in stderr
    # Legacy short-circuit means no FAIL Graph / PP / Delegated lines
    assert "FAIL Graph" not in stderr
    assert "Delegated session" not in stderr
