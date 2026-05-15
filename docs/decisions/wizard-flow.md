# First-Run Wizard Flow — Baseline Sequence Diagram

Baseline sequence diagram of the first-run setup wizard, captured **2026-05-15**.
This is the **current state**, not a target architecture. It exists so we can
spot optimization opportunities (attention switches, wall-time hotspots,
redundant API round-trips) before redesigning anything. Re-render in
<https://mermaid.live/> if you edit it.

## Sequence diagram

```mermaid
sequenceDiagram
    autonumber
    participant Op as Operator
    participant W as Wizard (Streamlit)
    participant L as WizardLogic
    participant Sub as Subprocess
    participant Sh as SetupScript
    participant AZ as AzureCLI
    participant G as MSGraph
    participant PP as PPAdminAPI
    participant DV as Dataverse
    participant FS as LocalDisk
    participant PS as PowerShell
    participant PPAC as PPAdminCenter

    Note over Op,W: Step 1 — Prerequisites & Sign In
    Op->>W: open /First_Run_Setup
    Op->>W: click "Sign in with Azure CLI"
    W->>Sub: az login --use-device-code --allow-no-subscriptions
    Sub->>AZ: start device-code flow
    Note over Op,AZ: Operator opens microsoft.com/devicelogin, pastes code (browser switch)
    AZ-->>Sub: account JSON
    Sub-->>W: rc=0
    W->>Sub: az account show --query tenantId -o tsv
    Sub->>AZ: account show
    AZ-->>Sub: tenantId
    Sub-->>W: tenantId (prefills Step 2 form)

    Note over Op,W: Step 2 — Confirm tenant + app name
    Op->>W: submit tenantId + appName
    W->>L: validate_tenant_id, validate_app_name
    L-->>W: ok (no I/O)

    Note over Op,W: Step 3 — Provision tenant (advertised ~50s)
    Op->>W: click "Provision tenant"
    W->>Sub: bash scripts/setup-scanner.sh <tenantId> <appName>
    Sub->>Sh: exec (streams stdout into st.status)

    Note over Sh,G: Step 0/7 — preconditions
    Sh->>AZ: az version
    Sh->>AZ: az account show
    Sh->>AZ: az rest GET /v1.0/me/memberOf/microsoft.graph.directoryRole
    AZ->>G: GET memberOf
    G-->>AZ: role list
    AZ-->>Sh: Global Admin verified
    Sh->>AZ: az ad app list --display-name (idempotency guard)
    AZ->>G: GET /applications?filter=displayName
    G-->>AZ: [] (or abort if non-empty)

    Note over Sh: Step 1/7 — create Entra app
    Sh->>AZ: az ad app create --sign-in-audience AzureADMyOrg
    AZ->>G: POST /applications
    G-->>AZ: 201 (appId, objectId)

    Note over Sh: Step 2/7 — enable public client flows
    Sh->>AZ: az ad app update --set isFallbackPublicClient=true
    AZ->>G: PATCH /applications/{objectId}
    G-->>AZ: 204

    Note over Sh: Step 3/7 — service principal
    Sh->>AZ: az ad sp create --id appId
    AZ->>G: POST /servicePrincipals
    G-->>AZ: 201 (spObjectId)

    Note over Sh: Step 4/7 — 5 app + 3 delegated perms in one PATCH (target <5s)
    Sh->>AZ: az rest PATCH /v1.0/applications/{objectId} (requiredResourceAccess: 8 perms)
    AZ->>G: PATCH
    G-->>AZ: 204

    Note over Sh: Step 5/7 — admin consent (non-fatal)
    Sh->>AZ: az ad app permission admin-consent --id appId
    AZ->>G: POST grant
    G-->>AZ: 204 (or warn-and-continue)

    Note over Sh: Step 6/7 — 1-year client secret
    Sh->>AZ: az ad app credential reset --years 1 --append
    AZ->>G: POST /addPassword
    G-->>AZ: 200 (secret value, never logged)

    Note over Sh: Step 7/7 — Power Platform Administrator role
    Sh->>AZ: az rest GET /v1.0/directoryRoles?$filter=roleTemplateId eq ...
    AZ->>G: GET directoryRoles
    G-->>AZ: role or empty
    alt role not yet activated in tenant
        Sh->>AZ: az rest POST /v1.0/directoryRoles (activate from template)
        AZ->>G: POST
        G-->>AZ: 201 (roleObjectId)
    end
    Sh->>AZ: az rest POST /v1.0/directoryRoles/{roleObjectId}/members/$ref
    AZ->>G: POST member
    G-->>AZ: 204 (or idempotent "already exists")

    Sh->>FS: write ~/.m365-mcp-scanner/.setup-output.json (mode 600)
    Sh-->>Sub: exit 0 + duration log
    Sub-->>W: rc=0

    W->>L: ingest_setup_output()
    L->>FS: read .setup-output.json
    L->>FS: write config.toml (mode 0o600, tenant_id+client_id+client_secret)
    L->>FS: delete .setup-output.json
    L-->>W: settings ready

    Note over Op,W: Step 4 — Register as Power Platform Management App
    alt happy path (automated pwsh)
        Op->>W: click "Run PowerShell registration"
        W->>Sub: pwsh -NoProfile -NonInteractive -Command "..." (env: MCP_APP_ID)
        Sub->>PS: Import-Module Microsoft.PowerApps.Administration.PowerShell
        Sub->>PS: Add-PowerAppsAccount
        Note over Op,PS: potential browser popup — first-run interactive sign-in
        Sub->>PS: New-PowerAppManagementApp -ApplicationId $env:MCP_APP_ID
        PS->>PP: register management app
        PP-->>PS: applicationId row
        PS-->>Sub: stdout (table)
        Sub-->>W: rc=0 + captured lines
        W->>L: verify_pp_registration_output(lines, client_id)
        L-->>W: pass (regex match)
    else manual fallback (expander)
        W-->>Op: render 3 cmdlets to copy
        Note over Op,PS: operator opens own pwsh terminal, runs cmdlets, returns (terminal switch)
        Op->>W: click "Re-check"
        W->>L: doctor.check_power_platform(settings)
        L->>PP: GET environments (AppOnlyTokenProvider)
        PP-->>L: 200
        L-->>W: pass
    end

    Note over Op,W: Step 5 — Verify
    W->>L: doctor.run_all(settings)
    L->>G: GET /v1.0/me (app-only token sanity)
    G-->>L: 200
    L->>PP: GET environments
    PP-->>L: 200
    L->>FS: read msal_token_cache.bin (check_delegated_session)
    FS-->>L: miss (expected at first run)
    loop for each environment
        L->>DV: HEAD https://{env}.crm.dynamics.com/api/data/v9.2/bots?$top=1
        DV-->>L: 403 (expected pre-Step-6)
    end
    L-->>W: Graph pass, PP pass, Delegated fail, Dataverse fail

    Note over Op,W: Step 6 — Per-environment Dataverse provisioning
    W->>L: list_environments_sync()
    L->>PP: GET https://api.bap.microsoft.com/.../environments
    PP-->>L: env list
    loop for each environment
        W-->>Op: render row + deep link to admin.powerplatform.microsoft.com/...
        Note over Op,PPAC: operator switches to PPAC, creates application user, assigns sec-roles (manual, minutes per env)
        Op->>W: click "Re-check"
        W->>L: doctor.check_dataverse(settings, env)
        L->>DV: HEAD https://{env}.crm.dynamics.com/api/data/v9.2/bots?$top=1
        DV-->>L: 200 or 403
        L-->>W: per-env status
    end

    Note over Op,W: Step 7 — Finish
    Op->>W: click "Continue to Status"
    W->>FS: write .wizard-completed (ISO timestamp)
    W-->>Op: st.switch_page -> /Status
```

