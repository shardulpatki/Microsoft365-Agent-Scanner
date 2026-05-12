"""DEPRECATED — superseded by :mod:`m365_mcp_scanner.auth.file_cache`.

The Windows Credential Manager's ``CredWriteW`` enforces a ~2,560-byte
cap on the password blob of a Generic Credential, which serialized MSAL
caches routinely exceed. Microsoft's own Azure CLI sidesteps this with
an encrypted file; this scanner now does the same via ``file_cache``.

This module is left in place for reference (and to keep the
``keyring`` dep imported lazily for anyone wiring it back in on POSIX).
It is no longer used by :class:`DelegatedTokenProvider`.
"""
from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)

SERVICE_NAME = "m365-mcp-scanner"


class KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


def _default_backend() -> KeyringBackend:
    import keyring  # imported lazily so tests can stub before import

    return keyring


def _username(tenant_id: str, client_id: str) -> str:
    return f"{tenant_id}:{client_id}:delegated"


def load(
    tenant_id: str, client_id: str, *, backend: KeyringBackend | None = None
) -> str | None:
    backend = backend or _default_backend()
    try:
        return backend.get_password(SERVICE_NAME, _username(tenant_id, client_id))
    except Exception:  # noqa: BLE001 - keyring backend errors must not crash
        logger.exception("keyring load failed; treating as no cached session")
        return None


def save(
    tenant_id: str,
    client_id: str,
    payload: str,
    *,
    backend: KeyringBackend | None = None,
) -> None:
    backend = backend or _default_backend()
    backend.set_password(SERVICE_NAME, _username(tenant_id, client_id), payload)


def clear(
    tenant_id: str, client_id: str, *, backend: KeyringBackend | None = None
) -> None:
    backend = backend or _default_backend()
    try:
        backend.delete_password(SERVICE_NAME, _username(tenant_id, client_id))
    except Exception:  # noqa: BLE001 - delete on absent entry is a no-op
        logger.debug("keyring delete: no entry to remove (ok)")
