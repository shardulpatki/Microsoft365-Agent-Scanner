"""Reusable scan selector + lazy loader for the Agents / MCP Servers / Errors pages."""
from __future__ import annotations

import streamlit as st

from m365_mcp_scanner.models import ScanDocument
from m365_mcp_scanner.ui.loaders import ScanSummary, list_scans, load_scan


def _label(summary: ScanSummary) -> str:
    started = (
        summary.started_at.strftime("%Y-%m-%d %H:%M")
        if summary.started_at is not None
        else "unknown"
    )
    return f"{started} · {summary.scan_id[:8]} ({summary.status or '—'})"


def render_scan_picker(key: str = "scan_picker") -> ScanDocument | None:
    summaries = list_scans()
    if not summaries:
        st.info("No scans found. Run a scan from the Run Scan page.")
        return None

    by_id = {s.scan_id: s for s in summaries}
    options = [s.scan_id for s in summaries]
    default = st.session_state.scan.selected_scan_id
    if default not in by_id:
        default = options[0]
    selected = st.selectbox(
        "Scan",
        options=options,
        index=options.index(default),
        format_func=lambda sid: _label(by_id[sid]),
        key=key,
    )
    st.session_state.scan.selected_scan_id = selected
    return load_scan(by_id[selected].path)
