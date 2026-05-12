"""Typed exceptions raised by Graph and Power Platform clients.

These are categorized by failure mode so discoverers can attach a stable
``code`` to ``ScanError`` rows without parsing message strings.
"""
from __future__ import annotations


class GraphClientError(RuntimeError):
    """Base for client-side Graph errors that should be caught by discoverers."""

    code: str = "graph_error"


class ReauthRequiredError(GraphClientError):
    """401 from Graph — the delegated session expired or was revoked."""

    code = "reauth_required"


class TenantNotEligibleError(GraphClientError):
    """403 with a Frontier/eligibility marker — the tenant cannot use this surface.

    Currently observed against ``/beta/copilot/admin/catalog/packages``: even
    with ``CopilotPackages.Read.All`` consented, tenants outside the Frontier
    program get 403 with an error message indicating the API is not available.
    """

    code = "tenant_not_eligible"


class PermissionMissingError(GraphClientError):
    """403 from Graph — the caller lacks the required scope/role."""

    code = "permission_missing"


class ForbiddenError(GraphClientError):
    """403 from Graph that matches neither a licensing nor a permission marker."""

    code = "forbidden"


class DataverseAccessDeniedError(RuntimeError):
    """401/403 from a Dataverse Web API call against a specific environment.

    The scanner SP must be registered as an application user with a sufficient
    security role in each env's Dataverse. Environments without that grant
    raise this so the discoverer can record a per-env error and continue.
    """

    code = "no_dataverse_access"

    def __init__(self, env_id: str, org_url: str, status_code: int) -> None:
        self.env_id = env_id
        self.org_url = org_url
        self.status_code = status_code
        super().__init__(
            f"Dataverse {status_code} for env {env_id} at {org_url} — "
            "scanner SP is likely not added as application user with a sufficient role"
        )

