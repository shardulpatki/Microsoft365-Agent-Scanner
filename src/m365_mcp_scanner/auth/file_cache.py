"""Encrypted-file persistence for the MSAL serialized token cache.

Replaces :mod:`keyring_cache` because the Windows Credential Manager
imposes a ~2,560-byte cap on Generic Credential password blobs, which
serialized MSAL caches routinely exceed (refresh + ID tokens + per-scope
access tokens + account metadata). Microsoft's own Azure CLI uses the
same encrypted-file approach on Windows for the same reason.

Security model: a Fernet key is derived (PBKDF2-HMAC-SHA256, 600k
iterations) from ``tenant_id : client_id : Path.home()`` with a per-install
random salt stored next to the cache. Matches OS-keyring guarantees on
the same host (any process running as the user can read either way),
with the added property that a copied cache file is useless without the
original home path.
"""
from __future__ import annotations

import base64
import logging
import os
import platform
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

CACHE_FILENAME = "msal_token_cache.bin"
SALT_FILENAME = "msal_token_cache.salt"
_PBKDF2_ITERATIONS = 600_000


def _default_cache_dir() -> Path:
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        d = base / "m365-mcp-scanner"
    else:
        d = Path.home() / ".m365-mcp-scanner"
    d.mkdir(parents=True, exist_ok=True)
    if platform.system() != "Windows":
        try:
            os.chmod(d, 0o700)
        except OSError:
            logger.debug("could not chmod cache dir %s", d)
    return d


def _resolve_dir(cache_dir: Path | None) -> Path:
    if cache_dir is None:
        return _default_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _derive_key(tenant_id: str, client_id: str, cache_dir: Path) -> bytes:
    salt_path = cache_dir / SALT_FILENAME
    if salt_path.exists():
        salt = salt_path.read_bytes()
    else:
        salt = os.urandom(16)
        salt_path.write_bytes(salt)
        if platform.system() != "Windows":
            try:
                os.chmod(salt_path, 0o600)
            except OSError:
                logger.debug("could not chmod salt file %s", salt_path)

    # Include home path so a copied cache+salt is useless on another host.
    material = f"{tenant_id}:{client_id}:{Path.home()}".encode("utf-8")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(material))


def load(
    tenant_id: str,
    client_id: str,
    *,
    cache_dir: Path | None = None,
) -> str | None:
    d = _resolve_dir(cache_dir)
    cache_path = d / CACHE_FILENAME
    if not cache_path.exists():
        return None
    try:
        encrypted = cache_path.read_bytes()
        key = _derive_key(tenant_id, client_id, d)
        return Fernet(key).decrypt(encrypted).decode("utf-8")
    except (InvalidToken, OSError, ValueError):
        logger.warning(
            "delegated token cache at %s could not be decrypted; "
            "treating as no session",
            cache_path,
        )
        return None


def save(
    tenant_id: str,
    client_id: str,
    payload: str,
    *,
    cache_dir: Path | None = None,
) -> None:
    d = _resolve_dir(cache_dir)
    cache_path = d / CACHE_FILENAME
    key = _derive_key(tenant_id, client_id, d)
    encrypted = Fernet(key).encrypt(payload.encode("utf-8"))
    cache_path.write_bytes(encrypted)
    if platform.system() != "Windows":
        try:
            os.chmod(cache_path, 0o600)
        except OSError:
            logger.debug("could not chmod cache file %s", cache_path)


def clear(
    tenant_id: str,  # noqa: ARG001 - signature parity with keyring_cache
    client_id: str,  # noqa: ARG001 - signature parity with keyring_cache
    *,
    cache_dir: Path | None = None,
) -> None:
    d = _resolve_dir(cache_dir)
    cache_path = d / CACHE_FILENAME
    try:
        cache_path.unlink()
    except FileNotFoundError:
        logger.debug("cache file already absent at %s (ok)", cache_path)
