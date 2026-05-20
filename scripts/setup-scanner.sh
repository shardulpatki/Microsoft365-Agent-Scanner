#!/usr/bin/env bash
#
# DEPRECATED 2026-05-18: The first-run wizard no longer calls this script.
# See src/m365_mcp_scanner/provisioning/provisioner.py for the in-process
# Python replacement. This file is retained for reference and rollback.
#
# setup-scanner.sh — Automated tenant provisioning for the M365 MCP Scanner.
#
# Codifies docs/tenant-setup.md Steps 1–5 (programmatic portion) plus the
# Power Platform Administrator role assignment, and writes a machine-readable
# JSON output the Streamlit first-run wizard ingests.
#
# Usage:
#   ./scripts/setup-scanner.sh <tenant-id> <app-display-name>
#
# Output (on success):
#   ~/.m365-mcp-scanner/.setup-output.json   (mode 600)
#
# Dependencies:
#   az CLI >= 2.50.0   (signed in as Global Administrator, active subscription
#                       in the target tenant)
#   jq                 (for safe JSON emission)
#
# This script does NOT perform:
#   - Step 6 of docs/tenant-setup.md (Dataverse application user per
#     environment) — that is per-environment manual, surfaced in the wizard.
#   - Post-setup verification — the wizard runs `mcp-scan doctor` afterwards.
#   - Partial-resume cleanup — if it fails mid-flow, operator cleans up by
#     hand (e.g. `az ad app delete --id <object-id>`).
#
# Manual VERIFY checklist (run on Armor19 against a real tenant):
#   1. Cold run on a tenant with no existing scanner app:
#      Expect app + SP created, .setup-output.json written with mode 600,
#      admin_consent_granted=true.
#   2. Re-run with the same display name: expect early abort with a clear
#      message (idempotency guard).
#   3. Re-run with a different display name: expect success; second app
#      appears in the tenant.
#   4. Cleanup: `az ad app delete --id <app object id>` for every test app.
#   5. After cold run, run `mcp-scan doctor` directly against the new
#      credentials. Expect Graph ✅; Power Platform admin calls will
#      return 403 until the wizard's "Register as Power Platform
#      Management App" step is completed by the operator (the script
#      no longer performs this; see docs/ui-trd.md §5).
#   6. Step 4 should complete in under 5 seconds (one Graph PATCH
#      call). Earlier versions used 8 sequential `az ad app permission
#      add` calls and could take 30+ seconds on Armor19; if Step 4 is
#      noticeably slow now, the PATCH may have regressed to per-call
#      behavior.
#
# Exit codes:
#   0  success (Step 5 admin-consent may have fallen back to manual)
#   1  unexpected runtime failure (trapped)
#   2  bad invocation (missing/empty args)
#   3  precondition failure (az missing/old, wrong tenant, not GA,
#      duplicate app, jq missing)
#   4  step failure (any of Steps 1-4, 6, 7)
#
# See docs/tenant-setup.md for the manual equivalent and full reference.

set -euo pipefail
IFS=$'\n\t'

# ---------------------------------------------------------------------------
# Logging helpers — never echo $CLIENT_SECRET.
# ---------------------------------------------------------------------------
_ts() { date -u +'%Y-%m-%dT%H:%M:%SZ'; }
log_step() { printf '[%s] [%s] %s\n' "$(_ts)" "$1" "$2"; }
log_info() { printf '[%s]       %s\n' "$(_ts)" "$1"; }
log_warn() { printf '[%s] WARN  %s\n' "$(_ts)" "$1" >&2; }
log_err()  { printf '[%s] ERROR %s\n' "$(_ts)" "$1" >&2; }

on_err() {
    local exit_code=$?
    local line=${BASH_LINENO[0]:-?}
    local cmd=${BASH_COMMAND:-?}
    log_err "Unexpected failure at line ${line} (exit ${exit_code}): ${cmd}"
    exit "${exit_code}"
}
trap on_err ERR

START_TS=$(date +%s)

