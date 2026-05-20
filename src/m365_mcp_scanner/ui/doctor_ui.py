"""In-process adapter that turns the async doctor module into UI-friendly data.

Streamlit is synchronous; the doctor checks are async. We wrap them with
``asyncio.run`` and collapse the per-audience results into a ``HealthSummary``
that the Status page renders directly.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from m365_mcp_scanner.auth import doctor as doctor_module
from m365_mcp_scanner.auth.doctor import CheckResult, check_delegated_session
from m365_mcp_scanner.config import Settings


@dataclass
class HealthSummary:
    graph_ok: bool | None = None
    pp_admin_ok: bool | None = None
    delegated_account: str | None = None
    dataverse_envs: dict[str, bool] = field(default_factory=dict)
    details: list[CheckResult] = field(default_factory=list)
    last_checked: datetime | None = None

    @property
    def all_green(self) -> bool:
        return bool(self.graph_ok) and bool(self.pp_admin_ok)


def _settings_or_none() -> Settings | None:
    try:
        return Settings()
    except Exception:  # noqa: BLE001 - empty/missing config should not crash UI
        return None


def quick_health_check() -> HealthSummary:
    """Lightweight check used by the bootstrap router. No API calls.

    Reads the cached delegated session status and reflects whether the config
    has values for graph/pp; this lets the router decide between Status and
    Run Scan landing without going to the network.
    """
    summary = HealthSummary(last_checked=datetime.now(timezone.utc))
    settings = _settings_or_none()
    if settings is None or not (settings.tenant_id and settings.client_id):
        return summary
    summary.graph_ok = bool(settings.client_secret.get_secret_value())
    summary.pp_admin_ok = summary.graph_ok
    try:
        delegated = check_delegated_session(settings)
    except Exception:  # noqa: BLE001
        return summary
    summary.details.append(delegated)
    if delegated.status == "pass":
        summary.delegated_account = delegated.detail
    return summary


def full_health_check(settings: Settings | None = None) -> HealthSummary:
    """Run the same three checks as ``mcp-scan doctor`` and aggregate."""
    settings = settings if settings is not None else Settings()
    results = asyncio.run(doctor_module.run_all(settings))
    summary = HealthSummary(
        details=list(results), last_checked=datetime.now(timezone.utc)
    )
    for r in results:
        if r.audience == "graph":
            summary.graph_ok = r.status == "pass"
        elif r.audience == "power_platform":
            summary.pp_admin_ok = r.status == "pass"
        elif r.audience == "delegated":
            summary.delegated_account = r.detail if r.status == "pass" else None
    return summary
