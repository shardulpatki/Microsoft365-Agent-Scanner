from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import streamlit as st


@dataclass
class WizardState:
    step: int = 1
    tenant_id: Optional[str] = None
    app_name: str = "M365 MCP Scanner"
    az_logged_in: bool = False
    provisioned_at: Optional[datetime] = None
    target_env_id: Optional[str] = None
    client_id: Optional[str] = None
    app_object_id: Optional[str] = None
    step_2_editing: bool = False
    step_4_started: bool = False
    step_6_started: bool = False


@dataclass
class StatusCache:
    graph_ok: Optional[bool] = None
    pp_admin_ok: Optional[bool] = None
    dataverse_envs: dict[str, bool] = field(default_factory=dict)
    delegated_account: Optional[str] = None
    last_checked: Optional[datetime] = None


@dataclass
class ScanContext:
    selected_scan_id: Optional[str] = None
    last_run_scan_id: Optional[str] = None
    current_run_proc: Optional[int] = None


def init_session_state() -> None:
    if "wizard" not in st.session_state:
        st.session_state.wizard = WizardState()
    if "status" not in st.session_state:
        st.session_state.status = StatusCache()
    if "scan" not in st.session_state:
        st.session_state.scan = ScanContext()
