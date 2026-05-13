from __future__ import annotations

import sys
import types

import pytest


class _SessionState(dict):  # type: ignore[type-arg]
    """Minimal stand-in for streamlit.session_state supporting attr + item access."""

    def __getattr__(self, name: str) -> object:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: object) -> None:
        self[name] = value


@pytest.fixture
def fake_streamlit(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    fake = types.ModuleType("streamlit")
    fake.session_state = _SessionState()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "streamlit", fake)
    # Force re-import so state.py picks up the fake.
    monkeypatch.delitem(sys.modules, "m365_mcp_scanner.ui.state", raising=False)
    return fake


def test_init_session_state_creates_three_dataclasses(
    fake_streamlit: types.ModuleType,
) -> None:
    from m365_mcp_scanner.ui.state import (
        ScanContext,
        StatusCache,
        WizardState,
        init_session_state,
    )

    init_session_state()
    s = fake_streamlit.session_state  # type: ignore[attr-defined]

    assert isinstance(s["wizard"], WizardState)
    assert isinstance(s["status"], StatusCache)
    assert isinstance(s["scan"], ScanContext)
    assert s["wizard"].step == 1
    assert s["wizard"].app_name == "M365 MCP Scanner"
    assert s["wizard"].tenant_id is None
    assert s["wizard"].az_logged_in is False
    assert s["status"].dataverse_envs == {}
    assert s["status"].graph_ok is None
    assert s["scan"].selected_scan_id is None
    assert s["scan"].current_run_proc is None


def test_init_session_state_idempotent(
    fake_streamlit: types.ModuleType,
) -> None:
    from m365_mcp_scanner.ui.state import init_session_state

    init_session_state()
    fake_streamlit.session_state["wizard"].step = 4  # type: ignore[attr-defined]
    init_session_state()
    assert fake_streamlit.session_state["wizard"].step == 4  # type: ignore[attr-defined]
