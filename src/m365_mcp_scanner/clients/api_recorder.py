from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ApiCall:
    timestamp: datetime
    client: str
    method: str
    url: str
    status: int | None
    elapsed_ms: float
    attempts: int
    error: str | None = None


@dataclass
class ApiCallRecorder:
    calls: list[ApiCall] = field(default_factory=list)

    def record(
        self,
        *,
        client: str,
        method: str,
        url: str,
        status: int | None,
        elapsed_ms: float,
        attempts: int,
        error: str | None = None,
    ) -> None:
        self.calls.append(
            ApiCall(
                timestamp=datetime.now(timezone.utc),
                client=client,
                method=method,
                url=url,
                status=status,
                elapsed_ms=elapsed_ms,
                attempts=attempts,
                error=error,
            )
        )
