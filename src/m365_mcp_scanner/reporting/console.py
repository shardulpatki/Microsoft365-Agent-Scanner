from __future__ import annotations

from collections.abc import Iterable

from rich.console import Console
from rich.table import Table

from m365_mcp_scanner.models import NormalizedMcpServer, ScanDocument
from m365_mcp_scanner.storage import ScanSummaryRow

err_console = Console(stderr=True)
out_console = Console()


def render_summary(doc: ScanDocument, console: Console = err_console) -> None:
    table = Table(title=f"Scan {doc.scan_id} ({doc.status.value})", show_lines=False)
    table.add_column("metric")
    table.add_column("value", justify="right")
    s = doc.summary
    table.add_row("scope", ", ".join(doc.scope))
    table.add_row("mcp servers (total)", str(s.mcp_servers_total))
    table.add_row("mcp servers (first-party)", str(s.mcp_servers_first_party))
    table.add_row("mcp servers (external)", str(s.mcp_servers_external))
    table.add_row("agents", str(s.agents_total))
    table.add_row("findings", str(s.findings_total))
    table.add_row("errors", str(len(doc.errors)))
    discover = doc.stages.get("discover")
    if discover and discover.duration_ms is not None:
        table.add_row("discover duration", f"{discover.duration_ms} ms")
    console.print(table)


def render_servers_table(
    servers: Iterable[NormalizedMcpServer], console: Console = out_console
) -> None:
    table = Table(title="MCP servers")
    table.add_column("server_id")
    table.add_column("transport")
    table.add_column("auth")
    table.add_column("first-party", justify="center")
    table.add_column("url / identity")
    table.add_column("discovered_via")
    for srv in servers:
        table.add_row(
            srv.server_id,
            srv.transport.value,
            srv.auth_type.value,
            "✓" if srv.is_first_party else "",
            srv.url,
            srv.discovered_via,
        )
    console.print(table)


def render_scans_table(rows: Iterable[ScanSummaryRow], console: Console = out_console) -> None:
    table = Table(title="Scans")
    table.add_column("scan_id")
    table.add_column("started_at")
    table.add_column("status")
    table.add_column("servers", justify="right")
    table.add_column("findings", justify="right")
    table.add_column("file")
    for row in rows:
        table.add_row(
            row.scan_id,
            row.started_at,
            row.status,
            str(row.mcp_servers_total),
            str(row.findings_total),
            row.path.name,
        )
    console.print(table)