# ---------------------------------------------------------------------------
# Arg parsing.
# ---------------------------------------------------------------------------
usage() {
    cat >&2 <<EOF
Usage: $0 <tenant-id> <app-display-name>

Both arguments are required.

  <tenant-id>          Entra tenant GUID. Must match the tenant of the active
                       az subscription (run \`az login --tenant <id>\` first).
  <app-display-name>   Display name for the Entra app registration to create
                       (e.g. "M365 MCP Scanner"). Must not already exist in
                       the tenant.

See docs/tenant-setup.md for the manual equivalent and the full reference.
EOF
}

if [[ $# -ne 2 ]]; then
    log_err "Expected 2 arguments, got $#."
    usage
    exit 2
fi

TENANT_ID="${1:-}"
APP_NAME="${2:-}"

if [[ -z "${TENANT_ID}" || -z "${APP_NAME}" ]]; then
    log_err "Both <tenant-id> and <app-display-name> must be non-empty."
    usage
    exit 2
fi

# ---------------------------------------------------------------------------
# Constants — Microsoft Graph permission IDs (docs/tenant-setup.md §8).
# ---------------------------------------------------------------------------
GRAPH_APP_ID='00000003-0000-0000-c000-000000000000'
PP_ADMIN_ROLE_TEMPLATE_ID='11648597-926c-4cf3-9c36-bcebb0ba8dcc'

# 5 application permissions (=Role)
APP_PERMS=(
    '9a5d68dd-52b0-4cc2-bd40-abcf44ac3a30'  # Application.Read.All
    '81b4724a-58aa-41c1-8a55-84ef97466587'  # DelegatedPermissionGrant.Read.All
    'df021288-bdef-4463-88db-98f22de89214'  # User.Read.All
    'b0afded3-3588-46d8-8b3d-9842eff778da'  # AuditLog.Read.All
    '5e1e9171-754d-478c-812c-f1755a9a4c2d'  # TeamsApp.Read.All
)
# 3 delegated permissions (=Scope)
DEL_PERMS=(
    'bf5bb47b-1e74-4f1f-be32-f33d3066c5dd'  # CopilotPackages.Read.All
    '06da0dbc-49e2-44d2-8312-53f166ab848a'  # Directory.Read.All
    'e1fe6dd8-ba31-4d61-89e7-88639da4683d'  # User.Read
)

OUTPUT_DIR="${HOME}/.m365-mcp-scanner"
OUTPUT_FILE="${OUTPUT_DIR}/.setup-output.json"
PORTAL_URL_TEMPLATE='https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationMenuBlade/~/CallAnAPI/appId/'

# ---------------------------------------------------------------------------
# Preconditions.
# ---------------------------------------------------------------------------
log_step '0/7' "Checking preconditions..."

if ! command -v az >/dev/null 2>&1; then
    log_err "Azure CLI ('az') not found on PATH."
    log_err "Install from https://learn.microsoft.com/cli/azure/install-azure-cli and re-run."
    exit 3
fi

if ! command -v jq >/dev/null 2>&1; then
    log_err "'jq' not found on PATH. Required for safe JSON emission."
    log_err "Install jq (e.g. \`brew install jq\`, \`apt-get install jq\`, or https://jqlang.github.io/jq/download/)."
    exit 3
fi

AZ_VERSION=$(az version --query '"azure-cli"' -o tsv 2>/dev/null || true)
if [[ -z "${AZ_VERSION}" ]]; then
    log_err "Could not determine az CLI version (\`az version\` failed)."
    exit 3
fi
MIN_AZ='2.50.0'
LOWEST=$(printf '%s\n%s\n' "${AZ_VERSION}" "${MIN_AZ}" | sort -V | head -n1)
if [[ "${LOWEST}" != "${MIN_AZ}" ]]; then
    log_err "az CLI ${AZ_VERSION} is older than required ${MIN_AZ}. Upgrade with \`az upgrade\`."
    exit 3
fi
log_info "az CLI ${AZ_VERSION} (>= ${MIN_AZ}) OK; jq present."

ACCOUNT_JSON=$(az account show -o json 2>/dev/null || true)
if [[ -z "${ACCOUNT_JSON}" ]]; then
    log_err "\`az account show\` failed. Run \`az login --tenant ${TENANT_ID}\` first."
    exit 3
fi
ACTIVE_TENANT=$(printf '%s' "${ACCOUNT_JSON}" | jq -r '.tenantId')
ACTIVE_USER=$(printf '%s' "${ACCOUNT_JSON}" | jq -r '.user.name // "unknown"')
if [[ "${ACTIVE_TENANT}" != "${TENANT_ID}" ]]; then
    log_err "Active subscription is in tenant ${ACTIVE_TENANT}, but argument was ${TENANT_ID}."
    log_err "Run \`az login --tenant ${TENANT_ID}\` and re-run."
    exit 3
fi
log_info "Active tenant ${ACTIVE_TENANT} matches argument; signed in as ${ACTIVE_USER}."

# Global Administrator check via Graph.
GA_JSON=$(az rest --method GET \
    --url "https://graph.microsoft.com/v1.0/me/memberOf/microsoft.graph.directoryRole?\$select=id,displayName,roleTemplateId" \
    --headers ConsistencyLevel=eventual \
    -o json 2>/dev/null || true)
GA_HIT=$(printf '%s' "${GA_JSON}" | jq -r '
    .value // []
    | map(select(.displayName == "Global Administrator"
                 or .roleTemplateId == "62e90394-69f5-4237-9190-012177145e10"))
    | length' 2>/dev/null || echo 0)
if ! [[ "${GA_HIT}" =~ ^[0-9]+$ ]] || (( GA_HIT < 1 )); then
    log_err "Signed-in user (${ACTIVE_USER}) is not a Global Administrator in this tenant."
    log_err "Sign in as a Global Administrator and re-run."
    exit 3
fi
log_info "Signed-in user is Global Administrator."

# Idempotency: refuse to create a duplicate.
EXISTING=$(az ad app list --display-name "${APP_NAME}" --query '[].appId' -o tsv 2>/dev/null || true)
if [[ -n "${EXISTING}" ]]; then
    log_err "An Entra app with display name '${APP_NAME}' already exists (appId(s): ${EXISTING})."
    log_err "Pick a different name, or delete the existing app first:"
    log_err "  az ad app delete --id <appId>"
    exit 3
fi
log_info "No existing app with display name '${APP_NAME}'. Proceeding."

# ---------------------------------------------------------------------------
# Step 1/7 — Create the Entra app registration.
# ---------------------------------------------------------------------------
log_step '1/7' "Creating Entra app registration '${APP_NAME}'..."
APP_JSON=$(az ad app create \
    --display-name "${APP_NAME}" \
    --sign-in-audience AzureADMyOrg \
    -o json) || { log_err "az ad app create failed."; exit 4; }
APP_ID=$(printf '%s' "${APP_JSON}" | jq -r '.appId')
APP_OBJECT_ID=$(printf '%s' "${APP_JSON}" | jq -r '.id')
if [[ -z "${APP_ID}" || -z "${APP_OBJECT_ID}" ]]; then
    log_err "Created app but could not parse appId / object id from response."
    exit 4
fi
log_info "App created. appId=${APP_ID} objectId=${APP_OBJECT_ID}"

# ---------------------------------------------------------------------------
# Step 2/7 — Enable public client flows (isFallbackPublicClient=true).
# Required to avoid AADSTS7000218 when the scanner uses device-code/public
# client auth. See docs/tenant-setup.md Step 2 and troubleshooting §6.
# ---------------------------------------------------------------------------
log_step '2/7' "Enabling public client flows (isFallbackPublicClient=true)..."
az ad app update --id "${APP_OBJECT_ID}" --set isFallbackPublicClient=true \
    || { log_err "az ad app update isFallbackPublicClient=true failed."; exit 4; }
log_info "Public client flows enabled."

# ---------------------------------------------------------------------------
# Step 3/7 — Create the service principal.
# ---------------------------------------------------------------------------
log_step '3/7' "Creating service principal for appId ${APP_ID}..."
SP_JSON=$(az ad sp create --id "${APP_ID}" -o json) \
    || { log_err "az ad sp create failed."; exit 4; }
SP_OBJECT_ID=$(printf '%s' "${SP_JSON}" | jq -r '.id')
if [[ -z "${SP_OBJECT_ID}" ]]; then
    log_err "Created SP but could not parse object id."
    exit 4
fi
log_info "Service principal created. spObjectId=${SP_OBJECT_ID}"

# ---------------------------------------------------------------------------
# Step 4/7 — Add the 8 Microsoft Graph permissions.
# Permission IDs sourced from docs/tenant-setup.md §8 (stable MS Graph IDs).
# ---------------------------------------------------------------------------
log_step '4/7' "Adding 5 application + 3 delegated Graph permissions..."
log_info "  applying all 8 permissions in one Graph call..."
APP_PERMS_JSON=$(printf '%s\n' "${APP_PERMS[@]}" | jq -R . | jq -s 'map({id: ., type: "Role"})')
DEL_PERMS_JSON=$(printf '%s\n' "${DEL_PERMS[@]}" | jq -R . | jq -s 'map({id: ., type: "Scope"})')
PATCH_BODY=$(jq -n \
    --arg graph "${GRAPH_APP_ID}" \
    --argjson app_perms "${APP_PERMS_JSON}" \
    --argjson del_perms "${DEL_PERMS_JSON}" \
    '{
        requiredResourceAccess: [{
            resourceAppId: $graph,
            resourceAccess: ($app_perms + $del_perms)
        }]
     }')
set +e
PATCH_OUT=$(az rest --method PATCH \
    --uri "https://graph.microsoft.com/v1.0/applications/${APP_OBJECT_ID}" \
    --headers "Content-Type=application/json" \
    --body "${PATCH_BODY}" 2>&1)
PATCH_RC=$?
set -e
if [[ ${PATCH_RC} -ne 0 ]]; then
    log_err "Failed to apply Graph permissions via PATCH."
    printf '%s\n' "${PATCH_OUT}" | sed 's/^/        /' >&2
    exit 4
fi
log_info "All 8 permissions queued (consent pending in next step)."

# ---------------------------------------------------------------------------
# Step 5/7 — Grant admin consent.
# Non-fatal: if this fails (eventual-consistency race, missing role-elevation,
# corporate policy), the operator can grant consent manually in the Entra
# portal. Script still produces a usable app + secret.
# ---------------------------------------------------------------------------
log_step '5/7' "Granting admin consent..."
ADMIN_CONSENT_GRANTED=false
set +e
CONSENT_OUT=$(az ad app permission admin-consent --id "${APP_ID}" 2>&1)
CONSENT_RC=$?
set -e
if [[ ${CONSENT_RC} -eq 0 ]]; then
    ADMIN_CONSENT_GRANTED=true
    log_info "Admin consent granted."
else
    log_warn "Admin consent failed (exit ${CONSENT_RC}). Output:"
    printf '%s\n' "${CONSENT_OUT}" | sed 's/^/        /' >&2
    log_warn "Continuing — operator must grant consent manually in the Entra portal:"
    log_warn "  ${PORTAL_URL_TEMPLATE}${APP_ID}"
    log_warn "The wizard will surface this as a blocking warning."
fi

# ---------------------------------------------------------------------------
# Step 6/7 — Create a 1-year client secret. The value is only visible once.
# After this point: do NOT enable `set -x`; do NOT echo $CLIENT_SECRET.
# ---------------------------------------------------------------------------
log_step '6/7' "Creating 1-year client secret..."
SECRET_DISPLAY_NAME="scanner-setup-$(date -u +%Y%m%d)"
SECRET_JSON=$(az ad app credential reset \
    --id "${APP_ID}" \
    --display-name "${SECRET_DISPLAY_NAME}" \
    --years 1 \
    --append \
    -o json) || { log_err "az ad app credential reset failed."; exit 4; }
CLIENT_SECRET=$(printf '%s' "${SECRET_JSON}" | jq -r '.password')
if [[ -z "${CLIENT_SECRET}" || "${CLIENT_SECRET}" == "null" ]]; then
    log_err "Created credential but could not parse secret value."
    exit 4
fi
SECRET_KEY_ID=$(printf '%s' "${SECRET_JSON}" | jq -r '.keyId // "unknown"')
log_info "Secret created (keyId=${SECRET_KEY_ID}, lifetime=1y). Value not logged."

# ---------------------------------------------------------------------------
# Step 7/7 — Assign Power Platform Administrator directory role to the SP.
# No `az role` command covers directory roles for SPs, so we use Graph.
# Activate the role if its directoryRole instance doesn't exist yet.
# ---------------------------------------------------------------------------
log_step '7/7' "Assigning Power Platform Administrator role to the service principal..."
ROLE_LIST=$(az rest --method GET \
    --url "https://graph.microsoft.com/v1.0/directoryRoles?\$filter=roleTemplateId%20eq%20'${PP_ADMIN_ROLE_TEMPLATE_ID}'" \
    -o json) || { log_err "Failed to list directoryRoles."; exit 4; }
ROLE_OBJECT_ID=$(printf '%s' "${ROLE_LIST}" | jq -r '.value[0].id // empty')
if [[ -z "${ROLE_OBJECT_ID}" ]]; then
    log_info "Power Platform Administrator role not yet activated in this tenant; activating..."
    ACTIVATE_JSON=$(az rest --method POST \
        --url 'https://graph.microsoft.com/v1.0/directoryRoles' \
        --headers 'Content-Type=application/json' \
        --body "{\"roleTemplateId\":\"${PP_ADMIN_ROLE_TEMPLATE_ID}\"}" \
        -o json) || { log_err "Failed to activate Power Platform Administrator role."; exit 4; }
    ROLE_OBJECT_ID=$(printf '%s' "${ACTIVATE_JSON}" | jq -r '.id // empty')
fi
if [[ -z "${ROLE_OBJECT_ID}" ]]; then
    log_err "Could not resolve directoryRole object id for Power Platform Administrator."
    exit 4
fi
log_info "directoryRole object id: ${ROLE_OBJECT_ID}"

set +e
ASSIGN_OUT=$(az rest --method POST \
    --url "https://graph.microsoft.com/v1.0/directoryRoles/${ROLE_OBJECT_ID}/members/\$ref" \
    --headers 'Content-Type=application/json' \
    --body "{\"@odata.id\":\"https://graph.microsoft.com/v1.0/directoryObjects/${SP_OBJECT_ID}\"}" \
    2>&1)
ASSIGN_RC=$?
set -e
if [[ ${ASSIGN_RC} -ne 0 ]]; then
    # Idempotency: treat "already exists / conflicting object" as success.
    if printf '%s' "${ASSIGN_OUT}" | grep -qiE 'already exist|conflicting object|One or more added object references'; then
        log_info "Service principal is already a member of Power Platform Administrator."
    else
        log_err "Failed to assign Power Platform Administrator role."
        printf '%s\n' "${ASSIGN_OUT}" | sed 's/^/        /' >&2
        exit 4
    fi
else
    log_info "Service principal assigned to Power Platform Administrator."
fi

# ---------------------------------------------------------------------------
# Write .setup-output.json atomically with mode 600.
# ---------------------------------------------------------------------------
log_step 'out' "Writing ${OUTPUT_FILE}..."
mkdir -p "${OUTPUT_DIR}"
chmod 700 "${OUTPUT_DIR}" 2>/dev/null || true
OLD_UMASK=$(umask); umask 077
TMP_FILE=$(mktemp "${OUTPUT_DIR}/.setup-output.json.XXXXXX")
# shellcheck disable=SC2064 # expand path now so trap sees correct value
trap "rm -f '${TMP_FILE}'" EXIT
jq -n \
    --arg client_id        "${APP_ID}" \
    --arg client_secret    "${CLIENT_SECRET}" \
    --arg tenant_id        "${TENANT_ID}" \
    --arg app_object_id    "${APP_OBJECT_ID}" \
    --argjson admin_consent_granted "${ADMIN_CONSENT_GRANTED}" \
    --arg completed_at     "$(_ts)" \
    '{
        client_id: $client_id,
        client_secret: $client_secret,
        tenant_id: $tenant_id,
        app_object_id: $app_object_id,
        admin_consent_granted: $admin_consent_granted,
        completed_at: $completed_at
     }' >"${TMP_FILE}"
chmod 600 "${TMP_FILE}"
mv "${TMP_FILE}" "${OUTPUT_FILE}"
trap on_err ERR
trap - EXIT
umask "${OLD_UMASK}"
log_info "Output written (mode 600)."

# ---------------------------------------------------------------------------
# Summary — never print the secret.
# ---------------------------------------------------------------------------
END_TS=$(date +%s)
DURATION=$((END_TS - START_TS))
echo
log_info "================ setup-scanner.sh complete ================"
log_info "  app display name        : ${APP_NAME}"
log_info "  client_id (appId)       : ${APP_ID}"
log_info "  app object id           : ${APP_OBJECT_ID}"
log_info "  sp object id            : ${SP_OBJECT_ID}"
log_info "  tenant id               : ${TENANT_ID}"
log_info "  admin consent granted   : ${ADMIN_CONSENT_GRANTED}"
log_info "  output                  : ${OUTPUT_FILE}"
log_info "  duration                : ${DURATION}s"
log_info "==========================================================="

if [[ "${ADMIN_CONSENT_GRANTED}" != "true" ]]; then
    log_warn "Admin consent was NOT granted automatically. Grant it manually at:"
    log_warn "  ${PORTAL_URL_TEMPLATE}${APP_ID}"
fi

exit 0