## Friction inventory

Observations only — each item is a place where operator attention switches
context or wall time is non-trivial. No remedies proposed here; that comes in
a follow-up session.

1. **Step 1 — device-code browser switch.** Operator leaves Streamlit/terminal
   to paste a code at `microsoft.com/devicelogin`, then returns.
2. **Step 3 — single ~50s blocking subprocess.** One `bash` call wrapping ten
   `az`/`az rest` invocations against Graph; the UI only sees streamed log
   lines, no per-call progress.
3. **Step 3 — round-trip on disk for one secret.** `.setup-output.json` is
   written by bash (mode 600), read by Python, then deleted, with
   `config.toml` written in between. Three FS touches for one handoff.
4. **Step 3 — Step 4/7 PATCH is the only step with a measured target (<5s).**
   The other ~nine `az` calls have no documented timings; total script
   duration is only known post-hoc from the script's own summary line.
5. **Step 4 — second auth surface.** `Add-PowerAppsAccount` may open a browser
   even though Step 1 already authenticated the operator via `az`.
6. **Step 4 — heterogeneous shell handoff.** Streamlit → `pwsh` subprocess →
   `Microsoft.PowerApps.Administration.PowerShell` module load on every run.
   Module import dominates pwsh wall time.
7. **Step 4 fallback — terminal switch.** Operator opens their own pwsh
   window, runs three cmdlets, returns to click Re-check. Two app switches
   plus a manual copy-paste.
8. **Step 5 — full doctor run despite known-failures.** Delegated session and
   Dataverse are both probed even though both are expected to fail
   pre-Step-6; the failures are informational only at this point.
9. **Step 6 — manual per-environment loop in PPAC.** Operator switches to
   `admin.powerplatform.microsoft.com` for *every* environment, creates an
   application user, assigns security roles, returns, clicks Re-check. Wall
   time scales linearly with environment count and dominates the wizard.
10. **Step 6 — Re-check is single-environment.** No batched re-verification
    across all envs; each row is its own round-trip.
11. **Cross-cutting — three independent token surfaces.** `az` CLI session
    (Step 1), Power Apps PowerShell session (Step 4), and the scanner's own
    app-only token via `AppOnlyTokenProvider` (Steps 5–6) all authenticate
    independently. No reuse.
12. **Cross-cutting — no concrete latency data.** Only the Step 4/7 PATCH has
    a documented target (<5s) and the script logs its own total duration;
    every other API call's cost is unknown. A measurement pass is a
    prerequisite to prioritizing the items above.
