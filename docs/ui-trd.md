# TRD — m365-mcp-scanner UI

**Status:** Draft, Phase 4 planning
**Owner:** Abuzar Amini
**Date:** 2026-05-13
**Companion doc:** `docs/ui-prd.md`

---

## 1. Summary

In-process Streamlit app added as a subpackage under `src/m365_mcp_scanner/ui/`. Installed via an optional dependency extra (`pip install m365-mcp-scanner[ui]`) and launched by a new `mcp-scan ui` CLI command. Reuses existing scanner modules for auth, doctor checks, and scan reading. Shells out to `mcp-scan run` for scan execution only, where subprocess isolation actually buys something.

No changes to existing CLI surface or scan JSON schema.

---

## 2. Architecture

### 2.1 High-level diagram

```
┌─────────────────────────────────────────────────────────────────┐
│  Browser (localhost:8501)                                        │
└────────────────────────┬────────────────────────────────────────┘
                         │ HTTP
┌────────────────────────▼────────────────────────────────────────┐
│  Streamlit server (one process, single OS user)                  │
│                                                                  │
│  ui/                                                             │
│  ├── app.py              # entry, routing, session bootstrap     │
│  ├── pages/              # 6 pages (00-05)                       │
│  ├── components/         # shared widgets (status panel, etc.)   │
│  ├── state.py            # session_state schema + helpers        │
│  ├── loaders.py          # reads ~/.m365-mcp-scanner/scans/      │
│  ├── runners.py          # subprocess wrappers (az, setup, scan) │
│  └── doctor_ui.py        # in-process wrapper around doctor      │
│                                                                  │
│  Imports (in-process):                                           │
│  ├── auth.msal_broker    # delegated login                       │
│  ├── auth.token_provider # all three audiences                   │
│  └── clients.*           # for doctor checks                     │
│                                                                  │
│  Subprocess calls (isolated):                                    │
│  ├── az login / az account show                                  │
│  ├── setup-scanner.sh                                            │
│  └── mcp-scan run                                                │
└──────────────────────────────────────────────────────────────────┘
                         │
                         ▼
                 ~/.m365-mcp-scanner/
                 ├── config.toml
                 ├── scans/*.json
                 ├── latest.json
                 └── msal_token_cache.bin
```

### 2.2 Module layout

```
src/m365_mcp_scanner/ui/
├── __init__.py
├── app.py                       # st.set_page_config + bootstrap + routing
├── pages/
│   ├── 00_First_Run_Setup.py
│   ├── 01_Status.py
│   ├── 02_Run_Scan.py
│   ├── 03_Agents.py
│   ├── 04_MCP_Servers.py
│   └── 05_Errors.py
├── components/
│   ├── __init__.py
│   ├── status_panel.py          # tenant + audience + delegated panel
│   ├── env_row.py               # per-environment Dataverse row
│   ├── scan_picker.py           # selectbox for choosing a scan
│   └── error_section.py         # collapsible error category section
├── state.py                     # SessionState dataclass + helpers
├── loaders.py                   # scan JSON reading + parsing
├── runners.py                   # subprocess wrappers with streaming
└── doctor_ui.py                 # in-process doctor adapter
```

The UI subpackage does not depend on the CLI module. Both depend on the same underlying clients and auth layers.

---

## 3. Stack and dependencies

| Component | Choice | Justification |
|---|---|---|
| UI framework | Streamlit ≥ 1.30 | Lowest-friction Python UI; in-process imports; rapid iteration |
| Tabular rendering | `st.dataframe` (uses pandas) | Filter/sort built-in; pandas required as transitive dep |
| Subprocess | `subprocess.Popen` with line-buffered stdout | Required for streaming setup-scanner.sh output |
| Local persistence | Existing JSON + TOML files | No new storage layer |
| Bootstrap auth (wizard) | Azure CLI (`az login`) | Already in operator's toolkit; avoids reimplementing OAuth in setup |
| Per-tenant auth (runtime) | Existing MSAL flow unchanged | Reuses `auth/msal_broker.py` and encrypted file cache |

