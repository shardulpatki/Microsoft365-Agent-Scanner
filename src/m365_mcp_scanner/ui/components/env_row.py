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
from m365_mcp_scanner.ui.wizard_logic import admin_center_deep_link


def deep_link(env_id: str) -> str | None:
    """Build the admin-center deep link, or None if env_id is malformed."""
    return admin_center_deep_link(env_id)


def render(env: dict[str, Any], settings: Settings) -> None:
    """Render one environment row."""
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
