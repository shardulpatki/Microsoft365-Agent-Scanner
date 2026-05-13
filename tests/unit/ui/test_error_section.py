from __future__ import annotations

import types
from datetime import datetime, timezone


def _err(code: str | None) -> "object":
    from m365_mcp_scanner.models.scan_document import ScanError

    return ScanError(
        stage="discover",
        surface="copilot_studio",
        code=code,
        message=f"message for {code!r}",
        timestamp=datetime.now(timezone.utc),
    )


def test_categorize_groups_by_code(fake_streamlit: types.ModuleType) -> None:
    from m365_mcp_scanner.ui.components.error_section import CATEGORIES, categorize

    errors = [
        _err("no_dataverse_access"),
        _err("no_dataverse_access"),
        _err("delegated_session_required"),
        _err("tenant_not_eligible"),
        _err("manifest_endpoint_unavailable"),
        _err("unknown"),
    ]
    buckets = categorize(errors)  # type: ignore[arg-type]
    assert set(buckets) == set(CATEGORIES)
    assert len(buckets["no_dataverse_access"]) == 2
    assert len(buckets["delegated_session_required"]) == 1
    assert len(buckets["tenant_not_eligible"]) == 1
    assert len(buckets["manifest_endpoint_unavailable"]) == 1
    # "unknown" is not in CATEGORIES — must not be silently bucketed.
    total_in_buckets = sum(len(v) for v in buckets.values())
    assert total_in_buckets == 5


def test_uncategorized_detection(fake_streamlit: types.ModuleType) -> None:
    from m365_mcp_scanner.ui.components.error_section import has_uncategorized

    assert has_uncategorized([_err("unknown")]) is True  # type: ignore[list-item]
    assert has_uncategorized([_err(None)]) is True  # type: ignore[list-item]
    assert has_uncategorized([_err("no_dataverse_access")]) is False  # type: ignore[list-item]
    assert has_uncategorized([]) is False
