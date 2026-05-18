from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import streamlit as st


@dataclass
class WizardState:
    step: int = 1
    tenant_id: Optional[str] = None
    app_name: str = "M365 MCP Scanner"
    bootstrap_token: Optional[str] = None
    bootstrap_account: Optional[dict] = None
    bootstrap_upn: Optional[str] = None
    provisioned_at: Optional[datetime] = None
    target_env_id: Optional[str] = None
    client_id: Optional[str] = None
    app_object_id: Optional[str] = None
    pp_admin_role_assigned: Optional[bool] = None
    pp_admin_role_error: Optional[str] = None
    step_2_editing: bool = False
    step_4_started: bool = False
    step_6_started: bool = False
    powerplatform_signin_attempted: bool = False
    powerplatform_signin_succeeded: bool = False


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


# Schema-change resilience: Streamlit can hold pickled dataclass
# instances across launches. When a field is added to a dataclass,
# old instances lack the field and access raises AttributeError.
# This helper detects the mismatch so init_session_state can replace
# the stale instance.
def _needs_reset(key: str, dataclass_type: type) -> bool:
    """Return True if session_state[key] is missing, of the wrong
    type, or missing fields the dataclass now defines."""
    if key not in st.session_state:
        return True
    obj = st.session_state[key]
    if not isinstance(obj, dataclass_type):
        return True
    expected_fields = {f.name for f in dataclasses.fields(dataclass_type)}
    actual_fields = set(vars(obj).keys())
    missing = expected_fields - actual_fields
    return bool(missing)


def init_session_state() -> None:
    if _needs_reset("wizard", WizardState):
        st.session_state.wizard = WizardState()
    if _needs_reset("status", StatusCache):
        st.session_state.status = StatusCache()
    if _needs_reset("scan", ScanContext):
        st.session_state.scan = ScanContext()
