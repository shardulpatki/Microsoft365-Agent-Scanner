from __future__ import annotations

from datetime import datetime
from pathlib import Path


def ensure_data_dir(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    scan_dir(data_dir).mkdir(parents=True, exist_ok=True)
    return data_dir


def scan_dir(data_dir: Path) -> Path:
    return data_dir / "scans"


def latest_pointer_path(data_dir: Path) -> Path:
    return scan_dir(data_dir) / "latest.json"


def lock_path(data_dir: Path) -> Path:
    return data_dir / "scans.lock"


def scan_filename(started_at: datetime, scan_id: str) -> str:
    ts = started_at.strftime("%Y-%m-%dT%H-%M-%S")
    return f"{ts}_{scan_id[:8]}.json"
