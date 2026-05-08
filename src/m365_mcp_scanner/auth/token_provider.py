from __future__ import annotations

from typing import Protocol


class TokenProvider(Protocol):
    """Mints bearer tokens for a given resource scope."""

    async def get_token(self, scope: str) -> str: ...
