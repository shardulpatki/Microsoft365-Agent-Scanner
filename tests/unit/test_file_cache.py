"""Unit tests for the encrypted file token cache."""
from __future__ import annotations

import os
from pathlib import Path

from m365_mcp_scanner.auth import file_cache


def test_save_load_round_trip(tmp_path: Path) -> None:
    payload = '{"access_token": "AT", "refresh_token": "RT"}'
    file_cache.save("t", "c", payload, cache_dir=tmp_path)
    assert file_cache.load("t", "c", cache_dir=tmp_path) == payload


def test_load_returns_none_on_empty_dir(tmp_path: Path) -> None:
    assert file_cache.load("t", "c", cache_dir=tmp_path) is None


def test_load_returns_none_on_corrupt_file(tmp_path: Path) -> None:
    (tmp_path / file_cache.CACHE_FILENAME).write_bytes(os.urandom(256))
    # Salt also needs to exist for the key derivation to reach decrypt.
    (tmp_path / file_cache.SALT_FILENAME).write_bytes(os.urandom(16))
    assert file_cache.load("t", "c", cache_dir=tmp_path) is None


def test_key_is_deterministic_across_invocations(tmp_path: Path) -> None:
    file_cache.save("tenant-a", "client-a", "payload-1", cache_dir=tmp_path)
    # Wipe the cache file but keep the salt: a fresh save must produce
    # something the original key can decrypt — proving derivation is stable.
    (tmp_path / file_cache.CACHE_FILENAME).unlink()
    file_cache.save("tenant-a", "client-a", "payload-2", cache_dir=tmp_path)
    assert file_cache.load("tenant-a", "client-a", cache_dir=tmp_path) == "payload-2"


def test_different_tenant_or_client_cannot_decrypt(tmp_path: Path) -> None:
    file_cache.save("tenant-a", "client-a", "secret", cache_dir=tmp_path)
    assert file_cache.load("tenant-b", "client-a", cache_dir=tmp_path) is None
    assert file_cache.load("tenant-a", "client-b", cache_dir=tmp_path) is None


def test_clear_removes_cache_file(tmp_path: Path) -> None:
    file_cache.save("t", "c", "payload", cache_dir=tmp_path)
    assert (tmp_path / file_cache.CACHE_FILENAME).exists()
    file_cache.clear("t", "c", cache_dir=tmp_path)
    assert not (tmp_path / file_cache.CACHE_FILENAME).exists()


def test_clear_is_idempotent(tmp_path: Path) -> None:
    file_cache.clear("t", "c", cache_dir=tmp_path)  # no file — should not raise
