"""Provider registry. Maps a ``type`` string in config to a Provider factory."""

from __future__ import annotations

from ai_usage_indicator.providers.base import Provider
from ai_usage_indicator.providers.claude import ClaudeProvider
from ai_usage_indicator.providers.codex import CodexProvider
from ai_usage_indicator.providers.mock import MockProvider


def build_provider(entry: dict) -> Provider:
    """Instantiate a provider from a config entry.

    An entry looks like ``{"id": "claude", "type": "claude", ...type-specific keys}``.
    """
    ptype = entry.get("type")
    pid = entry.get("id") or ptype
    cfg = {k: v for k, v in entry.items() if k not in ("id", "type")}

    if ptype == "mock":
        return MockProvider(pid, cfg)
    if ptype == "claude":
        return ClaudeProvider(pid, cfg)
    if ptype == "codex":
        return CodexProvider(pid, cfg)
    raise ValueError(f"Unknown provider type: {ptype!r}")


__all__ = ["Provider", "build_provider"]
