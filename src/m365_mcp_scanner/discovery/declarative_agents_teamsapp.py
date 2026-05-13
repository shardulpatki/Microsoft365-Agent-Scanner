"""Discover declarative agents deployed to the tenant Teams app catalog.

This is the practical demo path for declarative agents authored with the
M365 Agents Toolkit: the toolkit packages each agent as a Teams app and the
deploy step registers it under the tenant's organization-distributed apps.
The agent's MCP wiring lives in the manifest's ``actions`` block.

Required delegated permissions: ``TeamsApp.Read.All`` (or
``Directory.Read.All``) plus ``User.Read``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from m365_mcp_scanner.clients.declarative_resolver import (
    manifest_from_bytes,
    parse_declarative_manifest,
)
from m365_mcp_scanner.clients.exceptions import (
    ForbiddenError,
    GraphClientError,
    ManifestNotAvailableError,
    PermissionMissingError,
    ReauthRequiredError,
    TenantNotEligibleError,
)
from m365_mcp_scanner.discovery.base import DiscoveryContext, DiscoveryResult
from m365_mcp_scanner.models import AgentPath, NormalizedAgent, ScanError
from m365_mcp_scanner.models.ids import compute_agent_id

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _has_copilot_extension(manifest: dict[str, Any]) -> bool:
    """Heuristic: this Teams app embeds a declarative agent."""
    if not isinstance(manifest, dict):
        return False
    if isinstance(manifest.get("copilotExtensions"), dict):
        return True
    if isinstance(manifest.get("copilotAgents"), dict):
        return True
    actions = manifest.get("actions")
    if isinstance(actions, list):
        for a in actions:
            if isinstance(a, dict) and isinstance(a.get("type"), str):
                if a["type"].lower().startswith("mcp"):
                    return True
    return False


def _pick_latest_definition(app: dict[str, Any]) -> dict[str, Any] | None:
    defs = app.get("appDefinitions")
    if not isinstance(defs, list) or not defs:
        return None
    # Prefer publishingState == "published"; fall back to the last entry.
    for d in defs:
        if isinstance(d, dict) and d.get("publishingState") == "published":
            return d
    last = defs[-1]
    return last if isinstance(last, dict) else None


class DeclarativeAgentsTeamsAppDiscoverer:
    surface = "declarative_agents_teamsapp"

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
                        "delegated session required for Teams App Catalog; "
                        "run `mcp-scan login` to enable this surface"
                    ),
                    timestamp=_utcnow(),
                )
            )
            return result

        try:
            apps: list[dict[str, Any]] = [
                a async for a in graph.list_teams_app_catalog(distribution_method="organization")
            ]
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
        except PermissionMissingError as exc:
            result.errors.append(
                ScanError(
                    stage="discover",
                    surface=self.surface,
                    code="permission_missing",
                    message=(
                        "delegated permission TeamsApp.Read.All / "
                        f"Directory.Read.All missing: {exc}"
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
                    message=f"teams app catalog list failed: {exc}",
                    timestamp=_utcnow(),
                )
            )
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("declarative_agents_teamsapp: list crashed")
            result.errors.append(
                ScanError(
                    stage="discover",
                    surface=self.surface,
                    code="unknown",
                    message=f"{type(exc).__name__}: {exc}",
                    timestamp=_utcnow(),
                )
            )
            return result

        for app in apps:
            app_id = app.get("id")
            if not isinstance(app_id, str) or not app_id:
                logger.warning("teams app row had no id; skipping")
                continue
            definition = _pick_latest_definition(app)
            if definition is None:
                continue
            def_id = definition.get("id")
            if not isinstance(def_id, str) or not def_id:
                continue
            try:
                blob = await graph.get_teams_app_manifest(app_id, def_id)
            except ManifestNotAvailableError as exc:
                # Microsoft Graph returns 400 for declarative-agent-only Teams
                # apps. Emit an agent shell from catalog metadata; MCP wiring
                # is not discoverable through this path.
                display_name = (
                    definition.get("displayName")
                    or app.get("displayName")
                    or "(unnamed declarative agent)"
                )
                shell_source_ref: dict[str, object] = {
                    "kind": "teams_app",
                    "source_id": app_id,
                    "app_definition_id": def_id,
                    "publishing_state": definition.get("publishingState"),
                    "display_name": display_name,
                    "version": definition.get("version"),
                    "distribution_method": app.get("distributionMethod"),
                    "manifest_fetch_status": "unavailable",
                    "manifest_fetch_reason": (
                        "Microsoft Graph returns 400 for declarative-agent-only "
                        "Teams apps; manifest endpoint not usable for this app type"
                    ),
                }
                result.agents.append(
                    NormalizedAgent(
                        agent_id=compute_agent_id("declarative", app_id),
                        path=AgentPath.declarative,
                        display_name=display_name,
                        published=definition.get("publishingState") == "published",
                        source_ref=shell_source_ref,
                    )
                )
                result.errors.append(
                    ScanError(
                        stage="discover",
                        surface=self.surface,
                        code="manifest_endpoint_unavailable",
                        message=f"app {app_id}: {exc}",
                        timestamp=_utcnow(),
                    )
                )
                continue
            except GraphClientError as exc:
                result.errors.append(
                    ScanError(
                        stage="discover",
                        surface=self.surface,
                        code=getattr(exc, "code", "graph_error"),
                        message=f"app {app_id}: manifest fetch failed: {exc}",
                        timestamp=_utcnow(),
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001
                logger.exception("teams app manifest fetch crashed id=%s", app_id)
                result.errors.append(
                    ScanError(
                        stage="discover",
                        surface=self.surface,
                        code="unknown",
                        message=f"app {app_id}: {type(exc).__name__}: {exc}",
                        timestamp=_utcnow(),
                    )
                )
                continue

            manifest = manifest_from_bytes(blob)
            if manifest is None:
                logger.warning("teams app %s: manifest payload unparseable", app_id)
                continue
            if not _has_copilot_extension(manifest):
                # Plain Teams app, not a declarative agent — skip silently.
                continue

            parsed = parse_declarative_manifest(
                manifest,
                source_id=app_id,
                source_kind="teams_app",
                source_ref={
                    "app_definition_id": def_id,
                    "publishing_state": definition.get("publishingState"),
                    "display_name": definition.get("displayName")
                    or app.get("displayName"),
                    "version": definition.get("version"),
                    "distribution_method": app.get("distributionMethod"),
                },
            )
            if parsed.agent is not None:
                result.agents.append(parsed.agent)
            result.mcp_servers.extend(parsed.mcp_servers)
            result.consumption_edges.extend(parsed.edges)
        return result
