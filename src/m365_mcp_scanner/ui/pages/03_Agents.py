from __future__ import annotations

import json
from collections import Counter

import pandas as pd
import streamlit as st

from m365_mcp_scanner.ui.components import render_scan_picker
from m365_mcp_scanner.ui.state import init_session_state

init_session_state()

st.title("Agents")

scan = render_scan_picker(key="agents_scan_picker")
if scan is None:
    st.stop()

edge_counts: Counter[str] = Counter(e.agent_id for e in scan.consumption_edges)

rows = [
    {
        "display_name": a.display_name,
        "agent_id": a.agent_id,
        "path": str(a.path),
        "environment_id": a.environment_id or "",
        "owner_id": a.owner_id or "",
        "published": a.published,
        "mcp_server_count": edge_counts.get(a.agent_id, 0),
    }
    for a in scan.agents
]

df = pd.DataFrame(rows)
if df.empty:
    st.info("No agents in this scan.")
    st.stop()

st.caption(f"{len(df)} agents")

event = st.dataframe(
    df,
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
)

st.download_button(
    "Export CSV",
    data=df.to_csv(index=False).encode("utf-8"),
    file_name=f"{scan.scan_id[:8]}_agents.csv",
    mime="text/csv",
)

selected_rows = (
    event.selection.rows if hasattr(event, "selection") else []  # type: ignore[union-attr]
)
if selected_rows:
    idx = selected_rows[0]
    agent = scan.agents[idx]
    st.divider()
    st.subheader(agent.display_name)

    server_ids = [
        e.server_id for e in scan.consumption_edges if e.agent_id == agent.agent_id
    ]
    if server_ids:
        servers = {s.server_id: s for s in scan.mcp_servers}
        attached = [
            {
                "server_id": sid,
                "url": servers[sid].url if sid in servers else "(unknown)",
                "transport": str(servers[sid].transport) if sid in servers else "",
            }
            for sid in server_ids
        ]
        st.write("**Attached MCP servers**")
        st.dataframe(pd.DataFrame(attached), use_container_width=True, hide_index=True)
    else:
        st.caption("No attached MCP servers.")

    related_errors = [
        e for e in scan.errors if e.surface and agent.agent_id in (e.surface or "")
    ]
    if related_errors:
        st.write("**Related errors**")
        for err in related_errors:
            st.code(f"[{err.code}] {err.message}", language="text")

    st.write("**source_ref**")
    st.code(json.dumps(agent.source_ref, indent=2, default=str), language="json")
