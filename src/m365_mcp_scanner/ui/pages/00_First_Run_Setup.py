"""First-Run Setup Wizard — 7-step Streamlit flow.

Step 1 signs in via in-process MSAL (auth-code-PKCE with localhost
listener; device-code as fallback). Step 3 provisions the scanner's
Entra app + service principal directly via Microsoft Graph (async httpx)
and persists ``config.toml``. Step 4 registers the Power Platform
management app via pwsh as before.

Manual verification checklist (Armor19, post-review, not a Claude Code task):

  1. Wipe state:
       rm ~/.m365-mcp-scanner/config.toml \\
          ~/.m365-mcp-scanner/.wizard-completed
  2. Launch:    mcp-scan ui
  3. Confirm browser lands on First Run Setup (not Run Scan).
  4. Complete all 7 steps end-to-end against Armor19.
  5. End-to-end elapsed time under 5 minutes (PRD §8 #1).
  6. After "Setup complete" → "Continue to Run Scan":
       - Scan completes in ~12s.
       - All 3 Armor19 environments visible (1 ✅, 2 ❌ as expected).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import streamlit as st

logger = logging.getLogger(__name__)

from m365_mcp_scanner.auth import doctor
from m365_mcp_scanner.auth.msal_bootstrap import (
    BootstrapAuthError,
    BootstrapAuthTimeout,
)
from m365_mcp_scanner.config import Settings
from m365_mcp_scanner.provisioning import ProvisionError
from m365_mcp_scanner.ui.components import env_row
from m365_mcp_scanner.ui.state import init_session_state
from m365_mcp_scanner.ui import wizard_logic
from m365_mcp_scanner.ui.wizard_logic import (
    detect_cli,
    list_environments_sync,
    validate_app_name,
    validate_tenant_id,
)

DATA_DIR = Path.home() / ".m365-mcp-scanner"
CONFIG_TOML = DATA_DIR / "config.toml"
WIZARD_DONE_MARKER = DATA_DIR / ".wizard-completed"

_LOG_KEYS = {
    "provision": "_wizard_provision_log",
    "pp_register": "_wizard_pp_register_log",
}


# ---------------------------------------------------------------------------
# Async wrappers (Streamlit is sync; clients are async)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Step renderers
# ---------------------------------------------------------------------------


def _advance(step: int) -> None:
    wizard = st.session_state.wizard
    if step == 4:
        wizard.step_4_started = False
    if step == 6:
        wizard.step_6_started = False
    wizard.step = step
    st.rerun()


def _render_step_1() -> None:
    st.header("Step 1 of 7 — Prerequisites and Sign In")

    st.subheader("Prerequisites")
    st.write(
        "Verifying that PowerShell 7+ (`pwsh`) is installed. pwsh runs the "
        "Power Platform Management App registration in Step 4."
    )

    pwsh = detect_cli("pwsh")
    if pwsh.status == "ok":
        st.success("PowerShell 7+ (pwsh) detected")
    elif pwsh.status == "not_on_path":
        st.error(
            "pwsh not found on PATH. Install PowerShell 7+: "
            "<https://learn.microsoft.com/powershell/scripting/install/installing-powershell>"
        )
    else:
        st.error(pwsh.error or "pwsh failed to run.")

    prereqs_ok = pwsh.status == "ok"

    st.divider()

    st.subheader("Sign in with Microsoft")
    st.write(
        "Click below to sign in. Your default browser will open to "
        "Microsoft's sign-in page; sign in as a Global Administrator of the "
        "target tenant and return here. No copy-paste required."
    )

    wizard = st.session_state.wizard
    if wizard.bootstrap_token:
        st.success(
            f"Signed in as `{wizard.bootstrap_upn or 'unknown'}`. "
            f"Active tenant: `{wizard.tenant_id}`"
        )
        if st.button("Continue"):
            _advance(2)
        return

    use_device = st.session_state.get("_wizard_use_device_code", False)

    primary_label = (
        "Sign in with device code" if use_device else "Sign in with Microsoft"
    )
    start = st.button(primary_label, disabled=not prereqs_ok, type="primary")

    if start:
        with st.spinner("Waiting for browser sign-in…"):
            try:
                if use_device:
                    result = wizard_logic.bootstrap_sign_in_device_code(
                        on_prompt=_device_code_prompt
                    )
                else:
                    result = wizard_logic.bootstrap_sign_in()
            except BootstrapAuthTimeout as exc:
                st.error(f"Sign-in timed out: {exc}")
                _offer_device_code_fallback()
                return
            except BootstrapAuthError as exc:
                st.error(f"Sign-in failed: {exc}")
                _offer_device_code_fallback()
                return
            except Exception as exc:  # noqa: BLE001
                st.error(f"Sign-in raised: {exc}")
                _offer_device_code_fallback()
                return

        wizard.bootstrap_token = result.access_token
        wizard.bootstrap_account = result.account
        wizard.bootstrap_upn = result.user_principal_name
        wizard.tenant_id = result.tenant_id
        _advance(2)


def _device_code_prompt(flow: dict[str, Any]) -> None:
    """Show the device-code prompt to the operator (called from the worker
    thread; uses ``st.session_state`` only for storage, no widget mutation)."""
    st.session_state["_wizard_device_flow_message"] = flow.get("message") or (
        f"Open {flow.get('verification_uri')} and enter code "
        f"{flow.get('user_code')}."
    )


def _offer_device_code_fallback() -> None:
    if not st.session_state.get("_wizard_use_device_code"):
        st.info(
            "If a browser cannot open on this machine (locked-down network, "
            "headless host), try the device-code fallback."
        )
        if st.button("Switch to device-code sign-in"):
            st.session_state["_wizard_use_device_code"] = True
            st.rerun()
    msg = st.session_state.get("_wizard_device_flow_message")
    if msg:
        st.code(msg, language="text")


def _kick_off_prewarm() -> None:
    """Spawn a daemon thread that runs Add-PowerAppsAccount in the background.

    The thread discards subprocess output (it is not on the Streamlit thread
    and must not touch ``st.*``). Status is communicated via the on-disk
    prewarm status file that ``wizard_logic.prewarm_powerapps_account``
    writes.
    """
    def _run() -> None:
        try:
            for _line, _code in wizard_logic.prewarm_powerapps_account():
                pass
        except Exception:  # noqa: BLE001 — best-effort warm-up
            pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def _render_step_2() -> None:
    st.header("Step 2 of 7 — Confirm tenant + app name")
    wizard = st.session_state.wizard
    default_tenant = wizard.tenant_id or ""
    default_app_name = wizard.app_name

    if not wizard.step_2_editing:
        st.write(
            "These values came from `az account show` and the wizard "
            "default. Click **Confirm and continue** to use them as-is, "
            "or **Edit** to change either field."
        )
        st.markdown("**Tenant ID**")
        st.code(default_tenant, language="text")
        st.markdown("**App display name**")
        st.code(default_app_name, language="text")

        col_a, col_b = st.columns([1, 1])
        confirm = col_a.button("Confirm and continue", type="primary")
        edit = col_b.button("Edit")
        st.caption(
            "Tip: A second browser sign-in may appear shortly — that's "
            "the Power Platform session warming up. You can complete it "
            "any time before Step 4."
        )

        if confirm:
            if not validate_tenant_id(default_tenant):
                st.error(
                    "Tenant ID from `az account show` is not a valid "
                    "GUID. Click **Edit** to enter it manually."
                )
                return
            wizard.tenant_id = default_tenant
            wizard.app_name = default_app_name
            wizard.step_2_editing = False
            _kick_off_prewarm()
            _advance(3)
        if edit:
            wizard.step_2_editing = True
            st.rerun()
        return

    with st.form("step2_form"):
        tenant_id = st.text_input(
            "Tenant ID",
            value=default_tenant,
            help="GUID, copied from `az account show`.",
        )
        app_name = st.text_input(
            "App display name",
            value=default_app_name,
            help=(
                "Must match ^[A-Za-z0-9 _-]{1,64}$ "
                "(no shell metacharacters)."
            ),
        )
        submitted = st.form_submit_button("Confirm and continue")

    if submitted:
        if not validate_tenant_id(tenant_id):
            st.error("Tenant ID must be a GUID.")
            return
        if not validate_app_name(app_name):
            st.error(
                "App name must be 1–64 characters of letters, digits, spaces, "
                "underscores, or hyphens."
            )
            return
        wizard.tenant_id = tenant_id
        wizard.app_name = app_name
        wizard.step_2_editing = False
        _kick_off_prewarm()
        _advance(3)


def _render_step_3() -> None:
    st.header("Step 3 of 7 — Provision tenant")
    wizard = st.session_state.wizard
    st.write(
        f"Provisioning the scanner's Entra app + service principal against "
        f"tenant `{wizard.tenant_id}` via Microsoft Graph. Takes ~30 "
        "seconds. Creates the app, secret, permissions, admin consent, and "
        "the Power Platform Administrator role assignment."
    )

    if not wizard.bootstrap_token:
        st.error("Missing bootstrap sign-in — go back to Step 1.")
        return

    if st.button("Provision tenant (~30s)", type="primary"):
        if not (
            validate_tenant_id(wizard.tenant_id or "")
            and validate_app_name(wizard.app_name)
        ):
            st.error(
                "Invalid tenant ID or app name in session — go back to Step 2."
            )
            return

        log_lines: list[str] = []
        with st.status("Provisioning…", expanded=True):
            progress = st.progress(0.0, text="Starting…")
            with st.expander("Detailed output", expanded=False):
                log_area = st.empty()

            def _on_progress(n: int, message: str) -> None:
                log_lines.append(f"[{n}/8] {message}")
                log_area.code("\n".join(log_lines[-30:]), language="text")
                progress.progress(min(n / 8, 1.0), text=message)

            try:
                result = wizard_logic.run_provisioning(
                    wizard.bootstrap_token,
                    wizard.bootstrap_account or {},
                    wizard.tenant_id or "",
                    wizard.app_name,
                    progress_callback=_on_progress,
                    data_dir=DATA_DIR,
                )
            except ProvisionError as exc:
                st.error(
                    f"Provisioning failed at step {exc.step}: {exc.message}"
                )
                st.session_state[_LOG_KEYS["provision"]] = log_lines
                return
            except Exception as exc:  # noqa: BLE001
                st.error(f"Provisioning raised: {exc}")
                st.session_state[_LOG_KEYS["provision"]] = log_lines
                return
            progress.progress(1.0, text="Provisioned.")

        st.session_state[_LOG_KEYS["provision"]] = log_lines
        wizard.client_id = result.client_id
        wizard.app_object_id = result.app_object_id
        wizard.provisioned_at = datetime.now(timezone.utc)
        wizard.pp_admin_role_assigned = result.pp_admin_role_assigned
        wizard.pp_admin_role_error = result.pp_admin_role_error

        if not result.admin_consent_granted:
            st.warning(
                "Provisioning succeeded but admin consent could not be "
                "fully granted automatically. Grant consent manually in "
                "the Entra portal under App registrations → API "
                "permissions → Grant admin consent."
            )
        if not result.pp_admin_role_assigned:
            st.warning(
                "Provisioning succeeded but the Power Platform "
                "Administrator role could not be assigned automatically: "
                f"{result.pp_admin_role_error}. Assign it manually in the "
                "Entra portal under Identity → Roles → Power Platform "
                "Administrator → Add assignment, then continue."
            )
        _advance(4)


def _render_step_4() -> None:
    st.header("Step 4 of 7 — Register as Power Platform Management App")
    st.write(
        "Power Platform requires a separate registration before service "
        "principals can call its admin API. The wizard runs the PowerShell "
        "cmdlet for you. The first run prompts for a browser sign-in to "
        "`Add-PowerAppsAccount`. See ADR-0001 "
        "(`docs/decisions/0001-power-platform-management-app-in-wizard.md`) "
        "for the rationale."
    )

    client_id = st.session_state.wizard.client_id or ""
    st.subheader("App (client) ID")
    st.code(client_id, language="text")

    log_area = st.empty()

    prewarm_status = wizard_logic.read_prewarm_status()
    print(f"step 4 entered; prewarm status: {prewarm_status}", flush=True)
    skip_signin = prewarm_status == "succeeded"
    if skip_signin:
        st.caption(
            "Power Platform sign-in was warmed up during Step 2 — this "
            "should complete in ~5 seconds with no second browser pop."
        )

    _render_step_4_manual_fallback(client_id)

    wizard = st.session_state.wizard
    if not wizard.step_4_started:
        wizard.step_4_started = True
        _run_step_4_registration(client_id, log_area, skip_signin=skip_signin)
    else:
        if st.button("Retry", type="primary", key="step4_retry"):
            wizard.step_4_started = False
            st.rerun()


def _run_step_4_registration(
    client_id: str,
    log_area: Any,
    *,
    skip_signin: bool,
) -> None:
    lines: list[str] = []
    rc: int | None = None
    with st.status(
        "Registering with Power Platform…",
        state="running",
        expanded=True,
    ) as status:
        try:
            for line, code in wizard_logic.run_pp_management_registration(
                client_id, skip_signin=skip_signin
            ):
                if code is None:
                    if line:
                        lines.append(line)
                        log_area.code(
                            "\n".join(lines[-40:]), language="text"
                        )
                else:
                    rc = code
        except FileNotFoundError as exc:
            status.update(state="error")
            st.error(str(exc))
            return

        st.session_state[_LOG_KEYS["pp_register"]] = lines
        confirmed = (
            rc == 0
            and wizard_logic.verify_pp_registration_output(lines, client_id)
        )
        if confirmed:
            status.update(state="complete")
            st.success("Registered ✅")
            _advance(5)
            return
        status.update(state="error")
        if rc == 0:
            st.error(
                "PowerShell completed but registration could not be "
                "confirmed. Check the output above."
            )
        else:
            st.error(f"PowerShell exited with code {rc}.")


def _render_step_4_manual_fallback(client_id: str) -> None:
    with st.expander("Manual fallback", expanded=False):
        st.write(
            "If the automated run keeps failing, run the cmdlets below in "
            "your own PowerShell window, then click Re-check."
        )
        commands = (
            "Install-Module -Name Microsoft.PowerApps.Administration.PowerShell "
            "-Force -AllowClobber -Scope CurrentUser\n"
            "Add-PowerAppsAccount\n"
            f"New-PowerAppManagementApp -ApplicationId {client_id}"
        )
        st.code(commands, language="powershell")
        st.info(
            "Note: `New-PowerAppManagementApp` may take 30–60 seconds to "
            "propagate after the cmdlet completes. If the first Re-check "
            "fails, wait and try again."
        )
        if st.button("Re-check", type="primary", key="step4_manual_recheck"):
            try:
                settings = Settings()
                result = asyncio.run(doctor.check_power_platform(settings))
            except Exception as exc:  # noqa: BLE001 — surface anything to operator
                st.error(f"Re-check raised: {exc}")
                return
            if result.status == "pass":
                st.success(f"Registered ✅ — {result.detail}")
                _advance(5)
            else:
                st.error(f"Not registered yet — {result.detail}")


def _render_step_5() -> None:
    st.header("Step 5 of 7 — Verify")
    st.write(
        "Running the full doctor check. Expect Graph ✅, Power Platform ✅, "
        "and Delegated ❌ (no `mcp-scan login` yet — that's fine). "
        "Per-environment Dataverse access is provisioned in the next step."
    )

    settings = Settings()
    try:
        results = asyncio.run(doctor.run_all(settings))
    except Exception as exc:  # noqa: BLE001
        st.error(f"doctor.run_all raised: {exc}")
        return

    for r in results:
        icon = "✅" if r.status == "pass" else "❌"
        cols = st.columns([1, 3, 6])
        cols[0].write(icon)
        cols[1].write(f"**{r.name}** ({r.audience})")
        cols[2].caption(r.detail)

    graph_ok = any(
        r.audience == "graph" and r.status == "pass" for r in results
    )
    pp_ok = any(
        r.audience == "power_platform" and r.status == "pass" for r in results
    )

    if st.button(
        "Continue to environment provisioning",
        disabled=not (graph_ok and pp_ok),
    ):
        _advance(6)


def _render_step_6() -> None:
    st.header("Step 6 of 7 — Provision Dataverse access per environment")
    st.write(
        "Power Platform admin user creation per environment is not exposed by "
        "Microsoft as an API. Follow the deep link for each environment, then "
        "click Re-check. You can skip environments — skipped ones will show "
        "`no_dataverse_access` errors in scan output, which is correct "
        "behavior."
    )

    settings = Settings()
    try:
        envs = list_environments_sync(settings)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not list environments: {exc}")
        envs = []

    target = st.session_state.wizard.target_env_id
    if target:
        matching = next(
            (e for e in envs if str(e.get("name")) == target),
            None,
        )
        if matching is not None:
            display = (
                (matching.get("properties") or {}).get("displayName") or target
            )
            st.info(f"Highlighted environment: {display}")

    with st.expander("Steps to provision an environment", expanded=False):
        st.markdown(
            "1. In the row below, click **Open in admin center**.\n"
            "2. Click **+ New app user** at the top.\n"
            "3. Click **+ Add an app**, search for the scanner app name, "
            "select it, click **Add**.\n"
            "4. Under **Security roles**, click the pencil icon, select "
            "**System Administrator** (or a custom least-privilege role), "
            "click **Save**.\n"
            "5. Click **Create**. Return here and click **Re-check** on the "
            "row.\n\n"
            "(Source of truth: `docs/tenant-setup.md` §4 Step 6.)"
        )

    st.divider()
    wizard = st.session_state.wizard
    if envs:
        header = st.columns([3, 4, 1, 2, 2])
        header[0].write("**Environment**")
        header[1].write("**Dataverse host**")
        header[2].write("**Status**")
        header[3].write("")
        header[4].write("")
        if not wizard.step_6_started:
            wizard.step_6_started = True
            status_placeholders: dict[str, Any] = {}
            for env in envs:
                env_id = str(env.get("name", ""))
                status_placeholders[env_id] = env_row.render(
                    env, settings, status_override="Checking…"
                )
            results = asyncio.run(
                wizard_logic.check_all_envs_dataverse(settings, envs)
            )
            for env, result in zip(envs, results):
                env_id = str(env.get("name", ""))
                if isinstance(result, BaseException):
                    passed = False
                else:
                    passed = result.status == "pass"
                st.session_state.status.dataverse_envs[env_id] = passed
                placeholder = status_placeholders.get(env_id)
                if placeholder is not None:
                    placeholder.write("✅" if passed else "❌")
        else:
            for env in envs:
                env_row.render(env, settings)
    else:
        st.caption("No environments returned by Power Platform admin.")

    if st.button("Continue", type="primary"):
        st.session_state.wizard.target_env_id = None
        _advance(7)


def _render_step_7() -> None:
    st.header("Step 7 of 7 — Setup complete")

    wizard = st.session_state.wizard
    settings = Settings()
    try:
        envs = list_environments_sync(settings)
    except Exception:  # noqa: BLE001
        envs = []

    accessible = sum(
        1
        for env in envs
        if st.session_state.status.dataverse_envs.get(str(env.get("name")))
    )
    total = len(envs)

    expires = (
        wizard.provisioned_at + timedelta(days=365)
        if wizard.provisioned_at
        else None
    )

    st.markdown(
        f"""
