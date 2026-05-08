from datetime import datetime, timezone
from pathlib import Path

from m365_mcp_scanner.models import ScanDocument, ScanStatus, ScanSummary
from m365_mcp_scanner.storage import (
    ensure_data_dir,
    list_scans,
    scan_dir,
    write_scan_document,
)


def _doc(scan_id: str, servers: int = 0) -> ScanDocument:
    return ScanDocument(
        scan_id=scan_id,
        tenant_id="t",
        started_at=datetime(2026, 5, 8, tzinfo=timezone.utc),
        status=ScanStatus.completed,
        scope=["copilot_connectors"],
        summary=ScanSummary(mcp_servers_total=servers),
    )


def test_list_scans_summarizes_files(tmp_path: Path) -> None:
    ensure_data_dir(tmp_path)
    a = _doc("aaaaaaaaaaaaaaaa", servers=3)
    b = _doc("bbbbbbbbbbbbbbbb", servers=7)
    write_scan_document(a, scan_dir(tmp_path) / "2026-05-08T00-00-00_aaaaaaaa.json")
    write_scan_document(b, scan_dir(tmp_path) / "2026-05-08T01-00-00_bbbbbbbb.json")
    rows = list_scans(tmp_path)
    by_id = {r.scan_id: r for r in rows}
    assert by_id["aaaaaaaaaaaaaaaa"].mcp_servers_total == 3
    assert by_id["bbbbbbbbbbbbbbbb"].mcp_servers_total == 7
    assert all(r.status == "completed" for r in rows)


def test_list_scans_skips_latest_pointer(tmp_path: Path) -> None:
    ensure_data_dir(tmp_path)
    (scan_dir(tmp_path) / "latest.json").write_text('{"latest": "x.json"}', encoding="utf-8")
    rows = list_scans(tmp_path)
    assert rows == []
