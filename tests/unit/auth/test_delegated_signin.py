"""Unit tests for the interactive (browser-popup) delegated sign-in."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from m365_mcp_scanner.auth import delegated_signin


@pytest.fixture()
def _fake_app() -> MagicMock:
    app = MagicMock()
    app.initiate_auth_code_flow.return_value = {
        "auth_uri": "https://login.microsoftonline.com/fake/authorize?x=1",
        "state": "s",
    }
    app.acquire_token_by_auth_code_flow.return_value = {
        "access_token": "abc",
        "expires_in": 3600,
        "id_token_claims": {
            "tid": "11111111-1111-1111-1111-111111111111",
            "preferred_username": "admin@contoso.onmicrosoft.com",
        },
    }
    return app


@pytest.fixture()
def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch, _fake_app: MagicMock
) -> dict[str, Any]:
    """Wire up a fake DelegatedTokenProvider, fake browser, and a listener
    stub that immediately fires the callback with a stub params dict.
    """
    fake_provider = MagicMock()
    fake_provider._get_app.return_value = _fake_app
    fake_provider._persist_cache = MagicMock()

    provider_ctor = MagicMock(return_value=fake_provider)
    monkeypatch.setattr(
        delegated_signin, "DelegatedTokenProvider", provider_ctor
    )

    monkeypatch.setattr(
        delegated_signin, "_bind_localhost_port", lambda: 54321
    )

    def _stub_listener(
        port: int, state: delegated_signin._CallbackState, timeout_s: int
    ) -> None:
        state.params = {"code": "stub-code", "state": "s"}
        state.event.set()

    monkeypatch.setattr(delegated_signin, "_run_listener", _stub_listener)
    monkeypatch.setattr(delegated_signin.webbrowser, "open", lambda _uri: True)

    return {"provider": fake_provider, "ctor": provider_ctor, "app": _fake_app}


def test_success_returns_success_and_persists(
    _patch_runtime: dict[str, Any],
) -> None:
    result = delegated_signin.interactive_delegated_signin(
        tenant_id="t", client_id="c", timeout_s=5
    )
    assert result.status == "success"
    assert "admin@contoso.onmicrosoft.com" in result.detail
    _patch_runtime["provider"]._persist_cache.assert_called_once()


def test_aadsts500113_returns_needs_device_code(
    _patch_runtime: dict[str, Any],
) -> None:
    _patch_runtime["app"].acquire_token_by_auth_code_flow.return_value = {
        "error": "invalid_grant",
        "error_description": (
            "AADSTS500113: No reply address is registered for the application."
        ),
    }
    result = delegated_signin.interactive_delegated_signin(
        tenant_id="t", client_id="c", timeout_s=5
    )
    assert result.status == "needs_device_code"
    _patch_runtime["provider"]._persist_cache.assert_not_called()


def test_missing_auth_uri_returns_error(
    _patch_runtime: dict[str, Any],
) -> None:
    _patch_runtime["app"].initiate_auth_code_flow.return_value = {
        "error": "bad_request",
        "error_description": "bad",
    }
    result = delegated_signin.interactive_delegated_signin(
        tenant_id="t", client_id="c", timeout_s=5
    )
    assert result.status == "error"
    assert "bad" in result.detail
    _patch_runtime["provider"]._persist_cache.assert_not_called()


def test_generic_acquire_error_returns_error(
    _patch_runtime: dict[str, Any],
) -> None:
    _patch_runtime["app"].acquire_token_by_auth_code_flow.return_value = {
        "error": "interaction_required",
        "error_description": "something else",
    }
    result = delegated_signin.interactive_delegated_signin(
        tenant_id="t", client_id="c", timeout_s=5
    )
    assert result.status == "error"
    assert "something else" in result.detail
    _patch_runtime["provider"]._persist_cache.assert_not_called()


def test_scopes_exactly_default_and_offline_access(
    _patch_runtime: dict[str, Any],
) -> None:
    delegated_signin.interactive_delegated_signin(
        tenant_id="t", client_id="c", timeout_s=5
    )
    call = _patch_runtime["app"].initiate_auth_code_flow.call_args
    scopes = call.kwargs["scopes"]
    assert scopes == [
        "https://graph.microsoft.com/.default",
    ]
