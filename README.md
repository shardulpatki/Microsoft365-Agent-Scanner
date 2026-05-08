# M365 Copilot MCP Scanner

CLI tool that discovers MCP (Model Context Protocol) server usage across a Microsoft 365 tenant.

**Read-only.** No write operations against any Microsoft API. No telemetry.

## Phase 1 status

| Surface | Status | API |
|---|---|---|
| Synced Copilot Connectors (Microsoft Graph connectors) | ✅ Phase 1 | `GET /external/connections` |
| First-party MCP servers | ✅ Phase 1 | `GET /servicePrincipals?$filter=appId eq '…'` |
| Copilot Studio agents | ⏳ Phase 2 | Power Platform Inventory + Dataverse |
| Declarative agents | ⏳ Phase 3 | Graph Copilot Packages |
| Custom connectors with MCP shape | ⏳ Phase 4 | Power Apps connector defs |
| **Federated Copilot Connectors (MCP-tagged)** | 🚧 **Phase 6 — blocked** | No public Graph API yet; managed via `Connector.Cmd` PowerShell module + internal admin-center API |

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

# Run a scan (default scope = both Phase 1 surfaces)
mcp-scan run

# Or pick surfaces explicitly
mcp-scan run --scope synced_copilot_connectors,first_party_mcp

# List discovered servers from latest scan
mcp-scan servers list

# JSON output for piping
mcp-scan run --format json | jq '.mcp_servers | length'
```

The legacy alias `--scope copilot_connectors` is still accepted and resolves to
`synced_copilot_connectors`.

## Required Entra app permissions

Phase 1 requires the following **Microsoft Graph application permissions**, all
admin-consented:

| Permission | Used for |
|---|---|
| `ExternalConnection.Read.All` | `synced_copilot_connectors` discoverer |
| `Application.Read.All` | `first_party_mcp` discoverer |

## Persistence

Scans are stored as JSON files under `~/.m365-mcp-scanner/scans/`. There is no database.

A `latest.json` pointer is updated after each successful scan (symlink on POSIX, small JSON file on Windows).
