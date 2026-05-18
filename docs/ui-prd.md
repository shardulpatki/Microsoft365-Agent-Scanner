# PRD — m365-mcp-scanner UI

**Status:** Draft, Phase 4 planning
**Owner:** Abuzar Amini
**Date:** 2026-05-13
**Related docs:** `README.md`, `docs/tenant-setup.md`, `handover.md` Sections 2 & 8

---

## 1. Summary

A Streamlit-based web UI that extends the existing `mcp-scan` CLI into an end-to-end app. Operators install once with `pip install m365-mcp-scanner[ui]`, run `mcp-scan ui`, and never need to touch a terminal again — including the one-time tenant provisioning.

The UI is local-only (binds `127.0.0.1`), read-only against the tenant, and runs in the same process model as the CLI: one operator, one machine, one tenant.

---

## 2. Problem and motivation

The scanner today is functional but has two friction points that block adoption:

1. **First-time setup is a 30-minute multi-portal click-through.** The runbook (`docs/tenant-setup.md`) walks an admin through six steps across `entra.microsoft.com` and `admin.powerplatform.microsoft.com`. Even with `setup-scanner.sh`, the operator must run `az login`, execute the script, copy outputs into env vars, and then provision Dataverse users per environment manually. There is no single artifact that takes them from zero to first scan.
2. **The CLI produces JSON output that requires terminal fluency to interpret.** A boss-level demo audience cannot read a 6,000-line scan JSON. The current Rich-rendered console tables are good for an engineer but not for showing surface coverage and findings to a non-CLI audience.

The UI solves both by collapsing setup into one wizard and rendering scan output as scannable tables.

---

## 3. Users

| Persona | Frequency | What they need |
|---|---|---|
| **Tenant admin (first-time)** | Once per tenant | Click-through provisioning that handles Entra app creation, permissions, consent, secret generation, and PP admin role assignment without leaving the app |
| **Security operator (recurring)** | Weekly or on-demand | Run scans, view agents/servers/errors, re-check delegated session, re-verify a newly provisioned environment |
| **Demo audience (boss + colleagues)** | One-off review | See surface coverage status, scan results, and the four documented Microsoft blockers in a form they can read |

The first two personas may be the same human; the third is read-only over their shoulder.

---

## 4. Goals and non-goals

### Goals (v1)

1. **Zero-to-first-scan in under 5 minutes** on a tenant with `az` CLI installed and the operator logged in as Global Admin.
2. **No terminal commands required** after `mcp-scan ui` launches. Setup, login, scan, view — all in the browser.
3. **Self-detecting first-run experience.** If `~/.m365-mcp-scanner/config.toml` is absent, the app lands on the setup wizard automatically.
4. **Honest treatment of Microsoft's blockers.** Surfaces 3 and 6a (license-gated) and Surface 6b's manifest gap show in the UI with the cited Microsoft reason, not as generic errors.
5. **Per-environment Dataverse provisioning is guided, not automated.** Deep-link to admin center + re-check button. (See Section 7 below for why.)

### Non-goals (v1)

1. Multi-user / multi-tenant deployment. One operator, one machine, one tenant.
2. Authentication of the UI itself. It binds `127.0.0.1`; the OS user is the auth boundary.
3. Persistent server-side state beyond what the CLI already writes to `~/.m365-mcp-scanner/`.
4. Scan scheduling, retention policies, or alerting. Cron + the existing JSON output are sufficient.
5. Mutating actions against the tenant (delete scan, rotate secret, modify findings). All UI actions are reads against the tenant; setup is the one exception, and only during the wizard.

---

## 5. User scenarios

### 5.1 First-run (clean machine, fresh tenant)

The operator installs the package, runs `mcp-scan ui`, and lands on the setup wizard because no config exists. They click through:

