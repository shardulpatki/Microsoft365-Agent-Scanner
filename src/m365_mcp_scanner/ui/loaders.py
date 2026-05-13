"""Read-side helpers for the Streamlit UI: enumerate and parse scan documents.

Cache invalidation strategy is per TRD §11: ``@st.cache_data`` keyed on the file
path and its mtime in nanoseconds. Scans are append-only per ``scan_id``, so
mtime is a safe key for v1.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import streamlit as st

from m365_mcp_scanner.config import Settings
from m365_mcp_scanner.models import ScanDocument
from m365_mcp_scanner.storage.json_store import resolve_latest
from m365_mcp_scanner.storage.paths import latest_pointer_path, scan_dir


@dataclass(frozen=True)
class ScanSummary:
    scan_id: str
    started_at: datetime | None
    status: str | None
    path: Path


def scans_dir(settings: Settings | None = None) -> Path:
    settings = settings if settings is not None else Settings()
    return scan_dir(settings.data_dir)


def list_scans(settings: Settings | None = None) -> list[ScanSummary]:
    """Enumerate scan JSON files, newest first.

    Reads each file's pydantic header lazily — we only validate enough to fill
    the summary. The ``latest.json`` pointer is skipped.
    """
    folder = scans_dir(settings)
    if not folder.exists():
        return []
    pointer = latest_pointer_path(settings.data_dir if settings else Settings().data_dir)
    rows: list[tuple[float, ScanSummary]] = []
    for path in folder.glob("*.json"):
        if path == pointer or path.name == "latest.json":
            continue
        try:
            doc = _load_scan_cached(str(path), path.stat().st_mtime_ns)
        except (ValueError, OSError):
            continue
        rows.append(
            (
                path.stat().st_mtime,
                ScanSummary(
                    scan_id=doc.scan_id,
                    started_at=doc.started_at,
                    status=str(doc.status),
                    path=path,
                ),
            )
        )
    rows.sort(key=lambda r: r[0], reverse=True)
    return [s for _, s in rows]


@st.cache_data(show_spinner=False)
def _load_scan_cached(path_str: str, mtime_ns: int) -> ScanDocument:  # noqa: ARG001
    # mtime_ns is part of the cache key only; we re-read the file.
    return ScanDocument.model_validate_json(Path(path_str).read_text(encoding="utf-8"))


def load_scan(path: Path) -> ScanDocument:
    return _load_scan_cached(str(path), path.stat().st_mtime_ns)


def load_latest_scan(settings: Settings | None = None) -> ScanDocument | None:
    settings = settings if settings is not None else Settings()
    pointer_target = resolve_latest(settings.data_dir)
    if pointer_target is not None and pointer_target.exists():
        try:
            return load_scan(pointer_target)
        except (ValueError, OSError):
            pass
    rows = list_scans(settings)
    if not rows:
        return None
    return load_scan(rows[0].path)
