"""Unit tests for the wizard bootstrap MSAL flow."""
from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.request import urlopen

import pytest

from m365_mcp_scanner.auth import msal_bootstrap
from m365_mcp_scanner.auth.msal_bootstrap import (
    AZURE_CLI_CLIENT_ID,
    BootstrapAuthError,
    BootstrapAuthTimeout,
    GRAPH_DEFAULT_SCOPE,
    acquire_bootstrap_token,
)


def _fake_msal_app(
    *,
    auth_uri: str = "https://login.microsoftonline.com/fake/authorize?x=1",
    token: dict[str, Any] | None = None,
) -> MagicMock:
    app = MagicMock()
    app.initiate_auth_code_flow.return_value = {
        "auth_uri": auth_uri,
        "state": "s",
    }
    app.acquire_token_by_auth_code_flow.return_value = token or {
        "access_token": "abc",
        "expires_in": 3600,
        "id_token_claims": {
            "tid": "11111111-1111-1111-1111-111111111111",
            "preferred_username": "admin@contoso.onmicrosoft.com",
        },
    }
    app.get_accounts.return_value = [
        {"username": "admin@contoso.onmicrosoft.com", "home_account_id": "h"}
    ]
    return app


def _hit_callback(port_holder: list[int], params: str) -> None:
    # Wait for the listener to bind, then send a GET.
    import time

    for _ in range(50):
        if port_holder:
            break
        time.sleep(0.05)
    port = port_holder[0]
    try:
        urlopen(f"http://127.0.0.1:{port}/?{params}", timeout=2).read()
    except Exception:  # noqa: BLE001
        pass


async def test_acquire_bootstrap_token_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_app = _fake_msal_app()
    port_holder: list[int] = []
    pca_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    real_bind = msal_bootstrap._bind_localhost_port

    def capturing_bind() -> int:
        p = real_bind()
        port_holder.append(p)
        return p

    def fake_pca(*args: Any, **kwargs: Any) -> MagicMock:
        pca_calls.append((args, kwargs))
        return fake_app

    monkeypatch.setattr(msal_bootstrap, "_bind_localhost_port", capturing_bind)
    monkeypatch.setattr(msal_bootstrap.msal, "PublicClientApplication", fake_pca)
    monkeypatch.setattr(msal_bootstrap.webbrowser, "open", lambda _u: True)

    threading.Thread(
        target=_hit_callback,
        args=(port_holder, "code=abc&state=s"),
        daemon=True,
    ).start()

    result = await acquire_bootstrap_token(tenant_id=None, timeout_s=10)

    assert result.access_token == "abc"
    assert result.tenant_id == "11111111-1111-1111-1111-111111111111"
    assert result.user_principal_name == "admin@contoso.onmicrosoft.com"
    init_kwargs = fake_app.initiate_auth_code_flow.call_args.kwargs
    assert init_kwargs["scopes"] == [GRAPH_DEFAULT_SCOPE]
    assert init_kwargs["redirect_uri"].startswith("http://localhost:")
    assert pca_calls and AZURE_CLI_CLIENT_ID in pca_calls[0][0]


async def test_acquire_bootstrap_token_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_app = _fake_msal_app()
    monkeypatch.setattr(
        msal_bootstrap.msal, "PublicClientApplication", lambda *a, **k: fake_app
    )
    monkeypatch.setattr(msal_bootstrap.webbrowser, "open", lambda _u: True)

    with pytest.raises(BootstrapAuthTimeout):
        await acquire_bootstrap_token(tenant_id=None, timeout_s=1)


async def test_acquire_bootstrap_token_error_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_app = _fake_msal_app()
    port_holder: list[int] = []
    real_bind = msal_bootstrap._bind_localhost_port

    def capturing_bind() -> int:
        p = real_bind()
        port_holder.append(p)
        return p

    monkeypatch.setattr(msal_bootstrap, "_bind_localhost_port", capturing_bind)
    monkeypatch.setattr(
        msal_bootstrap.msal, "PublicClientApplication", lambda *a, **k: fake_app
    )
    monkeypatch.setattr(msal_bootstrap.webbrowser, "open", lambda _u: True)

    threading.Thread(
        target=_hit_callback,
        args=(port_holder, "error=access_denied&error_description=user+cancelled"),
        daemon=True,
    ).start()

    with pytest.raises(BootstrapAuthError, match="user cancelled|access_denied"):
        await acquire_bootstrap_token(tenant_id=None, timeout_s=10)


async def test_acquire_bootstrap_token_browser_open_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_app = _fake_msal_app()
    monkeypatch.setattr(
        msal_bootstrap.msal, "PublicClientApplication", lambda *a, **k: fake_app
    )
    monkeypatch.setattr(msal_bootstrap.webbrowser, "open", lambda _u: False)

    with pytest.raises(BootstrapAuthError, match="no browser"):
        await acquire_bootstrap_token(tenant_id=None, timeout_s=5)
