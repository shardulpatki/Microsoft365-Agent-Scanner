"""Shared streamlit fake for UI unit tests.

The real ``streamlit`` package is an optional extra. Tests must not require it
to be installed, so we install a minimal stub in ``sys.modules`` before any
``m365_mcp_scanner.ui`` modules are imported.
"""
from __future__ import annotations

import sys
import types
from collections.abc import Callable
from typing import Any

import pytest


class _SessionState(dict):  # type: ignore[type-arg]
    def __getattr__(self, name: str) -> object:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: object) -> None:
        self[name] = value


def _identity_decorator(*_args: Any, **_kwargs: Any) -> Callable[..., Any]:
    """Stand-in for ``@st.cache_data(...)`` that simply returns the wrapped fn."""

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.clear = lambda: None  # type: ignore[attr-defined]
        return fn

    # ``@st.cache_data`` works as both ``@st.cache_data`` and ``@st.cache_data(...)``.
    if _args and callable(_args[0]) and not _kwargs:
        return wrap(_args[0])
    return wrap


@pytest.fixture
def fake_streamlit(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    fake = types.ModuleType("streamlit")
    fake.session_state = _SessionState()  # type: ignore[attr-defined]
    fake.cache_data = _identity_decorator  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "streamlit", fake)
    # Force re-import of UI modules so they pick up the fake.
    for mod in list(sys.modules):
        if mod.startswith("m365_mcp_scanner.ui"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
    return fake
