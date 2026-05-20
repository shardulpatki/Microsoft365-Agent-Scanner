from __future__ import annotations

from pathlib import Path

import pytest

from m365_mcp_scanner.config import Settings


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect Path.home() to tmp_path and return the scanner config dir."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_dir = tmp_path / ".m365-mcp-scanner"
    cfg_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    for var in ("M365_MCP_TENANT_ID", "M365_MCP_CLIENT_ID", "M365_MCP_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    return cfg_dir


def _write_toml(cfg_dir: Path, **kv: str) -> None:
    body = "".join(f'{k} = "{v}"\n' for k, v in kv.items())
    (cfg_dir / "config.toml").write_text(body, encoding="utf-8")


def _write_dotenv(cwd: Path, **kv: str) -> None:
    body = "".join(f"{k}={v}\n" for k, v in kv.items())
    (cwd / ".env").write_text(body, encoding="utf-8")


def test_settings_reads_config_toml(fake_home: Path) -> None:
    _write_toml(
        fake_home,
        tenant_id="toml-tenant",
        client_id="toml-client",
        client_secret="toml-secret",
    )
    s = Settings()
    assert s.tenant_id == "toml-tenant"
    assert s.client_id == "toml-client"
    assert s.client_secret.get_secret_value() == "toml-secret"


def test_env_var_overrides_config_toml(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_toml(fake_home, tenant_id="toml-tenant", client_id="toml-client")
    monkeypatch.setenv("M365_MCP_TENANT_ID", "env-tenant")
    s = Settings()
    assert s.tenant_id == "env-tenant"
    assert s.client_id == "toml-client"


def test_config_toml_overrides_env_file(fake_home: Path) -> None:
    # Regression: stale .env in cwd must NOT override wizard-written toml.
    _write_dotenv(
        Path.cwd(),
        M365_MCP_TENANT_ID="dotenv-tenant",
        M365_MCP_CLIENT_ID="dotenv-client",
    )
    _write_toml(fake_home, tenant_id="toml-tenant", client_id="toml-client")
    s = Settings()
    assert s.tenant_id == "toml-tenant"
    assert s.client_id == "toml-client"


def test_init_kwarg_overrides_all(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_dotenv(Path.cwd(), M365_MCP_TENANT_ID="dotenv-tenant")
    _write_toml(fake_home, tenant_id="toml-tenant")
    monkeypatch.setenv("M365_MCP_TENANT_ID", "env-tenant")
    s = Settings(tenant_id="kwarg-tenant")
    assert s.tenant_id == "kwarg-tenant"


def test_missing_config_toml_is_silent(fake_home: Path) -> None:
    # fake_home creates the dir but no config.toml; no .env; no env vars.
    s = Settings()
    assert s.tenant_id == ""
    assert s.client_id == ""
    assert s.client_secret.get_secret_value() == ""
