"""Unit tests for the Phase 4c wizard helpers (no Streamlit runtime)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from types import SimpleNamespace

from m365_mcp_scanner.ui import wizard_logic
from m365_mcp_scanner.ui.wizard_logic import (
    MIN_AZ_VERSION,
    CliDetection,
    az_account_tenant,
    detect_cli,
    ingest_setup_output,
    parse_az_version,
    validate_app_name,
    validate_env_id,
    validate_tenant_id,
    write_config_toml,
)


# ---------------------------------------------------------------------------
# validate_app_name — TRD §12 mitigation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("M365 MCP Scanner", True),
        ("scanner-prod_01", True),
        ("a", True),
        ("a" * 64, True),
        ("", False),
        ("a" * 65, False),
        ("rm -rf /", False),
        ("name;echo pwned", False),
        ("name`whoami`", False),
        ("name$(id)", False),
        ("name\nnewline", False),
    ],
)
def test_validate_app_name(name: str, expected: bool) -> None:
    assert validate_app_name(name) is expected


# ---------------------------------------------------------------------------
# validate_tenant_id / validate_env_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("6cf34320-1234-5678-9abc-def012345678", True),
        ("6CF34320-1234-5678-9ABC-DEF012345678", True),
        ("not-a-guid", False),
        ("", False),
        ("6cf34320-1234-5678-9abc-def01234567", False),  # too short
    ],
)
def test_validate_tenant_id(value: str, expected: bool) -> None:
    assert validate_tenant_id(value) is expected


def test_validate_env_id_accepts_bare_guid() -> None:
    assert validate_env_id("6cf34320-1234-5678-9abc-def012345678") is True
    assert validate_env_id("A787C566-03FD-E1C6-972D-9549DAAAD71C") is True


def test_validate_env_id_accepts_default_prefix() -> None:
    assert (
        validate_env_id("Default-6cf34320-e817-4ad9-81b6-460c24c7a4e7") is True
    )
    assert (
        validate_env_id("Default-6CF34320-E817-4AD9-81B6-460C24C7A4E7") is True
    )


def test_validate_env_id_rejects_tenant_id_like_garbage() -> None:
    assert validate_env_id("'; DROP TABLE x; --") is False
    assert validate_env_id("") is False
    assert validate_env_id("not-a-guid") is False


def test_validate_env_id_rejects_default_prefix_with_bad_guid() -> None:
    assert validate_env_id("Default-not-a-guid") is False
    assert validate_env_id("Default-") is False
    assert validate_env_id("default-6cf34320-1234-5678-9abc-def012345678") is False


# ---------------------------------------------------------------------------
# parse_az_version
# ---------------------------------------------------------------------------


def test_parse_az_version_extracts_first_line() -> None:
    stdout = "azure-cli                         2.86.0\n\ncore         2.86.0\n"
    assert parse_az_version(stdout) == (2, 86, 0)


def test_parse_az_version_returns_none_when_absent() -> None:
    assert parse_az_version("some unrelated output") is None


def test_min_az_version_constant() -> None:
    assert MIN_AZ_VERSION == (2, 50, 0)


# ---------------------------------------------------------------------------
# detect_cli — Armor19 prerequisite-check robustness
# ---------------------------------------------------------------------------


def _fail_if_called(*args: object, **kwargs: object) -> None:
    raise AssertionError(
        f"subprocess.run must not be called when shutil.which returns None "
        f"(args={args!r}, kwargs={kwargs!r})"
    )


def test_detect_cli_returns_not_on_path_when_which_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wizard_logic.shutil, "which", lambda _cmd: None)
    monkeypatch.setattr(wizard_logic.subprocess, "run", _fail_if_called)

    result = detect_cli("az")

    assert result.status == "not_on_path"
    assert result.path is None
    assert result.error is not None
    assert "not found on PATH" in result.error


def test_detect_cli_returns_ok_when_subprocess_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wizard_logic.shutil, "which", lambda _cmd: "/usr/bin/az")
    monkeypatch.setattr(
        wizard_logic.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(
            returncode=0, stdout="azure-cli 2.86.0\n", stderr=""
        ),
    )

    result = detect_cli("az")

    assert result.status == "ok"
    assert result.path == "/usr/bin/az"
    assert "azure-cli 2.86.0" in result.stdout
    assert result.error is None


def test_detect_cli_returns_found_but_failed_on_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wizard_logic.shutil, "which", lambda _cmd: "/usr/bin/az")
    monkeypatch.setattr(
        wizard_logic.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )

    result = detect_cli("az")

    assert result.status == "found_but_failed"
    assert result.path == "/usr/bin/az"
    assert result.error is not None
    assert "/usr/bin/az" in result.error
    assert "boom" in result.error


def test_detect_cli_returns_found_but_failed_on_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wizard_logic.shutil, "which", lambda _cmd: "/usr/bin/az")

    def _raise(*_a: object, **_kw: object) -> None:
        raise OSError("Exec format error")

    monkeypatch.setattr(wizard_logic.subprocess, "run", _raise)

    result = detect_cli("az")

    assert result.status == "found_but_failed"
    assert result.path == "/usr/bin/az"
    assert result.error is not None
    assert "Exec format error" in result.error


def test_detect_cli_returns_found_but_failed_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wizard_logic.shutil, "which", lambda _cmd: "/usr/bin/az")

    def _raise(*_a: object, **_kw: object) -> None:
        raise wizard_logic.subprocess.TimeoutExpired(cmd="az", timeout=30)

    monkeypatch.setattr(wizard_logic.subprocess, "run", _raise)

    result = detect_cli("az")

    assert result.status == "found_but_failed"
    assert result.path == "/usr/bin/az"
    assert isinstance(result, CliDetection)


def test_detect_cli_default_timeout_is_30() -> None:
    """The wizard's default timeout must accommodate Windows AV/process overhead."""
    import inspect

    assert inspect.signature(detect_cli).parameters["timeout"].default == 30.0
    assert inspect.signature(az_account_tenant).parameters["timeout"].default == 30.0


