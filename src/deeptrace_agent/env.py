"""Environment configuration: DEEPTRACE_* variables, with the pre-rename RESEARCHLOOP_* names as fallback."""

from __future__ import annotations

import os


def getenv(name: str, default: str | None = None) -> str | None:
    """``DEEPTRACE_<name>``, falling back to ``RESEARCHLOOP_<name>`` so existing .env files keep working."""
    for prefix in ("DEEPTRACE_", "RESEARCHLOOP_"):
        value = os.getenv(prefix + name)
        if value:
            return value
    return default
