from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from m365_mcp_scanner.storage.json_store import load_scan
from m365_mcp_scanner.storage.paths import scan_dir

logger = logging.getLogger(__name__)


@dataclass
class ScanSummaryRow:
    path: Path
    scan_id: str
    started_at: str
    status: str
    mcp_servers_total: int
    findings_total: int


def list_scans(data_dir: Path) -> list[ScanSummaryRow]:
    """Cheap scan-index read: extracts summary fields without loading full arrays.

    The keys we want (schema_version, scan_id, started_at, status, scope,
    config_snapshot, stages, summary) all appear before the large arrays
    (agents, mcp_servers, ...) because we serialize with model fields in
    declaration order. We try a json.loads of the full file but bail to
    a partial parse if the file is too big.
    """
    folder = scan_dir(data_dir)
    if not folder.is_dir():
        return []
    rows: list[ScanSummaryRow] = []
    for path in sorted(folder.glob("*.json")):
        if path.name == "latest.json":
            continue
        row = _summarize(path)
        if row is not None:
            rows.append(row)
    return rows


def _summarize(path: Path) -> ScanSummaryRow | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        try:
            doc = load_scan(path)
        except Exception:
            logger.warning("could not parse scan file %s; skipping", path)
            return None
        return ScanSummaryRow(
            path=path,
            scan_id=doc.scan_id,
            started_at=doc.started_at.isoformat(),
            status=doc.status.value,
            mcp_servers_total=doc.summary.mcp_servers_total,
            findings_total=doc.summary.findings_total,
        )
    summary = payload.get("summary") or {}
    return ScanSummaryRow(
        path=path,
        scan_id=str(payload.get("scan_id", "")),
        started_at=str(payload.get("started_at", "")),
        status=str(payload.get("status", "")),
        mcp_servers_total=int(summary.get("mcp_servers_total", 0)),
        findings_total=int(summary.get("findings_total", 0)),
    )
