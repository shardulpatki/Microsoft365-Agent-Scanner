from __future__ import annotations

import streamlit as st

from m365_mcp_scanner.ui.components import (
    CATEGORIES,
    categorize,
    render_error_section,
    render_scan_picker,
    render_uncategorized_alert,
)
from m365_mcp_scanner.ui.state import init_session_state

init_session_state()

st.title("Errors")

scan = render_scan_picker(key="errors_scan_picker")
if scan is None:
    st.stop()

errors = list(scan.errors)
render_uncategorized_alert(errors)

if not errors:
    st.success("No errors in this scan.")
    st.stop()

buckets = categorize(errors)
rendered = 0
for code in CATEGORIES:
    bucket = buckets.get(code, [])
    if bucket:
        render_error_section(code, bucket)
        rendered += 1

if rendered == 0:
    st.info(
        "Scan has errors, but none match the four documented categories. "
        "See the alert above."
    )
