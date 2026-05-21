"""Interactive (browser-popup) delegated sign-in for the wizard's Step 5.

Mirrors the auth-code mechanics used by Step 1's bootstrap sign-in
(``msal_bootstrap._do_auth_code_flow``) but runs under the scanner's own
``client_id`` instead of the Azure CLI public-client identity. The resulting
delegated refresh token is persisted via :class:`DelegatedTokenProvider`'s
own cache plumbing, so the wizard sign-in produces the same on-disk session
that ``mcp-scan login`` writes today — the two are interchangeable.

The localhost listener helpers are copied (not imported) from
``msal_bootstrap`` so this module is self-contained and the bootstrap path
remains untouched.
"""
from __future__ import annotations

import logging
import socket
import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from m365_mcp_scanner.auth.msal_broker import AuthError, DelegatedTokenProvider

logger = logging.getLogger(__name__)


# Interactive auth-code flow requests only the resource's
# ``.default``. MSAL automatically injects the reserved scopes
# (offline_access, openid, profile) for auth-code flows, so a
# refresh token is always issued — passing offline_access
# explicitly is rejected by MSAL as a reserved scope. ``.default``
# yields a token carrying every delegated permission the scanner
# app is already consented for (Step 3 grants admin consent for
# CopilotPackages.Read.All). ``.default`` must not be combined with
# named delegated scopes (AADSTS70011).
_SCOPES: tuple[str, ...] = (
    "https://graph.microsoft.com/.default",
)

_CALLBACK_HTML = (
    b"<!doctype html><meta charset='utf-8'><title>Sign-in complete</title>"
    b"<body style='font-family:sans-serif;padding:2rem'>"
    b"<h2>You may close this window.</h2>"
    b"<p>The M365 MCP Scanner wizard received your sign-in.</p>"
    b"</body>"
)


@dataclass
class DelegatedSigninResult:
    """Outcome of an :func:`interactive_delegated_signin` attempt.

    ``status`` is one of:

    * ``"success"`` — refresh token persisted; doctor's delegated row will
      flip to ✓ on the next read.
    * ``"error"`` — sign-in could not complete (no browser, timeout, MSAL
      error, etc.). ``detail`` carries a human-readable message.
    * ``"needs_device_code"`` — Entra rejected the loopback redirect
      (typically AADSTS500113 because the scanner app was created before
      publicClient.redirectUris was added). The UI should fall back to
      device-code sign-in.
    """

    status: str
    detail: str


