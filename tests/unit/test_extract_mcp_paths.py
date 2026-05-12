from __future__ import annotations

from m365_mcp_scanner.discovery.custom_connectors import _extract_mcp_paths


def test_no_paths_returns_empty() -> None:
    assert _extract_mcp_paths({}) == []
    assert _extract_mcp_paths({"paths": None}) == []
    # paths as a list (defensive):
    assert _extract_mcp_paths({"paths": []}) == []


def test_op_level_extension_matches() -> None:
    spec = {
        "paths": {
            "/mcp": {
                "post": {
                    "x-ms-agentic-protocol": "mcp-streamable-1.0",
                    "operationId": "InvokeMCP",
                }
            }
        }
    }
    matches = _extract_mcp_paths(spec)
    assert len(matches) == 1
    path, method, _op = matches[0]
    assert path == "/mcp"
    assert method == "post"


def test_top_level_extension_matches_all_ops() -> None:
    spec = {
        "x-ms-agentic-protocol": "mcp-streamable-1.0",
        "paths": {
            "/run": {"post": {"operationId": "Run"}},
            "/run2": {"get": {"operationId": "Run2"}},
        },
    }
    matches = _extract_mcp_paths(spec)
    assert {(p, m) for p, m, _ in matches} == {("/run", "post"), ("/run2", "get")}


def test_operation_id_heuristic_matches() -> None:
    spec = {
        "paths": {
            "/x": {"post": {"operationId": "InvokeMcpTool"}},
        }
    }
    matches = _extract_mcp_paths(spec)
    assert len(matches) == 1


def test_non_mcp_spec_returns_empty() -> None:
    spec = {
        "paths": {
            "/sites": {"get": {"operationId": "GetSites"}},
        }
    }
    assert _extract_mcp_paths(spec) == []


def test_malformed_operation_skipped() -> None:
    spec = {
        "paths": {
            "/x": {"post": "not-a-dict"},
            "/y": {
                "post": {
                    "x-ms-agentic-protocol": "mcp-streamable-1.0",
                    "operationId": "Y",
                }
            },
        }
    }
    matches = _extract_mcp_paths(spec)
    assert {(p, m) for p, m, _ in matches} == {("/y", "post")}


def test_non_http_method_keys_ignored() -> None:
    spec = {
        "paths": {
            "/x": {
                "parameters": [{"name": "id"}],  # non-http key
                "post": {"x-ms-agentic-protocol": "mcp-streamable-1.0"},
            }
        }
    }
    matches = _extract_mcp_paths(spec)
    assert len(matches) == 1


def test_non_dict_spec_returns_empty() -> None:
    assert _extract_mcp_paths("not a spec") == []  # type: ignore[arg-type]
    assert _extract_mcp_paths(None) == []  # type: ignore[arg-type]
