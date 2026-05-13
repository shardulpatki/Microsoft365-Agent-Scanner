"""Render the four-panel health dashboard used on the Status page."""
from __future__ import annotations

import streamlit as st

from m365_mcp_scanner.config import Settings
from m365_mcp_scanner.ui.doctor_ui import HealthSummary


def _badge(ok: bool | None) -> str:
    if ok is None:
        return "—"
    return "🟢 OK" if ok else "🔴 FAIL"


def render_status_panel(summary: HealthSummary, settings: Settings) -> None:
    st.subheader("Tenant")
    cols = st.columns(3)
    cols[0].metric("Tenant ID", settings.tenant_id or "—")
    cols[1].metric("Client ID", settings.client_id or "—")
    # Secret expiry is not in Settings; needs a Graph call to /applications/{id}
    # and lives in Phase 4c with the wizard. Render a placeholder for now.
    cols[2].metric("Secret expiry", "—")

    st.subheader("App-only audiences")
    cols = st.columns(2)
    cols[0].metric("Graph", _badge(summary.graph_ok))
    cols[1].metric("Power Platform admin", _badge(summary.pp_admin_ok))

    st.subheader("Per-environment Dataverse")
    if not summary.dataverse_envs:
        st.caption("No environments checked yet.")
    else:
        for env_id, ok in summary.dataverse_envs.items():
            cols = st.columns([4, 1])
            cols[0].write(env_id)
            cols[1].write(_badge(ok))

    st.subheader("Delegated session")
    if summary.delegated_account:
        st.write(f"🟢 Signed in as **{summary.delegated_account}**")
    else:
        st.write("🔴 Not signed in")

    for detail in summary.details:
        if detail.status != "pass":
            st.caption(f"{detail.name}: {detail.detail}")