1. Prerequisites check (`pwsh` installed; `az` and `jq` no longer required — see ADR-0003)
2. Sign in with Microsoft (in-process MSAL public-client auth-code-PKCE flow opens the operator's browser; device-code fallback available)
3. Confirm tenant ID and app name
4. Provision tenant (in-process Microsoft Graph calls, ~30 s; see ADR-0003)
5. Register as Power Platform Management App (guided PowerShell copy-paste; see ADR-0001)
6. Verify (Graph + PP admin checks)
7. Per-environment Dataverse provisioning (deep links + re-check)
8. Finish → lands on Status (which has a one-click link to Run Scan)

End state: a scan-ready tenant configuration with all six surfaces represented (three live, three correctly blocked with cited reasons).

### 5.2 Recurring use (returning operator)

Operator runs `mcp-scan ui`, app detects existing config, silently runs `doctor` checks. If green, lands on Run Scan. If yellow/red, lands on Status with the gap highlighted (e.g., delegated session expired, or one environment newly returning 403).

### 5.3 Broken-state recovery

Common scenarios the UI handles without sending the operator back to the runbook:

| Trigger | UI response |
|---|---|
| Delegated session expired | Status page shows "❌ not signed in"; Sign-in button renders device code in-app |
| New Power Platform environment added since last scan | Errors page lists it under `no_dataverse_access`; "Fix this" jumps to wizard Step 6 for that env |
| Secret expired | Status page shows expiry date; "Re-run setup" jumps wizard to Step 3 (new secret only) |
| `tenant_not_eligible` (Surface 3 or 6a) | Shows Microsoft's verbatim 403 reason; no Fix button (this is licensing, not a config gap) |

---

## 6. Functional requirements

Six pages total. Page numbering matches the file ordering in `ui/pages/`.

### Page 0 — First-Run Setup (wizard)

Linear, step-gated via `st.session_state["wizard_step"]`. Operator cannot skip ahead.

| Step | Required behavior |
|---|---|
| 1. Prerequisites and Sign In | Detect `pwsh` (PowerShell 7+) only; click **Sign in with Microsoft** to run in-process MSAL auth-code-PKCE with a localhost redirect listener; device-code fallback button surfaces if the local listener fails. See ADR-0003. |
| 2. Confirm tenant + app name | Auto-populated tenant (from the id_token `tid` claim) and app name (default "M365 MCP Scanner") shown as read-only; one-click **Confirm and continue** with an **Edit** fallback that re-renders the validated two-field form. On confirm, prewarms Power Platform sign-in in a background daemon thread so Step 4 can skip the second browser pop. |
| 3. Provision | In-process async httpx calls to Microsoft Graph (~30 s); progress callback drives the progress bar substep-by-substep; collapsible **Detailed output** expander shows the substep log. On success, writes `config.toml` with the new app's client_id + secret. See ADR-0003. |
| 4. PP Management App | Run `New-PowerAppManagementApp` via a pwsh subprocess; if the Step 2 prewarm succeeded, skip `Add-PowerAppsAccount` so registration completes in ~5s with no second browser pop; auto-advance on confirmed success; Manual fallback expander preserves the copy-paste + Re-check flow (see ADR-0001 update) |
| 5. Verify | In-process doctor checks for Graph, PP admin, and delegated session audiences only. Per-env Dataverse is Step 6. |
| 6. Per-env Dataverse | One row per environment from PP admin enumeration. Each row: status, "Open in admin center" link, "Re-check" button |
| 7. Finish | Persists `.wizard-completed` marker; redirects to Status (one click from Run Scan via the sidebar) |

**Note (2026-05-18):** The earlier `setup-scanner.sh` → `.setup-output.json` handoff has been replaced by in-process Graph provisioning. The provisioner returns a structured `ProvisionResult` directly to the wizard; no JSON file round-trip. See ADR-0003.

### Page 1 — Status

Health dashboard. Always available post-setup.

Required panels:
- **Tenant** — tenant ID, scanner app display name + client ID, secret expiry date with days remaining
- **App-only audiences** — Graph (green/red), PP admin (green/red)
- **Per-environment Dataverse** — one row per env, status from last check
- **Delegated session** — signed-in status, account UPN, token expiry

Required actions:
- **Re-run all checks** — calls doctor in-process, repaints panels
- **Sign in for delegated surfaces** — triggers MSAL device-code flow in-process, renders code via `st.code()`, polls for completion
- **Sign out** — clears delegated cache
- **Open setup wizard** — for re-doing setup (e.g., expired secret)

### Page 2 — Run Scan

The page operators land on most often.

Required controls:
- Scope multiselect (defaults to all six surfaces)
- `--probe` checkbox (off by default; warns about outbound calls to non-Microsoft domains)
- `--since` slider (greyed out until Enrich stage exists)
- Run button

On click: shells `mcp-scan run` with selected flags, streams stdout into `st.status`. On completion, captures new scan_id by diffing `~/.m365-mcp-scanner/scans/` contents. Shows "View results →" link to Agents page with scan preselected.

Cancel button (during run) calls `proc.kill()`.

### Page 3 — Agents

Scan picker at top. Table of agents with columns: name, path, environment, owner, MCP server count, published status. Filterable by each column.

Row click expands an inline detail panel: full agent metadata, attached MCP servers, source_ref evidence, raw error if applicable.

Export-to-CSV button for the current filtered view.

### Page 4 — MCP Servers

Same shape as Agents. Table columns: URL, transport, auth type, first-party flag, consumer count, external domain flag.

Row expansion shows: consuming agents, advertised tools (populated only if `--probe` was on), connection reference evidence.

### Page 5 — Errors

Grouped by error `code`. One collapsible section per category, in this order:

1. `no_dataverse_access` (actionable)
2. `delegated_session_required` (actionable)
3. `tenant_not_eligible` (informational — licensing)
4. `manifest_endpoint_unavailable` (informational — Microsoft API gap)

Each section header shows count and severity. Each error row shows env_id (where relevant), the verbatim Microsoft response, and a "What this means" expander pulled from the README's undocumented-behaviors section.

Required actions:
- **Fix this** on `no_dataverse_access` rows — jumps to wizard Step 6 for that env
- **Fix this** on `delegated_session_required` rows — jumps to Status page sign-in
- No Fix buttons on the two informational categories

---

## 7. Open product decisions

### 7.1 Auto-provision Dataverse application users?

The runbook explicitly defers this because PPAC has no stable API. A Dataverse Web API path exists (`POST systemusers` + role association), but its reliability across tenants and environments is unverified.

**Recommendation: manual-with-deep-links in v1.** Reasons:

- One-click setup is preserved for the 5 of 6 setup steps that have stable APIs
- The "Re-check" button still gives a tight feedback loop (click admin link → 4 clicks in PPAC → click Re-check → green)
- Shipping experimental auto-provision risks the user catching a failure mid-demo and losing trust in the wizard
- Auto-provision can ship in v1.1 once tested in 3+ tenants

### 7.2 Should the UI run in-process or as a sidecar?

In-process. The UI imports scanner modules directly for status checks and login, and only shells out for `mcp-scan run` (where subprocess isolation actually buys something). See TRD Section 4.

### 7.3 What happens to the existing Rich console output?

Unchanged. The CLI keeps its current rendering. The UI is additive and does not replace the CLI for headless / CI use cases.

---

## 8. Success criteria

Numbered, verifiable.

1. A new tenant goes from `pip install m365-mcp-scanner[ui]` to first successful scan in **under 5 minutes** with the operator already logged in to Azure CLI as Global Admin.
2. Zero terminal commands needed after `mcp-scan ui` launches.
3. All 6 surfaces appear in the UI with correct status (3 producing data, 3 blocked with cited Microsoft reasons).
4. All 4 error codes from `handover.md` Section 4 ("Error categorization") render correctly on Page 5 with their categorization preserved.
5. Existing 78 unit tests still pass. UI adds at least 15 new tests (loaders, state transitions, subprocess wrappers).
6. Total UI bundle adds **no more than 3 dependencies** to the optional `[ui]` extra: `streamlit`, `pandas`, and one of `streamlit-extras` / `streamlit-tags` if needed for the picker UX.

---

## 9. Out of scope (explicit)

| Feature | Reason |
|---|---|
| Multi-user web app | Different security model; v1 is local-only |
| Scan scheduling | Cron is sufficient |
| Findings page (was Page 6 in design draft) | Phase 5 work; revisit when Score stage lands |
| Diff page (was Page 7 in design draft) | CLI `mcp-scan diff` is sufficient for v1 |
| Delete scan UI | Destructive operation; filesystem is fine |
| Secret rotation flow | One-off admin task; runbook is sufficient |
| Mobile / tablet layout | Desktop browser only |

---

## 10. Rollout

1. **Internal demo build** — runs locally for the boss demo (target: this sprint)
2. **Internal v1** — packaged in the repo as `[ui]` extra; documented in README; tested across the 3 Armor19 environments
3. **External v1** — once the Phase 5 scoring rules land, ship UI + scoring together as `m365-mcp-scanner 1.0`

No migration concerns. The UI reads existing scan JSON; pre-existing scans render correctly without conversion.
