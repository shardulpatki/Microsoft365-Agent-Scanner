"""Unit tests for the BAP ``addAppUser`` provisioner (Phase B)."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from m365_mcp_scanner.provisioning.app_user_provisioner import (
    provision_app_user,
    provision_app_user_batch,
)

_BAP = "https://api.bap.microsoft.com"
_API_VERSION = "2020-10-01"


def _settings(client_id: str = "00000000-0000-0000-0000-000000000001") -> MagicMock:
    s = MagicMock()
    s.client_id = client_id
    return s


def _env(env_id: str = "env-1") -> dict[str, Any]:
    return {
        "name": env_id,
        "id": (
            "/providers/Microsoft.BusinessAppPlatform/scopes/admin"
            f"/environments/{env_id}"
        ),
        "type": "Microsoft.BusinessAppPlatform/scopes/environments",
        "properties": {},
    }


def _path(env_id: str) -> str:
    return (
        f"/providers/Microsoft.BusinessAppPlatform/scopes/admin"
        f"/environments/{env_id}/addAppUser"
    )


def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Patch the provisioner's ``asyncio.sleep`` to record durations only."""
    calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        calls.append(float(delay))

    monkeypatch.setattr(
        "m365_mcp_scanner.provisioning.app_user_provisioner.asyncio.sleep",
        _fake_sleep,
    )
    return calls


async def test_provision_single_env_success() -> None:
    with respx.mock(base_url=_BAP, assert_all_called=True) as router:
        router.post(_path("env-1")).mock(return_value=httpx.Response(200))
        result = await provision_app_user(_env(), _settings(), "fake-token")

    assert result.status == "success"
    assert result.env_id == "env-1"
    assert result.http_status == 200


async def test_provision_honors_retry_after_on_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps = _patch_sleep(monkeypatch)
    with respx.mock(base_url=_BAP, assert_all_called=True) as router:
        router.post(_path("env-1")).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "2"}),
                httpx.Response(200),
            ]
        )
        result = await provision_app_user(_env(), _settings(), "tok")

    assert sleeps == [2.0]
    assert result.status == "success"
    assert result.http_status == 200


async def test_provision_exponential_backoff_on_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps = _patch_sleep(monkeypatch)
    with respx.mock(base_url=_BAP, assert_all_called=True) as router:
        router.post(_path("env-1")).mock(
            side_effect=[httpx.Response(500) for _ in range(4)]
        )
        result = await provision_app_user(_env(), _settings(), "tok")

    assert sleeps == [1.0, 2.0, 4.0]
    assert result.status == "error"
    assert result.http_status == 500


async def test_provision_no_retry_on_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps = _patch_sleep(monkeypatch)
    with respx.mock(base_url=_BAP, assert_all_called=True) as router:
        router.post(_path("env-1")).mock(
            return_value=httpx.Response(
                403,
                json={"error": {"code": "Forbidden", "message": "Not allowed"}},
            )
        )
        result = await provision_app_user(_env(), _settings(), "tok")

    assert sleeps == []
    assert result.status == "error"
    assert result.error_code == "Forbidden"
    assert result.error_message is not None
    assert "Not allowed" in result.error_message
    assert result.http_status == 403


async def test_batch_respects_concurrency_limit() -> None:
    envs = [_env(f"env-{i}") for i in range(20)]

    inflight = 0
    peak = 0

    async def _track(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        inflight += 1
        if inflight > peak:
            peak = inflight
        await asyncio.sleep(0.01)
        inflight -= 1
        return httpx.Response(200)

    with respx.mock(base_url=_BAP, assert_all_called=False) as router:
        router.route(
            method="POST",
            url__regex=r".*/addAppUser\?api-version=" + _API_VERSION + r"$",
        ).mock(side_effect=_track)

        results = await provision_app_user_batch(
            envs, _settings(), "tok", concurrency=8
        )

    assert peak <= 8
    assert peak > 0
    assert len(results) == 20
    assert all(r.status == "success" for r in results.values())


async def test_batch_per_env_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sleep(monkeypatch)
    envs = [_env(f"env-{i}") for i in range(20)]

    def _raise(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("boom")

    with respx.mock(base_url=_BAP, assert_all_called=False) as router:
        router.post(_path("env-7")).mock(side_effect=_raise)
        router.route(
            method="POST",
            url__regex=r".*/addAppUser\?api-version=" + _API_VERSION + r"$",
        ).mock(return_value=httpx.Response(200))

        results = await provision_app_user_batch(
            envs, _settings(), "tok", concurrency=8
        )

    assert len(results) == 20
    assert results["env-7"].status == "error"
    successes = [eid for eid, r in results.items() if r.status == "success"]
    assert len(successes) == 19
    assert "env-7" not in successes
