from __future__ import annotations

import json
import os
import types
from pathlib import Path

from pydantic import SecretStr


def _make_scan_payload(scan_id: str, started: str = "2026-05-01T00:00:00+00:00") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "scan_id": scan_id,
            "tenant_id": "tenant",
            "started_at": started,
            "status": "completed",
            "scope": ["copilot_studio"],
        }
    )


def _settings(data_dir: Path):  # type: ignore[no-untyped-def]
    from m365_mcp_scanner.config import Settings

    return Settings(
        tenant_id="t",
        client_id="c",
        client_secret=SecretStr("s"),
        data_dir=data_dir,
    )


def test_list_scans_orders_by_mtime_desc(
    fake_streamlit: types.ModuleType, tmp_path: Path
) -> None:
    from m365_mcp_scanner.ui.loaders import list_scans

    scans = tmp_path / "scans"
    scans.mkdir()
    paths = []
    for i, sid in enumerate(["aaa11111", "bbb22222", "ccc33333"]):
        p = scans / f"2026-05-0{i + 1}T00-00-00_{sid}.json"
        p.write_text(_make_scan_payload(sid), encoding="utf-8")
        ts = 1_700_000_000 + i * 1000
        os.utime(p, (ts, ts))
        paths.append((p, sid))

    rows = list_scans(_settings(tmp_path))
    assert [r.scan_id for r in rows] == ["ccc33333", "bbb22222", "aaa11111"]


def test_list_scans_skips_latest_pointer(
    fake_streamlit: types.ModuleType, tmp_path: Path
) -> None:
    from m365_mcp_scanner.ui.loaders import list_scans

    scans = tmp_path / "scans"
    scans.mkdir()
    p = scans / "2026-05-01T00-00-00_aaa11111.json"
    p.write_text(_make_scan_payload("aaa11111"), encoding="utf-8")
    (scans / "latest.json").write_text('{"latest": "2026-05-01T00-00-00_aaa11111.json"}\n')

    rows = list_scans(_settings(tmp_path))
    assert len(rows) == 1
    assert rows[0].scan_id == "aaa11111"


def test_load_scan_parses_scan_document(
    fake_streamlit: types.ModuleType, tmp_path: Path
) -> None:
    from m365_mcp_scanner.ui.loaders import load_scan

    p = tmp_path / "one.json"
    p.write_text(_make_scan_payload("deadbeef"), encoding="utf-8")
    doc = load_scan(p)
    assert doc.scan_id == "deadbeef"
    assert doc.tenant_id == "tenant"


def test_load_scan_cache_keyed_by_mtime(
    fake_streamlit: types.ModuleType, tmp_path: Path
) -> None:
    from m365_mcp_scanner.ui.loaders import _load_scan_cached

    p = tmp_path / "one.json"
    p.write_text(_make_scan_payload("first"), encoding="utf-8")
    mtime_ns_1 = p.stat().st_mtime_ns
    doc1 = _load_scan_cached(str(p), mtime_ns_1)

    # Same mtime key → cached call returns same content (no re-read happens in
    # the fake decorator, but the key is identical so behavior is consistent).
    doc1_again = _load_scan_cached(str(p), mtime_ns_1)
    assert doc1_again.scan_id == doc1.scan_id

    # Different mtime key → fresh read picks up new content.
    p.write_text(_make_scan_payload("second"), encoding="utf-8")
    mtime_ns_2 = mtime_ns_1 + 1
    doc2 = _load_scan_cached(str(p), mtime_ns_2)
    assert doc2.scan_id == "second"


def test_load_latest_scan_pointer_then_fallback(
    fake_streamlit: types.ModuleType, tmp_path: Path
) -> None:
    from m365_mcp_scanner.ui.loaders import load_latest_scan

    scans = tmp_path / "scans"
    scans.mkdir()
    # Empty dir → None.
    assert load_latest_scan(_settings(tmp_path)) is None

    # Add two scans, no pointer → falls back to newest.
    p1 = scans / "2026-05-01T00-00-00_oldold00.json"
    p1.write_text(_make_scan_payload("oldold00"), encoding="utf-8")
    os.utime(p1, (1_700_000_000, 1_700_000_000))
    p2 = scans / "2026-05-02T00-00-00_newnew00.json"
    p2.write_text(_make_scan_payload("newnew00"), encoding="utf-8")
    os.utime(p2, (1_700_001_000, 1_700_001_000))

    doc = load_latest_scan(_settings(tmp_path))
    assert doc is not None and doc.scan_id == "newnew00"
