"""Pure helpers for the First-Run Setup wizard (Phase 4c).

Lives outside the page module so unit tests don't trigger top-level Streamlit
script execution. The page imports from here and orchestrates the UI; this
module knows nothing about Streamlit.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from m365_mcp_scanner.auth import doctor
from m365_mcp_scanner.auth.doctor import CheckResult
from m365_mcp_scanner.auth.msal_bootstrap import (
    BootstrapAuthError,
    BootstrapAuthResult,
    BootstrapAuthTimeout,
    acquire_bootstrap_token,
    acquire_bootstrap_token_device_code,
)
from m365_mcp_scanner.auth.msal_broker import AppOnlyTokenProvider
from m365_mcp_scanner.clients.power_platform_admin import PowerPlatformAdminClient
from m365_mcp_scanner.config import Settings
from m365_mcp_scanner.provisioning import (
    ProvisionError,
    ProvisionResult,
    provision_scanner_app,
)
from m365_mcp_scanner.ui.runners import stream_subprocess


@dataclass(frozen=True)
class CliDetection:
    status: Literal["ok", "not_on_path", "found_but_failed"]
    path: str | None
    stdout: str
    stderr: str
    error: str | None


def detect_cli(
    cmd: str,
    *,
    version_args: tuple[str, ...] = ("--version",),
    timeout: float = 30.0,
) -> CliDetection:
    """Two-step CLI detection: shutil.which, then subprocess invocation.

    shutil.which honors PATHEXT on Windows, so az.cmd / az.bat resolve correctly
    where bare-name subprocess resolution may miss them.
    """
    resolved = shutil.which(cmd)
    if resolved is None:
        return CliDetection(
            status="not_on_path",
            path=None,
            stdout="",
            stderr="",
            error=(
                f"{cmd} binary not found on PATH. Confirm it's installed and "
                f"that the directory containing the executable is on PATH."
            ),
        )
    try:
        proc = subprocess.run(
            [resolved, *version_args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CliDetection(
            status="found_but_failed",
            path=resolved,
            stdout="",
            stderr=str(exc),
            error=f"{cmd} found at {resolved} but failed to run — {exc}",
        )
    if proc.returncode != 0:
        return CliDetection(
            status="found_but_failed",
            path=resolved,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            error=(
                f"{cmd} found at {resolved} but failed to run — "
                f"output: {(proc.stderr or proc.stdout).strip()}"
            ),
        )
    return CliDetection(
        status="ok",
        path=resolved,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        error=None,
    )


APP_NAME_RE = re.compile(r"^[A-Za-z0-9 _-]{1,64}$")
GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
ENV_ID_RE = re.compile(
    r"^(?:Default-)?"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

ADMIN_CENTER_TMPL = (
    "https://admin.powerplatform.microsoft.com/manage/environments/"
    "{env_id}/appusers"
)

def validate_app_name(name: str) -> bool:
    return bool(APP_NAME_RE.match(name))


def validate_tenant_id(value: str) -> bool:
    return bool(GUID_RE.match(value))


def validate_env_id(value: str) -> bool:
    return bool(ENV_ID_RE.match(value))


def admin_center_deep_link(env_id: str) -> str | None:
    """Build the admin-center deep link for an environment, or None if invalid."""
    if not validate_env_id(env_id):
        return None
    return ADMIN_CENTER_TMPL.format(env_id=env_id)


def write_config_toml(
    *,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    data_dir: Path,
) -> Path:
    """Write a minimal config.toml that pydantic-settings can load."""
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = data_dir / "config.toml"
    body = (
        f'tenant_id = "{tenant_id}"\n'
        f'client_id = "{client_id}"\n'
        f'client_secret = "{client_secret}"\n'
    )
    cfg.write_text(body, encoding="utf-8")
    try:
        os.chmod(cfg, 0o600)
    except OSError:
        # Best effort on Windows filesystems that may not honor chmod.
        pass
    return cfg


def bootstrap_sign_in(
    tenant_id: str | None = None, timeout_s: int = 300
) -> BootstrapAuthResult:
    """Sync wrapper around :func:`acquire_bootstrap_token` for Streamlit."""
    return asyncio.run(acquire_bootstrap_token(tenant_id, timeout_s=timeout_s))


def bootstrap_sign_in_device_code(
    on_prompt: Any,
    tenant_id: str | None = None,
    timeout_s: int = 600,
) -> BootstrapAuthResult:
    """Sync wrapper around device-code fallback. ``on_prompt`` is called once
    the user code is known; the wizard renders it to the operator."""
    return asyncio.run(
        acquire_bootstrap_token_device_code(
            tenant_id, timeout_s=timeout_s, on_prompt=on_prompt
        )
    )


def run_provisioning(
    bootstrap_token: str,
    bootstrap_account: dict[str, Any],
    tenant_id: str,
    app_name: str,
    *,
    progress_callback: Any = None,
    data_dir: Path | None = None,
) -> ProvisionResult:
    """Sync wrapper that runs the async provisioner and writes config.toml.

    Persists ``client_id``/``client_secret``/``tenant_id`` to
    ``config.toml`` (mode 600) on success so subsequent doctor runs pick up
    the new credentials with no additional plumbing.
    """
    result = asyncio.run(
        provision_scanner_app(
            bootstrap_token,
            bootstrap_account,
            tenant_id,
            app_name,
            progress_callback=progress_callback,
        )
    )
    target_dir = data_dir or (Path.home() / ".m365-mcp-scanner")
    write_config_toml(
        tenant_id=tenant_id,
        client_id=result.client_id,
        client_secret=result.client_secret,
        data_dir=target_dir,
    )
    return result


_PWSH_REGISTER_SCRIPT = (
    "Import-Module Microsoft.PowerApps.Administration.PowerShell "
    "-ErrorAction Stop; "
    "Add-PowerAppsAccount; "
    "New-PowerAppManagementApp -ApplicationId $env:MCP_APP_ID"
)

_PWSH_REGISTER_SCRIPT_SKIP_SIGNIN = (
    "Import-Module Microsoft.PowerApps.Administration.PowerShell "
    "-ErrorAction Stop; "
    "New-PowerAppManagementApp -ApplicationId $env:MCP_APP_ID"
)


def run_pp_management_registration(
    app_id: str,
    timeout_s: int = 300,
    *,
    skip_signin: bool = False,
) -> Iterator[tuple[str, int | None]]:
    """Stream pwsh ``New-PowerAppManagementApp`` output.

    App ID is passed via the ``MCP_APP_ID`` env var and read inside pwsh as
    ``$env:MCP_APP_ID`` so it cannot be shell-interpolated.

    When ``skip_signin`` is True (a successful prewarm has populated the
    PowerApps session cache), ``Add-PowerAppsAccount`` is omitted from the
    inline script — registration runs against the cached session.
    """
    env = {**os.environ, "MCP_APP_ID": app_id}
    script = (
        _PWSH_REGISTER_SCRIPT_SKIP_SIGNIN
        if skip_signin
        else _PWSH_REGISTER_SCRIPT
    )
    cmd = [
        "pwsh",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
    ]
    yield from stream_subprocess(cmd, env=env, timeout_s=timeout_s)


PREWARM_STATUS_PATH = Path.home() / ".m365-mcp-scanner" / ".prewarm-status"

_PWSH_PREWARM_SCRIPT = (
    "Import-Module Microsoft.PowerApps.Administration.PowerShell "
    "-ErrorAction Stop; Add-PowerAppsAccount"
)


def read_prewarm_status(path: Path = PREWARM_STATUS_PATH) -> str:
    """Return prewarm status. Missing/malformed file ≡ ``"not_started"``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        status = data["status"]
        if status in ("running", "succeeded", "failed"):
            return str(status)
        return "not_started"
    except (OSError, ValueError, KeyError):
        return "not_started"


