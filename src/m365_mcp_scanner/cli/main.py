from __future__ import annotations

import asyncio
import logging
import sys
from enum import StrEnum
from pathlib import Path

import typer

from m365_mcp_scanner.auth import AppOnlyTokenProvider
from m365_mcp_scanner.auth.msal_broker import AuthError
from m365_mcp_scanner.clients.graph import GraphClient
from m365_mcp_scanner.config import Settings
from m365_mcp_scanner.orchestrator import run_pipeline
from m365_mcp_scanner.reporting import (
    dump_list,
    dump_scan_document,
    err_console,
    render_scans_table,
    render_servers_table,
    render_summary,
)
from m365_mcp_scanner.reporting.json_writer import write_stdout
from m365_mcp_scanner.storage import (
    ScanLockedError,
    acquire_scan_lock,
    ensure_data_dir,
    list_scans,
    load_scan,
    scan_dir,
    scan_filename,
    update_latest_pointer,
    write_scan_document,
)
from m365_mcp_scanner.storage.json_store import resolve_latest


class OutputFormat(StrEnum):
    table = "table"
    json = "json"


app = typer.Typer(help="M365 Copilot MCP Scanner", no_args_is_help=True)
scans_app = typer.Typer(help="Manage stored scan documents")
servers_app = typer.Typer(help="Inspect MCP servers from a scan")
app.add_typer(scans_app, name="scans")
app.add_typer(servers_app, name="servers")


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    _configure_logging(verbose)


@app.command()
def login() -> None:
    """Phase 3: device-code flow. Not yet wired."""
    err_console.print(
        "[yellow]delegated auth not yet wired (Phase 3). "
        "App-only auth via env vars works today for `mcp-scan run --scope copilot_connectors`.[/]"
    )


@app.command()
def doctor() -> None:
    """Verify env-config + Graph reachability."""

    async def _run() -> int:
        try:
            settings = Settings()
        except Exception as exc:  # noqa: BLE001
            err_console.print(f"[red]config load failed:[/] {exc}")
            return 1
        try:
            provider = AppOnlyTokenProvider(
                tenant_id=settings.tenant_id,
                client_id=settings.client_id,
                client_secret=settings.client_secret.get_secret_value(),
            )
        except AuthError as exc:
            err_console.print(f"[red]auth misconfigured:[/] {exc}")
            return 1
        async with GraphClient(provider) as graph:
            try:
                await provider.get_token()
            except AuthError as exc:
                err_console.print(f"[red]token mint failed:[/] {exc}")
                return 1
            ok, msg = await graph.doctor_ping()
        if ok:
            err_console.print(f"[green]OK[/] {msg}")
            return 0
        err_console.print(f"[red]FAIL[/] {msg}")
        return 1

    raise typer.Exit(asyncio.run(_run()))


@app.command()
def run(
    scope: str = typer.Option(
        "synced_copilot_connectors,first_party_mcp",
        "--scope",
        help=(
            "Comma-separated surfaces. Phase 1 implements: "
            "synced_copilot_connectors, first_party_mcp. "
            "Alias: copilot_connectors → synced_copilot_connectors."
        ),
    ),
    fmt: OutputFormat = typer.Option(OutputFormat.table, "--format"),
    out: Path | None = typer.Option(None, "--out", help="Write ScanDocument JSON to this path"),
) -> None:
    """Run a scan."""
    settings = Settings()
    scopes = [s.strip() for s in scope.split(",") if s.strip()]

    async def _exec() -> int:
        if fmt is OutputFormat.json and out is None:
            # JSON-to-stdout mode: skip persistence, no lock needed.
            doc = await run_pipeline(scopes, settings)
            write_stdout(dump_scan_document(doc))
            return 0

        ensure_data_dir(settings.data_dir)
        try:
            with acquire_scan_lock(settings.data_dir):
                doc = await run_pipeline(scopes, settings)
                target = out if out is not None else scan_dir(settings.data_dir) / scan_filename(
                    doc.started_at, doc.scan_id
                )
                write_scan_document(doc, target)
                if out is None:
                    update_latest_pointer(target, settings.data_dir)
        except ScanLockedError as exc:
            err_console.print(f"[red]{exc}[/]")
            return 1

        render_summary(doc)
        write_stdout(str(target))
        return 0

    raise typer.Exit(asyncio.run(_exec()))


@scans_app.command("list")
def scans_list(fmt: OutputFormat = typer.Option(OutputFormat.table, "--format")) -> None:
    settings = Settings()
    rows = list_scans(settings.data_dir)
    if fmt is OutputFormat.json:
        import json

        write_stdout(
            json.dumps(
                [
                    {
                        "scan_id": r.scan_id,
                        "started_at": r.started_at,
                        "status": r.status,
                        "mcp_servers_total": r.mcp_servers_total,
                        "findings_total": r.findings_total,
                        "file": r.path.name,
                    }
                    for r in rows
                ],
                indent=2,
            )
        )
        return
    render_scans_table(rows)


@servers_app.command("list")
def servers_list(
    scan: str | None = typer.Option(None, "--scan", help="Scan id or filename; defaults to latest"),
    fmt: OutputFormat = typer.Option(OutputFormat.table, "--format"),
) -> None:
    settings = Settings()
    path = _resolve_scan_path(settings.data_dir, scan)
    if path is None:
        err_console.print("[red]no scan found[/]")
        raise typer.Exit(1)
    doc = load_scan(path)
    if fmt is OutputFormat.json:
        write_stdout(dump_list(doc.mcp_servers))
        return
    render_servers_table(doc.mcp_servers)


def _resolve_scan_path(data_dir: Path, scan: str | None) -> Path | None:
    folder = scan_dir(data_dir)
    if scan is None:
        return resolve_latest(data_dir)
    if scan.endswith(".json"):
        candidate = folder / scan
        return candidate if candidate.exists() else None
    for path in folder.glob("*.json"):
        if path.name == "latest.json":
            continue
        if scan in path.stem:
            return path
    return None


if __name__ == "__main__":
    app()
