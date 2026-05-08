from m365_mcp_scanner.models.ids import compute_agent_id, compute_scan_id, compute_server_id


def test_server_id_stable_for_same_inputs() -> None:
    a = compute_server_id("https://example.com", "managed")
    b = compute_server_id("https://example.com", "managed")
    assert a == b
    assert len(a) == 16


def test_server_id_changes_when_url_changes() -> None:
    a = compute_server_id("https://a.example", "managed")
    b = compute_server_id("https://b.example", "managed")
    assert a != b


def test_server_id_changes_when_auth_changes() -> None:
    a = compute_server_id("https://example.com", "managed")
    b = compute_server_id("https://example.com", "none")
    assert a != b


def test_agent_id_uses_environment() -> None:
    a = compute_agent_id("copilot_studio", "bot1", environment_id="env1")
    b = compute_agent_id("copilot_studio", "bot1", environment_id="env2")
    assert a != b


def test_scan_id_is_16_hex() -> None:
    sid = compute_scan_id()
    assert len(sid) == 16
    int(sid, 16)
