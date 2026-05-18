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
    CliDetection,
    detect_cli,
    prewarm_powerapps_account,
    read_prewarm_status,
    run_pp_management_registration,
    validate_app_name,
    validate_env_id,
    validate_tenant_id,
    verify_pp_registration_output,
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
# Cross-page entry: target_env_id present → wizard step jumps to 6
# ---------------------------------------------------------------------------


def test_errors_page_fix_button_targets_step_6(fake_streamlit: "types.ModuleType") -> None:
    """error_section._jump_to_wizard_env jumps to step 6 and clears the latch.

    Reproduces the Fix-this re-entry bug: a stale ``step_6_started`` latch
    would suppress the auto-trigger when the operator lands on Step 6.
    """
    import inspect

    from m365_mcp_scanner.ui.components import error_section
    from m365_mcp_scanner.ui.state import WizardState

    src = inspect.getsource(error_section._jump_to_wizard_env)
    assert "wizard.step = 6" in src
    assert "wizard.step = 7" not in src
    assert "step_6_started = False" in src

    fake_streamlit.switch_page = lambda *_a, **_kw: None  # type: ignore[attr-defined]
    wizard = WizardState()
    wizard.step_6_started = True
    fake_streamlit.session_state.wizard = wizard

    error_section._jump_to_wizard_env("env-123")

    assert fake_streamlit.session_state.wizard.step == 6
    assert fake_streamlit.session_state.wizard.step_6_started is False
    assert fake_streamlit.session_state.wizard.target_env_id == "env-123"


# ---------------------------------------------------------------------------
# Step 1 — MSAL bootstrap sign-in advances to Step 2
# ---------------------------------------------------------------------------


def test_render_step_1_uses_msal_bootstrap_sign_in() -> None:
    """Step 1 renders an in-process MSAL sign-in, not az login."""
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
    end = src.index("def _device_code_prompt(")
    body = src[start:end]
    assert "wizard_logic.bootstrap_sign_in" in body
    assert "Sign in with Microsoft" in body
    assert "_advance(2)" in body
    # The az path is gone.
    assert "az_logged_in" not in body
    assert "az login" not in body
    assert "stream_subprocess" not in body


def test_render_step_1_offers_device_code_fallback() -> None:
    page = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "m365_mcp_scanner"
        / "ui"
        / "pages"
        / "00_First_Run_Setup.py"
    )
    src = page.read_text(encoding="utf-8")
    assert "bootstrap_sign_in_device_code" in src
    assert "BootstrapAuthError" in src
    assert "BootstrapAuthTimeout" in src


# ---------------------------------------------------------------------------
# Step 7 (Finish) routes to Status, not Run Scan
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# verify_pp_registration_output — Step 4 PowerShell success detection
# ---------------------------------------------------------------------------


def test_verify_pp_registration_success() -> None:
    """Header with applicationId column + appId in data row within 2 lines → True."""
    app_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    stdout = [
        "",
        "tenantId                             applicationId                        ",
        "--------                             -------------                        ",
        f"6cf34320-1234-5678-9abc-def012345678 {app_id}",
        "",
    ]
    assert verify_pp_registration_output(stdout, app_id) is True


def test_verify_pp_registration_missing_appid() -> None:
    """Header present but row contains a different GUID → False (cmdlet ran on wrong app)."""
    app_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    other = "ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee"
    stdout = [
        "tenantId                             applicationId",
        "--------                             -------------",
        f"6cf34320-1234-5678-9abc-def012345678 {other}",
    ]
    assert verify_pp_registration_output(stdout, app_id) is False


def test_verify_pp_registration_no_table() -> None:
    """Plain text with no header and no GUID → False."""
    stdout = [
        "Add-PowerAppsAccount : Sign-in cancelled.",
        "At line:1 char:1",
    ]
    assert (
        verify_pp_registration_output(
            stdout, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        )
        is False
    )


def test_verify_pp_registration_empty_app_id_returns_false() -> None:
    stdout = ["applicationId", "-------------", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]
    assert verify_pp_registration_output(stdout, "") is False


