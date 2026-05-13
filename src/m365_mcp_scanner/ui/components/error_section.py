"""Errors page widgets: categorize and render scan errors by code."""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from m365_mcp_scanner.models.scan_document import ScanError

CATEGORIES: tuple[str, ...] = (
    "no_dataverse_access",
    "delegated_session_required",
    "tenant_not_eligible",
    "manifest_endpoint_unavailable",
)

_TITLES: dict[str, str] = {
    "no_dataverse_access": "No Dataverse access",
    "delegated_session_required": "Delegated session required",
    "tenant_not_eligible": "Tenant not eligible (licensing)",
    "manifest_endpoint_unavailable": "Manifest endpoint unavailable (Microsoft API gap)",
}

_EXPLANATIONS: dict[str, str] = {
    "no_dataverse_access": (
        "The scanner's service principal has not been added as an Application "
        "User in this Power Platform environment, or it lacks the required "
        "security role. Fix this by adding the SP in the admin center."
    ),
    "delegated_session_required": (
        "This surface requires a signed-in user (delegated auth). The cached "
        "delegated session is missing or expired."
    ),
    "tenant_not_eligible": (
        "Microsoft returned a 403 indicating the tenant is not licensed for "
        "this surface. This is a licensing decision, not a configuration gap."
    ),
    "manifest_endpoint_unavailable": (
        "Microsoft's Graph manifest endpoint returned 400/404 for this "
        "package. The endpoint is documented but inconsistently available; "
        "no scanner-side fix exists."
    ),
}

_SEVERITY: dict[str, str] = {
    "no_dataverse_access": "warning",
    "delegated_session_required": "warning",
    "tenant_not_eligible": "info",
    "manifest_endpoint_unavailable": "info",
}


def categorize(errors: list[ScanError]) -> dict[str, list[ScanError]]:
    buckets: dict[str, list[ScanError]] = {c: [] for c in CATEGORIES}
    for err in errors:
        if err.code in buckets:
            buckets[err.code].append(err)
    return buckets


def has_uncategorized(errors: list[ScanError]) -> bool:
    return any(
        (err.code is None) or err.code in ("", "unknown") for err in errors
    )


def render_uncategorized_alert(errors: list[ScanError]) -> None:
    if has_uncategorized(errors):
        st.error(
            "Regression: at least one error has `code: null` or `code: unknown`. "
            "This indicates the scanner failed to categorize the error — surface "
            "the raw scan JSON to a maintainer."
        )


def _env_id(err: ScanError) -> str | None:
    if err.surface and "/" in err.surface:
        return err.surface.split("/", 1)[1]
    return err.surface


def render_error_section(code: str, errors: list[ScanError]) -> None:
    if not errors:
        return
    sev = _SEVERITY.get(code, "warning")
    title = _TITLES.get(code, code)
    header = f"**{title}** — {len(errors)} ({sev})"
    with st.expander(header, expanded=(sev == "warning")):
        explanation = _EXPLANATIONS.get(code)
        if explanation:
            with st.expander("What this means", expanded=False):
                st.write(explanation)
        for i, err in enumerate(errors):
            cols = st.columns([3, 4, 1])
            env_id = _env_id(err)
            cols[0].caption(f"env: {env_id}" if env_id else f"stage: {err.stage}")
            cols[1].code(err.message, language="text")
            if code == "no_dataverse_access":
                if cols[2].button("Fix this", key=f"fix_{code}_{i}"):
                    _jump_to_wizard_env(env_id)
            elif code == "delegated_session_required":
                if cols[2].button("Fix this", key=f"fix_{code}_{i}"):
                    _jump_to_status_signin()
            else:
                cols[2].write("—")


def _wizard_page_path() -> Path:
    return Path(__file__).resolve().parent.parent / "pages" / "00_First_Run_Setup.py"


def _jump_to_wizard_env(env_id: str | None) -> None:
    wizard = _wizard_page_path()
    if not wizard.exists():
        st.info("Setup wizard ships in Phase 4c.")
        return
    st.session_state.wizard.step = 6
    st.session_state.wizard.target_env_id = env_id
    st.switch_page("pages/00_First_Run_Setup.py")


def _jump_to_status_signin() -> None:
    st.session_state["focus_signin"] = True
    st.switch_page("pages/01_Status.py")
