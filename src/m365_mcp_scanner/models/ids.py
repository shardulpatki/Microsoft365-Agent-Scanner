"""Stable content-hashed IDs for scanned entities.

server_id and agent_id are sha256 of normalized inputs, hex-truncated to 16 chars.
scan_id is a random 16-hex token (per-run, not content-hashed).
"""
from __future__ import annotations

import hashlib
import secrets


def _sha16(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def compute_server_id(url: str, auth_type: str) -> str:
    return _sha16(f"{url}|{auth_type}")


def compute_agent_id(path: str, source_id: str, environment_id: str | None = None) -> str:
    env = environment_id or ""
    return _sha16(f"{path}|{env}|{source_id}")


def compute_scan_id() -> str:
    return secrets.token_hex(8)
