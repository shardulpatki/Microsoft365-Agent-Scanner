"""Parse declarative-agent manifests into normalized scanner entities.

Declarative agents (M365 Agents Toolkit / Copilot agents) carry their MCP
wiring in the manifest's ``actions`` block. Two shapes exist in the wild:

* **Old / top-level**::

    { "actions": [ { "type": "mcpServer", "url": "...", ... } ] }

* **Newer / Copilot extensions**::

    { "copilotExtensions": { "declarativeAgents": [
        { "id": "...", "actions": [ { "type": "mcpServer", "url": "..." } ] }
    ] } }

Some manifests also use ``"copilotAgents"`` instead of ``"copilotExtensions"``;
we accept both. The parser is defensive: any unknown shape returns the agent
shell with an empty server list and logs a warning, never raises.
"""
from __future__ import annotations

import json
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from m365_mcp_scanner.models import NormalizedAgent, NormalizedMcpServer
from m365_mcp_scanner.models.consumption import ConsumptionEdge
from m365_mcp_scanner.models.enums import (
    AgentPath,
    AuthType,
    Transport,
    WiredVia,
)
from m365_mcp_scanner.models.ids import compute_agent_id, compute_server_id

logger = logging.getLogger(__name__)


@dataclass
class ParsedManifest:
    agent: NormalizedAgent | None
    mcp_servers: list[NormalizedMcpServer] = field(default_factory=list)
    edges: list[ConsumptionEdge] = field(default_factory=list)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_microsoft_host(host: str) -> bool:
    if not host:
        return False
    suffixes = (
        ".microsoft.com",
        ".dynamics.com",
        ".sharepoint.com",
        ".office.com",
        ".azure.com",
        ".azurewebsites.net",
    )
    return any(host.lower().endswith(s) for s in suffixes)


def _host_of(url: str) -> str:
    # Cheap parse — sufficient for first-party / external_domain flagging.
    if "://" not in url:
        return ""
    rest = url.split("://", 1)[1]
    return rest.split("/", 1)[0].lower()


def _map_auth_type(raw: Any) -> AuthType:
    if not isinstance(raw, dict):
        return AuthType.none
    raw_type = raw.get("type") or raw.get("scheme") or raw.get("kind")
    if not isinstance(raw_type, str):
        return AuthType.none
    lowered = raw_type.lower()
    if "oauth" in lowered:
        return AuthType.oauth2_static
    if "apikey" in lowered or "api_key" in lowered or "api-key" in lowered:
        return AuthType.api_key
    if "managed" in lowered:
        return AuthType.managed
    if lowered in {"none", "anonymous", "noauth"}:
        return AuthType.none
    return AuthType.none


def _redact_action(action: dict[str, Any]) -> dict[str, Any]:
    """Strip secret-shaped fields from a raw action before storing as evidence."""
    redacted = dict(action)
    auth = redacted.get("authentication")
    if isinstance(auth, dict):
        cleaned = {}
        for k, v in auth.items():
            if k.lower() in {"apikey", "api_key", "secret", "client_secret", "value", "key"}:
                cleaned[k] = "***redacted***"
            else:
                cleaned[k] = v
        redacted["authentication"] = cleaned
    return redacted


def _action_is_mcp(action: dict[str, Any]) -> bool:
    raw_type = action.get("type")
    if not isinstance(raw_type, str):
        return False
    lowered = raw_type.lower()
    if lowered in {"mcpserver", "mcp_server", "mcp"}:
        return True
    # Some manifests nest under spec/runtime — accept "type": "openapi" with an
    # MCP marker too, as a defensive future-proof path.
    return False


