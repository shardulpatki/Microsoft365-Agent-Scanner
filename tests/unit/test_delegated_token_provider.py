"""Unit tests for DelegatedTokenProvider.

The encrypted file cache is redirected to a per-test ``tmp_path`` via the
``cache_dir`` constructor arg, so tests never touch the user's real cache
directory. MSAL's PublicClientApplication is also patched so no network
calls occur.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from m365_mcp_scanner.auth import file_cache
from m365_mcp_scanner.auth.msal_broker import (
    AuthError,
    DelegatedTokenProvider,
)


def test_is_logged_in_false_on_empty_cache(tmp_path: Path) -> None:
    provider = DelegatedTokenProvider(
        tenant_id="t", client_id="c", cache_dir=tmp_path
    )
    assert provider.is_logged_in() is False
    assert provider.account_username() is None


def test_clear_cache_removes_cache_file(tmp_path: Path) -> None:
    file_cache.save("t", "c", "{}", cache_dir=tmp_path)
    assert (tmp_path / file_cache.CACHE_FILENAME).exists()
    provider = DelegatedTokenProvider(
        tenant_id="t", client_id="c", cache_dir=tmp_path
    )
    provider.clear_cache()
    assert not (tmp_path / file_cache.CACHE_FILENAME).exists()


@pytest.mark.asyncio
async def test_silent_token_path_used_when_account_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = DelegatedTokenProvider(
        tenant_id="t", client_id="c", cache_dir=tmp_path
    )

    fake_account = {"username": "user@contoso.com", "home_account_id": "h"}
    # Patch the cache-level accounts read (used by is_logged_in and the silent
    # path's pre-check) so we don't depend on a real MSAL app.
    monkeypatch.setattr(provider, "_cached_accounts", lambda: [fake_account])

    class FakeApp:
        @staticmethod
        def get_accounts() -> list[dict[str, Any]]:
            return [fake_account]

        @staticmethod
        def acquire_token_silent(
            scopes: list[str], account: dict[str, Any]
        ) -> dict[str, Any]:
            silent_calls.append({"scopes": scopes, "account": account})
            return {"access_token": "AT", "expires_in": 3600}

        @staticmethod
        def initiate_device_flow(scopes: list[str]) -> dict[str, Any]:
            pytest.fail("device flow should not run")
            return {}

    silent_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(provider, "_get_app", lambda: FakeApp())

    token = await provider.get_token("https://graph.microsoft.com/.default")
    assert token == "AT"
    assert provider.is_logged_in() is True
    assert provider.account_username() == "user@contoso.com"
    assert silent_calls and silent_calls[0]["scopes"] == [
        "https://graph.microsoft.com/.default"
    ]


@pytest.mark.asyncio
async def test_get_token_without_account_raises_with_login_hint(
    tmp_path: Path,
) -> None:
    provider = DelegatedTokenProvider(
        tenant_id="t", client_id="c", cache_dir=tmp_path
    )
    with pytest.raises(AuthError) as excinfo:
        await provider.get_token()
    assert "mcp-scan login" in str(excinfo.value)


def test_init_requires_tenant_and_client() -> None:
    with pytest.raises(AuthError):
        DelegatedTokenProvider(tenant_id="", client_id="c")
    with pytest.raises(AuthError):
        DelegatedTokenProvider(tenant_id="t", client_id="")


# -- device-flow split (start/complete) --------------------------------------


class _FakeDeviceFlowApp:
    """Stand-in for msal.PublicClientApplication used by device-flow tests."""

    def __init__(
        self,
        initiate_result: dict[str, Any],
        acquire_result: dict[str, Any] | None = None,
    ) -> None:
        self._initiate_result = initiate_result
        self._acquire_result = acquire_result
        self.initiate_calls: list[list[str]] = []
        self.acquire_calls: list[dict[str, Any]] = []

    def initiate_device_flow(self, scopes: list[str]) -> dict[str, Any]:
        self.initiate_calls.append(scopes)
        return self._initiate_result

    def acquire_token_by_device_flow(self, flow: dict[str, Any]) -> dict[str, Any]:
        self.acquire_calls.append(flow)
        assert self._acquire_result is not None, "acquire not expected in this test"
        return self._acquire_result


@pytest.mark.asyncio
async def test_start_device_flow_returns_user_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = DelegatedTokenProvider(
        tenant_id="t", client_id="c", cache_dir=tmp_path
    )
    fake_flow = {
        "user_code": "ABCD-1234",
        "verification_uri": "https://microsoft.com/devicelogin",
        "expires_in": 900,
        "message": "Go to https://microsoft.com/devicelogin and enter ABCD-1234",
    }
    fake_app = _FakeDeviceFlowApp(initiate_result=fake_flow)
    monkeypatch.setattr(provider, "_get_app", lambda: fake_app)

    flow = await provider.start_device_flow()
    assert flow is fake_flow
    assert flow["user_code"] == "ABCD-1234"
    assert fake_app.initiate_calls == [["https://graph.microsoft.com/.default"]]


@pytest.mark.asyncio
async def test_start_device_flow_raises_when_user_code_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = DelegatedTokenProvider(
        tenant_id="t", client_id="c", cache_dir=tmp_path
    )
    fake_app = _FakeDeviceFlowApp(
        initiate_result={"error": "x", "error_description": "tenant blocked"},
    )
    monkeypatch.setattr(provider, "_get_app", lambda: fake_app)

    with pytest.raises(AuthError) as excinfo:
        await provider.start_device_flow()
    assert "device flow initiation failed" in str(excinfo.value)
    assert "tenant blocked" in str(excinfo.value)


@pytest.mark.asyncio
async def test_complete_device_flow_persists_cache_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = DelegatedTokenProvider(
        tenant_id="t", client_id="c", cache_dir=tmp_path
    )
    fake_app = _FakeDeviceFlowApp(
        initiate_result={},
        acquire_result={"access_token": "AT", "expires_in": 3600},
    )
    monkeypatch.setattr(provider, "_get_app", lambda: fake_app)
    persist_calls: list[None] = []
    monkeypatch.setattr(
        provider, "_persist_cache", lambda: persist_calls.append(None)
    )

    fake_flow = {"user_code": "X", "device_code": "d"}
    result = await provider.complete_device_flow(fake_flow)
    assert result["access_token"] == "AT"
    assert fake_app.acquire_calls == [fake_flow]
    assert len(persist_calls) == 1


@pytest.mark.asyncio
async def test_complete_device_flow_raises_on_missing_access_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = DelegatedTokenProvider(
        tenant_id="t", client_id="c", cache_dir=tmp_path
    )
    fake_app = _FakeDeviceFlowApp(
        initiate_result={},
        acquire_result={"error_description": "expired_token"},
    )
    monkeypatch.setattr(provider, "_get_app", lambda: fake_app)
    persist_calls: list[None] = []
    monkeypatch.setattr(
        provider, "_persist_cache", lambda: persist_calls.append(None)
    )

    with pytest.raises(AuthError) as excinfo:
        await provider.complete_device_flow({"device_code": "d"})
    assert "device flow did not yield a token" in str(excinfo.value)
    assert "expired_token" in str(excinfo.value)
    assert persist_calls == []


@pytest.mark.asyncio
async def test_login_wrapper_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI's `mcp-scan login` path (cli/main.py:99-117) must keep working."""
    provider = DelegatedTokenProvider(
        tenant_id="t", client_id="c", cache_dir=tmp_path
    )
    fake_flow = {
        "user_code": "WXYZ-9876",
        "verification_uri": "https://microsoft.com/devicelogin",
        "expires_in": 900,
    }
    fake_app = _FakeDeviceFlowApp(
        initiate_result=fake_flow,
        acquire_result={"access_token": "AT", "expires_in": 3600},
    )
    monkeypatch.setattr(provider, "_get_app", lambda: fake_app)
    monkeypatch.setattr(provider, "_persist_cache", lambda: None)

    prompted: list[dict[str, Any]] = []

    def _on_prompt(flow: dict[str, Any]) -> None:
        prompted.append(flow)

    result = await provider.login(on_prompt=_on_prompt)
    assert result["access_token"] == "AT"
    assert prompted == [fake_flow]
    assert fake_app.acquire_calls == [fake_flow]
