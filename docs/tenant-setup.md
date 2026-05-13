# M365 MCP Scanner — Tenant Setup Runbook

End-to-end provisioning of permissions, app registration, role assignments, and Dataverse access required to run the scanner against a Microsoft 365 tenant.

| | |
|---|---|
| **Audience** | Tenant administrator with Global Admin or equivalent rights |
| **Time required** | 30 minutes one-time + 5 minutes per Power Platform environment |
| **Prerequisites** | Global Administrator role; Azure CLI optionally for the scripted path |
| **Output** | Tenant ID, client ID, client secret, and a service principal provisioned across the right scopes |

---

## Table of contents

1. [Overview](#1-overview)
2. [Surface coverage and permission justification](#2-surface-coverage-and-permission-justification)
3. [Network egress requirements](#3-network-egress-requirements)
4. [Step-by-step setup](#4-step-by-step-setup)
5. [Scripted setup (optional)](#5-scripted-setup-optional)
6. [Configure and run the scanner](#6-configure-and-run-the-scanner)
7. [Troubleshooting](#7-troubleshooting)
8. [Decommissioning the scanner](#8-decommissioning-the-scanner)
9. [Reference](#9-reference)
10. [Document control](#10-document-control)

---

## 1. Overview

This runbook walks a tenant administrator through everything required to enable the M365 MCP Scanner against a Microsoft 365 tenant. Setup completes in approximately 30 minutes and consists of five distinct configuration steps.

The scanner is a read-only security tool. It enumerates Model Context Protocol (MCP) server usage across the tenant by querying Microsoft Graph, Power Platform admin APIs, and Dataverse. It performs no write operations, no data modifications, and no automated remediation. All findings are stored locally on the operator's machine.

### What gets configured

- An Entra ID application registration named (by default) "M365 MCP Scanner"
- Eight Microsoft Graph permissions: five application-level and three delegated
- Admin consent on all granted permissions
- A client secret with one-year expiry
- Power Platform Administrator role assigned to the application's service principal
- A Dataverse application user added to each Power Platform environment in scope, with the System Administrator security role

### What does NOT get configured

- Any write permissions to Microsoft Graph, Dataverse, or Power Platform
- Any user accounts, groups, or licenses beyond the single service principal
- Any conditional access policies, network restrictions, or firewall rules
- Any data egress outside the tenant boundary

The scanner runs locally and stores results in a JSON file on the operator's machine. No telemetry is sent to the scanner vendor or any third party.

---

## 2. Surface coverage and permission justification

The scanner covers six distinct surfaces where MCP servers can exist in a Microsoft 365 tenant. Each surface requires specific permissions. The table below maps surfaces to the permissions they require and explains why each is necessary.

| Surface | Required permissions | Why needed |
|---|---|---|
| Synced Copilot Connectors | `Application.Read.All`<br>`User.Read.All` | Enumerate service principals and resolve owner names for synced connector resources |
| First-party MCP servers | `Application.Read.All`<br>`DelegatedPermissionGrant.Read.All` | Identify Microsoft-published MCP server applications and the clients that consume them |
| Custom Power Platform connectors | Power Platform Administrator role (directory role, not a Graph permission) | List environments and enumerate custom connectors tenant-wide via the Power Platform admin API |
| Copilot Studio agents | Power Platform Administrator<br>Dataverse System Administrator (per environment) | Query the Dataverse `bot` and `botcomponent` tables in each environment to find agents and their MCP tool wiring |
| Declarative agents via Copilot admin catalog | `CopilotPackages.Read.All` (delegated) | Query the Copilot admin catalog for pro-code declarative agents. Requires Agent 365 license on the tenant. |
| Declarative agents via Teams app catalog | `TeamsApp.Read.All`<br>`Directory.Read.All` (delegated, fallback path) | Enumerate org-distributed Teams apps and identify those that bundle a declarative agent |

### Additional permissions for future enrichment

The scanner is built to support a forthcoming Score stage that will enrich findings with consent and runtime activity data. Two additional permissions are granted now to avoid a second consent flow later:

- `DelegatedPermissionGrant.Read.All` — read OAuth permission grants for MCP servers; used by the score stage to compute consent scope risk
- `AuditLog.Read.All` — read Graph activity logs; used by the score stage to identify dormant or never-invoked MCP wirings

If your security policy prefers a least-privilege initial deployment, these two permissions can be omitted until the score stage is enabled. Doing so will not affect the inventory phase of the scanner.

---

## 3. Network egress requirements

The scanner makes HTTPS calls to the following Microsoft endpoints. If your environment uses egress filtering or a proxy, allow these domains for the machine that runs the scanner.

| Domain | Purpose |
|---|---|
| `login.microsoftonline.com` | Entra ID token acquisition (app-only and delegated) |
| `graph.microsoft.com` | Microsoft Graph API (all Graph-based surfaces) |
| `api.bap.microsoft.com` | Power Platform admin API (environment enumeration) |
| `api.powerapps.com` | Power Apps connector inventory and swagger fetch |
| `*.crm.dynamics.com`<br>`*.api.crm.dynamics.com` | Dataverse Web API (per environment) for `bot` and `botcomponent` table reads |
| `*.blob.core.windows.net` | Connector swagger files served via SAS-signed Azure Blob URLs (an undocumented Microsoft pattern) |

### Optional egress (only with `--probe` flag)

If the scanner is run with the `--probe` flag, it will additionally reach out to the URLs of any MCP servers it discovers in the tenant in order to verify reachability and capture advertised tools. These URLs are not known in advance — they depend on what the scan finds.

Probing is off by default. If your security policy disallows outbound calls to non-Microsoft domains from the scanner machine, leave the default disabled state.

---

## 4. Step-by-step setup

This section describes the manual click-through setup. A scripted version using Azure CLI is provided in [Section 5](#5-scripted-setup-optional) for environments where automation is preferred.

### Step 1: Create the Entra ID application registration

Sign in to the Entra admin center as Global Administrator.

1. Navigate to <https://entra.microsoft.com>
2. In the left navigation, select **Identity → Applications → App registrations**
3. Click **+ New registration** at the top
4. Enter a name (recommended: `M365 MCP Scanner`)
5. Under Supported account types, select **Accounts in this organizational directory only (Single tenant)**
6. Leave Redirect URI blank
7. Click **Register**

On the newly created app's overview page, copy and save the **Application (client) ID** value. This is the `client_id` the scanner will use.

### Step 2: Enable public client flows

The scanner uses two authentication modes: app-only (with the client secret you'll create in Step 4) and delegated device-code (for admin-only Graph endpoints like the Copilot admin catalog). The device-code flow requires the app to be configured as a public client.

1. In the app's left navigation, click **Authentication**
2. Click **+ Add a platform**
3. Select **Mobile and desktop applications**
4. Check the box for `https://login.microsoftonline.com/common/oauth2/nativeclient`
5. Click **Configure**
6. Scroll down to **Advanced settings**
7. Set **Allow public client flows** to **Yes**
8. Click **Save** at the top

> **Important:** Without the "Allow public client flows" toggle set to Yes, the delegated login command (`mcp-scan login`) will return error `AADSTS7000218`. This is a non-obvious configuration requirement that has cost projects hours of debugging — do not skip it.

### Step 3: Add API permissions

Eight permissions in total: five application-level and three delegated. The application-level permissions are granted to the app's service principal and consumed via the client secret. The delegated permissions are granted to whoever signs in via the device-code flow.

#### 3a. Add application permissions

1. In the app, click **API permissions** in the left navigation
2. Click **+ Add a permission**
3. Click **Microsoft Graph**
4. Click **Application permissions**
5. Search for and check each of the following:
   - `Application.Read.All`
   - `DelegatedPermissionGrant.Read.All`
   - `User.Read.All`
   - `AuditLog.Read.All`
   - `TeamsApp.Read.All`
6. Click **Add permissions** at the bottom

#### 3b. Add delegated permissions

1. Click **+ Add a permission** again
2. Click **Microsoft Graph**
3. Click **Delegated permissions**
4. Search for and check each of the following:
   - `CopilotPackages.Read.All`
   - `Directory.Read.All`
   - `User.Read`
5. Click **Add permissions** at the bottom

#### 3c. Grant admin consent

Permissions are added but not yet active. A Global Administrator must explicitly grant tenant-wide consent.

1. At the top of the API permissions page, click **Grant admin consent for &lt;tenant name&gt;**
2. Click **Yes** in the confirmation dialog

Every row in the permissions table should now show a green checkmark in the Status column. If any row remains yellow or red, the permission was not consented; re-run the consent flow.

### Step 4: Create a client secret

1. In the app, click **Certificates & secrets** in the left navigation
2. On the **Client secrets** tab, click **+ New client secret**
3. Enter a description (recommended: `Scanner secret`)
4. Set **Expires** to 6 months or 1 year, depending on your secret rotation policy
5. Click **Add**

> **Critical:** Immediately copy and securely save the **Value** column of the new secret. This is the only time it will ever be displayed. After you navigate away from this page, the value is permanently hidden, and you will need to create a new secret to recover access.

Do not copy the "Secret ID" column — that is not the secret value. The value column contains a long opaque string.

### Step 5: Assign Power Platform Administrator role

Power Platform's admin API is not gated by Graph permissions. It uses a separate directory role that must be assigned to the scanner's service principal.

1. In the Entra admin center, navigate to **Identity → Roles & admins → Roles & admins**
2. Search for **Power Platform Administrator**
3. Click the role name to open it
4. Click **+ Add assignments**
5. In the search box, type the name of your scanner app (`M365 MCP Scanner` or whatever name you chose in Step 1)
6. Select the matching service principal from the dropdown
7. Click **Add** at the bottom

If your tenant requires Privileged Identity Management (PIM) for admin roles, you may need to convert this to an active assignment rather than an eligible one for service principals to use it.

### Step 6: Add the scanner as a Dataverse application user (per environment)

To discover Copilot Studio agents and their MCP wiring, the scanner reads Dataverse tables directly. Dataverse access is scoped per-environment and requires explicit user creation.

> **Repeat this step for every Power Platform environment in scope.** At minimum, include the default environment and any environment where Copilot Studio is used. If you skip an environment, the scanner will produce a clean `no_dataverse_access` error for that environment rather than crashing — but it will not discover any agents there.

1. Navigate to <https://admin.powerplatform.microsoft.com>
2. In the left navigation, click **Environments**
3. Click into the target environment
4. Click **Settings** at the top right
5. Under **Users + permissions**, click **Application users**
6. Click **+ New app user** at the top
7. Click **+ Add an app** in the panel that opens
8. Search for the scanner app name, select it, and click **Add**
9. Business unit: leave as the default value
10. Under **Security roles**, click the pencil icon
11. Select **System Administrator** (or a custom least-privilege role if your organization has one)
12. Click **Save**
13. Click **Create**

Verify the application user was created by refreshing the Application users list. The scanner app should appear with its assigned security role.

---

## 5. Scripted setup (optional)

For environments that prefer infrastructure-as-code or automated provisioning, the steps in [Section 4](#4-step-by-step-setup) can be partially automated using Azure CLI. The Dataverse application user step (Step 6) must remain manual because the Power Platform Admin Center does not currently expose a stable API for application user creation.

### Prerequisites

- Azure CLI installed (verify with `az --version`)
- Sign in via `az login --tenant <tenant-id>`
- Current user must hold Global Administrator or equivalent rights

### Script outline

The setup script performs the following operations:

1. `az ad app create` — creates the app registration
2. `az ad app update --set isFallbackPublicClient=true` — enables public client flows
3. `az ad sp create` — creates the service principal
4. `az ad app permission add` (×8) — adds the required Graph permissions
5. `az ad app permission admin-consent` — attempts admin consent (may require manual fallback)
6. `az ad app credential reset` — creates the client secret
7. `az rest` to Graph — activates the Power Platform Administrator role and assigns the service principal

The complete script is available in the scanner repository as `setup-scanner.sh`. Use it as follows:

```bash
./setup-scanner.sh <tenant-id> ["App display name"]
```

After the script completes, perform Step 6 manually for each environment, then proceed to [Section 6](#6-configure-and-run-the-scanner) to configure the scanner.

---

## 6. Configure and run the scanner

### Configuration

The scanner reads configuration from environment variables or a TOML config file at `~/.m365-mcp-scanner/config.toml`. The values you need to provide are:

| Value | Source |
|---|---|
| `M365_MCP_TENANT_ID` | The tenant's Directory (tenant) ID, visible on the Entra overview page |
| `M365_MCP_CLIENT_ID` | The Application (client) ID copied in Step 1 |
| `M365_MCP_CLIENT_SECRET` | The client secret value copied in Step 4 (the long opaque string, not the secret ID) |

To configure via environment variables (Linux/macOS):

```bash
export M365_MCP_TENANT_ID="6cf34320-..."
export M365_MCP_CLIENT_ID="<your-app-id>"
export M365_MCP_CLIENT_SECRET="<your-secret>"
```

Or via PowerShell (Windows):

```powershell
$env:M365_MCP_TENANT_ID = "6cf34320-..."
$env:M365_MCP_CLIENT_ID = "<your-app-id>"
$env:M365_MCP_CLIENT_SECRET = "<your-secret>"
```

### Verify connectivity

Before running a real scan, verify that the scanner can mint tokens for all three required audiences and reach each API surface:

```bash
mcp-scan doctor
```

The doctor command runs a series of checks and prints a green/red status for each. A clean run shows green checkmarks for:

- Microsoft Graph token acquisition (app-only)
- Power Platform admin API token acquisition
- Dataverse token acquisition for at least one environment
- Successful test call against Microsoft Graph
- Successful test call against the Power Platform admin API

If any check fails, the error message indicates which setup step needs revisiting. Common failure modes:

- **Graph 401:** Admin consent missing (Step 3c) or wrong client_id/secret (Steps 1/4)
- **Power Platform admin 403:** Role assignment not active (Step 5)
- **Dataverse 403:** Application user missing in at least one environment (Step 6)

### Enable delegated session (one-time, per machine)

Surfaces 5a and 5b use delegated authentication. A one-time interactive sign-in is required before these surfaces will function. The cached refresh token persists for approximately 90 days.

```bash
mcp-scan login
```

The command prints a URL and a short device code. Open the URL in a browser, enter the code, sign in as a Global Administrator or AI Administrator, and approve the consent dialog. The terminal will then confirm sign-in.

The cached token is stored in an encrypted file at `~/.m365-mcp-scanner/msal_token_cache.bin` (POSIX) or `%LOCALAPPDATA%\m365-mcp-scanner\msal_token_cache.bin` (Windows). The encryption key is derived from tenant ID, client ID, and the user's home path. To clear the cache, run `mcp-scan logout`.

### Run a scan

```bash
mcp-scan run
```

This executes all six surfaces and writes the scan document to `~/.m365-mcp-scanner/scans/<scan-id>.json`. A summary table is printed to the terminal.

To run only specific surfaces:

```bash
mcp-scan run --scope custom_connectors,copilot_studio
```

To enable live MCP server probing (off by default):

```bash
mcp-scan run --probe
```

---

## 7. Troubleshooting

### Login error AADSTS7000218

**Symptom:** `mcp-scan login` returns `AADSTS7000218` "The request body must contain the following parameter: 'client_assertion' or 'client_secret'".

**Cause:** The Entra app is configured as a confidential client but the device-code flow expects public-client mode.

**Fix:** Step 2 of this runbook. In the Entra portal, on the app's Authentication page, set "Allow public client flows" to Yes.

### Login error WinError 1783 on Windows

**Symptom:** `mcp-scan login` returns `OSError WinError 1783` "The stub received bad data" during token persistence.

**Cause:** This was an early issue with the keyring library's Windows Credential Manager backend, which cannot store MSAL token blobs exceeding ~2,560 bytes. The scanner now uses an encrypted file cache by default, which avoids this issue.

**Fix:** Ensure you are running a recent version of the scanner. The file-based cache was introduced to bypass this Windows limitation entirely.

### Surface 5a returns tenant_not_eligible

**Symptom:** A scan completes but the Copilot Packages surface reports "tenant not eligible for Copilot Packages API: Customer must be a licensed for Agent 365 in order to use Agent 365 Graph APIs."

**Cause:** The Copilot admin catalog API requires Agent 365 licensing on the tenant. This is a Microsoft product license, distinct from the Frontier preview program and from the standard Microsoft 365 Copilot end-user license.

**Fix:** This is expected behavior in tenants without Agent 365. The scanner code is functioning correctly; the surface simply has no data to return. In a tenant with Agent 365 licensing, this surface returns declarative agent records normally.

### Surface 5b returns delegated_session_required

**Symptom:** The Teams App Catalog surface reports "delegated session required for Teams App Catalog; run `mcp-scan login` to enable this surface."

**Cause:** Surface 5b uses delegated authentication and no cached session exists.

**Fix:** Run `mcp-scan login` once. The cached refresh token persists for ~90 days.

### Multiple no_dataverse_access errors

**Symptom:** A scan completes but reports `no_dataverse_access` errors for one or more environments.

**Cause:** The scanner's service principal was not added as an application user in those environments.

**Fix:** Repeat Step 6 of this runbook for each environment that reported the error. The error message includes the environment ID and Dataverse host URL to help identify which environments need attention.

### Surface 5b returns manifest_endpoint_unavailable

**Symptom:** A scan discovers a declarative agent in the Teams catalog but the surface reports `manifest_endpoint_unavailable` with a Microsoft 400 BadRequest error citing "Resource not found for the segment 'manifest'".

**Cause:** Microsoft Graph's `/appCatalogs/teamsApps/{id}/appDefinitions/{def-id}/manifest` endpoint returns 400 for declarative-agent-only Teams apps. This is undocumented behavior; the endpoint works for traditional Teams apps but not for declarative-agent-only apps.

**Fix:** This is a Microsoft Graph API gap, not a scanner bug. The scanner records the agent's metadata (display name, app ID, publishing state) but cannot retrieve the full plugin manifest content. Microsoft has signaled that the Agent Registry APIs slated for May 2026 may close this gap. Until then, the agent's MCP wiring details must be inspected via the Teams Developer Portal or the app's source repository.

---

## 8. Decommissioning the scanner

To remove all scanner provisioning from the tenant, reverse the setup steps in approximately the same order. The scanner has no automated teardown tooling at this time.

### Cleanup checklist

1. Sign in to <https://entra.microsoft.com> as Global Administrator
2. Navigate to **Identity → Applications → App registrations**
3. Find the scanner app, click into it, and click **Delete** at the top
4. In **Roles & admins**, find Power Platform Administrator, and remove the service principal from the role
5. In the Power Platform Admin Center, for each environment, navigate to **Settings → Users + permissions → Application users**, find the scanner's app user, and delete it
6. On the operator's machine, delete `~/.m365-mcp-scanner/` (Linux/macOS) or `%LOCALAPPDATA%\m365-mcp-scanner\` (Windows) to remove cached tokens and scan results

After completing this checklist, the tenant has no remaining trace of the scanner. Audit logs from the time the scanner was active are retained per the tenant's standard retention policy.

---

## 9. Reference

### Permission identifiers

For audit or scripting purposes, the GUIDs for the eight Graph permissions used by the scanner are listed below.

| Permission name | Type | GUID |
|---|---|---|
| `Application.Read.All` | Application | `9a5d68dd-52b0-4cc2-bd40-abcf44ac3a30` |
| `DelegatedPermissionGrant.Read.All` | Application | `81b4724a-58aa-41c1-8a55-84ef97466587` |
| `User.Read.All` | Application | `df021288-bdef-4463-88db-98f22de89214` |
| `AuditLog.Read.All` | Application | `b0afded3-3588-46d8-8b3d-9842eff778da` |
| `TeamsApp.Read.All` | Application | `5e1e9171-754d-478c-812c-f1755a9a4c2d` |
| `CopilotPackages.Read.All` | Delegated | `bf5bb47b-1e74-4f1f-be32-f33d3066c5dd` |
| `Directory.Read.All` | Delegated | `06da0dbc-49e2-44d2-8312-53f166ab848a` |
| `User.Read` | Delegated | `e1fe6dd8-ba31-4d61-89e7-88639da4683d` |

### Directory role identifiers

| Role name | Template ID |
|---|---|
| Power Platform Administrator | `11648597-926c-4cf3-9c36-bcebb0ba8dcc` |

### Known Microsoft API quirks

The following undocumented Microsoft behaviors were discovered while building the scanner. They are noted here so administrators and security reviewers are not surprised by them.

#### Dataverse hostnames include an `.api.` subdomain

Microsoft's documentation describes the Dataverse Web API base URL as `https://<orgid>.crm.dynamics.com/`. In practice, the `linkedEnvironmentMetadata.instanceApiUrl` field returned by the Power Platform admin API uses the form `https://<orgid>.api.crm.dynamics.com/` (with an additional `.api.` segment). Both forms appear to work, but the scanner uses the URL exactly as returned by Microsoft's own API.

#### Power Apps connector swaggers are served from SAS-signed Azure Blob URLs

When the scanner fetches the swagger definition for a custom connector, the URL returned by the Power Apps admin API in the `properties.apiDefinitions.originalSwaggerUrl` field is a SAS-signed Azure Blob Storage URL, not a Microsoft API endpoint. This is undocumented but consistent behavior. The scanner follows the URL verbatim; no special handling is required, but the egress requirement to `*.blob.core.windows.net` stems from this pattern.

#### Surface 5a gate is Agent 365 licensing, not Frontier program

Earlier Microsoft documentation suggested the Copilot Packages API was gated by the Frontier preview program. The actual 403 response from Microsoft cites Agent 365 licensing. Frontier and Agent 365 are related but distinct programs — Frontier is an invitation-based preview program; Agent 365 is a licensed product. The scanner labels this surface `tenant_not_eligible` to disambiguate from permission-consent errors.

#### Graph manifest endpoint returns 400 for declarative-agent-only apps

Microsoft Graph's `/v1.0/appCatalogs/teamsApps/{id}/appDefinitions/{def-id}/manifest` endpoint should return the full Teams app manifest. For traditional Teams apps (tabs, bots, message extensions) it does. For declarative-agent-only apps, it returns 400 BadRequest with the message `"Resource not found for the segment 'manifest'"`. This is undocumented. The scanner detects the gap structurally, emits the agent metadata it can see from the `appDefinitions` expand, and records `manifest_fetch_status: unavailable` in the agent's source reference. Microsoft has signaled that the Agent Registry APIs slated for May 2026 may close this gap.

---

## 10. Document control

Use this section to record completion of the setup for audit purposes.

| Field | Value |
|---|---|
| Tenant ID | |
| Tenant display name | |
| App registration created | Date and operator: |
| App ID (client_id) | |
| Admin consent granted | Date and approving admin: |
| Client secret expiry | Date: |
| Power Platform role assigned | Date: |
| Environments added (Step 6) | List each environment by name: |
| First successful scan | Scan ID and date: |
