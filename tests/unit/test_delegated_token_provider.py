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