def test_az_version_below_minimum_still_uses_existing_parse_logic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wizard_logic.shutil, "which", lambda _cmd: "/usr/bin/az")
    monkeypatch.setattr(
        wizard_logic.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(
            returncode=0, stdout="azure-cli 2.49.0\n", stderr=""
        ),
    )

    result = detect_cli("az")
    parsed = parse_az_version(result.stdout)

    assert result.status == "ok"
    assert parsed == (2, 49, 0)
    assert parsed is not None and parsed < MIN_AZ_VERSION


# ---------------------------------------------------------------------------
# az_account_tenant — Windows PATHEXT resolution (parallel to detect_cli fix)
# ---------------------------------------------------------------------------


def test_az_account_tenant_returns_none_when_which_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wizard_logic.shutil, "which", lambda _cmd: None)
    monkeypatch.setattr(wizard_logic.subprocess, "run", _fail_if_called)

    assert az_account_tenant() is None


def test_az_account_tenant_returns_tenant_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wizard_logic.shutil, "which", lambda _cmd: "/usr/bin/az")
    monkeypatch.setattr(
        wizard_logic.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(
            returncode=0,
            stdout="6cf34320-1234-5678-9abc-def012345678\n",
            stderr="",
        ),
    )

    assert az_account_tenant() == "6cf34320-1234-5678-9abc-def012345678"


# ---------------------------------------------------------------------------
# write_config_toml
# ---------------------------------------------------------------------------


def test_write_config_toml_emits_expected_body(tmp_path: Path) -> None:
    cfg = write_config_toml(
        tenant_id="6cf34320-1234-5678-9abc-def012345678",
        client_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        client_secret="s3cr3t",
        data_dir=tmp_path,
    )
    body = cfg.read_text(encoding="utf-8")
    assert 'tenant_id = "6cf34320-1234-5678-9abc-def012345678"' in body
    assert 'client_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"' in body
    assert 'client_secret = "s3cr3t"' in body
    assert cfg == tmp_path / "config.toml"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="chmod is best-effort on Windows")
def test_write_config_toml_sets_mode_600(tmp_path: Path) -> None:
    cfg = write_config_toml(
        tenant_id="t",
        client_id="c",
        client_secret="s",
        data_dir=tmp_path,
    )
    assert (os.stat(cfg).st_mode & 0o777) == 0o600


