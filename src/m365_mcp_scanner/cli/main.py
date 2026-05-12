from __future__ import annotations

import asyncio
import logging
import sys
from enum import StrEnum
from pathlib import Path

import typer

from m365_mcp_scanner.auth import AppOnlyTokenProvider, DelegatedTokenProvider
from m365_mcp_scanner.auth.msal_broker import AuthError
from m365_mcp_scanner.clients.api_recorder import ApiCallRecorder
from m365_mcp_scanner.clients.graph import GraphClient
from m365_mcp_scanner.clients.power_platform_admin import PowerPlatformAdminClient
from m365_mcp_scanner.config import Settings
from m365_mcp_scanner.orchestrator import run_pipeline
from m365_mcp_scanner.reporting import (
    dump_list,
    dump_scan_document,
    err_console,
    render_scans_table,
    render_servers_table,
    render_summary,
    write_markdown_report,
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
    """Run device-code flow and persist a delegated session for Phase 3 surfaces."""

    async def _run() -> int:
        try:
            settings = Settings()
        except Exception as exc:  # noqa: BLE001
            err_console.print(f"[red]config load failed:[/] {exc}")
            return 1
        try:
            provider = DelegatedTokenProvider(
                tenant_id=settings.tenant_id,
                client_id=settings.client_id,
            )
        except AuthError as exc:
            err_console.print(f"[red]delegated auth misconfigured:[/] {exc}")
            return 1

        if provider.is_logged_in():
            try:
                await provider.get_token()
                upn = provider.account_username() or "(unknown user)"
                err_console.print(f"[green]already logged in as[/] {upn}")
                return 0
            except AuthError:
                err_console.print(
                    "[yellow]cached session exists but silent refresh failed; "
                    "re-running device flow…[/]"
                )

        def _on_prompt(flow: dict[str, object]) -> None:
            message = flow.get("message")
            if isinstance(message, str) and message:
                err_console.print(f"[cyan]{message}[/]")
            else:
                user_code = flow.get("user_code")
                verification = flow.get("verification_uri")
                err_console.print(
                    f"[cyan]Open[/] {verification} [cyan]and enter code[/] {user_code}"
                )

        try:
            await provider.login(on_prompt=_on_prompt)
        except AuthError as exc:
            err_console.print(f"[red]login failed:[/] {exc}")
            return 1
        upn = provider.account_username() or "(unknown user)"
        err_console.print(f"[green]logged in as[/] {upn}")
        return 0

    raise typer.Exit(asyncio.run(_run()))


@app.command()
def logout() -> None:
    """Clear the cached delegated session (removes the encrypted cache file)."""
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001
        err_console.print(f"[red]config load failed:[/] {exc}")
        raise typer.Exit(1)
    try:
        provider = DelegatedTokenProvider(
            tenant_id=settings.tenant_id,
            client_id=settings.client_id,
        )
    except AuthError as exc:
        err_console.print(f"[red]delegated auth misconfigured:[/] {exc}")
        raise typer.Exit(1) from exc
    provider.clear_cache()
    err_console.print("[green]logged out[/] (delegated session cleared)")


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
            ok_graph, msg_graph = await graph.doctor_ping()

        pp = PowerPlatformAdminClient(token_provider=provider)
        try:
            ok_pp, msg_pp = await pp.doctor_ping()
        finally:
            await pp.aclose()

        if ok_graph:
            err_console.print(f"[green]OK[/] Graph: {msg_graph}")
        else:
            err_console.print(f"[red]FAIL[/] Graph: {msg_graph}")
        if ok_pp:
            err_console.print(f"[green]OK[/] {msg_pp}")
        else:
            err_console.print(f"[red]FAIL[/] {msg_pp}")

        # Phase 3: delegated session is optional. Report status only.
        try:
            delegated = DelegatedTokenProvider(
                tenant_id=settings.tenant_id, client_id=settings.client_id
            )
        except AuthError as exc:
            err_console.print(
                f"[yellow]Delegated session:[/] not available ({exc})"
            )
        else:
            if delegated.is_logged_in():
                upn = delegated.account_username() or "(unknown user)"
                err_console.print(
                    f"[green]OK[/] Delegated session: {upn}"
                )
            else:
                err_console.print(
                    "[yellow]Delegated session:[/] not logged in "
                    "(run `mcp-scan login` to enable Phase 3 surfaces)"
                )
        return 0 if (ok_graph and ok_pp) else 1

    raise typer.Exit(asyncio.run(_run()))


@app.command()
def run(
    scope: str = typer.Option(
        "synced_copilot_connectors,first_party_mcp,custom_connectors,"
        "copilot_studio,declarative_agents_packages,declarative_agents_teamsapp",
        "--scope",
        help=(
            "Comma-separated surfaces. App-only: synced_copilot_connectors, "
            "first_party_mcp, custom_connectors, copilot_studio. Delegated "
            "(requires `mcp-scan login`): declarative_agents_packages, "
            "declarative_agents_teamsapp. Aliases: copilot_connectors -> "
            "synced_copilot_connectors; declarative -> both Phase 3 surfaces."
        ),
    ),
    fmt: OutputFormat = typer.Option(OutputFormat.table, "--format"),
    out: Path | None = typer.Option(None, "--out", help="Write ScanDocument JSON to this path"),
    md: bool = typer.Option(
        True,
        "--md/--no-md",
        help="Also emit a Markdown report next to the JSON capturing every API call and error.",
    ),
) -> None:
    """Run a scan."""
    settings = Settings()
    scopes = [s.strip() for s in scope.split(",") if s.strip()]

    async def _exec() -> int:
        recorder = ApiCallRecorder()

        if fmt is OutputFormat.json and out is None:
            # JSON-to-stdout mode: skip persistence, no lock needed.
            doc = await run_pipeline(scopes, settings, recorder=recorder)
            write_stdout(dump_scan_document(doc))
            return 0

        ensure_data_dir(settings.data_dir)
        try:
            with acquire_scan_lock(settings.data_dir):
                doc = await run_pipeline(scopes, settings, recorder=recorder)
                target = out if out is not None else scan_dir(settings.data_dir) / scan_filename(
                    doc.started_at, doc.scan_id
                )
                write_scan_document(doc, target)
                if out is None:
                    update_latest_pointer(target, settings.data_dir)
                md_path: Path | None = None
                if md:
                    md_path = target.with_suffix(".md")
                    write_markdown_report(doc, recorder.calls, md_path)
        except ScanLockedError as exc:
            err_console.print(f"[red]{exc}[/]")
            return 1

        render_summary(doc)
        write_stdout(str(target))
        if md and md_path is not None:
            err_console.print(
                f"[green]md report:[/] {md_path} ({len(recorder.calls)} API calls)"
            )
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
