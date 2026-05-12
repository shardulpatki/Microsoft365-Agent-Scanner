from __future__ import annotations

import json
from pathlib import Path

from m365_mcp_scanner.clients.declarative_resolver import (
    manifest_from_bytes,
    parse_declarative_manifest,
)
from m365_mcp_scanner.models.enums import AuthType, Transport, WiredVia

FIXTURE = (
    Path(__file__).parent.parent
    / "integration"
    / "fixtures"
    / "declarative_agent_manifest.json"
)


def test_parse_new_schema_extracts_two_mcp_servers() -> None:
    manifest = json.loads(FIXTURE.read_text())
    parsed = parse_declarative_manifest(
        manifest, source_id="ta-001", source_kind="teams_app"
    )
    assert parsed.agent is not None
    assert parsed.agent.display_name == "Hello MCP Agent"
    assert len(parsed.mcp_servers) == 2
    urls = sorted(s.url for s in parsed.mcp_servers)
    assert urls == [
        "https://mcp.example.com/search",
        "https://mcp.example.com/weather",
    ]
    assert all(s.transport is Transport.streamable_http for s in parsed.mcp_servers)
    types = sorted(s.auth_type for s in parsed.mcp_servers)
    assert AuthType.oauth2_static in types and AuthType.api_key in types
    # One edge per server, all pointing to the same agent.
    assert len(parsed.edges) == 2
    assert {e.agent_id for e in parsed.edges} == {parsed.agent.agent_id}
    assert all(e.wired_via is WiredVia.native_mcp_tool for e in parsed.edges)
    # API key value must be redacted in evidence.
    api_server = next(s for s in parsed.mcp_servers if s.auth_type is AuthType.api_key)
    raw = api_server.evidence["raw_action"]
    assert isinstance(raw, dict)
    auth = raw.get("authentication")
    assert isinstance(auth, dict)
    assert auth["value"] == "***redacted***"


def test_parse_old_schema_with_top_level_actions() -> None:
    manifest = {
        "manifestVersion": "1.0",
        "name": "Old Agent",
        "actions": [
            {
                "id": "a1",
                "type": "mcpServer",
                "url": "https://old.example.com/mcp",
                "authentication": {"type": "none"},
            }
        ],
    }
    parsed = parse_declarative_manifest(
        manifest, source_id="pkg-old", source_kind="copilot_package"
    )
    assert parsed.agent is not None
    assert parsed.agent.display_name == "Old Agent"
    assert len(parsed.mcp_servers) == 1
    assert parsed.mcp_servers[0].auth_type is AuthType.none


def test_parse_no_mcp_actions_yields_agent_only() -> None:
    manifest = {
        "manifestVersion": "1.0",
        "name": "Plain Agent",
        "actions": [
            {"id": "a1", "type": "openapi", "url": "https://example.com/api"}
        ],
    }
    parsed = parse_declarative_manifest(
        manifest, source_id="pkg-plain", source_kind="copilot_package"
    )
    assert parsed.agent is not None
    assert parsed.mcp_servers == []
    assert parsed.edges == []


def test_parse_malformed_manifest_returns_empty_agent_and_does_not_raise() -> None:
    # Not a dict — defensive return.
    parsed = parse_declarative_manifest(
        "not a dict",  # type: ignore[arg-type]
        source_id="x",
        source_kind="teams_app",
    )
    assert parsed.agent is None
    assert parsed.mcp_servers == []


def test_parse_action_missing_url_skipped() -> None:
    manifest = {
        "name": "Bad Action Agent",
        "actions": [
            {"id": "a1", "type": "mcpServer"},  # no url
            {"id": "a2", "type": "mcpServer", "url": "https://ok.example.com/mcp"},
        ],
    }
    parsed = parse_declarative_manifest(
        manifest, source_id="x", source_kind="teams_app"
    )
    assert parsed.agent is not None
    assert len(parsed.mcp_servers) == 1
    assert parsed.mcp_servers[0].url == "https://ok.example.com/mcp"


def test_manifest_from_bytes_parses_raw_json() -> None:
    blob = b'{"name": "x", "actions": []}'
    out = manifest_from_bytes(blob)
    assert out == {"name": "x", "actions": []}


def test_manifest_from_bytes_returns_none_for_garbage() -> None:
    assert manifest_from_bytes(b"\x00\x01\x02not-json") is None
    assert manifest_from_bytes(b"") is None


def test_manifest_from_bytes_unzips_packaged_manifest() -> None:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", '{"name": "zipped", "actions": []}')
    out = manifest_from_bytes(buf.getvalue())
    assert out == {"name": "zipped", "actions": []}
