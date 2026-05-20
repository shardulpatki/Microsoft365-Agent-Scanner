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

## Open questions for next session

- Need real timing numbers from the operator's machine for both
  `logger.info` lines.
- If `az account show` is the culprit: confirm `stream_subprocess` does
  not drop the trailing JSON block from `az login`'s stdout (it merges
  stderr→stdout, but the device-code prompt is on stderr — the JSON
  subscription dump is on stdout and should be in `lines`).
- Should the diagnostic `logger.info` lines stay in production? Cheap
  and useful for future support; lean toward keeping them.
