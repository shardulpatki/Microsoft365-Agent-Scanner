"""Discover Copilot Studio agents (Dataverse ``bot`` table) wired to MCP tools.

For each Power Platform environment with a linked Dataverse:

1. List ``bots`` (Copilot Studio agents).
2. For each bot, list ``botcomponents`` and inspect each component's ``data``
   blob (YAML, sometimes JSON).
3. Match the MCP fingerprint:
   ``kind: TaskDialog`` AND
   ``action.operationDetails.kind: ModelContextProtocolMetadata``.
4. Resolve the component's ``connectionReference`` logical name to a Power
   Apps ``connectorid`` via ``connectionreferences``.
5. Emit one ``NormalizedAgent`` + one ``NormalizedMcpServer`` + one
   ``ConsumptionEdge`` per MCP-wired component.

Per-env isolation: 401/403 from Dataverse → recorded ``no_dataverse_access``
error for that env; sibling envs continue. Missing
``properties.linkedEnvironmentMetadata.instanceApiUrl`` → ``org_url_not_resolved``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import yaml

from m365_mcp_scanner.clients.dataverse import DataverseClient
from m365_mcp_scanner.clients.exceptions import DataverseAccessDeniedError
from m365_mcp_scanner.discovery.base import DiscoveryContext, DiscoveryResult
from m365_mcp_scanner.models import (
    NormalizedAgent,
    NormalizedMcpServer,
    ScanError,
)
from m365_mcp_scanner.models.consumption import ConsumptionEdge
from m365_mcp_scanner.models.enums import (
    AgentPath,
    AuthType,
    Transport,
    WiredVia,
)
from m365_mcp_scanner.models.ids import compute_agent_id, compute_server_id

logger = logging.getLogger(__name__)

_TASKDIALOG = "TaskDialog"
_MCP_OP_KIND = "ModelContextProtocolMetadata"

_MAX_DATA_BYTES = 1_048_576  # 1 MiB cap on botcomponent.data blobs

_ENV_CONCURRENCY = 8


@dataclass
class _EnvResult:
    mcp_servers: list[NormalizedMcpServer] = field(default_factory=list)
    agents: list[NormalizedAgent] = field(default_factory=list)
    consumption_edges: list[ConsumptionEdge] = field(default_factory=list)
    errors: list[ScanError] = field(default_factory=list)


class CopilotStudioDiscoverer:
    surface = "copilot_studio"

    async def discover(self, ctx: DiscoveryContext) -> DiscoveryResult:
        result = DiscoveryResult(surface=self.surface)
        pp = ctx.power_platform
        if pp is None:
            result.errors.append(
                ScanError(
                    stage="discover",
                    surface=self.surface,
                    code="not_configured",
                    message="power_platform client not provided in DiscoveryContext",
                    timestamp=_utcnow(),
                )
            )
            return result
        if ctx.token_provider is None:
            result.errors.append(
                ScanError(
                    stage="discover",
                    surface=self.surface,
                    code="not_configured",
                    message="token_provider not provided in DiscoveryContext",
                    timestamp=_utcnow(),
                )
            )
            return result

        try:
            envs = [env async for env in pp.list_environments()]
        except Exception as exc:  # noqa: BLE001 - surface isolation
            logger.exception("copilot_studio: list_environments failed")
            result.errors.append(
                ScanError(
                    stage="discover",
                    surface=self.surface,
                    code="list_environments_failed",
                    message=f"{type(exc).__name__}: {exc}",
                    timestamp=_utcnow(),
                )
            )
            return result

        dv = DataverseClient(
            token_provider=ctx.token_provider, recorder=ctx.recorder
        )
        sem = asyncio.Semaphore(_ENV_CONCURRENCY)

        async def _scan(env: dict[str, Any]) -> _EnvResult:
            async with sem:
                return await self._scan_env(env, dv)

        try:
            per_env = await asyncio.gather(
                *(_scan(env) for env in envs), return_exceptions=True
            )
            for env, outcome in zip(envs, per_env):
                if isinstance(outcome, BaseException):
                    env_id = _env_id(env)
                    logger.exception(
                        "copilot_studio: env scan crashed env_id=%s", env_id
                    )
                    result.errors.append(
                        ScanError(
                            stage="discover",
                            surface=self.surface,
                            code="env_scan_crashed",
                            message=f"env {env_id}: {type(outcome).__name__}: {outcome}",
                            timestamp=_utcnow(),
                        )
                    )
                    continue
                result.mcp_servers.extend(outcome.mcp_servers)
                result.agents.extend(outcome.agents)
                result.consumption_edges.extend(outcome.consumption_edges)
                result.errors.extend(outcome.errors)
        finally:
            await dv.aclose()
        return result

    async def _scan_env(
        self, env: dict[str, Any], dv: DataverseClient
    ) -> _EnvResult:
        out = _EnvResult()
        env_id = _env_id(env)
        env_display = _env_display_name(env)
        org_url = _instance_api_url(env)
        if not org_url:
            out.errors.append(
                ScanError(
                    stage="discover",
                    surface=CopilotStudioDiscoverer.surface,
                    code="org_url_not_resolved",
                    message=(
                        f"env {env_id}: properties.linkedEnvironmentMetadata.instanceApiUrl missing "
                        "(env probably has no Dataverse)"
                    ),
                    timestamp=_utcnow(),
                )
            )
            return out

        try:
            bots = [b async for b in dv.list_bots(org_url, env_id)]
        except DataverseAccessDeniedError as exc:
            out.errors.append(
                ScanError(
                    stage="discover",
                    surface=CopilotStudioDiscoverer.surface,
                    code=exc.code,
                    message=str(exc),
                    timestamp=_utcnow(),
                )
            )
            return out
        except Exception as exc:  # noqa: BLE001 - per-env isolation
            logger.exception(
                "copilot_studio: list_bots failed env_id=%s", env_id
            )
            out.errors.append(
                ScanError(
                    stage="discover",
                    surface=CopilotStudioDiscoverer.surface,
                    code="bot_query_failed",
                    message=f"env {env_id}: {type(exc).__name__}: {exc}",
                    timestamp=_utcnow(),
                )
            )
            return out

        for bot in bots:
            botid = bot.get("botid")
            if not isinstance(botid, str) or not botid:
                continue
            try:
                components = [
                    c async for c in dv.list_botcomponents_for_bot(org_url, env_id, botid)
                ]
            except DataverseAccessDeniedError as exc:
                out.errors.append(
                    ScanError(
                        stage="discover",
                        surface=CopilotStudioDiscoverer.surface,
                        code=exc.code,
                        message=str(exc),
                        timestamp=_utcnow(),
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001 - per-bot isolation
                logger.exception(
                    "copilot_studio: list_botcomponents failed env_id=%s bot_id=%s",
                    env_id,
                    botid,
                )
                out.errors.append(
                    ScanError(
                        stage="discover",
                        surface=CopilotStudioDiscoverer.surface,
                        code="botcomponent_query_failed",
                        message=f"bot {botid}: {type(exc).__name__}: {exc}",
                        timestamp=_utcnow(),
                    )
                )
                continue

            agent_emitted = False
            for comp in components:
                parsed, parse_err = _parse_component_data(comp)
                if parse_err is not None:
                    out.errors.append(parse_err)
                    continue
                if not is_mcp_taskdialog(parsed):
                    continue
                assert parsed is not None  # narrow for mypy
                conn_logical = _connection_reference_name(parsed)
                if not conn_logical:
                    logger.info(
                        "copilot_studio: MCP component missing connectionReference "
                        "env=%s bot=%s component=%s",
                        env_id,
                        botid,
                        comp.get("botcomponentid"),
                    )
                    continue
                try:
                    conn_ref = await dv.get_connection_reference(
                        org_url, env_id, conn_logical
                    )
                except DataverseAccessDeniedError as exc:
                    out.errors.append(
                        ScanError(
                            stage="discover",
                            surface=CopilotStudioDiscoverer.surface,
                            code=exc.code,
                            message=str(exc),
                            timestamp=_utcnow(),
                        )
                    )
                    continue
                connector_id = (
                    conn_ref.get("connectorid") if isinstance(conn_ref, dict) else None
                )
                server = _build_server(
                    env_id=env_id,
                    env_display=env_display,
                    bot=bot,
                    comp=comp,
                    parsed=parsed,
                    conn_logical=conn_logical,
                    connector_id=connector_id if isinstance(connector_id, str) else None,
                )
                agent = _build_agent(
                    env_id=env_id, env_display=env_display, bot=bot
                )
                edge = ConsumptionEdge(
                    agent_id=agent.agent_id,
                    server_id=server.server_id,
                    wired_via=WiredVia.native_mcp_tool,
                    config_evidence={
                        "botcomponent_id": comp.get("botcomponentid"),
                        "connection_reference_logical_name": conn_logical,
                        "operation_id": _operation_id(parsed),
                    },
                )
                out.mcp_servers.append(server)
                if not agent_emitted:
                    out.agents.append(agent)
                    agent_emitted = True
                out.consumption_edges.append(edge)
        return out


# ---- fingerprint + parsers -------------------------------------------------


def is_mcp_taskdialog(parsed: Any) -> bool:
    """True iff the parsed botcomponent.data carries the MCP TaskDialog shape."""
    if not isinstance(parsed, dict):
        return False
    if str(parsed.get("kind", "")).strip() != _TASKDIALOG:
        return False
    action = parsed.get("action")
    if not isinstance(action, dict):
        return False
    op = action.get("operationDetails")
    if not isinstance(op, dict):
        return False
    kind = str(op.get("kind", "")).strip()
    if kind != _MCP_OP_KIND:
        if kind:
            logger.info(
                "copilot_studio: TaskDialog with unrecognized operationDetails.kind=%r",
                kind,
            )
        return False
    return True


def _parse_component_data(
    comp: dict[str, Any],
) -> tuple[Any, ScanError | None]:
    """Parse ``comp['data']`` as YAML (falling back to JSON). Returns (parsed, err)."""
    data = comp.get("data")
    if data is None or data == "":
        return None, None
    if not isinstance(data, str):
        return None, None
    if len(data) > _MAX_DATA_BYTES:
        return None, ScanError(
            stage="discover",
            surface=CopilotStudioDiscoverer.surface,
            code="botcomponent_too_large",
            message=(
                f"botcomponent {comp.get('botcomponentid')} data blob is "
                f"{len(data)} bytes (cap {_MAX_DATA_BYTES}); skipped"
            ),
            timestamp=_utcnow(),
        )
    try:
        return yaml.safe_load(data), None
    except yaml.YAMLError:
        try:
            return json.loads(data), None
        except (ValueError, TypeError) as exc:
            return None, ScanError(
                stage="discover",
                surface=CopilotStudioDiscoverer.surface,
                code="botcomponent_data_parse_failed",
                message=(
                    f"botcomponent {comp.get('botcomponentid')}: failed to parse data "
                    f"as YAML or JSON ({type(exc).__name__})"
                ),
                timestamp=_utcnow(),
            )


def _connection_reference_name(parsed: dict[str, Any]) -> str | None:
    action = parsed.get("action")
    if not isinstance(action, dict):
        return None
    name = action.get("connectionReference")
    return name if isinstance(name, str) and name else None


def _operation_id(parsed: dict[str, Any]) -> str | None:
    action = parsed.get("action")
    if not isinstance(action, dict):
        return None
    op = action.get("operationDetails")
    if not isinstance(op, dict):
        return None
    op_id = op.get("operationId")
    return op_id if isinstance(op_id, str) else None


# ---- builders --------------------------------------------------------------


def _build_server(
    *,
    env_id: str,
    env_display: str | None,
    bot: dict[str, Any],
    comp: dict[str, Any],
    parsed: dict[str, Any],
    conn_logical: str,
    connector_id: str | None,
) -> NormalizedMcpServer:
    # The actual MCP server URL lives inside the connector swagger which is
    # fetched by the custom_connectors discoverer. From copilot_studio we
    # surface a stable synthetic identifier that downstream enrichment can
    # join on ``evidence.connector_id``.
    if connector_id:
        url = f"powerplatform-connector://{env_id}{connector_id}"
    else:
        url = f"powerplatform-connector://{env_id}/connection-reference/{conn_logical}"
    auth = AuthType.managed  # connection refs are managed by Power Platform
    evidence: dict[str, object] = {
        "environment_id": env_id,
        "environment_display_name": env_display,
        "bot_id": bot.get("botid"),
        "bot_display_name": bot.get("name"),
        "botcomponent_id": comp.get("botcomponentid"),
        "botcomponent_display_name": parsed.get("modelDisplayName"),
        "botcomponent_description": parsed.get("modelDescription"),
        "connection_reference_logical_name": conn_logical,
        "connector_id": connector_id,
        "operation_id": _operation_id(parsed),
    }
    return NormalizedMcpServer(
        server_id=compute_server_id(url, auth.value),
        url=url,
        transport=Transport.custom_connector,
        auth_type=auth,
        is_first_party=False,
        external_domain=True,
        advertised_tools=None,
        discovered_via="copilot_studio",
        discovered_at=_utcnow(),
        evidence=evidence,
    )


def _build_agent(
    *,
    env_id: str,
    env_display: str | None,
    bot: dict[str, Any],
) -> NormalizedAgent:
    botid = str(bot.get("botid") or "")
    display = str(bot.get("name") or botid or "unknown")
    owner = bot.get("_ownerid_value")
    return NormalizedAgent(
        agent_id=compute_agent_id(
            path=AgentPath.copilot_studio.value,
            source_id=botid,
            environment_id=env_id,
        ),
        path=AgentPath.copilot_studio,
        display_name=display,
        owner_id=owner if isinstance(owner, str) else None,
        environment_id=env_id,
        published=False,
        source_ref={
            "bot_id": botid,
            "environment_id": env_id,
            "environment_display_name": env_display,
            "authentication_mode": bot.get("authenticationmode"),
            "ismanaged": bot.get("ismanaged"),
            "createdon": bot.get("createdon"),
            "modifiedon": bot.get("modifiedon"),
        },
    )


# ---- env helpers -----------------------------------------------------------


def _env_id(env: dict[str, Any]) -> str:
    name = env.get("name")
    if isinstance(name, str) and name:
        return name
    env_id = env.get("id")
    if isinstance(env_id, str) and env_id:
        return env_id
    return ""


def _env_display_name(env: dict[str, Any]) -> str | None:
    props = env.get("properties")
    if isinstance(props, dict):
        d = props.get("displayName")
        if isinstance(d, str):
            return d
    return None


def _instance_api_url(env: dict[str, Any]) -> str | None:
    props = env.get("properties")
    if not isinstance(props, dict):
        return None
    linked = props.get("linkedEnvironmentMetadata")
    if not isinstance(linked, dict):
        return None
    for key in ("instanceApiUrl", "instanceUrl"):
        v = linked.get(key)
        if isinstance(v, str) and v:
            return v.rstrip("/")
    return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "CopilotStudioDiscoverer",
    "is_mcp_taskdialog",
]
