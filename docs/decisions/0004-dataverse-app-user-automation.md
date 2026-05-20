# Dataverse application user automation via BAP addAppUser

## Status

Accepted — 2026-05-20. Supersedes the initial Dataverse Web API
`POST /systemusers` approach (see "Historical record" below).

## Context

The M365 MCP Scanner needs an application user provisioned in each
Power Platform environment it scans. The application user gives the
scanner's Entra service principal authorization to read the `bot`,
`botcomponent`, `connectionreference`, and `conversationtranscript`
Dataverse tables — required for the Copilot Studio discovery path.

This provisioning step must be:

- **Automated** — no manual admin-center clicks per env at install time.
- **Per-environment** — each env's Dataverse instance needs its own
  systemuser.
- **Idempotent** — re-running the wizard against an already-provisioned
  env must not error or duplicate.
- **Acceptable to a Power Platform administrator** — no additional Entra
  permissions beyond what Steps 1-4 of the wizard already establish.

Two API surfaces are documented for this:

1. **Dataverse Web API** — `POST /api/data/v9.2/systemusers` directly
   against each env's Dataverse Web API. The hand-rolled, explicit path.
   Requires the wizard to know each env's Dataverse org URL, mint a
   Dataverse-audience token, post a record with the right relationship
   bindings, then assign a security role via a separate call.
2. **BAP addAppUser** — `POST` against the Power Platform admin BAP
   endpoint at `api.bap.microsoft.com`. Microsoft's managed alternative.
   The body is just `{"servicePrincipalAppId": "<scanner-client-id>"}`.
   BAP creates the systemuser and assigns System Administrator
   automatically.

The initial implementation chose path 1 (Dataverse Web API). It failed
in ways that took ~6 hours of diagnostic work to fully understand, then
was abandoned for path 2.

## Decision

Use BAP `addAppUser` as the sole provisioning path. The endpoint is:

```
POST https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments/{envId}/addAppUser?api-version=2020-10-01
Authorization: Bearer <token, audience https://service.powerapps.com/>
Content-Type: application/json
Body: {"servicePrincipalAppId": "<settings.client_id>"}
```

Microsoft documentation:
<https://learn.microsoft.com/en-us/power-platform/admin/create-dataverseapplicationuser>

The call returns 200 OK with empty body on success. System Administrator
role is auto-assigned by BAP (verified empirically against the Armor1
tenant on 2026-05-20). No follow-up role-assignment call is needed.

## Implementation

Five coordinated components handle the realities of Microsoft's
eventually-consistent infrastructure:

### Component 1: BAP call with retry — `src/m365_mcp_scanner/provisioning/app_user_provisioner.py`

`provision_app_user` fires the `addAppUser` POST. Retry policy:

- HTTP 429: honor `Retry-After` header.
- HTTP 5xx: exponential backoff 1s/2s/4s, max 3 retries.
- HTTP 4xx: no retry; capture error code and message.
- Network errors: same as 5xx.

`env_id` derivation: `str(env.get("name", ""))`. The `"name"` field on
the BAP env dict is the env GUID; `"id"` is the full resource path. The
wizard, page handlers, env_row renderer, and provisioner must all use
the same key derivation. See `Consequences` below for why this matters.

`provision_app_user_batch` fans out per-env calls under
`asyncio.Semaphore(8)`. Per-env failures are isolated; one env raising
does not abort the batch.

### Component 2: Dataverse propagation polling — same file

BAP returns 200 immediately, but the resulting systemuser does not
appear in Dataverse for ~10-60 seconds (Microsoft-side asynchronous
provisioning).

After BAP returns 2xx, `provision_app_user` enters a polling phase:

- Acquire a Dataverse-audience token for the env's org URL (read from
  `env["properties"]["linkedEnvironmentMetadata"]["instanceApiUrl"]`).
- `GET {org_url}/api/data/v9.2/WhoAmI` every 2 seconds.
- HTTP 200 → propagation complete, return success.
- HTTP 401/403 → propagation in progress, sleep 2s, retry.
- 60-second total timeout via `time.monotonic()` deadline.
- On timeout: return `status="error"` with
  `error_code="propagation_timeout"`.
- Envs with no Dataverse (`linkedEnvironmentMetadata` absent) skip
  polling and return success immediately.

