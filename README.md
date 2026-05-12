# M365 Copilot MCP Scanner

CLI tool that discovers MCP (Model Context Protocol) server usage across a Microsoft 365 tenant.

**Read-only.** No write operations against any Microsoft API. No telemetry.

## Surface status

| Surface | Status | API |
|---|---|---|
| Synced Copilot Connectors (Microsoft Graph connectors) | ✅ shipping | `GET /external/connections` |
| First-party MCP servers | ✅ shipping | `GET /servicePrincipals?$filter=appId eq '…'` |
| Custom connectors with MCP shape | ✅ shipping | Power Platform admin + Power Apps connector defs |
| Declarative agents — Teams App Catalog | ✅ shipping (delegated) | `GET /v1.0/appCatalogs/teamsApps` |
| Declarative agents — Copilot Packages | ✅ shipping (delegated, Agent 365 license required) | `GET /beta/copilot/admin/catalog/packages` |
| Copilot Studio agents with MCP tools | ✅ shipping | Power Platform admin + Dataverse `bots` / `botcomponents` |
| **Federated Copilot Connectors (MCP-tagged)** | 🚧 **Phase 6 — blocked** | No public Graph API yet; managed via `Connector.Cmd` PowerShell module + internal admin-center API |

### Custom connectors with MCP shape

Power Apps custom connectors whose OpenAPI/Swagger spec carries
`x-ms-agentic-protocol: mcp-streamable-1.0` are MCP servers registered against
a Power Platform environment. The scanner enumerates every environment visible
to the admin app (`api.bap.microsoft.com`), lists each environment's connectors
(`api.powerapps.com`), and flags those whose spec contains the MCP extension —
either at the operation level (current shape) or at the spec top level
(legacy shape). Filtering is by extension only, never by name.

### Copilot Studio agents with MCP tools

Copilot Studio agents are stored in Dataverse's `bot` table; each topic, action,
and tool the agent uses is a row in `botcomponent`. When a Copilot Studio author
adds an MCP server as a tool, the resulting `botcomponent` carries a YAML `data`
blob with the shape:

```yaml
kind: TaskDialog
action:
  kind: InvokeExternalAgentTaskAction
  connectionReference: <logical name>
  operationDetails:
    kind: ModelContextProtocolMetadata
    operationId: InvokeServer
```

The scanner enumerates Power Platform environments (`api.bap.microsoft.com`)
with `$expand=properties/linkedEnvironmentMetadata` to surface each env's
Dataverse org URL, then per env queries `/api/data/v9.2/bots`,
`/api/data/v9.2/botcomponents` and `/api/data/v9.2/connectionreferences` to
resolve the wired connector. The scanner SP must be added as an **application
user** with a sufficient security role in each Dataverse env; envs without
that grant return 401/403 and are recorded as `no_dataverse_access` so
sibling envs continue.

### Declarative agents (Phase 3)

Declarative agents — Copilot agents authored with the M365 Agents Toolkit and
deployed as Teams apps — wire MCP servers through their manifest's `actions`
block (`type: "mcpServer"`). Two Graph surfaces expose them:

* **Teams App Catalog** (`/v1.0/appCatalogs/teamsApps`) — the practical demo
  path. Each org-distributed Teams app's manifest is fetched and parsed for
  MCP-shaped actions. Works against any tenant where the scanner's app has
  been admin-consented `AppCatalog.Read.All` (or `Directory.Read.All`).
  Live-validated in Phase 3 against the dev tenant: API returns 200 OK with
  an empty array (no org-distributed declarative agents in the tenant).
* **Copilot Packages** (`/beta/copilot/admin/catalog/packages`) — gated by
  **Agent 365 licensing**, not by the Frontier preview program as we
  originally documented. Live testing returned an authoritative Microsoft
  403 citing the license requirement verbatim:

  > Customer must be a licensed for Agent 365 in order to use Agent 365
  > Graph APIs

  The scanner records this as a `tenant_not_eligible` error and continues.
  Frontier (an invitation-based preview program) and Agent 365 (a licensed,
  purchasable product) are related but distinct gating mechanisms; this
  endpoint is now branded under Agent 365 Graph APIs.

Both surfaces require **delegated** authentication. Run `mcp-scan login`
once to consent and cache a refresh token in an encrypted local file (see
*Delegated login* below). Phase 1 + custom-connectors discovery continues
to work app-only without a delegated session — those surfaces are
unaffected by login state.

### Federated connectors caveat

