"""Unit tests for the in-process Graph provisioner."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from m365_mcp_scanner.provisioning.provisioner import (
    APP_PERMISSION_IDS,
    DELEGATED_PERMISSION_IDS,
    GRAPH_BASE,
    ProvisionError,
    provision_scanner_app,
)


_GRAPH_AUD_TOKEN = (
    # header (any) . payload (graph aud) . sig
    "h."
    + __import__("base64")
    .urlsafe_b64encode(json.dumps({"aud": "https://graph.microsoft.com"}).encode())
    .rstrip(b"=")
    .decode()
    + ".s"
)


def _ok(json_body: dict[str, Any] | None = None, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json=json_body or {},
        request=httpx.Request("GET", "http://t"),
    )


class _Recorder:
    """Sequence-driven httpx fake. Each call pops a (matcher, response) tuple."""

    def __init__(self, plan: list[tuple[str, str, httpx.Response]]) -> None:
        self.plan = list(plan)
        self.calls: list[tuple[str, str]] = []

    async def request(self, method: str, url: str, *, json: Any = None) -> httpx.Response:  # noqa: A002
        self.calls.append((method, url))
        if not self.plan:
            raise AssertionError(f"unexpected extra request: {method} {url}")
        want_method, url_substr, resp = self.plan.pop(0)
        if want_method != method or url_substr not in url:
            raise AssertionError(
                f"expected {want_method} {url_substr}, got {method} {url}"
            )
        return resp


def _full_happy_plan(app_object_id: str = "AOID", sp_id: str = "SPID") -> list[
    tuple[str, str, httpx.Response]
]:
    return [
        # Step 1: name collision check (empty)
        ("GET", "/applications?", _ok({"value": []})),
        # Step 2: create app
        ("POST", "/applications", _ok({"id": app_object_id, "appId": "CID"}, 201)),
        # Step 3: PATCH public client
        ("PATCH", f"/applications/{app_object_id}", _ok({}, 204)),
        # Step 4: create SP
        ("POST", "/servicePrincipals", _ok({"id": sp_id}, 201)),
        # Step 5: PATCH permissions
        ("PATCH", f"/applications/{app_object_id}", _ok({}, 204)),
        # Step 6: lookup Graph SP, app role assignments × 5, oauth2 grant × 1
        ("GET", "/servicePrincipals?", _ok({"value": [{"id": "GRAPHSP"}]})),
        *(
            ("POST", f"/servicePrincipals/{sp_id}/appRoleAssignments", _ok({}, 201))
            for _ in APP_PERMISSION_IDS
        ),
        ("POST", "/oauth2PermissionGrants", _ok({}, 201)),
        # Step 7: addPassword
        (
            "POST",
            f"/applications/{app_object_id}/addPassword",
            _ok({"secretText": "S3CR3T"}, 200),
        ),
        # Step 8: directory roles lookup + assign
        ("GET", "/directoryRoles?", _ok({"value": [{"id": "ROLE1"}]})),
        (
            "POST",
            "/directoryRoles/ROLE1/members/$ref",
            _ok({}, 204),
        ),
    ]


@pytest.fixture
def patch_client(monkeypatch: pytest.MonkeyPatch):
    """Patch httpx.AsyncClient to use our recorder."""

    def _factory(plan: list[tuple[str, str, httpx.Response]]) -> _Recorder:
        rec = _Recorder(plan)

        class _FakeClient:
            def __init__(self, *_a: Any, **_kw: Any) -> None:
                pass

            async def __aenter__(self) -> "_FakeClient":
                return self

            async def __aexit__(self, *exc_info: Any) -> None:
                return None

            async def request(self, method: str, url: str, *, json: Any = None) -> httpx.Response:  # noqa: A002
                return await rec.request(method, url, json=json)

        monkeypatch.setattr(
            "m365_mcp_scanner.provisioning.provisioner.httpx.AsyncClient",
            _FakeClient,
        )
        return rec

    return _factory


async def test_provision_executes_substeps_in_order(patch_client) -> None:
    rec = patch_client(_full_happy_plan())
    progress: list[tuple[int, str]] = []

    result = await provision_scanner_app(
        _GRAPH_AUD_TOKEN,
        {"username": "u"},
        "TID",
        "Scanner App",
        progress_callback=lambda n, m: progress.append((n, m)),
    )

    assert result.client_id == "CID"
    assert result.client_secret == "S3CR3T"
    assert result.admin_consent_granted is True
    assert result.pp_admin_role_assigned is True
    # Progress callbacks fired for steps 1..8 in order.
    assert [n for n, _ in progress] == [1, 2, 3, 4, 5, 6, 7, 8]


async def test_provision_fails_on_duplicate_app_name(patch_client) -> None:
    plan = [
        (
            "GET",
            "/applications?",
            _ok({"value": [{"id": "existing", "displayName": "Scanner App"}]}),
        )
    ]
    patch_client(plan)
    with pytest.raises(ProvisionError) as ei:
        await provision_scanner_app(
            _GRAPH_AUD_TOKEN, {}, "TID", "Scanner App"
        )
    assert ei.value.step == 1
    assert "already exists" in ei.value.message


async def test_provision_surfaces_4xx_immediately(patch_client) -> None:
    plan = [
        ("GET", "/applications?", _ok({"value": []})),
        ("POST", "/applications", _ok({"error": {"code": "BadRequest"}}, 400)),
    ]
    patch_client(plan)
    with pytest.raises(ProvisionError) as ei:
        await provision_scanner_app(
            _GRAPH_AUD_TOKEN, {}, "TID", "Scanner App"
        )
    assert ei.value.step == 2


async def test_provision_retries_5xx(patch_client) -> None:
    plan = _full_happy_plan()
    # Inject one 503 before the successful POST /applications.
    plan.insert(1, ("POST", "/applications", _ok({}, 503)))
    patch_client(plan)
    result = await provision_scanner_app(
        _GRAPH_AUD_TOKEN, {}, "TID", "Scanner App"
    )
    assert result.client_id == "CID"


async def test_provision_pp_role_failure_is_non_fatal(patch_client) -> None:
    plan = _full_happy_plan()
    # Replace step-8 directoryRoles lookup with a 403.
    plan[-2] = ("GET", "/directoryRoles?", _ok({"error": "forbidden"}, 403))
    plan.pop(-1)  # remove the member assignment — never reached
    patch_client(plan)
    result = await provision_scanner_app(
        _GRAPH_AUD_TOKEN, {}, "TID", "Scanner App"
    )
    assert result.pp_admin_role_assigned is False
    assert result.pp_admin_role_error is not None
    # Graph provisioning still succeeded.
    assert result.client_secret == "S3CR3T"


async def test_provision_rejects_non_graph_token() -> None:
    import base64

    pp_token = (
        "h."
        + base64.urlsafe_b64encode(
            json.dumps({"aud": "https://service.powerapps.com"}).encode()
        )
        .rstrip(b"=")
        .decode()
        + ".s"
    )
    with pytest.raises(ProvisionError) as ei:
        await provision_scanner_app(pp_token, {}, "TID", "Scanner App")
    assert ei.value.step == 1
    assert "graph.microsoft.com" in ei.value.message


def test_permission_id_count_matches_scanner_sh() -> None:
    """Guard against accidental drift from the retired bash script."""
    assert len(APP_PERMISSION_IDS) == 5
    assert len(DELEGATED_PERMISSION_IDS) == 3


def test_graph_base_is_v1() -> None:
    assert GRAPH_BASE.endswith("/v1.0")