### 3.1 Packaging

Add to `pyproject.toml`:

```toml
[project.optional-dependencies]
ui = [
    "streamlit>=1.30",
    "pandas>=2.0",
]
```

Base install (`pip install m365-mcp-scanner`) is unchanged. UI install (`pip install m365-mcp-scanner[ui]`) adds Streamlit and pandas.

### 3.2 CLI entry point

Add `ui` command to `cli/main.py`:

```python
@app.command()
def ui(
    port: int = 8501,
    scan: Optional[str] = None,
) -> None:
    """Launch the local web UI."""
    import importlib.resources as pkg_resources
    from m365_mcp_scanner import ui as ui_pkg
    app_path = pkg_resources.files(ui_pkg) / "app.py"
    cmd = [
        sys.executable, "-m", "streamlit", "run", str(app_path),
        "--server.port", str(port),
        "--server.address", "127.0.0.1",
        "--browser.gatherUsageStats", "false",
    ]
    if scan:
        cmd += ["--", "--scan-id", scan]
    os.execvp(cmd[0], cmd)
```

`os.execvp` replaces the current process with Streamlit so Ctrl+C stops cleanly.

---

## 4. In-process vs subprocess boundary

The cleanest design rule for this app: **import scanner modules for reads; shell out only when isolation matters.**

| UI action | Mechanism | Why |
|---|---|---|
| Status panel checks | In-process — call `doctor` module functions directly | UI needs structured per-audience results; CLI rendering is irrelevant |
| Delegated sign-in | In-process — call `msal_broker.acquire_token_interactive()` | UI needs the device-code string as a value, not as parsed stdout |
| Sign out | In-process — call `msal_broker.remove_account()` | Trivial; no subprocess overhead warranted |
| Scan reading | In-process — read JSON via `loaders.py` | Pure file I/O |
| Scan execution | Subprocess — `mcp-scan run` | Pipeline crashes shouldn't kill the UI; reuses CLI's error categorization |
| `az login` | Subprocess | Azure CLI is itself a subprocess world |
| `setup-scanner.sh` | Subprocess | Bash script that calls az and Graph; not import-able |

**Implication for the scanner codebase:** `auth/msal_broker.py`, `auth/token_provider.py`, and the doctor check functions must be import-safe — no side effects at module load, no `argparse` parsing, no auto-running on import. If any of those exist today, they need refactoring before the UI lands. Likely already clean given the project's structure, but worth verifying as the first task.

---

## 5. Setup wizard implementation

### 5.1 setup-scanner.sh contract change

The current `setup-scanner.sh` outputs human-readable log lines. The UI needs to parse out the client_id and client_secret it generates.

**Required change:** At end of successful run, write:

```bash
cat > ~/.m365-mcp-scanner/.setup-output.json <<EOF
{
  "client_id": "$APP_ID",
  "client_secret": "$SECRET_VALUE",
  "tenant_id": "$TENANT_ID",
  "app_object_id": "$APP_OBJECT_ID",
  "admin_consent_granted": true,
  "completed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
chmod 600 ~/.m365-mcp-scanner/.setup-output.json
```

`admin_consent_granted` is `false` when `az ad app permission admin-consent` failed and the operator must grant consent manually in the Entra portal; the wizard surfaces this as a blocking warning on Page 0 before allowing the user to advance to doctor checks. All other fields are required and non-empty.

The wizard's Step 4 handles the Power Platform Management App registration by shelling `pwsh` from Python via `wizard_logic.run_pp_management_registration` (which delegates to `runners.stream_subprocess`). Success is detected by `wizard_logic.verify_pp_registration_output`, which checks that stdout contains the `applicationId` column header and the exact appId on the header line or within two lines below it. On failure (non-zero exit, timeout, or unconfirmed output), a "Manual fallback" expander preserves the original copy-paste cmdlet flow with a Re-check button that calls `doctor.check_power_platform`. PowerShell stays out of `setup-scanner.sh`; pwsh enters only the Python-side wizard, where `shutil.which` + Python `Popen` avoid the bash→pwsh problems documented in §5.4.

