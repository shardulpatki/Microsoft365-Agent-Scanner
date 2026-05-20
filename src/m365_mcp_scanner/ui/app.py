from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import streamlit as st

from m365_mcp_scanner.ui.state import init_session_state

CONFIG_PATH = Path.home() / ".m365-mcp-scanner" / "config.toml"
WIZARD_DONE_MARKER = Path.home() / ".m365-mcp-scanner" / ".wizard-completed"


@dataclass
class HealthResult:
    all_green: bool


def quick_health_check() -> HealthResult:
    return HealthResult(all_green=True)


st.set_page_config(
    page_title="M365 MCP Scanner",
    layout="wide",
    initial_sidebar_state="expanded",
)

init_session_state()


def route_initial_landing() -> None:
    if st.session_state.get("initial_route_done"):
        return
    st.session_state.initial_route_done = True

    if not CONFIG_PATH.exists() or not WIZARD_DONE_MARKER.exists():
        st.switch_page("pages/00_First_Run_Setup.py")

    health = quick_health_check()
    if not health.all_green:
        st.switch_page("pages/01_Status.py")
    else:
        st.switch_page("pages/02_Run_Scan.py")


route_initial_landing()

st.title("M365 MCP Scanner")
st.caption("Phase 4a scaffold — select a page from the sidebar.")
