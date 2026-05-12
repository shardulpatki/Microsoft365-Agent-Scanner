from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from m365_mcp_scanner.clients.api_recorder import ApiCall
from m365_mcp_scanner.models import ScanDocument


def _esc(s: str | None) -> str:
    if not s:
        return ""
    return s.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _truncate(s: str | None, n: int) -> str:
    if not s:
        return ""
    s = s.replace("\r", " ").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def write_markdown_report(
    doc: ScanDocument,
    api_calls: Sequence[ApiCall],
    out_path: Path,
) -> None:
    lines: list[str] = []
    a = lines.append

    a("# Microsoft 365 Agent Scanner — Full Scan Report")
    a("")

    a("## Scan")
    a("")
    a("| field | value |")
    a("|---|---|")
    a(f"| scan_id | `{doc.scan_id}` |")
    a(f"| tenant_id | `{doc.tenant_id}` |")
    a(f"| status | {doc.status.value} |")
    a(f"| started_at | {doc.started_at.isoformat()} |")
    a(f"| finished_at | {doc.finished_at.isoformat() if doc.finished_at else ''} |")
    a(f"| scope | {', '.join(doc.scope)} |")
    a("")

    s = doc.summary
    sev = s.findings_by_severity
    a("## Summary")
    a("")
    a("| metric | value |")
    a("|---|---:|")
    for k, v in [
        ("agents_total", s.agents_total),
        ("agents_with_mcp", s.agents_with_mcp),
        ("mcp_servers_total", s.mcp_servers_total),
        ("mcp_servers_first_party", s.mcp_servers_first_party),
        ("mcp_servers_external", s.mcp_servers_external),
        ("findings_total", s.findings_total),
        ("findings.critical", sev.critical),
        ("findings.high", sev.high),
        ("findings.medium", sev.medium),
        ("findings.low", sev.low),
        ("findings.info", sev.info),
    ]:
        a(f"| {k} | {v} |")
    a("")

    a("## Per-stage results")
    a("")
    for name, stage in doc.stages.items():
        a(f"### {name}")
        a("")
        a(f"- started_at: {stage.started_at.isoformat() if stage.started_at else '-'}")
        a(f"- finished_at: {stage.finished_at.isoformat() if stage.finished_at else '-'}")
        a(f"- duration_ms: {stage.duration_ms if stage.duration_ms is not None else '-'}")
        a(f"- skipped: {stage.skipped}")
        a(f"- reason: {stage.reason or '-'}")
        a(f"- errors: {len(stage.errors)}")
        for e in stage.errors:
            a(
                f"  - surface=`{e.get('surface')}` code=`{e.get('code')}` "
                f"message={_esc(str(e.get('message')))}"
            )
        a("")

    a(f"## Errors ({len(doc.errors)})")
    a("")
    if not doc.errors:
        a("_None_")
    else:
        a("| timestamp | stage | surface | code | message |")
        a("|---|---|---|---|---|")
        for err in doc.errors:
            a(
                f"| {err.timestamp.isoformat()} | {err.stage} | "
                f"{_esc(err.surface)} | {_esc(err.code)} | {_esc(_truncate(err.message, 800))} |"
            )
    a("")

    a(f"## API calls ({len(api_calls)})")
    a("")
    if not api_calls:
        a("_No API calls recorded._")
    else:
        a("| # | timestamp | client | method | url | status | ms | attempts | error |")
        a("|---:|---|---|---|---|---:|---:|---:|---|")
        for i, call in enumerate(api_calls, start=1):
            a(
                f"| {i} | {call.timestamp.isoformat()} | {call.client} | {call.method} | "
                f"{_esc(call.url)} | "
                f"{'' if call.status is None else call.status} | "
                f"{call.elapsed_ms:.1f} | {call.attempts} | "
                f"{_esc(_truncate(call.error, 300))} |"
            )
    a("")

    a("## MCP servers discovered")
    a("")
    if not doc.mcp_servers:
        a("_None_")
    else:
        a("| server_id | url | transport | first_party | discovered_via |")
        a("|---|---|---|---|---|")
        for srv in doc.mcp_servers:
            transport = srv.transport.value if hasattr(srv.transport, "value") else str(srv.transport)
            a(
                f"| `{srv.server_id}` | {_esc(srv.url)} | {transport} | "
                f"{srv.is_first_party} | {srv.discovered_via} |"
            )
    a("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
