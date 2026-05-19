"""Phase A stub provisioner for Power Platform application users.

This module exposes the public surface the wizard UI needs
(``AppUserProvisionResult``, ``provision_app_user``,
``provision_app_user_batch``) but performs no network I/O. Phase B will
replace the bodies with the real BAP ``addAppUser`` POST.

The result dataclass is intentionally *only* re-exposed from this leaf
module. ``provisioning/__init__.py`` is not touched, because re-exporting
from the package root previously triggered Streamlit page-loader
ImportError in this codebase.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal


@dataclass
class AppUserProvisionResult:
    env_id: str
    status: Literal["success", "error"]
    error_code: str | None = None
    error_message: str | None = None
    http_status: int | None = None


async def provision_app_user(
    env: dict[str, Any],
    settings: Any,
    token: str | None,
) -> AppUserProvisionResult:
    """STUB — Phase A only. Sleeps 200ms then returns success.

    Phase B replaces the body with the real BAP ``addAppUser`` POST.
    """
    await asyncio.sleep(0.2)
    env_id = str(env.get("name", ""))
    return AppUserProvisionResult(env_id=env_id, status="success")


async def provision_app_user_batch(
    envs: list[dict[str, Any]],
    settings: Any,
    token: str | None,
    concurrency: int = 8,
) -> dict[str, AppUserProvisionResult]:
    """STUB — Phase A only. Fans ``provision_app_user`` across ``envs``."""
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
