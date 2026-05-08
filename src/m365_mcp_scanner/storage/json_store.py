from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import ValidationError

from m365_mcp_scanner.models import ScanDocument
from m365_mcp_scanner.storage.paths import latest_pointer_path, lock_path


class ScanLockedError(RuntimeError):
    """Another scan is in progress (scans.lock is held)."""


def write_scan_document(doc: ScanDocument, path: Path) -> None:
    """Atomic write: serialize, write to .tmp, then os.replace to target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = doc.model_dump_json(indent=2, exclude_none=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def load_scan(path: Path) -> ScanDocument:
    try:
        return ScanDocument.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise ValueError(f"invalid scan document at {path}: {exc}") from exc


def update_latest_pointer(target: Path, data_dir: Path) -> None:
    """Update latest.json: symlink on POSIX, text file holding the filename on Windows."""
    pointer = latest_pointer_path(data_dir)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        # Pointer is a tiny JSON document with the basename. We don't symlink on Windows
        # because it requires elevated privileges.
        pointer.write_text(f'{{"latest": "{target.name}"}}\n', encoding="utf-8")
        return
    if pointer.is_symlink() or pointer.exists():
        pointer.unlink()
    pointer.symlink_to(target.name)


def resolve_latest(data_dir: Path) -> Path | None:
    pointer = latest_pointer_path(data_dir)
    if not pointer.exists() and not pointer.is_symlink():
        return None
    if sys.platform == "win32":
        import json

        try:
            payload = json.loads(pointer.read_text(encoding="utf-8"))
            name = payload.get("latest")
        except (OSError, ValueError):
            return None
        if not isinstance(name, str):
            return None
        candidate = pointer.parent / name
        return candidate if candidate.exists() else None
    target = pointer.resolve()
    return target if target.exists() else None


@contextmanager
def acquire_scan_lock(data_dir: Path) -> Iterator[None]:
    """Exclusive scan lock. Raises ScanLockedError if already held."""
    path = lock_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ScanLockedError(
            f"another scan is in progress (lock file exists at {path}). "
            "If no scan is running, delete the lock file."
        ) from exc
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