The wizard reads this file after the subprocess exits with code 0, then writes `~/.m365-mcp-scanner/config.toml`, then deletes `.setup-output.json`.

Permissions: file is mode 600 throughout its brief existence; UI never displays secret in the browser.

**Precedence note:** `config.toml` is read by `Settings` at a higher priority than any `.env` file in the working directory, but is overridden by `M365_MCP_*` environment variables. Operators with `M365_MCP_*` set in their shell will see the wizard's output ignored — this is intentional for CI/headless flows. See `docs/tenant-setup.md §5 — Configuration precedence`.

### 5.2 Streaming subprocess pattern

```python
# ui/runners.py
import subprocess
from pathlib import Path
from typing import Iterator

def stream_subprocess(cmd: list[str], cwd: Path | None = None) -> Iterator[tuple[str, int | None]]:
    """Yield (line, returncode) tuples. returncode is None until the process exits."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=cwd,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        yield line.rstrip(), None
    proc.wait()
    yield "", proc.returncode
```

Used inside an `st.status` container:

```python
# ui/pages/00_First_Run_Setup.py (excerpt)
with st.status("Provisioning tenant...", expanded=True) as status:
    log_area = st.empty()
    lines: list[str] = []
    rc: int | None = None
    for line, exit_code in stream_subprocess([
        "bash", "setup-scanner.sh",
        st.session_state.wizard.tenant_id,
        st.session_state.wizard.app_name,
    ]):
        if exit_code is None:
            lines.append(line)
            log_area.code("\n".join(lines[-30:]), language="text")
        else:
            rc = exit_code
    if rc == 0:
        _ingest_setup_output()
        status.update(label="Provisioned", state="complete")
        st.session_state.wizard.step = 4
    else:
        status.update(label=f"Failed (exit {rc})", state="error")
```

### 5.2a Step 5 (Verify) scope

Step 5 runs `doctor.run_all(settings)` only — that's exactly three
`CheckResult`s: Graph, Power Platform admin, delegated session. It deliberately
does **not** enumerate environments or call `check_dataverse` per env;
per-environment Dataverse provisioning is Step 6's job, and rendering both
would show the operator the same ❌ list twice. The Continue button is gated
on Graph + PP admin passing; the delegated check is informational at this
stage (login is optional unless the operator wants Phase 3 surfaces).

### 5.3 Per-environment Dataverse provisioning

Manual + deep link, per the PRD decision. For each environment returned by the PP admin enumeration:

```python
# ui/components/env_row.py (sketch)
def render(env: Environment) -> None:
    cols = st.columns([2, 2, 1, 2])
    cols[0].write(env.display_name)
    cols[1].code(env.dataverse_url.host)
    cols[2].write("✅" if env.has_dataverse_access else "❌")
    deep_link = (
        f"https://admin.powerplatform.microsoft.com/environments/"
        f"environment/{env.id}/applicationusers"
    )
    cols[3].link_button("Open in admin center", deep_link)
    if cols[3].button("Re-check", key=f"recheck_{env.id}"):
        env.has_dataverse_access = check_dataverse_access(env)
        st.rerun()
```

The `check_dataverse_access()` helper makes a single HEAD request against `/api/data/v9.2/bots?$top=1`. Returns True on 200, False on 401/403.

### 5.4 Why Step 8 is not in the script

An earlier iteration (Option A) added a Step 8 to `setup-scanner.sh` that shelled out to PowerShell to run `Add-PowerAppsAccount` + `New-PowerAppManagementApp` from inside bash. Real-tenant verification on Armor19 failed at this step with exit 127 because GNU `timeout` is not on PATH in Git Bash on Windows. That was the second cross-platform issue with the bash→pwsh handoff (the first was PATH discovery for `pwsh` itself across Git Bash, WSL, and POSIX shells).

