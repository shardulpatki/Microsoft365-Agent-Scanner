"""Unit tests for ui.components.env_row deep-link construction."""
from __future__ import annotations

import types

import pytest


def test_deep_link_for_valid_env_id(fake_streamlit: types.ModuleType) -> None:
    from m365_mcp_scanner.ui.components import env_row

    env_id = "11111111-2222-3333-4444-555555555555"
    link = env_row.deep_link(env_id)
    assert link == (
        "https://admin.powerplatform.microsoft.com/manage/environments/"
        f"{env_id}/appusers"
    )


@pytest.mark.parametrize(
    "malformed",
    [
        "",
        "not-a-guid",
        "11111111-2222-3333-4444",
        "'; DROP TABLE x; --",
        "../../../etc/passwd",
        "11111111-2222-3333-4444-555555555555/extra",
    ],
)
def test_deep_link_rejects_malformed_env_id(
    fake_streamlit: types.ModuleType, malformed: str
) -> None:
    from m365_mcp_scanner.ui.components import env_row

    assert env_row.deep_link(malformed) is None


def test_admin_center_template_does_not_leak_other_params(
    fake_streamlit: types.ModuleType,
) -> None:
    from m365_mcp_scanner.ui.components import env_row

    env_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    link = env_row.deep_link(env_id)
    assert link is not None
    # Single env_id substitution, no extra path segments or query strings.
    assert link.count(env_id) == 1
    assert "?" not in link
    assert link.endswith("/appusers")


def test_default_env_id_accepted(fake_streamlit: types.ModuleType) -> None:
    from m365_mcp_scanner.ui.components import env_row

    env_id = "Default-6cf34320-e817-4ad9-81b6-460c24c7a4e7"
    link = env_row.deep_link(env_id)
    assert link is not None
    assert link.count(env_id) == 1
    assert "Default-6cf34320-e817-4ad9-81b6-460c24c7a4e7" in link


def test_url_contains_manage_prefix(fake_streamlit: types.ModuleType) -> None:
    from m365_mcp_scanner.ui.components import env_row

    for env_id in (
        "a787c566-03fd-e1c6-972d-9549daaad71c",
        "Default-6cf34320-e817-4ad9-81b6-460c24c7a4e7",
    ):
        link = env_row.deep_link(env_id)
        assert link is not None
        assert "/manage/environments/" in link
        assert link.endswith("/appusers")


@pytest.mark.parametrize(
    "injection",
    [
        "11111111-2222-3333-4444-555555555555/..",
        "..",
        "../11111111-2222-3333-4444-555555555555",
        "11111111-2222-3333-4444-555555555555\\evil",
        "Default-../6cf34320-e817-4ad9-81b6-460c24c7a4e7",
        " 11111111-2222-3333-4444-555555555555",
    ],
)
def test_path_injection_rejected(
    fake_streamlit: types.ModuleType, injection: str
) -> None:
    from m365_mcp_scanner.ui.components import env_row

    assert env_row.deep_link(injection) is None


def test_uppercase_guid_accepted(fake_streamlit: types.ModuleType) -> None:
    from m365_mcp_scanner.ui.components import env_row

    bare = "A787C566-03FD-E1C6-972D-9549DAAAD71C"
    prefixed = "Default-6CF34320-E817-4AD9-81B6-460C24C7A4E7"

    bare_link = env_row.deep_link(bare)
    prefixed_link = env_row.deep_link(prefixed)

    assert bare_link is not None and bare in bare_link
    assert prefixed_link is not None and prefixed in prefixed_link
