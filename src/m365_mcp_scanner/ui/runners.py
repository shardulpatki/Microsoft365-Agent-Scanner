"""Subprocess wrappers used by the UI's Run Scan page (and, later, the wizard).

The streaming generator yields ``(line, None)`` for each stdout line and a
final ``("", returncode)`` tuple once the process exits. All ``mcp-scan``
invocations go through ``sys.executable -m m365_mcp_scanner.cli.main`` so the
right interpreter is always used on Windows where ``PATH`` may not include the
console-script.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path


def stream_subprocess(
    cmd: list[str], cwd: Path | None = None
) -> Iterator[tuple[str, int | None]]:
    """Spawn ``cmd`` and yield (line, returncode).

    ``returncode`` is ``None`` while the process is still producing output and
    is set to the actual exit code on the final tuple.
    """
    resolved = shutil.which(cmd[0])
    if resolved is None:
        raise FileNotFoundError(
            f"{cmd[0]!r} not found on PATH. Install it or fix PATH and retry."
        )
    resolved_cmd = [resolved, *cmd[1:]]
    proc = subprocess.Popen(
        resolved_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(cwd) if cwd is not None else None,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            yield line.rstrip("\r\n"), None
    finally:
        proc.wait()
    yield "", proc.returncode


def run_scan_cmd(
    scope: list[str] | None = None,
    *,
    probe: bool = False,
    out: Path | None = None,
) -> list[str]:
    """Build the argv for ``mcp-scan run`` using the current interpreter."""
    cmd = [sys.executable, "-m", "m365_mcp_scanner.cli.main", "run"]
    if scope:
        cmd += ["--scope", ",".join(scope)]
    if probe:
        cmd.append("--probe")
    if out is not None:
        cmd += ["--out", str(out)]
    return cmd
