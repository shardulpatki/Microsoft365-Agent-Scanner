from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from m365_mcp_scanner.discovery.copilot_studio import (
    _connection_reference_name,
    _operation_id,
    _parse_component_data,
    is_mcp_taskdialog,
)

FIXTURES = Path(__file__).parent.parent / "integration" / "fixtures"


def _load_yaml(name: str) -> object:
    return yaml.safe_load((FIXTURES / name).read_text())


def test_fingerprint_positive_tavily() -> None:
    parsed = _load_yaml("botcomponent_mcp_tavily.yaml")
    assert is_mcp_taskdialog(parsed) is True


def test_fingerprint_negative_system_topic() -> None:
    parsed = _load_yaml("botcomponent_topic_system.yaml")
    assert is_mcp_taskdialog(parsed) is False


def test_fingerprint_negative_gpt_metadata() -> None:
    parsed = _load_yaml("botcomponent_gpt.yaml")
    assert is_mcp_taskdialog(parsed) is False


def test_fingerprint_handles_none_and_non_dict() -> None:
    assert is_mcp_taskdialog(None) is False
    assert is_mcp_taskdialog("just a string") is False
    assert is_mcp_taskdialog([{"kind": "TaskDialog"}]) is False


def test_fingerprint_missing_action() -> None:
    assert is_mcp_taskdialog({"kind": "TaskDialog"}) is False


def test_fingerprint_missing_operation_details() -> None:
    assert (
        is_mcp_taskdialog({"kind": "TaskDialog", "action": {"kind": "Other"}}) is False
    )


def test_fingerprint_casing_strict() -> None:
    parsed = {
        "kind": "TaskDialog",
        "action": {
            "operationDetails": {"kind": "modelcontextprotocolmetadata"}
        },
    }
    assert is_mcp_taskdialog(parsed) is False


def test_parse_component_data_yaml() -> None:
    blob = (FIXTURES / "botcomponent_mcp_tavily.yaml").read_text()
    parsed, err = _parse_component_data({"botcomponentid": "c1", "data": blob})
    assert err is None
    assert isinstance(parsed, dict)
    assert parsed["kind"] == "TaskDialog"


def test_parse_component_data_json_fallback() -> None:
    blob = '{"kind": "TaskDialog", "action": {"operationDetails": {"kind": "ModelContextProtocolMetadata"}}}'
    parsed, err = _parse_component_data({"botcomponentid": "c1", "data": blob})
    assert err is None
    assert is_mcp_taskdialog(parsed)


def test_parse_component_data_malformed_returns_error() -> None:
    # YAML "safe_load" is permissive; force a real parse error.
    blob = "kind: TaskDialog\n  bad: [unterminated"
    parsed, err = _parse_component_data({"botcomponentid": "c1", "data": blob})
    # Either parser may produce error or weird shape; both acceptable as long as no crash.
    if err is None:
        # parsed is "definitely not an MCP TaskDialog" — that's still safe
        assert is_mcp_taskdialog(parsed) is False
    else:
        assert err.code == "botcomponent_data_parse_failed"


def test_parse_component_data_too_large() -> None:
    blob = "x" * (1_048_576 + 1)
    _parsed, err = _parse_component_data({"botcomponentid": "c1", "data": blob})
    assert err is not None
    assert err.code == "botcomponent_too_large"


def test_parse_component_data_empty() -> None:
    parsed, err = _parse_component_data({"botcomponentid": "c1", "data": ""})
    assert parsed is None and err is None
    parsed, err = _parse_component_data({"botcomponentid": "c1"})
    assert parsed is None and err is None


def test_connection_reference_extraction() -> None:
    parsed = _load_yaml("botcomponent_mcp_tavily.yaml")
    assert _connection_reference_name(parsed) == (  # type: ignore[arg-type]
        "cra93_tavilyWebSearchAgent.shared_tavilymcp."
        "f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1"
    )


def test_operation_id_extraction() -> None:
    parsed = _load_yaml("botcomponent_mcp_tavily.yaml")
    assert _operation_id(parsed) == "InvokeServer"  # type: ignore[arg-type]


def test_connection_reference_missing_returns_none() -> None:
    assert _connection_reference_name({"kind": "TaskDialog"}) is None


@pytest.mark.parametrize(
    "shape",
    [
        {"action": "not-a-dict"},
        {"action": {"connectionReference": ""}},
    ],
)
def test_connection_reference_invalid_shapes(shape: dict[str, object]) -> None:
    assert _connection_reference_name(shape) is None
