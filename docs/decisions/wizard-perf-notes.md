# Wizard Performance Investigation Notes

Diagnostic session for two operator-reported slowness points in the
first-run setup wizard. Status: **instrumentation added; awaiting
runtime data from operator's machine.** No behavioural changes shipped.

## Issue 1 — Lag between `az login` and Step 2

### What we found from code inspection

`_render_step_1` (`src/m365_mcp_scanner/ui/pages/00_First_Run_Setup.py`
L200–230) runs two serial subprocesses on success:

1. `az login --use-device-code --allow-no-subscriptions` (interactive,
   user-bound).
2. `az_account_tenant()` →
   `az account show --query tenantId -o tsv` (synchronous, blocking
   `subprocess.run`, 30s timeout).

On Windows, every `az` invocation pays Python-startup cost; 1–2s is
typical and 10s+ is observed on cold machines. The second call is the
prime suspect.

Note: the user-suggested fix "pass `--tenant <id>` to `az login`" does
not apply at Step 1 — the wizard does not know the tenant until **after**
login completes. A real fix would have to parse the tenant out of `az
login`'s own subscription JSON output instead.

### What was measured

Not yet measured. Instrumentation added at
`00_First_Run_Setup.py:_render_step_1`:

```
az login completed in %.2fs (rc=%s)
az account show completed in %.2fs (tenant_resolved=%s)
```

Operator should re-run Steps 1→2 once and report the two `logger.info`
lines from the Streamlit terminal.

### What was changed

Diagnostic logging only. No behaviour change.

### Decision rule for next session

- If `az account show` > 2s: replace it by parsing `tenantId` from `az
  login`'s JSON subscription output (scan the captured `lines` for the
  trailing JSON block and `json.loads` it). Keep `az_account_tenant()`
  as a fallback.
- If `az account show` ≤ 1s but Step 2 still feels laggy: cause is
  elsewhere (Streamlit `st.rerun()` tail, device-code flow finalization).
  Document and stop.

## Issue 2 — Step 4 slow despite prewarm

### What we found from code inspection

Prewarm thread (`_kick_off_prewarm`,
`00_First_Run_Setup.py:233`) is correctly:

- `daemon=True`
- launched on Step 2 confirm/submit (L288, L324)
- writing status JSON to `~/.m365-mcp-scanner/.prewarm-status` via
  `wizard_logic._write_prewarm_status`.

`_render_step_4` reads the file via `read_prewarm_status()` and only
sets `skip_signin=True` if status is exactly `"succeeded"`. The
PowerShell command in `wizard_logic.run_pp_management_registration`
correctly switches between `_PWSH_REGISTER_SCRIPT_SKIP_SIGNIN` (no
`Add-PowerAppsAccount`) and `_PWSH_REGISTER_SCRIPT` (full sequence) on
that flag.

No obvious bug in the conditional. The most likely cause is the operator
finishing Steps 2–3 faster than the prewarm (which itself does
`Import-Module` + `Add-PowerAppsAccount`, both slow on a cold tenant) —
in which case the prewarm status is still `"running"` or
`"not_started"` when Step 4 starts, and the skip path is correctly *not*
taken.

### What was measured

Not yet measured. Instrumentation added at the top of `_render_step_4`:

```
step 4 entered; prewarm status: %s
```

### What was changed

Diagnostic logging only. No behaviour change.

### Decision rule for next session

- Status `"succeeded"` but operator still sees `Add-PowerAppsAccount`:
  there's a real bug in `run_pp_management_registration` (script string
  vs. flag plumbing). Inspect.
- Status `"running"` / `"not_started"`: prewarm did not finish in time.
  Not a wizard bug. Possible follow-ups (not in scope here): gate Step 4
  on `read_prewarm_status() == "succeeded"`, or block Step 3's "next"
  button until prewarm completes. Either trades latency for predictable
  UX.
- Status `"failed"`: prewarm hit an exception (likely missing PowerShell
  module or network). Document, don't auto-retry.

## Open questions for next session

- Need real timing numbers from the operator's machine for both
  `logger.info` lines.
- If `az account show` is the culprit: confirm `stream_subprocess` does
  not drop the trailing JSON block from `az login`'s stdout (it merges
  stderr→stdout, but the device-code prompt is on stderr — the JSON
  subscription dump is on stdout and should be in `lines`).
- Should the diagnostic `logger.info` lines stay in production? Cheap
  and useful for future support; lean toward keeping them.
