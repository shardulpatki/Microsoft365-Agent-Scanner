"""Discover declarative agents via the Frontier-gated Copilot Packages API.

Hits ``/beta/copilot/admin/catalog/packages`` with a delegated token. Most
tenants today are not Frontier-eligible and will see 403; the discoverer
records ``tenant_not_eligible`` and yields nothing in that case so the rest of
the scan completes normally.

Required delegated permission: ``CopilotPackages.Read.All``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from m365_mcp_scanner.clients.declarative_resolver import (
    parse_declarative_manifest,
)
from m365_mcp_scanner.clients.exceptions import (
    ForbiddenError,
    GraphClientError,
    PermissionMissingError,
    ReauthRequiredError,
    TenantNotEligibleError,
)
from m365_mcp_scanner.discovery.base import DiscoveryContext, DiscoveryResult
from m365_mcp_scanner.models import ScanError

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DeclarativeAgentsPackagesDiscoverer:
    surface = "declarative_agents_packages"

    async def discover(self, ctx: DiscoveryContext) -> DiscoveryResult:
        result = DiscoveryResult(surface=self.surface)
        graph = ctx.delegated_graph
        if graph is None:
            result.errors.append(
                ScanError(
                    stage="discover",
                    surface=self.surface,
                    code="delegated_session_required",
                    message=(
                        "delegated session required for Copilot Packages API; "
                        "run `mcp-scan login` to enable this surface"
                    ),
                    timestamp=_utcnow(),
                )
            )
            return result

        try:
            packages: list[dict[str, Any]] = [pkg async for pkg in graph.list_copilot_packages()]
        except TenantNotEligibleError as exc:
            result.errors.append(
                ScanError(
                    stage="discover",
                    surface=self.surface,
                    code="tenant_not_eligible",
                    message=str(exc),
                    timestamp=_utcnow(),
                )
            )
            return result
        except ReauthRequiredError as exc:
            result.errors.append(
                ScanError(
                    stage="discover",
                    surface=self.surface,
                    code="reauth_required",
                    message=f"delegated session expired; run `mcp-scan login`: {exc}",
                    timestamp=_utcnow(),
                )
            )
            return result
        except PermissionMissingError as exc:
            result.errors.append(
                ScanError(
                    stage="discover",
                    surface=self.surface,
                    code="permission_missing",
                    message=(
                        "delegated permission CopilotPackages.Read.All missing or "
                        f"not consented: {exc}"
                    ),
                    timestamp=_utcnow(),
                )
            )
            return result
        except ForbiddenError as exc:
            result.errors.append(
                ScanError(
                    stage="discover",
                    surface=self.surface,
                    code="forbidden",
                    message=str(exc),
                    timestamp=_utcnow(),
                )
            )
            return result
        except GraphClientError as exc:
            result.errors.append(
                ScanError(
                    stage="discover",
                    surface=self.surface,
                    code="graph_error",
                    message=f"Copilot Packages list failed: {exc}",
                    timestamp=_utcnow(),
                )
            )
            return result
        except Exception as exc:  # noqa: BLE001 - surface isolation
            logger.exception("declarative_agents_packages: list crashed")
            result.errors.append(
                ScanError(
                    stage="discover",
                    surface=self.surface,
                    message=f"{type(exc).__name__}: {exc}",
                    timestamp=_utcnow(),
                )
            )
            return result

        for package in packages:
            package_id = package.get("id") or package.get("packageId")
            if not isinstance(package_id, str) or not package_id:
                logger.warning("copilot package row had no id; skipping")
                continue
            try:
                full = await graph.get_copilot_package(package_id)
            except GraphClientError as exc:
                result.errors.append(
                    ScanError(
                        stage="discover",
                        surface=self.surface,
                        code=getattr(exc, "code", "graph_error"),
                        message=f"package {package_id}: {exc}",
                        timestamp=_utcnow(),
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001
                logger.exception("copilot package fetch crashed id=%s", package_id)
                result.errors.append(
                    ScanError(
                        stage="discover",
                        surface=self.surface,
                        message=f"package {package_id}: {type(exc).__name__}: {exc}",
                        timestamp=_utcnow(),
                    )
                )
                continue

            manifest = full.get("manifest") if isinstance(full.get("manifest"), dict) else full
            parsed = parse_declarative_manifest(
                manifest if isinstance(manifest, dict) else {},
                source_id=package_id,
                source_kind="copilot_package",
                source_ref={
                    "package_display_name": package.get("displayName")
                    or package.get("name"),
                },
            )
            if parsed.agent is not None:
                result.agents.append(parsed.agent)
            result.mcp_servers.extend(parsed.mcp_servers)
            result.consumption_edges.extend(parsed.edges)
        return result