def test_write_config_toml_creates_missing_data_dir(tmp_path: Path) -> None:
    nested = tmp_path / "deeply" / "nested"
    cfg = write_config_toml(
        tenant_id="t",
        client_id="c",
        client_secret="s",
        data_dir=nested,
    )
    assert cfg.exists()


# ---------------------------------------------------------------------------
# ingest_setup_output
# ---------------------------------------------------------------------------


def _setup_output_fixture() -> dict[str, object]:
    return {
        "client_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "client_secret": "the-secret",
        "tenant_id": "6cf34320-1234-5678-9abc-def012345678",
        "app_object_id": "11111111-2222-3333-4444-555555555555",
        "admin_consent_granted": True,
        "completed_at": "2026-05-14T12:00:00Z",
    }


def test_ingest_setup_output_writes_config_and_deletes_source(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / ".setup-output.json"
    output_path.write_text(json.dumps(_setup_output_fixture()), encoding="utf-8")

    client_id, app_object_id = ingest_setup_output(output_path, tmp_path)

    assert client_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert app_object_id == "11111111-2222-3333-4444-555555555555"
    assert not output_path.exists()

    body = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "the-secret" in body
    assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in body


def test_ingest_setup_output_raises_on_missing_field(tmp_path: Path) -> None:
    output_path = tmp_path / ".setup-output.json"
    incomplete = _setup_output_fixture()
    del incomplete["client_secret"]
    output_path.write_text(json.dumps(incomplete), encoding="utf-8")

    with pytest.raises(ValueError, match="client_secret"):
        ingest_setup_output(output_path, tmp_path)

    # Source file is left in place so the operator can retry.
    assert output_path.exists()


def test_ingest_setup_output_raises_on_malformed_json(tmp_path: Path) -> None:
    output_path = tmp_path / ".setup-output.json"
    output_path.write_text("not valid json", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        ingest_setup_output(output_path, tmp_path)


# ---------------------------------------------------------------------------
# Cross-page entry: target_env_id present → wizard step jumps to 6
# ---------------------------------------------------------------------------


def test_errors_page_fix_button_targets_step_6() -> None:
    """error_section._jump_to_wizard_env sets wizard.step = 6 (per-env Dataverse)."""
    import inspect

    from m365_mcp_scanner.ui.components import error_section

    src = inspect.getsource(error_section._jump_to_wizard_env)
    assert "wizard.step = 6" in src
    assert "wizard.step = 7" not in src


# ---------------------------------------------------------------------------
# Step 1 (merged Prereqs + Sign In) advances to Step 2 on successful az login
# ---------------------------------------------------------------------------


def test_render_step_1_advances_to_step_2_after_az_login() -> None:
    """Successful az login in merged Step 1 captures tenant_id and advances to step 2."""
    page = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "m365_mcp_scanner"
        / "ui"
        / "pages"
        / "00_First_Run_Setup.py"
    )
    src = page.read_text(encoding="utf-8")
    start = src.index("def _render_step_1(")
    end = src.index("def _render_step_2(")
    body = src[start:end]
    assert "st.session_state.wizard.tenant_id = tenant" in body
    assert "st.session_state.wizard.az_logged_in = True" in body
    assert "_advance(2)" in body
    assert "disabled=not all_prereqs_ok" in body


# ---------------------------------------------------------------------------
# Step 7 (Finish) routes to Status, not Run Scan
# ---------------------------------------------------------------------------


def test_render_step_7_finish_routes_to_status() -> None:
    """The Finish step's primary button routes to Status, not Run Scan."""
    page = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "m365_mcp_scanner"
        / "ui"
        / "pages"
        / "00_First_Run_Setup.py"
    )
    src = page.read_text(encoding="utf-8")
    start = src.index("def _render_step_7(")
    end = src.index("_RENDERERS = {")
    body = src[start:end]
    assert 'st.switch_page("pages/01_Status.py")' in body
    assert 'st.switch_page("pages/02_Run_Scan.py")' not in body
    assert '"Continue to Status"' in body
    assert '"Continue to Run Scan"' not in body
