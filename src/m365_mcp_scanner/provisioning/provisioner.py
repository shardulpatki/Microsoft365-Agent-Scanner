"""In-process replacement for ``scripts/setup-scanner.sh``.

Provisions the scanner's Entra app registration end-to-end by talking to
Microsoft Graph (and, best-effort, the Power Platform admin API) directly
via async httpx. Replaces the bash + az CLI orchestration the wizard used
previously; eliminates az CLI startup overhead and the device-code prompt
delay that pushed setup wall-clock to 2–3 minutes on Windows.

Substeps mirror the ``[N/7]`` markers in the retired bash script so the UI
progress experience is unchanged. Progress is surfaced via a callback fired
*before* each substep's HTTP call.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_RESOURCE_ID = "00000003-0000-0000-c000-000000000000"
PP_ADMIN_ROLE_TEMPLATE_ID = "11648597-926c-4cf3-9c36-bcebb0ba8dcc"

# Microsoft Graph permission IDs (sourced from scripts/setup-scanner.sh).
APP_PERMISSION_IDS = (
    "9a5d68dd-52b0-4cc2-bd40-abcf44ac3a30",  # Application.Read.All
    "81b4724a-58aa-41c1-8a55-84ef97466587",  # DelegatedPermissionGrant.Read.All
    "df021288-bdef-4463-88db-98f22de89214",  # User.Read.All
    "b0afded3-3588-46d8-8b3d-9842eff778da",  # AuditLog.Read.All
    "5e1e9171-754d-478c-812c-f1755a9a4c2d",  # TeamsApp.Read.All
)
DELEGATED_PERMISSION_IDS = (
    "bf5bb47b-1e74-4f1f-be32-f33d3066c5dd",  # CopilotPackages.Read.All
    "06da0dbc-49e2-44d2-8312-53f166ab848a",  # Directory.Read.All
    "e1fe6dd8-ba31-4d61-89e7-88639da4683d",  # User.Read
)

ProgressCallback = Callable[[int, str], None]


class ProvisionError(RuntimeError):
    """Fatal provisioning failure. Carries the failing substep number."""

    def __init__(self, step: int, message: str) -> None:
        super().__init__(message)
        self.step = step
        self.message = message


@dataclass
class ProvisionResult:
    client_id: str
    app_object_id: str
    service_principal_id: str
    client_secret: str
    admin_consent_granted: bool
    pp_admin_role_assigned: bool
    pp_admin_role_error: str | None
    completed_at: str


def _emit(progress_callback: ProgressCallback | None, n: int, msg: str) -> None:
    if progress_callback is not None:
        try:
            progress_callback(n, msg)
        except Exception:  # noqa: BLE001 — UI callback must not abort flow
            logger.exception("progress_callback raised; continuing")


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    step: int,
    json_body: Any = None,
    expected: tuple[int, ...] = (200, 201, 204),
    allow_409: bool = False,
) -> httpx.Response:
    """One request with one 5xx retry, 429 honoring Retry-After."""
    attempts = 0
    while True:
        attempts += 1
        try:
            resp = await client.request(method, url, json=json_body)
        except httpx.HTTPError as exc:
            if attempts < 2:
                await asyncio.sleep(1.0)
                continue
            raise ProvisionError(
                step, f"network error calling {method} {url}: {exc}"
            ) from exc

        if resp.status_code in expected:
            return resp
        if allow_409 and resp.status_code == 409:
            return resp
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "2"))
            await asyncio.sleep(min(retry_after, 30.0))
            continue
        if 500 <= resp.status_code < 600 and attempts < 2:
            await asyncio.sleep(2.0 * attempts)
            continue
        body_excerpt = resp.text[:500] if resp.text else ""
        raise ProvisionError(
            step,
            f"{method} {url} returned {resp.status_code}: {body_excerpt}",
        )


def _decoded_aud(token: str) -> str | None:
    try:
        import base64
        import json as _json

        _h, payload, _s = token.split(".")
        padded = payload + "=" * (-len(payload) % 4)
        claims = _json.loads(base64.urlsafe_b64decode(padded.encode()))
        return str(claims.get("aud") or "") or None
    except Exception:  # noqa: BLE001
        return None


async def provision_scanner_app(
    bootstrap_token: str,
    bootstrap_account: dict[str, Any],
    tenant_id: str,
    app_display_name: str,
    *,
    progress_callback: ProgressCallback | None = None,
) -> ProvisionResult:
    """Create the scanner's Entra app + SP + permissions + secret + PP role.

    Raises :class:`ProvisionError` on any fatal Graph failure (substeps 1–7).
    Substep 8 (Power Platform Administrator role) is best-effort: a failure
    populates ``pp_admin_role_error`` and leaves the rest of the result
    intact so the wizard can guide the operator through the manual fallback.
    """
    aud = _decoded_aud(bootstrap_token)
    if aud and "graph.microsoft.com" not in aud:
        raise ProvisionError(
            1,
            f"bootstrap token audience is '{aud}', not graph.microsoft.com",
        )

    headers = {
        "Authorization": f"Bearer {bootstrap_token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(
        headers=headers, timeout=httpx.Timeout(30.0)
    ) as client:
        # --- Step 1: precondition + duplicate-name check ---
        _emit(progress_callback, 1, "Checking for an existing app with this name…")
        filter_q = (
            f"$filter=displayName eq '{app_display_name.replace(chr(39), chr(39)*2)}'"
        )
        existing = await _request(
            client,
            "GET",
            f"{GRAPH_BASE}/applications?{filter_q}",
            step=1,
            expected=(200,),
        )
        if (existing.json().get("value") or []):
            raise ProvisionError(
                1,
                f"an Entra app named '{app_display_name}' already exists; "
                "pick a different name or delete the existing app first",
            )

        # --- Step 2: create application ---
        _emit(progress_callback, 2, f"Creating Entra app registration '{app_display_name}'…")
        created = await _request(
            client,
            "POST",
            f"{GRAPH_BASE}/applications",
            step=2,
            json_body={
                "displayName": app_display_name,
                "signInAudience": "AzureADMyOrg",
            },
            expected=(201,),
        )
        app_obj = created.json()
        app_object_id = str(app_obj["id"])
        client_id = str(app_obj["appId"])

        # --- Step 3: enable public-client flows ---
        _emit(progress_callback, 3, "Enabling public client flows…")
        await _request(
            client,
            "PATCH",
            f"{GRAPH_BASE}/applications/{app_object_id}",
            step=3,
            json_body={"isFallbackPublicClient": True},
            expected=(204,),
        )

        # --- Step 4: create service principal ---
        _emit(progress_callback, 4, "Creating service principal…")
        sp_resp = await _request(
            client,
            "POST",
            f"{GRAPH_BASE}/servicePrincipals",
            step=4,
            json_body={"appId": client_id},
            expected=(201,),
        )
        sp = sp_resp.json()
        sp_id = str(sp["id"])

        # --- Step 5: apply all permissions in one PATCH ---
        _emit(
            progress_callback,
            5,
            "Applying 5 application + 3 delegated Graph permissions…",
        )
        resource_access = [
            {"id": pid, "type": "Role"} for pid in APP_PERMISSION_IDS
        ] + [
            {"id": pid, "type": "Scope"} for pid in DELEGATED_PERMISSION_IDS
        ]
        await _request(
            client,
            "PATCH",
            f"{GRAPH_BASE}/applications/{app_object_id}",
            step=5,
            json_body={
                "requiredResourceAccess": [
                    {
                        "resourceAppId": GRAPH_RESOURCE_ID,
                        "resourceAccess": resource_access,
                    }
                ]
            },
            expected=(204,),
        )

        # --- Step 6: admin consent (app roles + oauth2 grants) ---
        _emit(progress_callback, 6, "Granting admin consent…")
        admin_consent_granted = await _grant_admin_consent(
            client, sp_id, app_object_id
        )

        # --- Step 7: client secret ---
        _emit(progress_callback, 7, "Creating 1-year client secret…")
        secret_resp = await _request(
            client,
            "POST",
            f"{GRAPH_BASE}/applications/{app_object_id}/addPassword",
            step=7,
            json_body={
                "passwordCredential": {
                    "displayName": (
                        f"scanner-setup-"
                        f"{datetime.now(timezone.utc).strftime('%Y%m%d')}"
                    )
                }
            },
            expected=(200, 201),
        )
        secret_payload = secret_resp.json()
        client_secret = str(secret_payload["secretText"])

        # --- Step 8: Power Platform Administrator role (best effort) ---
        _emit(
            progress_callback,
            8,
            "Assigning Power Platform Administrator role…",
        )
        pp_assigned, pp_err = await _assign_pp_admin_role(client, sp_id)

    return ProvisionResult(
        client_id=client_id,
        app_object_id=app_object_id,
        service_principal_id=sp_id,
        client_secret=client_secret,
        admin_consent_granted=admin_consent_granted,
        pp_admin_role_assigned=pp_assigned,
        pp_admin_role_error=pp_err,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )


async def _grant_admin_consent(
    client: httpx.AsyncClient, sp_id: str, _app_object_id: str
) -> bool:
    """Grant admin consent for both app roles and delegated scopes.

    Returns True iff every permission was granted (or already present).
    Failures here are non-fatal at the provisioner level — the wizard
    surfaces the consequence to the operator and points to the Entra portal
    as a manual fallback. We collect any error but only return False; the
    Graph token's identity is already known to be GA.
    """
    # Resolve Graph SP id once (target for app-role assignments).
    resp = await _request(
        client,
        "GET",
        f"{GRAPH_BASE}/servicePrincipals?$filter=appId eq '{GRAPH_RESOURCE_ID}'",
        step=6,
        expected=(200,),
    )
    graph_sps = resp.json().get("value") or []
    if not graph_sps:
        return False
    graph_sp_id = str(graph_sps[0]["id"])

    all_ok = True

    # App-role assignments (5 application permissions).
    for perm_id in APP_PERMISSION_IDS:
        r = await _request(
            client,
            "POST",
            f"{GRAPH_BASE}/servicePrincipals/{sp_id}/appRoleAssignments",
            step=6,
            json_body={
                "principalId": sp_id,
                "resourceId": graph_sp_id,
                "appRoleId": perm_id,
            },
            expected=(200, 201),
            allow_409=True,
        )
        if r.status_code not in (200, 201, 409):
            all_ok = False

    # Delegated grants — one oauth2PermissionGrants record covers all scopes.
    scope_value = " ".join(_scope_names_for_ids(DELEGATED_PERMISSION_IDS))
    r = await _request(
        client,
        "POST",
        f"{GRAPH_BASE}/oauth2PermissionGrants",
        step=6,
        json_body={
            "clientId": sp_id,
            "consentType": "AllPrincipals",
            "resourceId": graph_sp_id,
            "scope": scope_value,
        },
        expected=(200, 201),
        allow_409=True,
    )
    if r.status_code not in (200, 201, 409):
        all_ok = False

    return all_ok


def _scope_names_for_ids(ids: tuple[str, ...]) -> tuple[str, ...]:
    """Map known delegated permission IDs to their scope names.

    The oauth2PermissionGrants endpoint takes a space-delimited string of
    *scope names*, not IDs. The IDs above are stable Microsoft Graph values
    so this mapping is hard-coded.
    """
    mapping = {
        "bf5bb47b-1e74-4f1f-be32-f33d3066c5dd": "CopilotPackages.Read.All",
        "06da0dbc-49e2-44d2-8312-53f166ab848a": "Directory.Read.All",
        "e1fe6dd8-ba31-4d61-89e7-88639da4683d": "User.Read",
    }
    return tuple(mapping[i] for i in ids if i in mapping)


async def _assign_pp_admin_role(
    client: httpx.AsyncClient,
    sp_id: str,
) -> tuple[bool, str | None]:
    """Best-effort: assign Power Platform Administrator directory role to SP.

    Runs against Microsoft Graph (the directoryRoles endpoint), reusing the
    bootstrap Graph token — no separate PP-audience token is required. A GA
    token can assign directory roles. If the operator's token is insufficient
    (e.g. signed in as a non-GA), surface the error rather than failing the
    whole flow so the wizard can render a manual fallback.
    """
    try:
        # Look up activated directoryRole; activate if not yet present.
        list_resp = await _request(
            client,
            "GET",
            f"{GRAPH_BASE}/directoryRoles?$filter=roleTemplateId eq "
            f"'{PP_ADMIN_ROLE_TEMPLATE_ID}'",
            step=8,
            expected=(200,),
        )
        roles = list_resp.json().get("value") or []
        if roles:
            role_object_id = str(roles[0]["id"])
        else:
            activate = await _request(
                client,
                "POST",
                f"{GRAPH_BASE}/directoryRoles",
                step=8,
                json_body={"roleTemplateId": PP_ADMIN_ROLE_TEMPLATE_ID},
                expected=(200, 201),
            )
            role_object_id = str(activate.json()["id"])

        assign = await _request(
            client,
            "POST",
            f"{GRAPH_BASE}/directoryRoles/{role_object_id}/members/$ref",
            step=8,
            json_body={
                "@odata.id": (
                    f"https://graph.microsoft.com/v1.0/directoryObjects/{sp_id}"
                )
            },
            expected=(204,),
            allow_409=True,
        )
        if assign.status_code not in (204, 409):
            return False, f"unexpected status {assign.status_code} from PP role assignment"
        return True, None
    except ProvisionError as exc:
        return False, exc.message
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
