"""Verify ``mcp-scan run --probe`` flips ``Settings.probe_enabled`` before
the orchestrator runs."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from typer.testing import CliRunner

from m365_mcp_scanner.cli import main as cli_main
from m365_mcp_scanner.cli.main import app


def _patch_pipeline(monkeypatch: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def _fake_pipeline(scopes: Any, settings: Any, **_: Any) -> Any:
        captured["settings"] = settings
        captured["scopes"] = scopes
        return MagicMock()

    monkeypatch.setattr(cli_main, "run_pipeline", _fake_pipeline)
    monkeypatch.setattr(cli_main, "dump_scan_document", lambda _doc: "{}")
    monkeypatch.setattr(cli_main, "write_stdout", lambda _s: None)
    return captured


def test_run_with_probe_flag_sets_probe_enabled(monkeypatch: Any) -> None:
    monkeypatch.delenv("M365_MCP_PROBE_ENABLED", raising=False)
    captured = _patch_pipeline(monkeypatch)

    result = CliRunner().invoke(app, ["run", "--probe", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert captured["settings"].probe_enabled is True


def test_run_without_probe_flag_leaves_probe_disabled(monkeypatch: Any) -> None:
    monkeypatch.delenv("M365_MCP_PROBE_ENABLED", raising=False)
    captured = _patch_pipeline(monkeypatch)

    result = CliRunner().invoke(app, ["run", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert captured["settings"].probe_enabled is False
