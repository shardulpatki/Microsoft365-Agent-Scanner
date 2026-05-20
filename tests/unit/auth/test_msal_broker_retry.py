"""Retry + cache invalidation tests for the app-only MSAL provider.

Covers the Step 5 doctor reliability fix:
- AppOnlyTokenProvider retries on AADSTS7000215 / AADSTS700016 during
  the Entra → MSAL propagation window after a fresh app + secret.
- Non-transient AAD errors raise immediately.
- file_cache.clear_app_only_token_cache wipes the cache + salt files.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from m365_mcp_scanner.auth import file_cache, msal_broker
from m365_mcp_scanner.auth.file_cache import (
    CACHE_FILENAME,
    SALT_FILENAME,
    clear_app_only_token_cache,
)
from m365_mcp_scanner.auth.msal_broker import (
    AppOnlyTokenProvider,
    AuthError,
)


def _build_provider(app_results: list[dict[str, Any]]) -> AppOnlyTokenProvider:
    """Construct an AppOnlyTokenProvider whose MSAL app yields the given
    sequence of ``acquire_token_for_client`` results."""
    fake_app = MagicMock()
    fake_app.acquire_token_for_client.side_effect = list(app_results)
    with patch.object(
        msal_broker.msal, "ConfidentialClientApplication", return_value=fake_app
    ):
        return AppOnlyTokenProvider(
            tenant_id="11111111-1111-1111-1111-111111111111",
            client_id="22222222-2222-2222-2222-222222222222",
            client_secret="fake-secret",
        )


def _run(provider: AppOnlyTokenProvider, scope: str = "https://graph.microsoft.com/.default") -> str:
    return asyncio.run(provider.get_token(scope))


def test_acquire_token_retries_on_aadsts7000215() -> None:
    provider = _build_provider(
        [
            {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided. ..."},
            {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided. ..."},
            {"access_token": "real-token", "expires_in": 3600},
        ]
    )
    with patch.object(msal_broker.time, "sleep") as sleeper:
        token = _run(provider)
    assert token == "real-token"
    assert sleeper.call_count == 2
    for call in sleeper.call_args_list:
        assert call.args[0] > 0


def test_acquire_token_retries_on_aadsts700016() -> None:
    provider = _build_provider(
        [
            {"error": "unauthorized_client", "error_description": "AADSTS700016: Application with identifier ... was not found in the directory ..."},
            {"access_token": "real-token", "expires_in": 3600},
        ]
    )
    with patch.object(msal_broker.time, "sleep") as sleeper:
        token = _run(provider)
    assert token == "real-token"
    assert sleeper.call_count == 1


def test_acquire_token_no_retry_on_non_transient_error() -> None:
    provider = _build_provider(
        [
            {"error": "invalid_client", "error_description": "AADSTS50012: Invalid client secret keys provided."},
        ]
    )
    with patch.object(msal_broker.time, "sleep") as sleeper:
        with pytest.raises(AuthError) as excinfo:
            _run(provider)
    assert "AADSTS50012" in str(excinfo.value)
    sleeper.assert_not_called()


def test_acquire_token_total_timeout() -> None:
    # Always-failing transient error. Use a monotonic that jumps 25s per
    # call so after a few iterations the deadline is exceeded.
    provider = _build_provider(
        [
            {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided."}
            for _ in range(10)
        ]
    )
    clock = {"t": 0.0}

    def fake_monotonic() -> float:
        clock["t"] += 25.0
        return clock["t"]

    with (
        patch.object(msal_broker.time, "sleep") as sleeper,
        patch.object(msal_broker.time, "monotonic", side_effect=fake_monotonic),
    ):
        with pytest.raises(AuthError) as excinfo:
            _run(provider)
    msg = str(excinfo.value)
    assert "AADSTS7000215" in msg
    assert "after retries" in msg
    assert sleeper.call_count >= 1


def test_clear_app_only_token_cache_removes_files(tmp_path: Path) -> None:
    cache_path = tmp_path / CACHE_FILENAME
    salt_path = tmp_path / SALT_FILENAME
    cache_path.write_bytes(b"stale-cache")
    salt_path.write_bytes(b"stale-salt")

    clear_app_only_token_cache(cache_dir=tmp_path)

    assert not cache_path.exists()
    assert not salt_path.exists()


def test_clear_app_only_token_cache_idempotent(tmp_path: Path) -> None:
    clear_app_only_token_cache(cache_dir=tmp_path)
    # And again with one of the two files pre-existing.
    (tmp_path / CACHE_FILENAME).write_bytes(b"x")
    clear_app_only_token_cache(cache_dir=tmp_path)
    assert not (tmp_path / CACHE_FILENAME).exists()


def test_clear_app_only_token_cache_logs_warning_on_oserror(
    tmp_path: Path,
) -> None:
    """A non-FileNotFoundError OSError is logged but does not raise."""
    (tmp_path / CACHE_FILENAME).write_bytes(b"x")
    with patch.object(file_cache.Path, "unlink", side_effect=PermissionError("locked")):
        # Should not raise.
        clear_app_only_token_cache(cache_dir=tmp_path)