Without this polling, the wizard reports ✓ on Step 6 the moment BAP
returns 200, and the operator's first scan returns empty because the
scanner SP has no Dataverse access yet. The ✓ would be misleading;
polling makes it truthful.

### Component 3: Entra→MSAL propagation retry — `src/m365_mcp_scanner/auth/msal_broker.py`

A separate Microsoft propagation race affects Step 5 (the doctor check),
not Step 6. Step 3 of the wizard creates the scanner Entra app and
secret. Step 5 runs the doctor immediately afterward. Microsoft's Entra
subsystems converge on new apps at different speeds: the v2.0 OAuth
endpoint accepts the secret within seconds, MSAL's discovery path lags
by 10-30 seconds.

During this lag, MSAL returns `AADSTS7000215` ("Invalid client secret")
or `AADSTS700016` ("Application not found") even though the credentials
are valid. `curl` against `/oauth2/v2.0/token` at the same moment
succeeds.

`AppOnlyTokenProvider._acquire_blocking` now retries on these two
specific error codes:

- Backoff sequence `5s, 10s, 15s, 20s, 10s`.
- 60-second total timeout via `time.monotonic()` deadline.
- Non-matching errors (real bad secret, real auth misconfiguration)
  raise `AuthError` immediately, no retry.

Because all three doctor checks (`check_graph`, `check_power_platform`,
`check_dataverse`) use `AppOnlyTokenProvider`, they all inherit this
retry without per-check changes.

### Component 4: Token cache invalidation — `src/m365_mcp_scanner/auth/file_cache.py`

The on-disk MSAL cache at
`~/AppData/Local/m365-mcp-scanner/msal_token_cache.bin` persists across
wizard runs. When the operator deletes the scanner app in Entra and the
wizard creates a new one, the cache may still hold a token signed for
the deleted app. `AppOnlyTokenProvider` would silently return that
stale token. Authenticated calls would then fail with `AADSTS7000215`.

`provision_scanner_app` (Step 3 of the wizard) now calls
`clear_app_only_token_cache()` after successfully creating the new
scanner app + secret. Both `msal_token_cache.bin` and
`msal_token_cache.salt` are unlinked. Safe to call when no cache exists.

### Component 5: Step 5 re-run button — `src/m365_mcp_scanner/ui/pages/00_First_Run_Setup.py`

If the doctor's 60-second retry window is exceeded for some unexpected
reason (e.g., genuinely bad secret, or Microsoft propagation taking
unusually long), Step 5 now displays a "🔄 Re-run doctor check" button.
Clicking re-fires the doctor check without losing wizard state, which is
otherwise impossible in Streamlit's single-page architecture (a browser
refresh would reset to Step 1).

## Consequences

**Trade-offs**

- **Step 5 wait time**: when MSAL has not yet propagated, Step 5 may
  sit on a single render for up to 60 seconds while the retry loop runs
  inside `_acquire_blocking`. No spinner is displayed during this wait —
  the page appears frozen. Operators may think the wizard hung; in
  practice the doctor succeeds well before the timeout. Accepted for
  shipping; a future polish pass could surface intermediate status via
  `st.status` streaming.

- **Step 6 wait time**: `provision_app_user` takes 15-40 seconds per
  env on the cold path (BAP call ~8s + Dataverse propagation ~10-30s).
  Previously the operation appeared to take ~5s but the resulting ✓ was
  premature. Truthful-slower is preferred to deceptive-faster.

- **`env_id` key derivation is fragile**: BAP env dicts have both
  `"id"` (full resource path) and `"name"` (GUID). All sites — selection
  set, render lookup, provisioner result key — must use
  `env.get("name", "")` consistently. Any drift causes silent UI
  breakage (rows render with blank status because dict lookups miss).
  Captured by tests that fail loudly if the convention changes, but not
  enforced by the type system.

- **No "user already exists" handling**: BAP `addAppUser` is idempotent
  on Microsoft's side (re-adding an existing app user returns 200
  without error), so we don't need explicit handling. But we also can't
  tell from a 200 response whether the user was newly created or
  already existed. The Dataverse `WhoAmI` poll runs either way; on the
  "already existed" path it succeeds on the first attempt with no wait.

- **Token cache invalidation is total, not per-app**: Step 3 clears the
  entire MSAL cache after a new scanner app is created. A user juggling
  multiple scanner installs in the same Windows account would have all
  their delegated and app-only token caches invalidated. In practice
  this isn't a real use case; flagged for completeness.

