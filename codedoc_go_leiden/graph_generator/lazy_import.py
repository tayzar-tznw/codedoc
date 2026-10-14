"""Modules imported on first use.

The GCP client libraries (google.genai, google.cloud.spanner, google.api_core)
take about a second to import, and `analyze --graph --no-spanner`, `doctor`,
`evaluate` and `trial` never use them. A LazyModule stands in for such a module
at import time and imports it when an attribute is first read. Attribute
writes and deletes go to the real module too, so monkeypatch.setattr on the
stand-in patches the module itself, exactly as with a plain import.
"""

from __future__ import annotations

import importlib
from types import ModuleType


class LazyModule:
    """Stand-in for the module *name*, imported on first attribute access."""

    __slots__ = ("_lazy_name", "_lazy_module")

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_lazy_name", name)
        object.__setattr__(self, "_lazy_module", None)

    def _load(self) -> ModuleType:
        module = object.__getattribute__(self, "_lazy_module")
        if module is None:
            module = importlib.import_module(object.__getattribute__(self, "_lazy_name"))
            object.__setattr__(self, "_lazy_module", module)
        return module

    def __getattr__(self, attr: str):
        return getattr(self._load(), attr)

    def __setattr__(self, attr: str, value) -> None:
        setattr(self._load(), attr, value)

    def __delattr__(self, attr: str) -> None:
        delattr(self._load(), attr)

    def __repr__(self) -> str:
        state = "loaded" if object.__getattribute__(self, "_lazy_module") is not None else "not loaded"
        return f"<LazyModule {object.__getattribute__(self, '_lazy_name')} ({state})>"