The MCP-tagged connectors visible in `admin.cloud.microsoft/#/copilot/connectors`
(LSEG, Moody's, Notion, HubSpot, …) are **federated** Copilot Connectors and do
**not** appear under `/external/connections`. They have no public Graph API at
the moment — Microsoft manages them via the `Connector.Cmd` PowerShell module
and an internal admin-center API. Discovery for that surface is parked as Phase 6
until a public API ships.

`/external/connections` covers the *synced* connectors (Microsoft Graph
connectors that index external content into the search graph). That's what
this scanner reports today.

## Quickstart

```bash
pip install -e .[dev]

# Configure (or use a .env file in the repo root)
export M365_MCP_TENANT_ID=<tenant-guid>
export M365_MCP_CLIENT_ID=<app-registration-client-id>
export M365_MCP_CLIENT_SECRET=<client-secret>

# Verify auth + Graph reachability
mcp-scan doctor

# Run a scan (default scope = all shipping surfaces)
mcp-scan run

# Or pick surfaces explicitly
mcp-scan run --scope synced_copilot_connectors,first_party_mcp,custom_connectors

# List discovered servers from latest scan
mcp-scan servers list

# JSON output for piping
mcp-scan run --format json | jq '.mcp_servers | length'
```

The legacy alias `--scope copilot_connectors` is still accepted and resolves to
`synced_copilot_connectors`. The shorthand `--scope declarative` expands to
both `declarative_agents_packages` and `declarative_agents_teamsapp`.

### Delegated login (optional, for declarative agents)

```bash
# One-time interactive login. Prints a device code; visit the URL, paste the
# code, sign in. The refresh token is cached in an encrypted file (see below).
mcp-scan login

# Confirm the session is active.
mcp-scan doctor

# Clear the cached session.
mcp-scan logout
```

The delegated session is persisted to an encrypted file:

- Windows: `%LOCALAPPDATA%\m365-mcp-scanner\msal_token_cache.bin`
- macOS / Linux: `~/.m365-mcp-scanner/msal_token_cache.bin` (dir `0700`, file `0600`)

It is encrypted with a Fernet key derived (PBKDF2-HMAC-SHA256, 600 000
iterations, per-install random salt) from the tenant id, client id, and
the user's home path. This mirrors Microsoft Azure CLI's approach.

The previous implementation used the `keyring` library's Windows Credential
Manager backend. It failed reproducibly on Windows with:

> `OSError: [WinError 1783] The stub received bad data` (raised from
> `win32cred.CredWrite`)

Root cause: Windows Credential Manager caps the password/blob field of a
Generic Credential at ~2 560 bytes, and a serialized MSAL cache (access
tokens + refresh token + ID tokens + account metadata for one or more
scopes) routinely exceeds that. Azure CLI hit the same wall and also
moved off OS keyrings on Windows.

**Threat model.** A local attacker running as the user can read the cache
either way — both Windows Credential Manager and our file cache are
protected by the same OS-level user boundary, since the encryption key is
derivable from machine-local inputs. The file cache trades a
weaker-than-DPAPI primitive for actually working on Windows. Run
`mcp-scan logout` to delete it cleanly.

If you skip login, declarative-agent surfaces are still attempted but skipped
with a `delegated_session_required` entry in `errors[]`. All other surfaces
run normally.

## Required Entra app permissions

**Microsoft Graph application permissions** (admin-consented):

| Permission | Used for |
|---|---|
| `ExternalConnection.Read.All` | `synced_copilot_connectors` discoverer |
| `Application.Read.All` | `first_party_mcp` discoverer |

**Microsoft Graph delegated permissions** (admin-consented; Phase 3 only):

| Permission | Used for |
|---|---|
| `AppCatalog.Read.All` (or `Directory.Read.All`) | `declarative_agents_teamsapp` |
| `CopilotPackages.Read.All` | `declarative_agents_packages` (Agent 365-licensed tenants) |
| `User.Read` | identity resolution after device-code login |

**Power Platform admin** (one-time PowerShell registration of the SP, not a
Graph permission):

```powershell
# Run as Power Platform Administrator
Install-Module -Name Microsoft.PowerApps.Administration.PowerShell -Scope CurrentUser
New-PowerAppManagementApp -ApplicationId <client-id>
```

This grants the scanner's service principal read access to environments and
connectors via `api.bap.microsoft.com` / `api.powerapps.com`. Required for the
`custom_connectors` discoverer.

## Undocumented Microsoft behaviors

Engineering notes from building this scanner — three places where the
live API surface differs from (or extends) Microsoft's public docs:

1. **Power Apps custom-connector swagger via SAS-signed Azure Blob URL.**
   Power Platform admin's connector-definition endpoint exposes the
   OpenAPI spec on `properties.apiDefinitions.originalSwaggerUrl` as a
   pre-signed Azure Blob URL with an embedded SAS token, not as inline
   JSON. The scanner fetches that URL directly without any extra
   authentication header. (Phase 4 finding.)

2. **Dataverse `instanceApiUrl` uses an undocumented `.api.` subdomain.**
   The `linkedEnvironmentMetadata.instanceApiUrl` returned by the Power
   Platform Admin API takes the form
   `https://org<id>.api.crm.dynamics.com/` — note the `.api.` segment.
   Microsoft's documented Dataverse Web API URL is
   `https://org<id>.crm.dynamics.com/` without `.api.`. Both forms work
   identically; the scanner uses the value verbatim as both `base_url`
   and token audience. (Phase 2 finding.)

3. **Surface 5a gate is Agent 365 licensing, not the Frontier program.**
   The `/beta/copilot/admin/catalog/packages` endpoint is documented in
   Frontier-preview contexts but the actual 403 cites a licensed-product
   requirement (see the *Declarative agents (Phase 3)* section above for
   the verbatim error). Frontier and Agent 365 are distinct gating
   mechanisms — the first is an invitation-based preview, the second is
   a paid license. (Phase 3 finding.)

## Persistence

Scans are stored as JSON files under `~/.m365-mcp-scanner/scans/`. There is no database.

A `latest.json` pointer is updated after each successful scan (symlink on POSIX, small JSON file on Windows).
