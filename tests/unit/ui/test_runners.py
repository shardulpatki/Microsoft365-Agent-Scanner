from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest


class _FakePopen:
    def __init__(self, lines: list[str], returncode: int) -> None:
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self) -> int:
        return self.returncode


def test_stream_subprocess_yields_lines_then_returncode(
    fake_streamlit: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    from m365_mcp_scanner.ui import runners

    fake = _FakePopen(["alpha\n", "beta\r\n", "gamma"], returncode=0)
    monkeypatch.setattr(runners.subprocess, "Popen", lambda *a, **kw: fake)

    output = list(runners.stream_subprocess(["does", "not", "matter"]))
    assert output[:-1] == [("alpha", None), ("beta", None), ("gamma", None)]
    assert output[-1] == ("", 0)


@pytest.mark.parametrize("rc", [0, 1, 2, 4])
def test_stream_subprocess_exit_codes(
    fake_streamlit: types.ModuleType, monkeypatch: pytest.MonkeyPatch, rc: int
) -> None:
    from m365_mcp_scanner.ui import runners

    fake = _FakePopen([], returncode=rc)
    monkeypatch.setattr(runners.subprocess, "Popen", lambda *a, **kw: fake)

    output = list(runners.stream_subprocess(["x"]))
    assert output == [("", rc)]


def test_run_scan_cmd_uses_sys_executable(fake_streamlit: types.ModuleType) -> None:
    from m365_mcp_scanner.ui.runners import run_scan_cmd

    cmd = run_scan_cmd(scope=["copilot_studio", "first_party_mcp"])
    assert cmd[0] == sys.executable
    assert cmd[1:5] == ["-m", "m365_mcp_scanner.cli.main", "run", "--scope"]
    assert cmd[5] == "copilot_studio,first_party_mcp"


def test_run_scan_cmd_omits_scope_when_none(fake_streamlit: types.ModuleType) -> None:
    from m365_mcp_scanner.ui.runners import run_scan_cmd

    cmd = run_scan_cmd()
    assert "--scope" not in cmd