The bash→pwsh handoff is still rejected: `setup-scanner.sh` stays bash-only with no pwsh dependency. But the wizard (Python parent) does shell `pwsh` directly — Python avoids the three failure modes that killed Option A: `shutil.which` resolves pwsh on PATH on Windows, the timeout is enforced by a Python watchdog thread (no GNU `timeout`), and a fresh `pwsh -NonInteractive` session loads `Microsoft.PowerApps.Administration.PowerShell` cleanly from cached state. `pwsh` is added as a Step 1 prerequisite so the failure surfaces early.

If the automated pwsh path fails on a given machine, Step 4 falls back to the original guided manual flow — copy-paste cmdlets plus a Re-check button — preserved in a "Manual fallback" expander.

See `docs/decisions/0001-power-platform-management-app-in-wizard.md` (including the 2026-05-15 update) for the full rationale.

### 5.5 Step 2 prewarm of Add-PowerAppsAccount

`Add-PowerAppsAccount` is the slow part of Step 4 — on first use it pops a browser sign-in and takes 20–60 seconds. The wizard front-runs it: when the operator clicks **Confirm and continue** on Step 2, a daemon thread spawned from the page module runs `pwsh -Command "Import-Module Microsoft.PowerApps.Administration.PowerShell; Add-PowerAppsAccount"` in the background. The wizard advances to Step 3 immediately; the operator sees the prewarm browser tab pop while reading the Step 3 provisioning panel.

**Rendezvous via a status file, not session state.** Streamlit serializes `st.session_state` across reruns and `threading.Thread` is not serializable. The thread instead writes `~/.m365-mcp-scanner/.prewarm-status` (JSON: `{"status": "running" | "succeeded" | "failed", "completed_at": "..."}`). Step 4 polls this file via `wizard_logic.read_prewarm_status()` when its renderer runs.

**Conditional pwsh command in Step 4.** If `read_prewarm_status() == "succeeded"`, Step 4 calls `run_pp_management_registration(client_id, skip_signin=True)` which omits `Add-PowerAppsAccount` from the inline script — registration runs against the cached session in ~5s. Any other status (`running`, `failed`, `not_started`) falls back to the full original sequence; the prewarm is best-effort.

**Daemon thread.** The thread is started with `daemon=True` so it doesn't keep the Streamlit process alive on exit. The thread does not call any `st.*` functions (it's not on the Streamlit script-runner thread).

**The second browser pop is intentional.** Step 2 shows a caption ("a second browser sign-in may appear shortly — that's the Power Platform session warming up") so operators aren't surprised. The cost of automation is two back-to-back sign-ins instead of one mid-wizard pause.

---

## 6. State management

Streamlit reruns the entire script on every interaction. State must live in `st.session_state` keyed by stable names, not module-level variables.

### 6.1 Session state schema

```python
# ui/state.py
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

@dataclass
class WizardState:
    step: int = 1                           # 1..7
    tenant_id: Optional[str] = None
    app_name: str = "M365 MCP Scanner"
    az_logged_in: bool = False
    provisioned_at: Optional[datetime] = None
    target_env_id: Optional[str] = None     # for "Fix this" jumps from Errors
    client_id: Optional[str] = None         # captured from .setup-output.json
    app_object_id: Optional[str] = None     # captured from .setup-output.json

@dataclass
class StatusCache:
    graph_ok: Optional[bool] = None
    pp_admin_ok: Optional[bool] = None
    dataverse_envs: dict[str, bool] = field(default_factory=dict)
    delegated_account: Optional[str] = None
    last_checked: Optional[datetime] = None

@dataclass
class ScanContext:
    selected_scan_id: Optional[str] = None
    last_run_scan_id: Optional[str] = None
    current_run_proc: Optional[int] = None  # PID, for cancel

def init_session_state() -> None:
    if "wizard" not in st.session_state:
        st.session_state.wizard = WizardState()
    if "status" not in st.session_state:
        st.session_state.status = StatusCache()
    if "scan" not in st.session_state:
        st.session_state.scan = ScanContext()
```

