# Power Platform Management App registration lives in the wizard, not in setup-scanner.sh

## Status

Accepted — 2026-05-13

## Context

setup-scanner.sh provisions an Entra app with the Power Platform
Administrator directory role assigned to its service principal. This is
necessary for the service principal to be administratively recognized as a
Power Platform admin, but it is not sufficient to actually call the Power
Platform admin API. Microsoft additionally requires the service principal
to be registered as a "Power Platform Management App" via the
PowerShell-only cmdlet `New-PowerAppManagementApp` from the
`Microsoft.PowerApps.Administration.PowerShell` module. Without this
registration, every Power Platform admin API call returns 403.

## Options considered

A. Add a Step 8 to setup-scanner.sh that shells out to pwsh and runs
   `Import-Module` + `Add-PowerAppsAccount` + `New-PowerAppManagementApp`.
   Produces a one-button experience.

B. Document the registration as a guided step in the Phase 4c wizard, with
   a copy-paste command and a Re-check button. Matches the existing Step 6
   (per-environment Dataverse) UX pattern.

## Decision

Option B. Real-tenant verification of Option A on Armor19 failed because
GNU `timeout` is not available in Git Bash on Windows by default, and the
script's pwsh invocation depended on it. The bash → pwsh handoff also
depends on pwsh being on PATH (separately installed and PATH-fixed during
the same verification session) and on the
`Microsoft.PowerApps.Administration.PowerShell` module being installed and
not currently loaded by another PowerShell session. Each is fixable in
isolation; together they make the script unreliable across operator
environments.

Option B accepts that the wizard guides the operator through one manual
PowerShell step, but the script itself stays bash-only and predictable.

## Consequences

- setup-scanner.sh produces an app that returns 403 on Power Platform admin
  calls until the wizard's management-app registration step is completed.
- The wizard adds a step after the script's output is ingested, titled
  "Register as Power Platform Management App", that:
  1. Displays the required PowerShell commands with a copy-paste button.
  2. Provides a Re-check button that calls `mcp-scan doctor` and passes
     when PP admin returns 200.
- Future Microsoft changes that expose `New-PowerAppManagementApp` via a
  non-PowerShell API (az CLI, Graph, Power Platform admin REST) would
  allow folding this step back into the script. As of May 2026, no such
  API exists.

## References

- docs/ui-trd.md §5 (wizard step ordering)
- docs/ui-prd.md §7.1 (rationale for manual-with-deep-links UX in related
  cases)
- The original Option A attempt was verified to fail at Step 8 with exit
  127 (`timeout` not found) on Git Bash 4.4.23 / Windows 11 / Azure CLI
  2.86.0 on 2026-05-13.
