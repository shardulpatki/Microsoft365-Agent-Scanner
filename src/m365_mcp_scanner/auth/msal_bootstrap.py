"""MSAL public-client bootstrap auth for the first-run wizard.

Owns the wizard's Step 1 sign-in. Uses the well-known Azure CLI public
``client_id`` so the operator's existing tenant policies for the az CLI also
permit this flow — no additional app registration is required to *start* the
wizard. The resulting Graph token is consumed by the in-process provisioner
(``provisioning/provisioner.py``) to create the scanner's own Entra app
registration.

Two flows are exposed:

* :func:`acquire_bootstrap_token` — authorization code + PKCE against a
  one-shot ``http.server`` listening on ``localhost:<random-port>``. Default.
* :func:`acquire_bootstrap_token_device_code` — MSAL device flow, used as a
  fallback when localhost listener binding is blocked (locked-down corporate
  network) or the operator's default browser cannot be invoked.

Both return a :class:`BootstrapAuthResult` whose ``account`` field can be
fed back into MSAL's silent-acquisition path to obtain tokens for other
audiences (e.g. Power Platform admin) without a second sign-in.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import msal

logger = logging.getLogger(__name__)

# Well-known Azure CLI public client. Microsoft documents this for tools that
# need a public-client identity with broad pre-consented Graph scopes.
AZURE_CLI_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"

GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"

_CALLBACK_HTML = (
    b"<!doctype html><meta charset='utf-8'><title>Sign-in complete</title>"
    b"<body style='font-family:sans-serif;padding:2rem'>"
    b"<h2>You may close this window.</h2>"
    b"<p>The M365 MCP Scanner wizard received your sign-in.</p>"
    b"</body>"
)


class BootstrapAuthError(RuntimeError):
    """Raised when the bootstrap sign-in flow cannot complete."""


class BootstrapAuthTimeout(BootstrapAuthError):
    """Raised when the operator did not complete sign-in within ``timeout_s``."""


@dataclass
class BootstrapAuthResult:
    access_token: str
    tenant_id: str
    user_principal_name: str
    account: dict[str, Any]
    expires_in: int


def _authority(tenant_id: str | None) -> str:
    suffix = tenant_id or "organizations"
    return f"https://login.microsoftonline.com/{suffix}"


def _bind_localhost_port() -> int:
    """Bind a random localhost port and return it (socket released)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", 0))
        except OSError as exc:
            raise BootstrapAuthError(
                f"could not bind a localhost port for the OAuth redirect: {exc}"
            ) from exc
        return int(s.getsockname()[1])


class _CallbackState:
    __slots__ = ("params", "error", "event")

    def __init__(self) -> None:
        self.params: dict[str, str] | None = None
        self.error: str | None = None
        self.event = threading.Event()


def _make_handler(state: _CallbackState) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        # Silence default stderr logging — wizard owns the user-facing log.
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


def _run_listener(
    port: int, state: _CallbackState, timeout_s: int
) -> None:
    server = HTTPServer(("127.0.0.1", port), _make_handler(state))
    server.timeout = 1.0
    deadline = threading.Event()

    def _serve() -> None:
        # handle_request blocks; loop until our event fires or timeout.
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


