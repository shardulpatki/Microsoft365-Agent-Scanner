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
import threading
from collections.abc import Iterator
from pathlib import Path


def stream_subprocess(
    cmd: list[str],
    cwd: Path | None = None,
    *,
    env: dict[str, str] | None = None,
    timeout_s: float | None = None,
) -> Iterator[tuple[str, int | None]]:
    """Spawn ``cmd`` and yield (line, returncode).

    ``returncode`` is ``None`` while the process is still producing output and
    is set to the actual exit code on the final tuple.

    ``env`` is forwarded to ``Popen`` when not None. Callers are responsible
    for merging with ``os.environ`` if they want the existing environment.

    ``timeout_s`` enforces a wall-clock budget via a watchdog thread.
    ``proc.wait(timeout=...)`` cannot be used here because the read loop
    blocks on ``proc.stdout``, which never returns while the cmdlet is
    waiting on browser auth. On timeout the watchdog kills the process; the
    stdout iterator then closes and a final ``("", returncode)`` is yielded
    (typically a non-zero exit). A synthetic timeout marker line is emitted
    immediately before the final tuple so callers can surface a clear error.
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
        env=env,
    )
    assert proc.stdout is not None

    timed_out = threading.Event()
    watchdog: threading.Timer | None = None
    if timeout_s is not None:
        def _kill_on_timeout() -> None:
            timed_out.set()
            try:
                proc.kill()
            except OSError:
                pass

        watchdog = threading.Timer(timeout_s, _kill_on_timeout)
        watchdog.daemon = True
        watchdog.start()

    try:
        for line in proc.stdout:
            yield line.rstrip("\r\n"), None
    finally:
        if watchdog is not None:
            watchdog.cancel()
        proc.wait()

    if timed_out.is_set():
        yield (
            f"[timeout] process killed after {timeout_s:.0f}s",
            None,
        )
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