def _write_prewarm_status(
    status: str, path: Path = PREWARM_STATUS_PATH
) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "status": status,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def prewarm_powerapps_account(
    timeout_s: int = 300,
    status_path: Path = PREWARM_STATUS_PATH,
) -> Iterator[tuple[str, int | None]]:
    """Run ``Add-PowerAppsAccount`` via pwsh, writing prewarm status to disk.

    Yields ``stream_subprocess`` output. Caller should not block on this; it
    is a fire-and-forget warm-up. Failures (pwsh missing, module missing,
    sign-in cancelled, non-zero exit) are non-fatal — Step 4 retries the
    call as part of its normal flow.
    """
    _write_prewarm_status("running", status_path)
    rc: int | None = None
    try:
        for line, code in stream_subprocess(
            [
                "pwsh",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _PWSH_PREWARM_SCRIPT,
            ],
            timeout_s=timeout_s,
        ):
            if code is not None:
                rc = code
            yield line, code
    except (FileNotFoundError, OSError):
        _write_prewarm_status("failed", status_path)
        return
    _write_prewarm_status(
        "succeeded" if rc == 0 else "failed", status_path
    )


async def _collect_envs(settings: Settings) -> list[dict[str, Any]]:
    provider = AppOnlyTokenProvider(
        tenant_id=settings.tenant_id,
        client_id=settings.client_id,
        client_secret=settings.client_secret.get_secret_value(),
    )
    pp = PowerPlatformAdminClient(token_provider=provider)
    try:
        return [e async for e in pp.list_environments()]
    finally:
        await pp.aclose()


def list_environments_sync(settings: Settings) -> list[dict[str, Any]]:
    """Synchronous wrapper around PP admin env enumeration for Streamlit."""
    return asyncio.run(_collect_envs(settings))


async def check_all_envs_dataverse(
    settings: Settings, envs: list[dict[str, Any]]
) -> list[CheckResult | BaseException]:
    """Run :func:`doctor.check_dataverse` for every env concurrently.

    Results are returned in the same order as ``envs`` (``asyncio.gather``
    preserves input order). Per-env failures are surfaced as the exception
    object in place of a ``CheckResult`` so one bad env can't blank the row
    for the others — the caller decides how to map exceptions to UI state.
    """
    return await asyncio.gather(
        *(doctor.check_dataverse(settings, env) for env in envs),
        return_exceptions=True,
    )


def verify_pp_registration_output(
    stdout_lines: list[str],
    app_id: str,
) -> bool:
    """Return True iff stdout shows the ``applicationId`` column with the
    exact ``app_id`` appearing on the header line itself or within two lines
    below it.

    PowerShell's ``Format-Table`` output for ``New-PowerAppManagementApp``
    contains a header line with column names (one of which is
    ``applicationId``) and a data row with the appId GUID. False positives
    are worse than false negatives — the operator can always re-run or fall
    back to manual verification.
    """
    if not app_id:
        return False
    for idx, line in enumerate(stdout_lines):
        if "applicationId" not in line:
            continue
        window = stdout_lines[idx : idx + 3]
        for candidate in window:
            if app_id in candidate:
                return True
    return False