def test_run_pp_management_registration_passes_app_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The helper injects MCP_APP_ID env var and uses $env:MCP_APP_ID in pwsh."""
    captured: dict[str, object] = {}

    def fake_stream(
        cmd: list[str],
        cwd: Path | None = None,
        *,
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> object:
        captured["cmd"] = cmd
        captured["env"] = env
        captured["timeout_s"] = timeout_s

        def _gen() -> object:
            yield ("hello", None)
            yield ("", 0)

        return _gen()

    monkeypatch.setattr(wizard_logic, "stream_subprocess", fake_stream)

    app_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    list(run_pp_management_registration(app_id))

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["MCP_APP_ID"] == app_id

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd[0] == "pwsh"
    assert "-NoProfile" in cmd
    assert "-NonInteractive" in cmd
    script = cmd[-1]
    assert isinstance(script, str)
    assert "$env:MCP_APP_ID" in script
    assert "New-PowerAppManagementApp" in script
    # Guard against accidental interpolation: the literal appId must not
    # appear in the script string itself.
    assert app_id not in script

    assert captured["timeout_s"] == 300


def test_pwsh_added_to_step_1_prereqs() -> None:
    """Step 1 source includes pwsh detect_cli and all_prereqs_ok gates on it."""
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
    assert 'detect_cli("pwsh")' in body
    assert "PowerShell 7+" in body
    assert "pwsh.status ==" in body


def test_render_step_4_uses_pwsh_subprocess_and_fallback() -> None:
    """Step 4 source calls run_pp_management_registration and exposes the Manual fallback."""
    page = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "m365_mcp_scanner"
        / "ui"
        / "pages"
        / "00_First_Run_Setup.py"
    )
    src = page.read_text(encoding="utf-8")
    start = src.index("def _render_step_4(")
    end = src.index("def _render_step_5(")
    body = src[start:end]
    assert "wizard_logic.run_pp_management_registration" in body
    assert "wizard_logic.verify_pp_registration_output" in body
    assert "_render_step_4_manual_fallback" in body
    assert "Manual fallback" in src  # rendered inside helper below step 4


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


# ---------------------------------------------------------------------------
# Change 1 — Step 2 auto-confirm flow
# ---------------------------------------------------------------------------


def _step_2_source() -> str:
    page = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "m365_mcp_scanner"
        / "ui"
        / "pages"
        / "00_First_Run_Setup.py"
    )
    src = page.read_text(encoding="utf-8")
    start = src.index("def _render_step_2(")
    end = src.index("def _render_step_3(")
    return src[start:end]


def _step_3_source() -> str:
    page = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "m365_mcp_scanner"
        / "ui"
        / "pages"
        / "00_First_Run_Setup.py"
    )
    src = page.read_text(encoding="utf-8")
    start = src.index("def _render_step_3(")
    end = src.index("def _render_step_4(")
    return src[start:end]


def _step_4_source() -> str:
    page = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "m365_mcp_scanner"
        / "ui"
        / "pages"
        / "00_First_Run_Setup.py"
    )
    src = page.read_text(encoding="utf-8")
    start = src.index("def _render_step_4(")
    end = src.index("def _render_step_4_manual_fallback(")
    return src[start:end]


def test_step_2_confirm_advances_with_defaults() -> None:
    body = _step_2_source()
    assert '"Confirm and continue"' in body
    assert "type=\"primary\"" in body
    assert "step_2_editing" in body
    assert "_kick_off_prewarm()" in body
    assert "_advance(3)" in body


def test_step_2_edit_toggle_renders_form() -> None:
    body = _step_2_source()
    assert "wizard.step_2_editing = True" in body
    assert 'st.form("step2_form")' in body
    assert "if not wizard.step_2_editing:" in body


def test_step_2_edit_then_confirm_uses_edited_values() -> None:
    body = _step_2_source()
    edit_path_start = body.index('st.form("step2_form")')
    edit_body = body[edit_path_start:]
    assert "validate_tenant_id(tenant_id)" in edit_body
    assert "validate_app_name(app_name)" in edit_body
    assert "wizard.tenant_id = tenant_id" in edit_body
    assert "wizard.app_name = app_name" in edit_body
    assert "_advance(3)" in edit_body


def test_step_2_caption_mentions_second_browser_signin() -> None:
    body = _step_2_source()
    assert "second browser sign-in" in body


# ---------------------------------------------------------------------------
# Step 3 — in-process provisioner replaces bash subprocess
# ---------------------------------------------------------------------------


def test_render_step_3_uses_in_process_provisioner() -> None:
    body = _step_3_source()
    assert "wizard_logic.run_provisioning" in body
    assert "st.progress(" in body
    assert 'st.expander("Detailed output"' in body
    # No more bash / setup-scanner.sh.
    assert "setup-scanner.sh" not in body
    assert 'subprocess.Popen(["bash"' not in body
    assert "stream_subprocess" not in body


def test_render_step_3_handles_pp_admin_role_failure() -> None:
    body = _step_3_source()
    assert "pp_admin_role_assigned" in body
    assert "ProvisionError" in body


# ---------------------------------------------------------------------------
# Change 3 — prewarm Add-PowerAppsAccount
# ---------------------------------------------------------------------------


def _fake_stream_factory(yields: list[tuple[str, int | None]]):
    def fake_stream(
        cmd: list[str],
        cwd: Path | None = None,
        *,
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ):
        for item in yields:
            yield item

    return fake_stream


def test_prewarm_writes_status_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        wizard_logic,
        "stream_subprocess",
        _fake_stream_factory([("hello", None), ("", 0)]),
    )
    status_path = tmp_path / ".prewarm-status"
    list(prewarm_powerapps_account(status_path=status_path))
    data = json.loads(status_path.read_text(encoding="utf-8"))
    assert data["status"] == "succeeded"
    assert "completed_at" in data


def test_prewarm_writes_failed_status_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        wizard_logic,
        "stream_subprocess",
        _fake_stream_factory([("err", None), ("", 2)]),
    )
    status_path = tmp_path / ".prewarm-status"
    list(prewarm_powerapps_account(status_path=status_path))
    data = json.loads(status_path.read_text(encoding="utf-8"))
    assert data["status"] == "failed"


def test_prewarm_writes_failed_status_on_missing_pwsh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def raising(*_a: object, **_kw: object):
        raise FileNotFoundError("pwsh not on PATH")
        yield  # pragma: no cover — make this a generator

    monkeypatch.setattr(wizard_logic, "stream_subprocess", raising)
    status_path = tmp_path / ".prewarm-status"
    list(prewarm_powerapps_account(status_path=status_path))
    data = json.loads(status_path.read_text(encoding="utf-8"))
    assert data["status"] == "failed"


def test_read_prewarm_status_returns_not_started_for_missing_file(
    tmp_path: Path,
) -> None:
    assert read_prewarm_status(tmp_path / "does-not-exist") == "not_started"


def test_read_prewarm_status_returns_not_started_for_malformed_file(
    tmp_path: Path,
) -> None:
    p = tmp_path / ".prewarm-status"
    p.write_text("not json", encoding="utf-8")
    assert read_prewarm_status(p) == "not_started"


def test_step_4_command_skips_signin_when_prewarm_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_stream(
        cmd: list[str],
        cwd: Path | None = None,
        *,
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ):
        captured["cmd"] = cmd

        def _gen():
            yield ("ok", None)
            yield ("", 0)

        return _gen()

    monkeypatch.setattr(wizard_logic, "stream_subprocess", fake_stream)
    list(
        run_pp_management_registration(
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", skip_signin=True
        )
    )
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    script = cmd[-1]
    assert isinstance(script, str)
    assert "Add-PowerAppsAccount" not in script
    assert "New-PowerAppManagementApp" in script


def test_step_4_command_includes_signin_when_prewarm_not_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_stream(
        cmd: list[str],
        cwd: Path | None = None,
        *,
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ):
        captured["cmd"] = cmd

        def _gen():
            yield ("ok", None)
            yield ("", 0)

        return _gen()

    monkeypatch.setattr(wizard_logic, "stream_subprocess", fake_stream)
    list(
        run_pp_management_registration(
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        )
    )
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    script = cmd[-1]
    assert isinstance(script, str)
    assert "Add-PowerAppsAccount" in script
    assert "New-PowerAppManagementApp" in script


def test_render_step_4_branches_on_prewarm_status() -> None:
    body = _step_4_source()
    assert "wizard_logic.read_prewarm_status" in body
    assert "skip_signin=skip_signin" in body


def _step_6_source() -> str:
    page = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "m365_mcp_scanner"
        / "ui"
        / "pages"
        / "00_First_Run_Setup.py"
    )
    src = page.read_text(encoding="utf-8")
    start = src.index("def _render_step_6(")
    end = src.index("def _render_step_7(")
    return src[start:end]


def test_step_4_auto_starts_on_landing() -> None:
    body = _step_4_source()
    # Old button gate is gone — no more "Run PowerShell registration" click.
    assert (
        'st.button(\n        "Run PowerShell registration' not in body
    )
    assert '"Run PowerShell registration' not in body
    # Fire-once latch is in place and triggers the registration call.
    assert "wizard.step_4_started" in body
    assert "if not wizard.step_4_started:" in body
    assert "wizard.step_4_started = True" in body
    assert "_run_step_4_registration(" in body


def test_step_4_does_not_double_trigger() -> None:
    body = _step_4_source()
    # The trigger call must be guarded by the latch: the call to
    # _run_step_4_registration appears inside the `if not wizard.step_4_started:`
    # branch, never at the top level of the renderer.
    guard_idx = body.index("if not wizard.step_4_started:")
    trigger_idx = body.index("_run_step_4_registration(")
    assert guard_idx < trigger_idx, (
        "_run_step_4_registration must be invoked only after the latch check"
    )
    # The latch is flipped to True before invoking the subprocess, ensuring
    # subsequent reruns skip the trigger.
    latch_set_idx = body.index("wizard.step_4_started = True")
    assert guard_idx < latch_set_idx < trigger_idx
    # Retry path resets the latch via st.rerun, not by re-calling the trigger
    # synchronously.
    assert 'st.button("Retry"' in body
    assert "wizard.step_4_started = False" in body


def test_step_6_auto_starts_on_landing() -> None:
    body = _step_6_source()
    assert "wizard.step_6_started" in body
    assert "if not wizard.step_6_started:" in body
    assert "wizard.step_6_started = True" in body
    assert 'status_override="Checking…"' in body
    # Concurrent fan-out via wizard_logic.check_all_envs_dataverse — no more
    # sequential asyncio.run inside the per-env loop.
    assert "wizard_logic.check_all_envs_dataverse(settings, envs)" in body
    assert "asyncio.run(doctor.check_dataverse(settings, env))" not in body
    # No explicit button gate before the auto-check loop.
    assert '"Check Dataverse"' not in body


def test_step_6_continue_available_during_check() -> None:
    body = _step_6_source()
    continue_idx = body.index('st.button("Continue", type="primary"):')
    guard_idx = body.index("if not wizard.step_6_started:")
    # Continue button is rendered AFTER the in-flight check block, at the
    # function's top level — never inside the latched branch.
    assert continue_idx > guard_idx
    # The Continue button has no `disabled=` argument anywhere on its line.
    continue_line = body[continue_idx : body.index("\n", continue_idx)]
    assert "disabled" not in continue_line


# ---------------------------------------------------------------------------
# check_all_envs_dataverse — Step 6 concurrent fan-out
# ---------------------------------------------------------------------------


def _make_env(name: str) -> dict[str, Any]:
    return {"name": name, "properties": {"displayName": name}}


def _check_result(name: str, status: str = "pass") -> object:
    from m365_mcp_scanner.auth.doctor import CheckResult

    return CheckResult(
        name=name, audience="dataverse", status=status, detail=name
    )


def test_check_all_envs_dataverse_calls_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import time
    from typing import Any as _Any

    async def slow_check(
        _settings: _Any, env: dict[str, _Any]
    ) -> object:
        await asyncio.sleep(0.5)
        return _check_result(str(env["name"]))

    monkeypatch.setattr(wizard_logic.doctor, "check_dataverse", slow_check)

    envs = [_make_env("a"), _make_env("b"), _make_env("c")]
    start = time.perf_counter()
    results = asyncio.run(
        wizard_logic.check_all_envs_dataverse(object(), envs)
    )
    elapsed = time.perf_counter() - start

    assert len(results) == 3
    # Sequential would be ~1.5s; concurrent should be well under 1.0s.
    assert elapsed < 1.0, f"expected concurrent fan-out, took {elapsed:.2f}s"


def test_check_all_envs_dataverse_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from typing import Any as _Any

    async def echo_check(
        _settings: _Any, env: dict[str, _Any]
    ) -> object:
        # Sleep durations vary by env name so completion order would differ
        # from input order if gather did not preserve it.
        delays = {"a": 0.05, "b": 0.01, "c": 0.03}
        await asyncio.sleep(delays[str(env["name"])])
        return _check_result(str(env["name"]))

    monkeypatch.setattr(wizard_logic.doctor, "check_dataverse", echo_check)

    envs = [_make_env("a"), _make_env("b"), _make_env("c")]
    results = asyncio.run(
        wizard_logic.check_all_envs_dataverse(object(), envs)
    )

    assert [r.detail for r in results] == ["a", "b", "c"]


def test_check_all_envs_dataverse_propagates_individual_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from typing import Any as _Any

    async def mixed_check(
        _settings: _Any, env: dict[str, _Any]
    ) -> object:
        name = str(env["name"])
        if name == "b":
            return _check_result(name, status="fail")
        return _check_result(name, status="pass")

    monkeypatch.setattr(wizard_logic.doctor, "check_dataverse", mixed_check)

    envs = [_make_env("a"), _make_env("b"), _make_env("c")]
    results = asyncio.run(
        wizard_logic.check_all_envs_dataverse(object(), envs)
    )

    assert [r.status for r in results] == ["pass", "fail", "pass"]
    assert [r.name for r in results] == ["a", "b", "c"]
