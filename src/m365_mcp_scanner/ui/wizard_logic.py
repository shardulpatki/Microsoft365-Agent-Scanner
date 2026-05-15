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
from m365_mcp_scanner.auth.msal_broker import AppOnlyTokenProvider
from m365_mcp_scanner.clients.power_platform_admin import PowerPlatformAdminClient
from m365_mcp_scanner.config import Settings
from m365_mcp_scanner.ui.runners import stream_subprocess

MIN_AZ_VERSION = (2, 50, 0)


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


def az_account_tenant(*, timeout: float = 30.0) -> str | None:
    """Return the active Azure CLI tenant ID, or None if unavailable.

    Resolves ``az`` via ``shutil.which`` so Windows ``PATHEXT`` (``az.cmd``)
    is honored — bare-name subprocess resolution misses ``.cmd``/``.bat``.
    """
    az_path = shutil.which("az")
    if az_path is None:
        return None
    try:
        proc = subprocess.run(
            [az_path, "account", "show", "--query", "tenantId", "-o", "tsv"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    tenant = proc.stdout.strip()
    return tenant or None


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

_REQUIRED_OUTPUT_FIELDS = (
    "client_id",
    "client_secret",
    "tenant_id",
    "app_object_id",
    "admin_consent_granted",
    "completed_at",
)


def validate_app_name(name: str) -> bool:
    return bool(APP_NAME_RE.match(name))


def validate_tenant_id(value: str) -> bool:
    return bool(GUID_RE.match(value))


def validate_env_id(value: str) -> bool:
    return bool(ENV_ID_RE.match(value))


_STEP_MARKER_RE = re.compile(r"\[(\d+)/7\]")


def parse_step_marker(line: str) -> int | None:
    """Return N from a ``[N/7]`` substring in line, clamped to ``[0, 7]``.

    Returns ``None`` if no marker is present. Defensive against unexpected
    lines so the progress bar update path can't crash the wizard.
    """
    m = _STEP_MARKER_RE.search(line)
    if not m:
        return None
    n = int(m.group(1))
    return max(0, min(7, n))


def parse_az_version(stdout: str) -> tuple[int, int, int] | None:
    """Parse ``azure-cli  2.50.0`` from ``az --version`` stdout."""
    for line in stdout.splitlines():
        m = re.match(r"\s*azure-cli\s+([0-9]+)\.([0-9]+)\.([0-9]+)", line)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


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


def ingest_setup_output(
    output_path: Path, data_dir: Path
) -> tuple[str, str]:
    """Read .setup-output.json, write config.toml, delete the source.

    Returns ``(client_id, app_object_id)``. Raises ``ValueError`` on schema
    mismatch, ``json.JSONDecodeError`` on malformed JSON, ``OSError`` on file
    issues.
    """
    data = json.loads(output_path.read_text(encoding="utf-8"))
    for key in _REQUIRED_OUTPUT_FIELDS:
        if key not in data:
            raise ValueError(f".setup-output.json missing required key: {key}")
    write_config_toml(
        tenant_id=str(data["tenant_id"]),
        client_id=str(data["client_id"]),
        client_secret=str(data["client_secret"]),
        data_dir=data_dir,
    )
    output_path.unlink()
    return str(data["client_id"]), str(data["app_object_id"])


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
