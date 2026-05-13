from __future__ import annotations

import asyncio

import streamlit as st

from m365_mcp_scanner.auth.msal_broker import AuthError, DelegatedTokenProvider
from m365_mcp_scanner.config import Settings
from m365_mcp_scanner.ui.components import render_status_panel
from m365_mcp_scanner.ui.doctor_ui import HealthSummary, full_health_check
from m365_mcp_scanner.ui.state import init_session_state

init_session_state()

st.title("Status")

try:
    settings = Settings()
except Exception as exc:  # noqa: BLE001
    st.error(f"Config load failed: {exc}")
    st.stop()


if "status_summary" not in st.session_state:
    st.session_state.status_summary = HealthSummary()


col_run, col_signout = st.columns([1, 1])
if col_run.button("Re-run all checks"):
    with st.spinner("Running doctor checks…"):
        try:
            st.session_state.status_summary = full_health_check(settings)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Doctor checks failed: {exc}")

summary: HealthSummary = st.session_state.status_summary
render_status_panel(summary, settings)

st.divider()
st.subheader("Delegated sign-in")

try:
    broker = DelegatedTokenProvider(
        tenant_id=settings.tenant_id, client_id=settings.client_id
    )
except AuthError as exc:
    st.error(f"Delegated auth misconfigured: {exc}")
    broker = None

if broker is not None:
    if broker.is_logged_in():
        upn = broker.account_username() or "(unknown)"
        st.write(f"Signed in as **{upn}**")
        if col_signout.button("Sign out"):
            broker.clear_cache()
            st.session_state.status.delegated_account = None
            st.rerun()
    else:
        if st.button("Sign in for delegated surfaces", type="primary"):
            try:
                flow = asyncio.run(broker.start_device_flow())
            except AuthError as exc:
                st.error(f"Could not start device flow: {exc}")
            else:
                st.session_state["delegated_flow"] = flow

        flow = st.session_state.get("delegated_flow")
        if flow is not None:
            st.code(flow["user_code"], language="text")
            st.link_button("Open Microsoft sign-in", flow["verification_uri"])
            with st.spinner("Waiting for sign-in to complete…"):
                try:
                    asyncio.run(broker.complete_device_flow(flow))
                except AuthError as exc:
                    st.error(f"Sign-in failed: {exc}")
                    st.session_state.pop("delegated_flow", None)
                else:
                    st.session_state.pop("delegated_flow", None)
                    st.session_state.status.delegated_account = (
                        broker.account_username()
                    )
                    st.success("Signed in.")
                    st.rerun()
