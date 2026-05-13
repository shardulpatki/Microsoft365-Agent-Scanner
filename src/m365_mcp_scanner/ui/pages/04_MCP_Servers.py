from __future__ import annotations

import json
from collections import Counter

import pandas as pd
import streamlit as st

from m365_mcp_scanner.ui.components import render_scan_picker
from m365_mcp_scanner.ui.state import init_session_state

init_session_state()

st.title("MCP Servers")

scan = render_scan_picker(key="servers_scan_picker")
if scan is None:
    st.stop()

edge_counts: Counter[str] = Counter(e.server_id for e in scan.consumption_edges)

rows = [
    {
        "url": s.url,
        "transport": str(s.transport),
        "auth_type": str(s.auth_type),
        "is_first_party": s.is_first_party,
        "external_domain": s.external_domain,
        "consumer_count": edge_counts.get(s.server_id, 0),
    }
    for s in scan.mcp_servers
]
df = pd.DataFrame(rows)

if df.empty:
    st.info("No MCP servers in this scan.")
    st.stop()

st.caption(f"{len(df)} servers")

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
    file_name=f"{scan.scan_id[:8]}_servers.csv",
    mime="text/csv",
)

selected_rows = (
    event.selection.rows if hasattr(event, "selection") else []  # type: ignore[union-attr]
)
if selected_rows:
    idx = selected_rows[0]
    server = scan.mcp_servers[idx]
    st.divider()
    st.subheader(server.url)

    consuming = [
        e.agent_id for e in scan.consumption_edges if e.server_id == server.server_id
    ]
    if consuming:
        agents = {a.agent_id: a for a in scan.agents}
        rows2 = [
            {
                "agent_id": aid,
                "display_name": agents[aid].display_name if aid in agents else "(unknown)",
            }
            for aid in consuming
        ]
        st.write("**Consuming agents**")
        st.dataframe(pd.DataFrame(rows2), use_container_width=True, hide_index=True)
    else:
        st.caption("No consuming agents.")

    st.write("**Advertised tools**")
    if server.advertised_tools is None:
        st.caption("Not probed.")
    elif not server.advertised_tools:
        st.caption("No tools advertised.")
    else:
        for t in server.advertised_tools:
            st.write(f"• {t}")

    st.write("**Evidence**")
    st.code(json.dumps(server.evidence, indent=2, default=str), language="json")
