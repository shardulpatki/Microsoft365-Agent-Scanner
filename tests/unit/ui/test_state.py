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
    assert s["wizard"].bootstrap_token is None
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


def test_needs_reset_when_key_missing(
    fake_streamlit: types.ModuleType,
) -> None:
    from m365_mcp_scanner.ui.state import WizardState, _needs_reset

    assert _needs_reset("wizard", WizardState) is True


def test_needs_reset_when_wrong_type(
    fake_streamlit: types.ModuleType,
) -> None:
    from m365_mcp_scanner.ui.state import WizardState, _needs_reset

    fake_streamlit.session_state["wizard"] = "not a dataclass"  # type: ignore[attr-defined]
    assert _needs_reset("wizard", WizardState) is True


def test_needs_reset_when_fields_missing(
    fake_streamlit: types.ModuleType,
) -> None:
    from m365_mcp_scanner.ui.state import WizardState, _needs_reset

    stale = WizardState()
    delattr(stale, "step_6_provisioning")
    fake_streamlit.session_state["wizard"] = stale  # type: ignore[attr-defined]
    assert _needs_reset("wizard", WizardState) is True


def test_needs_reset_returns_false_when_valid(
    fake_streamlit: types.ModuleType,
) -> None:
    from m365_mcp_scanner.ui.state import WizardState, _needs_reset

    fake_streamlit.session_state["wizard"] = WizardState()  # type: ignore[attr-defined]
    assert _needs_reset("wizard", WizardState) is False


def test_init_session_state_resets_stale_wizard(
    fake_streamlit: types.ModuleType,
) -> None:
    from m365_mcp_scanner.ui.state import WizardState, init_session_state

    stale = WizardState()
    delattr(stale, "step_6_provisioning")
    fake_streamlit.session_state["wizard"] = stale  # type: ignore[attr-defined]

    init_session_state()

    fresh = fake_streamlit.session_state["wizard"]  # type: ignore[attr-defined]
    assert fresh is not stale
    assert isinstance(fresh, WizardState)
    assert hasattr(fresh, "step_6_provisioning")
    assert fresh.step_6_provisioning is False
