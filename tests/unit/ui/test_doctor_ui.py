from __future__ import annotations

import types

import pytest
from pydantic import SecretStr


def _fake_settings():  # type: ignore[no-untyped-def]
    from m365_mcp_scanner.config import Settings

    return Settings(
        tenant_id="t",
        client_id="c",
        client_secret=SecretStr("s"),
    )


def test_full_health_check_aggregates_run_all_results(
    fake_streamlit: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    from m365_mcp_scanner.auth.doctor import CheckResult
    from m365_mcp_scanner.ui import doctor_ui

    fake_results = [
        CheckResult(name="Graph", audience="graph", status="pass", detail="ok"),
        CheckResult(
            name="Power Platform",
            audience="power_platform",
            status="fail",
            detail="403",
        ),
        CheckResult(
            name="Delegated session",
            audience="delegated",
            status="pass",
            detail="user@example.com",
        ),
    ]

    async def fake_run_all(_settings):  # type: ignore[no-untyped-def]
        return fake_results

    monkeypatch.setattr(doctor_ui.doctor_module, "run_all", fake_run_all)

    summary = doctor_ui.full_health_check(_fake_settings())
    assert summary.graph_ok is True
    assert summary.pp_admin_ok is False
    assert summary.delegated_account == "user@example.com"
    assert summary.all_green is False
    assert len(summary.details) == 3


def test_quick_health_check_does_not_call_run_all(
    fake_streamlit: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    from m365_mcp_scanner.ui import doctor_ui

    async def boom(_settings):  # type: ignore[no-untyped-def]
        raise AssertionError("run_all must not be called from quick_health_check")

    monkeypatch.setattr(doctor_ui.doctor_module, "run_all", boom)

    # Empty config → delegated check is benign; quick_health_check must still return.
    summary = doctor_ui.quick_health_check()
    assert summary is not None