def _walk_actions(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every action dict found across known schema shapes."""
    out: list[dict[str, Any]] = []
    top_actions = manifest.get("actions")
    if isinstance(top_actions, list):
        out.extend(a for a in top_actions if isinstance(a, dict))
    for parent_key in ("copilotExtensions", "copilotAgents"):
        parent = manifest.get(parent_key)
        if not isinstance(parent, dict):
            continue
        for inner_key in ("declarativeAgents", "declarativeCopilots"):
            agents = parent.get(inner_key)
            if not isinstance(agents, list):
                continue
            for agent in agents:
                if not isinstance(agent, dict):
                    continue
                actions = agent.get("actions")
                if isinstance(actions, list):
                    out.extend(a for a in actions if isinstance(a, dict))
    return out


def _agent_display_name(manifest: dict[str, Any]) -> str:
    for key in ("name", "displayName"):
        v = manifest.get(key)
        if isinstance(v, str) and v:
            return v
    short = manifest.get("short_name") or manifest.get("shortName")
    if isinstance(short, dict):
        s = short.get("default")
        if isinstance(s, str) and s:
            return s
    if isinstance(short, str) and short:
        return short
    return "(unnamed declarative agent)"


def parse_declarative_manifest(
    manifest: dict[str, Any],
    *,
    source_id: str,
    source_kind: str,
    source_ref: dict[str, object] | None = None,
) -> ParsedManifest:
    """Extract the agent and any MCP-shaped actions from a declarative manifest.

    Args:
        manifest: parsed manifest JSON (a dict).
        source_id: stable id of the surfacing entity — package id (Copilot
            Packages) or Teams app id. Used for ``compute_agent_id``.
        source_kind: ``"copilot_package"`` or ``"teams_app"``; recorded in the
            agent's ``source_ref``.
        source_ref: extra evidence to attach to the agent (e.g. distribution
            method, manifest version, app definition id).
    """
    if not isinstance(manifest, dict):
        logger.warning("declarative manifest was not a dict; skipping")
        return ParsedManifest(agent=None)

    manifest_version = manifest.get("manifestVersion") or manifest.get("manifest_version")
    display_name = _agent_display_name(manifest)

    agent_source_ref: dict[str, object] = {
        "kind": source_kind,
        "source_id": source_id,
        "manifest_version": manifest_version,
    }
    if source_ref:
        agent_source_ref.update(source_ref)

    agent = NormalizedAgent(
        agent_id=compute_agent_id("declarative", source_id),
        path=AgentPath.declarative,
        display_name=display_name,
        published=True,
        source_ref=agent_source_ref,
    )

    servers: list[NormalizedMcpServer] = []
    edges: list[ConsumptionEdge] = []
    for action in _walk_actions(manifest):
        if not _action_is_mcp(action):
            continue
        url = action.get("url")
        if not isinstance(url, str) or not url:
            logger.warning(
                "declarative manifest action marked mcpServer but no url; skipping (source=%s)",
                source_id,
            )
            continue
        auth_type = _map_auth_type(action.get("authentication"))
        host = _host_of(url)
        evidence: dict[str, object] = {
            "source_kind": source_kind,
            "source_id": source_id,
            "manifest_version": manifest_version,
            "action_id": action.get("id"),
            "action_name": action.get("name"),
            "wired_via": "native_mcp_action",
            "raw_action": _redact_action(action),
        }
        if source_ref:
            evidence["source_ref"] = dict(source_ref)
        server = NormalizedMcpServer(
            server_id=compute_server_id(url, auth_type.value),
            url=url,
            transport=Transport.streamable_http,
            auth_type=auth_type,
            is_first_party=_is_microsoft_host(host),
            external_domain=not _is_microsoft_host(host),
            advertised_tools=None,
            discovered_via=f"declarative_agents_{source_kind}",
            discovered_at=_utcnow(),
            evidence=evidence,
        )
        servers.append(server)
        edges.append(
            ConsumptionEdge(
                agent_id=agent.agent_id,
                server_id=server.server_id,
                wired_via=WiredVia.native_mcp_tool,
                config_evidence={
                    "action_id": action.get("id"),
                    "source_kind": source_kind,
                    "source_id": source_id,
                },
            )
        )
    return ParsedManifest(agent=agent, mcp_servers=servers, edges=edges)


def manifest_from_bytes(blob: bytes) -> dict[str, Any] | None:
    """Best-effort parse of a manifest payload.

    The Teams App Catalog ``/manifest`` endpoint may return either raw JSON or
    a zipped package. Try JSON first; on failure, look for ``manifest.json``
    inside a zip. Returns ``None`` on unrecognised payloads.
    """
    if not blob:
        return None
    # Fast path: JSON
    try:
        text = blob.decode("utf-8")
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    # Zip path
    try:
        with zipfile.ZipFile(BytesIO(blob)) as zf:
            for name in zf.namelist():
                if name.lower().endswith("manifest.json"):
                    with zf.open(name) as f:
                        data = json.loads(f.read().decode("utf-8"))
                        if isinstance(data, dict):
                            return data
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("manifest payload was neither JSON nor a parseable zip")
    return None