`init_session_state()` is called at the top of `app.py` and every page.

### 6.2 Cross-page navigation

Two mechanisms work; pick one and use it consistently:

| Mechanism | Use case |
|---|---|
| `st.switch_page("pages/01_Status.py")` | Programmatic redirect (e.g., wizard finish → Status) |
| `st.page_link(...)` | User-driven navigation links |

Don't use raw URL changes; they break Streamlit's session state continuity.

### 6.3 "Fix this" jumps from Errors

```python
# Errors page → Status page (delegated)
if st.button("Fix this", key=f"fix_delegated_{i}"):
    st.session_state.wizard.target_env_id = None
    st.switch_page("pages/01_Status.py")

# Errors page → Wizard Step 6 (Dataverse env)
if st.button("Fix this", key=f"fix_env_{env_id}"):
    st.session_state.wizard.step = 6
    st.session_state.wizard.target_env_id = env_id
    st.switch_page("pages/00_First_Run_Setup.py")
```

The wizard page checks `target_env_id` on entry and scrolls / focuses the relevant row.

---

## 7. Routing and bootstrap

```python
# ui/app.py
import streamlit as st
from pathlib import Path
from m365_mcp_scanner.ui.state import init_session_state
from m365_mcp_scanner.ui.doctor_ui import quick_health_check

CONFIG_PATH = Path.home() / ".m365-mcp-scanner" / "config.toml"

st.set_page_config(
    page_title="M365 MCP Scanner",
    layout="wide",
    initial_sidebar_state="expanded",
)

init_session_state()

def route_initial_landing() -> None:
    if "initial_route_done" in st.session_state:
        return
    st.session_state.initial_route_done = True

    if not CONFIG_PATH.exists():
        st.switch_page("pages/00_First_Run_Setup.py")

    health = quick_health_check()
    if not health.all_green:
        st.switch_page("pages/01_Status.py")
    else:
        st.switch_page("pages/02_Run_Scan.py")

route_initial_landing()
```

`quick_health_check()` runs lightweight checks (token acquisition only, no test API calls) so the initial landing is sub-second.

### 7.1 Status page health refresh — dual-call pattern

The "Re-run all checks" button on `pages/01_Status.py` makes two calls in
sequence:

1. `doctor_ui.full_health_check(settings)` — runs `doctor.run_all`, which
   returns the three audience-level results (Graph, PP admin, delegated) and
   populates the `HealthSummary` audience fields.
2. `wizard_logic.list_environments_sync(settings)` then
   `wizard_logic.check_all_envs_dataverse(settings, envs)` — concurrently
   pings every environment's Dataverse Web API and writes the result map into
   `summary.dataverse_envs`, which `render_status_panel` reads to draw the
   per-environment rows.

Per-env Dataverse is intentionally kept out of `doctor.run_all` because (a)
the CLI `mcp-scan doctor` command would otherwise time-balloon on tenants
with many environments, and (b) the wizard's Step 5 should not duplicate
Step 6's per-env display. The Status page is the one surface that wants
both, so it makes both calls explicitly.

---

## 8. Auth integration

### 8.1 Reusing existing MSAL flow

Both the CLI and the UI call into the same `auth/msal_broker.py`. The UI's only addition is rendering the device code in the browser instead of stdout.

```python
# ui/pages/01_Status.py (delegated sign-in section)
if st.button("Sign in for delegated surfaces"):
    flow = broker.start_device_flow()
    st.code(flow["user_code"], language="text")
    st.link_button("Open Microsoft sign-in", flow["verification_uri"])
    with st.spinner("Waiting for sign-in..."):
        result = broker.complete_device_flow(flow, timeout_s=600)
    if result.success:
        st.session_state.status.delegated_account = result.account_upn
        st.rerun()
    else:
        st.error(f"Sign-in failed: {result.error_description}")
```