def _do_auth_code_flow(
    tenant_id: str | None,
    timeout_s: int,
) -> BootstrapAuthResult:
    port = _bind_localhost_port()
    redirect_uri = f"http://localhost:{port}"
    app = msal.PublicClientApplication(
        AZURE_CLI_CLIENT_ID,
        authority=_authority(tenant_id),
    )
    flow: dict[str, Any] = app.initiate_auth_code_flow(
        scopes=[GRAPH_DEFAULT_SCOPE],
        redirect_uri=redirect_uri,
    )
    if "auth_uri" not in flow:
        err = flow.get("error_description") or flow.get("error") or "unknown"
        raise BootstrapAuthError(
            f"MSAL could not initiate auth code flow: {err}"
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
        raise BootstrapAuthError(
            f"could not open a browser for sign-in: {exc}"
        ) from exc
    if not opened:
        raise BootstrapAuthError(
            "the system reports no browser was available to open. "
            "Use the device-code fallback."
        )

    if not state.event.wait(timeout=timeout_s):
        raise BootstrapAuthTimeout(
            f"no sign-in callback received within {timeout_s}s"
        )
    if state.error:
        raise BootstrapAuthError(
            f"sign-in returned an error: {state.error}"
        )
    if not state.params:
        raise BootstrapAuthError(
            "sign-in callback fired but no query parameters were captured"
        )

    result: dict[str, Any] = app.acquire_token_by_auth_code_flow(
        flow, state.params
    )
    return _to_bootstrap_result(app, result)


def _do_device_code_flow(
    tenant_id: str | None,
    timeout_s: int,
    on_prompt: Any | None = None,
) -> BootstrapAuthResult:
    app = msal.PublicClientApplication(
        AZURE_CLI_CLIENT_ID,
        authority=_authority(tenant_id),
    )
    flow: dict[str, Any] = app.initiate_device_flow(
        scopes=[GRAPH_DEFAULT_SCOPE]
    )
    if "user_code" not in flow:
        err = flow.get("error_description") or flow.get("error") or "unknown"
        raise BootstrapAuthError(
            f"MSAL could not initiate device flow: {err}"
        )
    if on_prompt is not None:
        try:
            on_prompt(flow)
        except Exception:  # noqa: BLE001 — prompt callback must not abort flow
            logger.exception("device-flow on_prompt callback raised")
    # MSAL's device flow has its own internal timeout (`expires_in`); cap it.
    flow["expires_in"] = min(int(flow.get("expires_in", timeout_s)), timeout_s)
    result: dict[str, Any] = app.acquire_token_by_device_flow(flow)
    return _to_bootstrap_result(app, result)


def _to_bootstrap_result(
    app: msal.PublicClientApplication, result: dict[str, Any]
) -> BootstrapAuthResult:
    if "access_token" not in result:
        err = (
            result.get("error_description")
            or result.get("error")
            or "unknown error"
        )
        raise BootstrapAuthError(
            f"sign-in did not produce an access token: {err}"
        )
    claims = result.get("id_token_claims") or {}
    tenant_id = str(claims.get("tid") or "")
    upn = str(
        claims.get("preferred_username")
        or claims.get("upn")
        or claims.get("email")
        or ""
    )
    if not tenant_id:
        raise BootstrapAuthError(
            "id_token did not include a tenant ('tid') claim"
        )
    accounts = app.get_accounts()
    account: dict[str, Any] = accounts[0] if accounts else {}
    return BootstrapAuthResult(
        access_token=str(result["access_token"]),
        tenant_id=tenant_id,
        user_principal_name=upn,
        account=account,
        expires_in=int(result.get("expires_in", 3600)),
    )


async def acquire_bootstrap_token(
    tenant_id: str | None = None,
    timeout_s: int = 300,
) -> BootstrapAuthResult:
    """Sign in via auth-code-PKCE with a localhost redirect listener.

    Opens the operator's default browser and blocks until the redirect comes
    back to ``http://localhost:<port>`` or ``timeout_s`` elapses.
    """
    return await asyncio.to_thread(_do_auth_code_flow, tenant_id, timeout_s)


async def acquire_bootstrap_token_device_code(
    tenant_id: str | None = None,
    timeout_s: int = 600,
    on_prompt: Any | None = None,
) -> BootstrapAuthResult:
    """Fallback flow: MSAL device code, for environments where the localhost
    listener cannot bind or the default browser is unavailable.

    ``on_prompt(flow_dict)`` is invoked once the user_code is known so the UI
    can render the code and verification URL to the operator.
    """
    return await asyncio.to_thread(
        _do_device_code_flow, tenant_id, timeout_s, on_prompt
    )
