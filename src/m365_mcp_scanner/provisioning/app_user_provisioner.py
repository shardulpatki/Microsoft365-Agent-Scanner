"""Provisioner for Power Platform application users (BAP ``addAppUser``).

Public surface used by the wizard UI:
``AppUserProvisionResult``, ``provision_app_user``, ``provision_app_user_batch``.

The result dataclass is intentionally *only* re-exposed from this leaf module.
``provisioning/__init__.py`` is not touched, because re-exporting from the
package root previously triggered Streamlit page-loader ImportError in this
codebase.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

import httpx


@dataclass
class AppUserProvisionResult:
    env_id: str
    status: Literal["success", "error"]
    error_code: str | None = None
    error_message: str | None = None
    http_status: int | None = None


_BAP_BASE = "https://api.bap.microsoft.com"
_API_VERSION = "2020-10-01"
_MAX_RETRIES = 3


def _extract_error(resp: httpx.Response) -> tuple[str | None, str | None]:
    try:
        data = resp.json()
    except Exception:
        text = (resp.text or "").strip() or None
        return None, text
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return err.get("code"), err.get("message")
    text = (resp.text or "").strip() or None
    return None, text


def _parse_retry_after(value: str | None) -> float:
    if not value:
        return 1.0
    try:
        return float(int(value.strip()))
    except (ValueError, TypeError):
        return 1.0


async def provision_app_user(
    env: dict[str, Any],
    settings: Any,
    token: str | None,
) -> AppUserProvisionResult:
    """POST to BAP ``addAppUser`` for one environment.

    addAppUser auto-assigns System Administrator per Microsoft docs, so no
    separate role-assignment step is performed here.
    """
    env_id = str(env.get("name", ""))
    client_id = getattr(settings, "client_id", None)
    if not client_id:
        return AppUserProvisionResult(
            env_id=env_id,
            status="error",
            error_code="missing_client_id",
            error_message="Settings.client_id is required for addAppUser",
        )

    url = (
        f"{_BAP_BASE}/providers/Microsoft.BusinessAppPlatform/scopes/admin"
        f"/environments/{env_id}/addAppUser?api-version={_API_VERSION}"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {"servicePrincipalAppId": client_id}
    timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)

    attempt = 0
    async with httpx.AsyncClient(timeout=timeout) as client:
        while True:
            try:
                resp = await client.post(url, headers=headers, json=body)
            except httpx.TransportError as exc:
                if attempt >= _MAX_RETRIES:
                    return AppUserProvisionResult(
                        env_id=env_id,
                        status="error",
                        error_code="network_error",
                        error_message=str(exc),
                    )
                await asyncio.sleep(float(2 ** attempt))
                attempt += 1
                continue

            status = resp.status_code

            if status == 200:
                return AppUserProvisionResult(
                    env_id=env_id, status="success", http_status=200
                )

            if status == 429:
                if attempt >= _MAX_RETRIES:
                    code, msg = _extract_error(resp)
                    return AppUserProvisionResult(
                        env_id=env_id,
                        status="error",
                        error_code=code or "throttled",
                        error_message=msg,
                        http_status=status,
                    )
                await asyncio.sleep(_parse_retry_after(resp.headers.get("Retry-After")))
                attempt += 1
                continue

            if 500 <= status < 600:
                if attempt >= _MAX_RETRIES:
                    code, msg = _extract_error(resp)
                    return AppUserProvisionResult(
                        env_id=env_id,
                        status="error",
                        error_code=code,
                        error_message=msg,
                        http_status=status,
                    )
                await asyncio.sleep(float(2 ** attempt))
                attempt += 1
                continue

            code, msg = _extract_error(resp)
            return AppUserProvisionResult(
                env_id=env_id,
                status="error",
                error_code=code,
                error_message=msg,
                http_status=status,
            )


async def provision_app_user_batch(
    envs: list[dict[str, Any]],
    settings: Any,
    token: str | None,
    concurrency: int = 8,
) -> dict[str, AppUserProvisionResult]:
    """Fans :func:`provision_app_user` across ``envs`` under a semaphore."""
    sem = asyncio.Semaphore(concurrency)

    async def _one(env: dict[str, Any]) -> AppUserProvisionResult:
        async with sem:
            return await provision_app_user(env, settings, token)

    results = await asyncio.gather(
        *(_one(e) for e in envs), return_exceptions=True
    )
    out: dict[str, AppUserProvisionResult] = {}
    for env, result in zip(envs, results):
        env_id = str(env.get("name", ""))
        if isinstance(result, BaseException):
            out[env_id] = AppUserProvisionResult(
                env_id=env_id,
                status="error",
                error_message=str(result),
            )
        else:
            out[env_id] = result
    return out