This requires `msal_broker.py` to expose `start_device_flow()` and `complete_device_flow()` as separate calls. If the current implementation only exposes a combined blocking call, refactor it. This is one of the import-safety changes flagged in Section 4.

### 8.2 Token cache compatibility

The existing Fernet-encrypted file cache at `%LOCALAPPDATA%\m365-mcp-scanner\msal_token_cache.bin` (or `~/.m365-mcp-scanner/msal_token_cache.bin` on POSIX) works unchanged. The UI runs as the same OS user as the CLI, so the cache file's encryption key derivation (tenant_id + client_id + home path per `handover.md` Section 4) resolves to the same key.

No new caching layer. No keyring. (See `handover.md` Section 4 on why keyring is off the table — WinError 1783.)

---

## 9. Error handling

### 9.1 Error taxonomy in the UI

The UI must preserve the scanner's structured error codes (`handover.md` Section 4):

| Scanner error code | UI rendering | Actionable? |
|---|---|---|
| `no_dataverse_access` | Errors page, "Fix this" → Wizard Step 6 | Yes |
| `delegated_session_required` | Errors page, "Fix this" → Status sign-in | Yes |
| `tenant_not_eligible` | Errors page, cited Microsoft 403 | No (licensing) |
| `manifest_endpoint_unavailable` | Errors page, cited Microsoft 400 | No (API gap) |
| `unknown` | Logged as a UI bug; show raw error |  N/A — should not occur |

**Regression guard:** the UI must never render an error with `code: null` or `code: unknown`. If it does, that's a regression in the scanner, not a UI bug. Surface it as a visible alert in the Errors page header.

### 9.2 Subprocess failure modes

Three exit codes the UI handles explicitly from `mcp-scan run`:

| Exit code | Meaning | UI response |
|---|---|---|
| 0 | Clean | Success state; navigate to results |
| 1 | Partial failure (expected blockers) | Success state with warning badge |
| 2 | Total failure | Error state; show stderr |
| 4 | Auth error | Error state with link to Status page |

Per `handover.md`, codes 3 (config error) and 4 (auth error) should also be handled if they appear in scan output.

### 9.3 az CLI not installed

Wizard Step 1 detects this and shows install links. The UI does not attempt to install `az` itself.

---

## 10. Testing strategy

### 10.1 Unit tests (new)

In `tests/unit/ui/`:

| File | Coverage |
|---|---|
| `test_loaders.py` | Scan JSON parsing, latest.json resolution, scan listing ordering |
| `test_state.py` | Session state init, wizard step transitions, jump-target handling |
| `test_runners.py` | Subprocess streaming, line buffering, exit code propagation (mocked subprocess) |
| `test_doctor_ui.py` | In-process doctor adapter, audience aggregation, env-by-env status |
| `test_env_row.py` | Re-check button toggles state, deep link URL construction |

Target: 15+ new tests. Existing 78 tests continue to pass.

### 10.2 Integration tests

Streamlit's `AppTest` framework (`streamlit.testing.v1.AppTest`) enables headless page testing. Add fixtures for:

- First-run on empty `~/.m365-mcp-scanner/`
- Returning-user on populated config + valid token
- Returning-user with expired delegated token
- Errors page with each of the 4 categorized codes present
- Wizard Step 6 with mixed-status envs

### 10.3 Manual demo checklist

Pre-demo verification on Armor19 tenant:

1. Fresh machine, fresh `pip install`, fresh `mcp-scan ui` → wizard appears
2. Wizard completes without manual stdout copying
3. Scan runs from UI in ~12s (matches CLI scan time)
4. All 3 Armor19 environments render correctly: 1 ✅, 2 ❌ with categorized errors
5. Errors page shows all 4 error categories with correct cited reasons
6. Cross-page Fix-this jumps land on the correct target

---

## 11. Performance budget