| | |
|---|---|
| **Tenant** | `{wizard.tenant_id}` |
| **Scanner app** | {wizard.app_name} (`{wizard.client_id}`) |
| **Secret expires** | {expires.date().isoformat() if expires else "—"} |
| **Environments accessible** | {accessible} of {total} |
"""
    )

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        WIZARD_DONE_MARKER.write_text(
            datetime.now(timezone.utc).isoformat(), encoding="utf-8"
        )
    except OSError as exc:
        st.warning(f"Could not write completion marker: {exc}")

    st.info(
        "The Status page shows scanner health and an optional delegated "
        "sign-in for two Microsoft surfaces that require a real user session "
        "(Copilot Packages API and Teams App Catalog). If your tenant doesn't "
        "use those, you can skip sign-in and click 'Run Scan' from the sidebar."
    )

    cols = st.columns(2)
    if cols[0].button("Continue to Status", type="primary"):
        st.switch_page("pages/01_Status.py")
    if cols[1].button("Stay and review setup"):
        pass


_RENDERERS = {
    1: _render_step_1,
    2: _render_step_2,
    3: _render_step_3,
    4: _render_step_4,
    5: _render_step_5,
    6: _render_step_6,
    7: _render_step_7,
}


# ---------------------------------------------------------------------------
# Page entry
# ---------------------------------------------------------------------------

init_session_state()

_wizard = st.session_state.wizard
if _wizard.step < 1 or _wizard.step > 7:
    _wizard.step = 1

st.title("First Run Setup")
st.caption(f"Step {_wizard.step} of 7")

_RENDERERS[_wizard.step]()
