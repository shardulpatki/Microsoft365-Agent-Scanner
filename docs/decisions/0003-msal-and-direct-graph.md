# Direct MSAL + Microsoft Graph replaces az CLI + setup-scanner.sh

## Status

Accepted — 2026-05-18

## Context

The first-run wizard previously delegated all bootstrap auth and tenant
provisioning to two external surfaces:

1. **Step 1 sign-in** — Streamlit shelled `az login --use-device-code`, then
   `az account show` to capture the active tenant ID.
2. **Step 3 provisioning** — Streamlit shelled `bash scripts/setup-scanner.sh`,
   which internally made ~10 `az ad` / `az rest` calls to Microsoft Graph
   and then assigned the Power Platform Administrator directory role.

This worked but had three meaningful pain points on Armor19 (Windows):

- **Latency.** Each `az` CLI invocation incurs Python + extension-loader
  startup overhead; on Windows this is 1–4 seconds *per call*. Step 3 ran
  10 calls back-to-back, pushing wall-clock to 2–3 minutes. Step 1's device
  code flow added ~30 seconds.
- **Friction.** Device code flow required the operator to copy a code into
  a browser tab. Streamlit can't streamline that handoff.
- **Prerequisite sprawl.** The wizard needed `az` (>=2.50), `jq`, and `pwsh`
  on PATH. Two of those existed only to serve setup-scanner.sh.

The MSAL Python SDK and `httpx` were already in the dependency tree (used
by the scanner runtime), so the building blocks for an in-process
replacement were already there.

## Decision

**Step 1** now uses MSAL public-client authorization-code-with-PKCE flow
(`src/m365_mcp_scanner/auth/msal_bootstrap.py`) against the well-known
Azure CLI public `client_id`
(`04b07795-8ddb-461a-bbee-02f9e1bf7b46`). A one-shot
`http.server` bound to a random localhost port receives the redirect; the
operator's default browser opens automatically via `webbrowser.open`. The
device-code variant is retained as a fallback for locked-down networks
where localhost listener binding is blocked.

**Step 3** now calls Microsoft Graph directly via async `httpx`
(`src/m365_mcp_scanner/provisioning/provisioner.py`). The 7 substeps
mirror the retired bash script's `[N/7]` markers; an 8th best-effort
substep assigns the Power Platform Administrator directory role.
Progress is surfaced via a callback the UI binds to its progress bar —
no more stdout parsing.

Both bash scripts (`scripts/setup-scanner.sh` and the matching arg
smoke-test) are retained on disk with a `DEPRECATED 2026-05-18` header,
so a rollback can re-enable them by reverting the wizard glue without
restoring deleted files.

## Consequences

**Wins**

- `az` is no longer a wizard prerequisite. `jq` is also gone (it only
  existed for the bash script's JSON output).
- Step 1 sign-in completes in <2 s once the operator finishes the
  browser flow (vs ~30 s for device code).
- Step 3 provisioning completes in 15–60 s (vs 2–3 min).
- All errors surface as Python exceptions with structured `step` and
  `message` fields, so the UI can map them onto a clear remediation
  story.

**Trade-offs**

- Localhost-port-binding can fail in some corporate environments
  (firewalled loopback, port ranges blocked). The device-code fallback
  covers this; the UI offers it after any `BootstrapAuthError`.
- The Power Platform Administrator role assignment is best-effort. If
  it fails (e.g. the operator signed in as something less than GA), the
  wizard surfaces a manual remediation step rather than rolling back
  the whole provisioning.
- We now own the eight Graph permission IDs as module constants in
  `provisioning/provisioner.py`. If Microsoft changes any of them (very
  unlikely — these are stable public values), the provisioner needs a
  matching code change. The retired bash script had the same coupling.

## Verification

Manual end-to-end against Armor19, per the plan file
`C:\Users\shard\.claude\plans\replace-az-login-lucky-giraffe.md`,
required before commit.

## See also

- [[0001-power-platform-management-app-in-wizard]] — explains why Step 4
  remains a separate PowerShell registration (the New-PowerAppManagementApp
  cmdlet is PowerShell-only; this ADR doesn't change that).
