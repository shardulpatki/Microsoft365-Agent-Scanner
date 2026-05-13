"""Reusable health checks shared by the CLI ``doctor`` command and the UI Status page.

Each public check returns a :class:`CheckResult` so callers can render it however they
like. The CLI keeps its existing Rich-formatted output by re-rendering these results;
the Streamlit UI consumes them as structured data. The module must remain renderer-free
(no Rich, no print) — it only produces data.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx

from m365_mcp_scanner.auth.msal_broker import (
    AppOnlyTokenProvider,
    AuthError,
    DelegatedTokenProvider,
    dataverse_scope,
)
from m365_mcp_scanner.clients.base import BaseAsyncClient
from m365_mcp_scanner.clients.graph import GraphClient
from m365_mcp_scanner.clients.power_platform_admin import PowerPlatformAdminClient
from m365_mcp_scanner.config import Settings

Audience = Literal["graph", "power_platform", "dataverse", "delegated"]
Status = Literal["pass", "fail"]

# Sentinel prefixes for the delegated check's three sub-states. The CLI renderer
# inspects ``CheckResult.detail`` to pick yellow vs red, since ``status`` is only
# pass/fail and both "misconfigured" and "not logged in" map to "fail".
DELEGATED_MISCONFIGURED_PREFIX = "not available"
DELEGATED_NOT_LOGGED_IN_PREFIX = "not logged in"


@dataclass(frozen=True)
class CheckResult:
    name: str
    audience: Audience
    status: Status
    detail: str


async def check_graph(settings: Settings) -> CheckResult:
    """Mint an app-only Graph token and ping ``/external/connections``."""
    try:
        provider = AppOnlyTokenProvider(
            tenant_id=settings.tenant_id,
            client_id=settings.client_id,
            client_secret=settings.client_secret.get_secret_value(),
        )
    except AuthError as exc:
        return CheckResult(
            name="Graph",
            audience="graph",
            status="fail",
            detail=f"auth misconfigured: {exc}",
        )
    async with GraphClient(provider) as graph:
        try:
            await provider.get_token()
        except AuthError as exc:
            return CheckResult(
                name="Graph",
                audience="graph",
                status="fail",
                detail=f"token mint failed: {exc}",
            )
        ok, msg = await graph.doctor_ping()
    return CheckResult(
        name="Graph",
        audience="graph",
        status="pass" if ok else "fail",
        detail=msg,
    )


async def check_power_platform(settings: Settings) -> CheckResult:
    """Hit the Power Platform admin environments endpoint."""
    try:
        provider = AppOnlyTokenProvider(
            tenant_id=settings.tenant_id,
            client_id=settings.client_id,
            client_secret=settings.client_secret.get_secret_value(),
        )
    except AuthError as exc:
        return CheckResult(
            name="Power Platform",
            audience="power_platform",
            status="fail",
            detail=f"auth misconfigured: {exc}",
        )
    pp = PowerPlatformAdminClient(token_provider=provider)
    try:
        ok, msg = await pp.doctor_ping()
    finally:
        await pp.aclose()
    return CheckResult(
        name="Power Platform",
        audience="power_platform",
        status="pass" if ok else "fail",
        detail=msg,
    )


async def check_dataverse(settings: Settings, env: dict[str, Any]) -> CheckResult:
    """Per-environment Dataverse reachability check.

    ``env`` is one item from :meth:`PowerPlatformAdminClient.list_environments`.
    Reads the Dataverse org URL from
    ``properties.linkedEnvironmentMetadata.instanceApiUrl`` and pings
    ``/api/data/v9.2/bots?$top=1``.
    """
    properties = env.get("properties") or {}
    linked = properties.get("linkedEnvironmentMetadata") or {}
    org_url = linked.get("instanceApiUrl")
    display = (
        properties.get("displayName")
        or env.get("name")
        or "(unknown env)"
    )
    if not org_url:
        return CheckResult(
            name=display,
            audience="dataverse",
            status="fail",
            detail="environment has no linked Dataverse",
        )
    try:
        provider = AppOnlyTokenProvider(
            tenant_id=settings.tenant_id,
            client_id=settings.client_id,
            client_secret=settings.client_secret.get_secret_value(),
        )
    except AuthError as exc:
        return CheckResult(
            name=display,
            audience="dataverse",
            status="fail",
            detail=f"auth misconfigured: {exc}",
        )
    client = BaseAsyncClient(
        token_provider=provider,
        scope=dataverse_scope(org_url),
        base_url=org_url.rstrip("/"),
        client_name=f"doctor-dataverse[{display}]",
    )
    try:
        try:
            resp = await client.request(
                "GET", "/api/data/v9.2/bots", params={"$top": 1}
            )
        except httpx.HTTPError as exc:
            return CheckResult(
                name=display,
                audience="dataverse",
                status="fail",
                detail=f"transport error: {exc}",
            )
    finally:
        await client.aclose()
    if resp.status_code == 200:
        return CheckResult(
            name=display,
            audience="dataverse",
            status="pass",
            detail=f"{org_url} reachable",
        )
    if resp.status_code in (401, 403):
        return CheckResult(
            name=display,
            audience="dataverse",
            status="fail",
            detail=(
                f"{resp.status_code} — scanner SP not added as application user "
                "or missing security role"
            ),
        )
    return CheckResult(
        name=display,
        audience="dataverse",
        status="fail",
        detail=f"unexpected status {resp.status_code}: {resp.text[:200]}",
    )


def check_delegated_session(settings: Settings) -> CheckResult:
    """Synchronous status read of the cached delegated session.

    Three sub-states encoded in ``detail`` (see module-level prefix constants):
    misconfigured, not-logged-in, or logged-in (UPN).
    """
    try:
        delegated = DelegatedTokenProvider(
            tenant_id=settings.tenant_id, client_id=settings.client_id
        )
    except AuthError as exc:
        return CheckResult(
            name="Delegated session",
            audience="delegated",
            status="fail",
            detail=f"{DELEGATED_MISCONFIGURED_PREFIX} ({exc})",
        )
    if delegated.is_logged_in():
        upn = delegated.account_username() or "(unknown user)"
        return CheckResult(
            name="Delegated session",
            audience="delegated",
            status="pass",
            detail=upn,
        )
    return CheckResult(
        name="Delegated session",
        audience="delegated",
        status="fail",
        detail=(
            f"{DELEGATED_NOT_LOGGED_IN_PREFIX} "
            "(run `mcp-scan login` to enable Phase 3 surfaces)"
        ),
    )


async def run_all(settings: Settings) -> list[CheckResult]:
    """Run the CLI's three checks in order: graph, power_platform, delegated."""
    graph_result = await check_graph(settings)
    pp_result = await check_power_platform(settings)
    delegated_result = check_delegated_session(settings)
    return [graph_result, pp_result, delegated_result]
