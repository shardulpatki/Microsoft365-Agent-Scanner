from __future__ import annotations

import os

import streamlit as st

from m365_mcp_scanner.config import Settings
from m365_mcp_scanner.ui.loaders import scans_dir
from m365_mcp_scanner.ui.runners import run_scan_cmd, stream_subprocess
from m365_mcp_scanner.ui.state import init_session_state

init_session_state()

st.title("Run Scan")

SURFACES = [
    "synced_copilot_connectors",
    "first_party_mcp",
    "custom_connectors",
    "copilot_studio",
    "declarative_agents_packages",
    "declarative_agents_teamsapp",
]

try:
    settings = Settings()
except Exception as exc:  # noqa: BLE001
    st.error(f"Config load failed: {exc}")
    st.stop()


scope = st.multiselect("Surfaces", SURFACES, default=SURFACES)
probe = st.checkbox(
    "Enable probe (--probe)",
    value=False,
    help="Makes outbound HEAD calls to non-Microsoft domains to fingerprint advertised tools.",
)
if probe:
    st.warning("Probe will issue outbound calls to non-Microsoft domains.")

st.slider(
    "Activity window (days)",
    min_value=1,
    max_value=90,
    value=30,
    disabled=True,
    help="Available once the Enrich stage ships.",
)


def _on_run() -> None:
    cmd = run_scan_cmd(scope=scope or None, probe=probe)
    env = os.environ.copy()
    if probe:
        env["M365_MCP_PROBE_ENABLED"] = "true"

    folder = scans_dir(settings)
    before = set(folder.glob("*.json")) if folder.exists() else set()

    with st.status("Running scan…", expanded=True) as status:
        log_area = st.empty()
        lines: list[str] = []
        rc: int | None = None
        for line, exit_code in stream_subprocess(cmd):
            if exit_code is None:
                lines.append(line)
                log_area.code("\n".join(lines[-30:]) or " ", language="text")
            else:
                rc = exit_code
        _handle_exit(status, rc, lines, before, folder)


def _handle_exit(status, rc, lines, before, folder) -> None:  # type: ignore[no-untyped-def]
    after = set(folder.glob("*.json")) if folder.exists() else set()
    new_files = sorted(after - before, key=lambda p: p.stat().st_mtime, reverse=True)
    new_scan_id: str | None = None
    if new_files:
        stem = new_files[0].stem
        if "_" in stem:
            new_scan_id = stem.rsplit("_", 1)[-1]
        st.session_state.scan.last_run_scan_id = new_scan_id
        st.session_state.scan.selected_scan_id = new_scan_id

    if rc == 0:
        status.update(label="Scan complete", state="complete")
        st.success(f"Scan finished. scan_id={new_scan_id or '?'}")
        st.page_link("pages/03_Agents.py", label="View results →")
    elif rc == 1:
        status.update(label="Completed with warnings", state="complete")
        st.warning(
            "Scan completed with code 1 (partial failure or expected blockers). "
            "Check the Errors page."
        )
        if new_scan_id:
            st.page_link("pages/05_Errors.py", label="View errors →")
    elif rc == 2:
        status.update(label="Scan failed", state="error")
        st.error("Scan failed (exit 2). Last output:")
        st.code("\n".join(lines[-50:]) or "(no output)", language="text")
    elif rc == 4:
        status.update(label="Auth error", state="error")
        st.error("Authentication failed (exit 4).")
        st.page_link("pages/01_Status.py", label="Open Status →")
    else:
        status.update(label=f"Scan failed (exit {rc})", state="error")
        st.error(f"Scan exited with code {rc}.")
        st.code("\n".join(lines) or "(no output)", language="text")


if st.button("Run scan", type="primary"):
    _on_run()