- **Dependence on Microsoft propagation behavior**: the 60-second
  timeouts in both retry loops assume Microsoft converges within that
  window. Empirically true today; not guaranteed. If Microsoft slows
  down, the timeouts must increase. The error messages on timeout
  include a "Retry" affordance that allows the operator to keep waiting
  past 60s.

## Historical record — Dataverse Web API POST /systemusers (abandoned)

The original implementation targeted Dataverse Web API
`POST /systemusers` directly. It failed with:

```
HTTP 400, code 0x80040530
"Unable to retrieve attribute=businessunitid for entityLogicalName=systemuser.
 Entity has Attribute Count=28."
```

The error was investigated against env `orgc5253638` (mcp-scanner-test
in Armor1). Findings:

- **Hypothesis 1 (nav property is `businessunit` without `id`)** —
  Wrong. `$expand=businessunitid` returns the BU object cleanly with
  the BU GUID; `$expand=businessunit` returns `0x80060888` "Could not
  find a property named 'businessunit'". The bind name `businessunitid`
  is correct for both read and write.

- **Hypothesis 2 (different nav names for read vs. write)** — Wrong.
  `EntityDefinitions('systemuser')/ManyToOneRelationships` shows the
  same nav property name for both directions on this OOTB relationship.

- **The error's "Attribute Count=28" was misleading.** The systemuser
  entity has ~165 attributes (confirmed via `Attributes` endpoint). The
  28 referred to attributes in the create transaction, not the entity
  itself. The error effectively said: "businessunitid is not in the
  attributes you sent to me." Which meant the `@odata.bind` line in the
  request body was being silently dropped before reaching the create
  pipeline.

- **Root cause of the silent drop — never isolated.** Two iterations on
  the POST body shape (different lookup syntax, different `@odata`
  annotation forms) produced the same error. Microsoft documentation
  for app user creation via `/systemusers` POST is sparse and
  inconsistent across versions.

After ~6 hours of diagnostic work, the decision was made to abandon
this path. The systemuser-POST approach is preserved at branch
`wip/dataverse-autoprovision-systemuser-path` for anyone who wants to
re-attempt it. Recommended: don't. The BAP path works, is documented,
and survives the kind of schema drift that bit us here.

## Future considerations

These are real cleanups but were deliberately deferred to keep this
ADR's scope tight:

1. **Wizard creates orphan Entra apps on re-run.** Step 3 creates a new
   "M365 MCP Scanner N" app every wizard walk because the name suffix
   increments. After 30 walks during development we had 30 orphan apps
   in the tenant. Filed as a separate issue: Step 3 should idempotently
   reuse an existing scanner app (by display name + tenant) and rotate
   the secret rather than minting a new app.

2. **Orphan systemusers in Dataverse.** When the wizard creates a new
   Entra app and provisions it, the systemuser for the previous (now-
   deleted) app remains in each env's Application Users list. These
   don't affect scanning but clutter the admin center. Cleanup would
   require enumerating systemusers per env and deleting those whose
   `applicationid` no longer exists in Entra. Cosmetic; defer.

3. **Step 5 wait UX.** The 60-second potential freeze on first-time
   doctor runs is jarring. A future change should pipe intermediate
   status through `st.status` / `st.empty` so the operator sees
   "retrying Graph audience (3/5)..." rather than an apparently-frozen
   page.

4. **No explicit "check Dataverse access" doctor step.** Step 5
   verifies Graph and Power Platform reachability, but not Dataverse —
   that gets verified implicitly by the Step 6 provisioning polling.
   Adding an explicit Dataverse audience check in the doctor would
   surface auth issues earlier.

5. **BAP propagation timeout assumes 60s is enough.** If Microsoft's
   propagation gets slower over time, the 60s wait isn't extensible
   without a code change. Move to settings-configurable, or add a
   second-tier "extended timeout" mode operators can opt into.

## See also

- [[0001-power-platform-management-app-in-wizard]] — explains the
  PowerShell management-app registration that Step 4 still performs;
  unrelated to addAppUser but adjacent in the wizard flow.
- [[0003-msal-and-direct-graph]] — establishes the in-process MSAL +
  `httpx` pattern that `AppOnlyTokenProvider` and the BAP call
  inherit from.