| Operation | Budget | Notes |
|---|---|---|
| App launch to landing page | < 2s | `quick_health_check()` must not make API calls |
| Status page full refresh | < 5s | Token acquisition + one test call per audience |
| Scan run via UI | ≤ CLI scan time + 1s | Subprocess overhead only |
| Scan reading (JSON parse, render table) | < 1s for current scan sizes | Scale-up concern when scans exceed ~10MB; not v1 |

Streamlit's full-script-rerun model can make heavy pages feel slow. Use `@st.cache_data` on `load_scan()` keyed by file mtime to avoid re-parsing on every interaction.

---

## 12. Security considerations

| Concern | Mitigation |
|---|---|
| UI binds public interface by mistake | `--server.address 127.0.0.1` hardcoded in `mcp-scan ui` |
| Client secret leaks to browser | Wizard writes secret directly to `config.toml`; never sent in HTTP responses to the browser |
| `.setup-output.json` contains secret on disk | Mode 600, deleted after wizard ingests it |
| Subprocess injection via app_name | `app_name` validated against `^[A-Za-z0-9 _-]{1,64}$` before being passed to `setup-scanner.sh` |
| Browser tab left open + machine sleeps | Streamlit session state is in-memory only; secrets in session state lost on Streamlit restart |
| `az login` cached creds shared across the OS user | Documented behavior; consistent with how the runbook expects `az` to work |

No new auth tokens, secrets, or PII are introduced by the UI beyond what the CLI already handles.

---

## 13. Open technical questions

1. **Streamlit's `st.switch_page` requires Streamlit ≥ 1.27.** Pinning `streamlit>=1.30` is safe but commits to a recent version. Acceptable?
2. **MSAL broker refactor scope.** If `msal_broker.py` currently exposes only `acquire_token_interactive()` as a single blocking call, splitting it into `start_device_flow()` + `complete_device_flow()` is required. Estimated 2 hours. Confirm shape of current implementation before estimating.
3. **`@st.cache_data` invalidation strategy.** Caching scan loads by `mtime` is simple but doesn't catch in-place file edits. For v1 this is fine (scans are append-only per `scan_id`); flag for revisit if scan post-processing (e.g., `consolidate.py`) is integrated.
4. **Bundle size.** Streamlit + pandas adds ~150MB to the install footprint. For a `[ui]` extra this is acceptable, but worth measuring on Windows specifically.

---

## 14. Effort estimate

| Task | Time |
|---|---|
| Verify import safety of existing modules (auth, doctor, clients) | 2h |
| Refactor MSAL broker to split device-flow start/complete if needed | 2h |
| `setup-scanner.sh` machine-readable output | 1h |
| `mcp-scan ui` CLI command + entrypoint | 1h |
| `app.py` + routing + session state | 2h |
| Page 0 — wizard (all 7 steps) | 6h |
| Page 1 — Status | 3h |
| Page 2 — Run Scan | 2h |
| Page 3 — Agents | 2h |
| Page 4 — MCP Servers | 2h |
| Page 5 — Errors | 2h |
| Components (status_panel, env_row, scan_picker, error_section) | 3h |
| Tests (15+ unit + AppTest integration) | 4h |
| Manual demo verification on Armor19 | 2h |
| **Total** | **~34h** (4–5 working days) |

---

## 15. Rollout plan

1. **Phase 4a — scaffolding.** `ui/` subpackage, `mcp-scan ui` command, empty routing. Verifies the import + packaging story without UI content. (~4h)
2. **Phase 4b — happy-path pages.** Status, Run Scan, Agents, MCP Servers, Errors. Operates against pre-existing config. (~12h)
3. **Phase 4c — wizard.** First-run setup wizard, env provisioning helper, cross-page Fix-this jumps. (~14h)
4. **Phase 4d — polish.** Caching, performance pass, manual demo checklist. (~4h)

Each phase ends in a runnable, demo-able state. The boss demo can target end of 4b — wizard is a separate sell.
