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
    monkeypatch.setattr(runners.shutil, "which", lambda name: f"/fake/{name}")
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
    monkeypatch.setattr(runners.shutil, "which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(runners.subprocess, "Popen", lambda *a, **kw: fake)

    output = list(runners.stream_subprocess(["x"]))
    assert output == [("", rc)]


def test_stream_subprocess_raises_when_binary_missing(
    fake_streamlit: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    from m365_mcp_scanner.ui import runners

    monkeypatch.setattr(runners.shutil, "which", lambda name: None)

    with pytest.raises(FileNotFoundError) as excinfo:
        list(runners.stream_subprocess(["nonesuch", "--flag"]))
    assert "nonesuch" in str(excinfo.value)


def test_stream_subprocess_passes_resolved_path_to_popen(
    fake_streamlit: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    from m365_mcp_scanner.ui import runners

    captured: dict[str, Any] = {}

    def fake_popen(argv: list[str], *args: Any, **kwargs: Any) -> _FakePopen:
        captured["argv"] = argv
        return _FakePopen([], returncode=0)

    monkeypatch.setattr(runners.shutil, "which", lambda name: "C:/fake/az.cmd")
    monkeypatch.setattr(runners.subprocess, "Popen", fake_popen)

    list(runners.stream_subprocess(["az", "login", "--use-device-code"]))
    assert captured["argv"][0] == "C:/fake/az.cmd"
    assert captured["argv"][1:] == ["login", "--use-device-code"]


def test_stream_subprocess_threads_env_to_popen(
    fake_streamlit: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    from m365_mcp_scanner.ui import runners

    captured: dict[str, Any] = {}

    def fake_popen(argv: list[str], *args: Any, **kwargs: Any) -> _FakePopen:
        captured["env"] = kwargs.get("env")
        return _FakePopen([], returncode=0)

    monkeypatch.setattr(runners.shutil, "which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(runners.subprocess, "Popen", fake_popen)

    list(
        runners.stream_subprocess(
            ["pwsh", "-Command", "echo hi"],
            env={"MCP_APP_ID": "abc", "PATH": "/usr/bin"},
        )
    )
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["MCP_APP_ID"] == "abc"


def test_stream_subprocess_default_call_passes_no_env(
    fake_streamlit: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing callers that omit env must continue to receive env=None."""
    from m365_mcp_scanner.ui import runners

    captured: dict[str, Any] = {}

    def fake_popen(argv: list[str], *args: Any, **kwargs: Any) -> _FakePopen:
        captured["env"] = kwargs.get("env", "MISSING")
        return _FakePopen([], returncode=0)

    monkeypatch.setattr(runners.shutil, "which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(runners.subprocess, "Popen", fake_popen)

    list(runners.stream_subprocess(["x"]))
    assert captured["env"] is None


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


def test_run_scan_cmd_includes_probe_when_enabled(fake_streamlit: types.ModuleType) -> None:
    from m365_mcp_scanner.ui.runners import run_scan_cmd

    cmd = run_scan_cmd(probe=True)
    assert "--probe" in cmd


def test_run_scan_cmd_omits_probe_by_default(fake_streamlit: types.ModuleType) -> None:
    from m365_mcp_scanner.ui.runners import run_scan_cmd

    assert "--probe" not in run_scan_cmd()
    assert "--probe" not in run_scan_cmd(probe=False)
