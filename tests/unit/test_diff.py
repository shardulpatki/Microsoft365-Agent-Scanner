from datetime import datetime, timezone

from m365_mcp_scanner.models import (
    NormalizedMcpServer,
    ScanDocument,
    ScanStatus,
)
from m365_mcp_scanner.models.enums import AuthType, Transport
from m365_mcp_scanner.storage import diff_scans


def _server(server_id: str, url: str = "https://x", auth: AuthType = AuthType.managed) -> NormalizedMcpServer:
    return NormalizedMcpServer(
        server_id=server_id,
        url=url,
        transport=Transport.copilot_connector,
        auth_type=auth,
        is_first_party=True,
        external_domain=False,
        discovered_via="copilot_connectors",
        discovered_at=datetime(2026, 5, 8, tzinfo=timezone.utc),
    )


def _doc(servers: list[NormalizedMcpServer]) -> ScanDocument:
    return ScanDocument(
        scan_id="0" * 16,
        tenant_id="t",
        started_at=datetime(2026, 5, 8, tzinfo=timezone.utc),
        status=ScanStatus.completed,
        scope=["copilot_connectors"],
        mcp_servers=servers,
    )


def test_diff_added_removed_changed() -> None:
    s1 = _server("a")
    s2_old = _server("b", url="https://old")
    s2_new = _server("b", url="https://new")
    s3 = _server("c")

    a = _doc([s1, s2_old])
    b = _doc([s2_new, s3])

    d = diff_scans(a, b)
    assert [s.server_id for s in d.mcp_servers.added] == ["c"]
    assert [s.server_id for s in d.mcp_servers.removed] == ["a"]
    assert len(d.mcp_servers.changed) == 1
    old, new = d.mcp_servers.changed[0]
    assert old.url == "https://old" and new.url == "https://new"
