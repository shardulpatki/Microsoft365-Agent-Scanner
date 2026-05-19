"""Per-environment Dataverse row for the setup wizard's Step 7.

Renders one row per Power Platform environment: display name, Dataverse host,
status indicator, a deep link to the admin center, and a Re-check button that
calls :func:`m365_mcp_scanner.auth.doctor.check_dataverse` and caches the
result in ``st.session_state.status.dataverse_envs``.
"""
from __future__ import annotations

import asyncio
from typing import Any

import streamlit as st

from m365_mcp_scanner.auth import doctor
from m365_mcp_scanner.config import Settings
from m365_mcp_scanner.provisioning.app_user_provisioner import (
    AppUserProvisionResult,
)
from m365_mcp_scanner.ui.wizard_logic import admin_center_deep_link


def deep_link(env_id: str) -> str | None:
    """Build the admin-center deep link, or None if env_id is malformed."""
    return admin_center_deep_link(env_id)


def render(
    env: dict[str, Any],
    settings: Settings,
    *,
    status_override: str | None = None,
) -> Any:
    """Render one environment row.

    When ``status_override`` is provided, the status cell is rendered into an
    :func:`st.empty` placeholder seeded with that text and returned, so the
    caller can update it as an in-flight check resolves.
    """
    env_id = str(env.get("name", ""))
    properties = env.get("properties") or {}
    display = properties.get("displayName") or env_id or "(unknown)"
    linked = properties.get("linkedEnvironmentMetadata") or {}
    instance_url = linked.get("instanceApiUrl") or "—"

    cached = st.session_state.status.dataverse_envs.get(env_id)
    if cached is True:
        status_icon = "✅"
    elif cached is False:
        status_icon = "❌"
    else:
        status_icon = "—"

    cols = st.columns([3, 4, 1, 2, 2])
    cols[0].write(display)
    cols[1].code(instance_url, language="text")
    status_slot: Any = None
    if status_override is not None:
        status_slot = cols[2].empty()
        status_slot.write(status_override)
    else:
        cols[2].write(status_icon)

    link = deep_link(env_id)
    if link is None:
        cols[3].error(f"malformed env_id: {env_id!r}")
    else:
        cols[3].link_button("Open in admin center", link)

    if cols[4].button("Re-check", key=f"recheck_{env_id}"):
        result = asyncio.run(doctor.check_dataverse(settings, env))
        st.session_state.status.dataverse_envs[env_id] = result.status == "pass"
        st.rerun()

    return status_slot


def render_step_6_row(
    env: dict[str, Any],
    is_selected: bool,
    result: AppUserProvisionResult | None,
    on_toggle: Any,
    on_retry: Any,
) -> None:
    """Render one row in the Step 6 auto-provision table.

    Columns: checkbox, display name, env id, status icon, retry button (only
    rendered when ``result`` is an error).
    """
    env_id = str(env.get("name", ""))
    properties = env.get("properties") or {}
    display = properties.get("displayName") or env_id or "(unknown)"

    if result is None:
        status_icon = ""
    elif result.status == "success":
        status_icon = "✓"
    else:
        status_icon = "✗"

    cols = st.columns([1, 3, 4, 1, 2])
    new_selected = cols[0].checkbox(
        " ",
        value=is_selected,
        key=f"step6_select_{env_id}",
        label_visibility="collapsed",
    )
    if new_selected != is_selected:
        on_toggle(env_id)

    cols[1].write(display)
    cols[2].code(env_id, language="text")
    cols[3].write(status_icon)

    if result is not None and result.status == "error":
        if cols[4].button("Retry", key=f"step6_retry_{env_id}"):
            on_retry(env)
    else:
        cols[4].write("")