def _bind_localhost_port() -> int:
    """Bind a random localhost port and return it (socket released)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _CallbackState:
    __slots__ = ("params", "error", "event")

    def __init__(self) -> None:
        self.params: dict[str, str] | None = None
        self.error: str | None = None
        self.event = threading.Event()


def _make_handler(state: _CallbackState) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any, **_kwargs: Any) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802 — http.server API
            parsed = urlparse(self.path)
            raw = parse_qs(parsed.query)
            params = {k: v[0] for k, v in raw.items() if v}
            if "error" in params:
                state.error = (
                    params.get("error_description") or params["error"]
                )
            else:
                state.params = params
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_CALLBACK_HTML)
            state.event.set()

    return _Handler


def _run_listener(port: int, state: _CallbackState, timeout_s: int) -> None:
    server = HTTPServer(("127.0.0.1", port), _make_handler(state))
    server.timeout = 1.0
    deadline = threading.Event()

    def _serve() -> None:
        while not state.event.is_set() and not deadline.is_set():
            server.handle_request()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    state.event.wait(timeout=timeout_s)
    deadline.set()
    try:
        server.server_close()
    except OSError:
        pass


def _looks_like_redirect_uri_rejection(detail: str) -> bool:
    if not detail:
        return False
    lower = detail.lower()
    return (
        "aadsts500113" in lower
        or "reply address" in lower
        or "redirect uri" in lower
        or "redirect_uri" in lower
    )


def _extract_upn(result: dict[str, Any]) -> str:
    claims = result.get("id_token_claims") or {}
    return str(
        claims.get("preferred_username")
        or claims.get("upn")
        or claims.get("email")
        or ""
    )


def interactive_delegated_signin(
    tenant_id: str,
    client_id: str,
    timeout_s: int = 300,
) -> DelegatedSigninResult:
    """Browser-popup auth-code sign-in under the scanner's own app.

    On success, persists the refresh token through the same path that
    ``mcp-scan login`` uses (``file_cache.save`` via
    :class:`DelegatedTokenProvider._persist_cache`), so subsequent scans
    and ``doctor`` reads see the new session.

    Step 3 of the wizard already grants admin consent for the scanner
    app's delegated scopes, so this sign-in should be authentication-only.
    If a consent prompt appears, that is a Step 3 bug — do not mask it
    here.
    """
    try:
        provider = DelegatedTokenProvider(
            tenant_id=tenant_id, client_id=client_id
        )
    except AuthError as exc:
        return DelegatedSigninResult(status="error", detail=str(exc))

    try:
        app = provider._get_app()
    except Exception as exc:  # noqa: BLE001 — MSAL OIDC discovery may fail
        return DelegatedSigninResult(
            status="error",
            detail=f"could not initialize MSAL client: {exc}",
        )

    try:
        port = _bind_localhost_port()
    except OSError as exc:
        return DelegatedSigninResult(
            status="error",
            detail=f"could not bind a localhost port for the OAuth redirect: {exc}",
        )
    redirect_uri = f"http://localhost:{port}"

    scopes = list(_SCOPES)
    try:
        flow: dict[str, Any] = app.initiate_auth_code_flow(
            scopes=scopes,
            redirect_uri=redirect_uri,
        )
    except Exception as exc:  # noqa: BLE001
        return DelegatedSigninResult(
            status="error",
            detail=f"MSAL could not initiate auth code flow: {exc}",
        )
    if "auth_uri" not in flow:
        err = flow.get("error_description") or flow.get("error") or "unknown"
        if _looks_like_redirect_uri_rejection(str(err)):
            return DelegatedSigninResult(status="needs_device_code", detail=str(err))
        return DelegatedSigninResult(
            status="error",
            detail=f"MSAL could not initiate auth code flow: {err}",
        )

    state = _CallbackState()
    listener = threading.Thread(
        target=_run_listener,
        args=(port, state, timeout_s),
        daemon=True,
    )
    listener.start()

    try:
        opened = webbrowser.open(flow["auth_uri"])
    except webbrowser.Error as exc:
        return DelegatedSigninResult(
            status="error",
            detail=f"could not open a browser for sign-in: {exc}",
        )
    if not opened:
        return DelegatedSigninResult(
            status="error",
            detail=(
                "the system reports no browser was available to open. "
                "Use the device-code fallback."
            ),
        )

    if not state.event.wait(timeout=timeout_s):
        return DelegatedSigninResult(
            status="error",
            detail=f"no sign-in callback received within {timeout_s}s",
        )
    if state.error:
        detail = state.error
        if _looks_like_redirect_uri_rejection(detail):
            return DelegatedSigninResult(status="needs_device_code", detail=detail)
        return DelegatedSigninResult(
            status="error",
            detail=f"sign-in returned an error: {detail}",
        )
    if not state.params:
        return DelegatedSigninResult(
            status="error",
            detail="sign-in callback fired but no query parameters were captured",
        )

    try:
        result: dict[str, Any] = app.acquire_token_by_auth_code_flow(
            flow, state.params
        )
    except Exception as exc:  # noqa: BLE001
        return DelegatedSigninResult(
            status="error",
            detail=f"token exchange failed: {exc}",
        )

    if "access_token" not in result:
        err = (
            result.get("error_description")
            or result.get("error")
            or "unknown error"
        )
        if _looks_like_redirect_uri_rejection(str(err)):
            return DelegatedSigninResult(status="needs_device_code", detail=str(err))
        return DelegatedSigninResult(status="error", detail=str(err))

    try:
        provider._persist_cache()
    except Exception as exc:  # noqa: BLE001 — persistence failure is recoverable
        logger.warning("delegated cache persist failed: %s", exc)
        return DelegatedSigninResult(
            status="error",
            detail=f"sign-in succeeded but cache could not be saved: {exc}",
        )

    upn = _extract_upn(result) or "(signed in)"
    return DelegatedSigninResult(status="success", detail=upn)
