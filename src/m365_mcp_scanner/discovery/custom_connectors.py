"""Discover Power Apps custom connectors carrying the MCP protocol extension.

Custom connectors are an older Power Platform route for surfacing tools to
Copilot. When the connector's OpenAPI/Swagger spec carries
``x-ms-agentic-protocol: mcp-streamable-1.0``, the connector is acting as an
MCP server registered against a Power Platform environment.

The extension may appear at the operation level (current shape) or at the
spec top level (older shape). We accept both, plus an ``InvokeMCP*``
``operationId`` heuristic as a defensive fallback.

Required: scanner Entra app registered as a Power Platform management app
(``New-PowerAppManagementApp -ApplicationId <client-id>``). No additional
Microsoft Graph permission needed.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from m365_mcp_scanner.clients.power_platform_admin import PowerPlatformAdminClient
from m365_mcp_scanner.discovery.base import DiscoveryContext, DiscoveryResult
from m365_mcp_scanner.models import NormalizedMcpServer, ScanError
from m365_mcp_scanner.models.enums import AuthType, Transport
from m365_mcp_scanner.models.ids import compute_server_id

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VALUE = "mcp-streamable-1.0"
MCP_PROTOCOL_KEY = "x-ms-agentic-protocol"

# Heuristic fallback for older or partially-shaped connector specs.
_MCP_OPERATION_ID_PREFIX = "invokemcp"

# Hosts known to be Microsoft-owned. Used to flag external_domain=False on
# matching MCP-shaped connectors. Not exhaustive — defensive default is
# external_domain=True for unknown hosts (which is the safer signal anyway).
_MICROSOFT_HOST_SUFFIXES: tuple[str, ...] = (
    ".microsoft.com",
    ".dynamics.com",
    ".sharepoint.com",
    ".office.com",
    ".office365.com",
    ".azure.com",
    ".azurewebsites.net",
)

_HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
)


class CustomConnectorsDiscoverer:
    surface = "custom_connectors"
    _ENV_CONCURRENCY = 8

    async def discover(self, ctx: DiscoveryContext) -> DiscoveryResult:
        result = DiscoveryResult(surface=self.surface)
        pp = ctx.power_platform
        if pp is None:
            result.errors.append(
                ScanError(
                    stage="discover",
                    surface=self.surface,
                    message="power_platform client not provided in DiscoveryContext",
                    timestamp=_utcnow(),
                )
            )
            return result

        try:
            envs = [env async for env in pp.list_environments()]
        except Exception as exc:  # noqa: BLE001 - surface isolation
            logger.exception("custom_connectors: list_environments failed")
            result.errors.append(
                ScanError(
                    stage="discover",
                    surface=self.surface,
                    message=f"list_environments failed: {type(exc).__name__}: {exc}",
                    timestamp=_utcnow(),
                )
            )
            return result

        sem = asyncio.Semaphore(self._ENV_CONCURRENCY)

        async def _scan(env: dict[str, Any]) -> tuple[
            dict[str, Any], list[NormalizedMcpServer], list[ScanError]
        ]:
            async with sem:
                servers, errors = await self._scan_environment(pp, env)
                return env, servers, errors

        gathered = await asyncio.gather(
            *(_scan(env) for env in envs), return_exceptions=True
        )
        for env, outcome in zip(envs, gathered):
            if isinstance(outcome, BaseException):
                env_id = _env_id(env)
                logger.exception(
                    "custom_connectors: env scan crashed env_id=%s", env_id
                )
                result.errors.append(
                    ScanError(
                        stage="discover",
                        surface=self.surface,
                        message=(
                            f"env {env_id}: {type(outcome).__name__}: {outcome}"
                        ),
                        timestamp=_utcnow(),
                    )
                )
                continue
            _scanned_env, servers, errors = outcome
            result.mcp_servers.extend(servers)
            result.errors.extend(errors)
        return result

    async def _scan_environment(
        self,
        pp: PowerPlatformAdminClient,
        env: dict[str, Any],
    ) -> tuple[list[NormalizedMcpServer], list[ScanError]]:
        servers: list[NormalizedMcpServer] = []
        errors: list[ScanError] = []
        env_id = _env_id(env)
        if not env_id:
            return servers, errors

        async for connector in pp.list_connectors(env_id):
            connector_id = connector.get("name") or connector.get("id")
            try:
                spec = await self._resolve_spec(pp, connector)
                if spec is None:
                    continue
                matches = _extract_mcp_paths(spec)
                if not matches:
                    continue
                for path, method, op in matches:
                    server = self._build_normalized(env, connector, spec, path, method, op)
                    if server is not None:
                        servers.append(server)
                        logger.debug(
                            "custom_connectors: matched connector_id=%s path=%s method=%s",
                            connector_id,
                            path,
                            method,
                        )
            except Exception as exc:  # noqa: BLE001 - per-connector isolation
                logger.exception(
                    "custom_connectors: connector mapping failed connector_id=%s",
                    connector_id,
                )
                errors.append(
                    ScanError(
                        stage="discover",
                        surface=self.surface,
                        message=(
                            f"connector {connector_id}: {type(exc).__name__}: {exc}"
                        ),
                        timestamp=_utcnow(),
                    )
                )
        return servers, errors

    @staticmethod
    async def _resolve_spec(
        pp: PowerPlatformAdminClient,
        connector: dict[str, Any],
    ) -> dict[str, Any] | None:
        properties = connector.get("properties") or {}
        if not isinstance(properties, dict):
            return None
        inline = properties.get("swagger")
        if isinstance(inline, dict) and inline:
            return inline
        api_defs = properties.get("apiDefinitions")
        if isinstance(api_defs, dict):
            for key in ("originalSwaggerUrl", "modifiedSwaggerUrl"):
                signed = api_defs.get(key)
                if isinstance(signed, str) and signed:
                    spec = await pp.fetch_swagger_url(signed)
                    if spec is not None:
                        return spec
        return None

    @staticmethod
    def _build_normalized(
        env: dict[str, Any],
        connector: dict[str, Any],
        spec: dict[str, Any],
        path: str,
        method: str,
        op: dict[str, Any],
    ) -> NormalizedMcpServer | None:
        url = _build_url(spec, path)
        if url is None:
            logger.warning(
                "custom_connectors: could not build URL connector=%s path=%s",
                connector.get("name"),
                path,
            )
            return None

        auth_type, raw_security = _derive_auth_type(spec)
        properties = connector.get("properties") or {}
        publisher = properties.get("publisher") if isinstance(properties, dict) else None
        is_first_party = publisher == "Microsoft"
        host = _spec_host(spec).lower()
        external_domain = not _is_microsoft_host(host)

        evidence: dict[str, object] = {
            "connector_id": connector.get("name") or connector.get("id"),
            "connector_display_name": (
                properties.get("displayName") if isinstance(properties, dict) else None
            ),
            "connector_publisher": publisher,
            "environment_id": _env_id(env),
            "environment_display_name": _env_display_name(env),
            "path": path,
            "method": method.upper(),
            "operation_id": op.get("operationId"),
            "host": host,
            "created_time": (
                properties.get("createdTime") if isinstance(properties, dict) else None
            ),
        }
        if raw_security is not None:
            evidence["raw_security"] = raw_security

        return NormalizedMcpServer(
            server_id=compute_server_id(url, auth_type.value),
            url=url,
            transport=Transport.custom_connector,
            auth_type=auth_type,
            is_first_party=is_first_party,
            external_domain=external_domain,
            advertised_tools=None,
            discovered_via="custom_connectors",
            discovered_at=_utcnow(),
            evidence=evidence,
        )


def _extract_mcp_paths(
    spec: dict[str, Any],
) -> list[tuple[str, str, dict[str, Any]]]:
    """Return a list of (path, method, operation) tuples that look MCP-shaped."""
    if not isinstance(spec, dict):
        return []
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return []
    top_level_match = spec.get(MCP_PROTOCOL_KEY) == MCP_PROTOCOL_VALUE
    matches: list[tuple[str, str, dict[str, Any]]] = []
    for path, methods in paths.items():
        if not isinstance(path, str) or not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if not isinstance(method, str) or method.lower() not in _HTTP_METHODS:
                continue
            if not isinstance(op, dict):
                continue
            op_match = op.get(MCP_PROTOCOL_KEY) == MCP_PROTOCOL_VALUE
            op_id = op.get("operationId")
            heuristic_match = (
                isinstance(op_id, str)
                and op_id.lower().startswith(_MCP_OPERATION_ID_PREFIX)
            )
            if op_match or top_level_match or heuristic_match:
                matches.append((path, method, op))
    return matches


def _build_url(spec: dict[str, Any], path: str) -> str | None:
    """Build the connector endpoint URL from a Swagger 2.0 or OpenAPI 3.0 spec."""
    # OpenAPI 3.0: prefer servers[0].url + path
    servers = spec.get("servers")
    if isinstance(servers, list) and servers:
        first = servers[0]
        if isinstance(first, dict):
            base = first.get("url")
            if isinstance(base, str) and base:
                return _join_url(base.rstrip("/"), path)

    # Swagger 2.0: schemes + host + basePath + path
    host = _spec_host(spec)
    if not host:
        return None
    schemes = spec.get("schemes")
    scheme = "https"
    if isinstance(schemes, list) and schemes:
        first_scheme = schemes[0]
        if isinstance(first_scheme, str) and first_scheme:
            scheme = first_scheme
    base_path = spec.get("basePath", "")
    if not isinstance(base_path, str):
        base_path = ""
    base_path = base_path.rstrip("/")
    return f"{scheme}://{host}{base_path}{_ensure_leading_slash(path)}"


def _spec_host(spec: dict[str, Any]) -> str:
    host = spec.get("host")
    if isinstance(host, str):
        return host
    return ""


def _ensure_leading_slash(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


def _join_url(base: str, path: str) -> str:
    return f"{base}{_ensure_leading_slash(path)}"


def _derive_auth_type(
    spec: dict[str, Any],
) -> tuple[AuthType, dict[str, Any] | None]:
    """Map Swagger/OpenAPI security definitions to an AuthType.

    Falls back to AuthType.none and returns the raw security blob in the
    ``raw_security`` slot of evidence so unparseable shapes are still surfaced.
    """
    sec_defs = spec.get("securityDefinitions")
    if not isinstance(sec_defs, dict) or not sec_defs:
        components = spec.get("components")
        if isinstance(components, dict):
            sec_defs = components.get("securitySchemes")
    if not isinstance(sec_defs, dict) or not sec_defs:
        return AuthType.none, None
    schemes_seen: list[str] = []
    for value in sec_defs.values():
        if not isinstance(value, dict):
            continue
        scheme_type = value.get("type")
        if isinstance(scheme_type, str):
            schemes_seen.append(scheme_type.lower())
    if "oauth2" in schemes_seen:
        return AuthType.oauth2_static, None
    if "apikey" in schemes_seen or "api_key" in schemes_seen:
        return AuthType.api_key, None
    return AuthType.none, sec_defs


def _is_microsoft_host(host: str) -> bool:
    if not host:
        return False
    for suffix in _MICROSOFT_HOST_SUFFIXES:
        if host.endswith(suffix):
            return True
    return False


def _env_id(env: dict[str, Any]) -> str:
    name = env.get("name")
    if isinstance(name, str) and name:
        return name
    env_id = env.get("id")
    if isinstance(env_id, str) and env_id:
        return env_id
    return ""


def _env_display_name(env: dict[str, Any]) -> str | None:
    properties = env.get("properties")
    if isinstance(properties, dict):
        display_name = properties.get("displayName")
        if isinstance(display_name, str):
            return display_name
    return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
