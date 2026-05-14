"""Pure helpers for the First-Run Setup wizard (Phase 4c).

Lives outside the page module so unit tests don't trigger top-level Streamlit
script execution. The page imports from here and orchestrates the UI; this
module knows nothing about Streamlit.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
