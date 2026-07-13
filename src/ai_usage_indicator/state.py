"""Backend → extension contract.

The Python service writes a small JSON snapshot here on each refresh; the GNOME Shell
extension reads it and renders the panel. The path is derived the same way on both sides
(XDG cache dir), so keep it in sync with extension.js if it ever changes.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ai_usage_indicator.usage import UsageRecord

STATE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ai-usage-indicator"
)
STATE_PATH = STATE_DIR / "state.json"


def record_to_dict(rec: UsageRecord) -> dict:
    return {
        "id": rec.provider_id,
        "display_name": rec.display_name,
        "percent": rec.percent,  # int 0-100, or null if unknown
        "pressure": rec.pressure.value,  # normal | warning | near-limit | unknown
        "label": rec.label or rec.detail_text(),
        "detail": rec.detail_text(),
        "reset_text": rec.reset_text(),
        "error": rec.error,
    }


def build_state(records: list[UsageRecord]) -> dict:
    return {
        "updated_at": int(time.time()),
        "providers": [record_to_dict(r) for r in records],
    }


def write_state(records: list[UsageRecord]) -> dict:
    """Atomically write the snapshot so the extension never reads a half-written file."""
    state = build_state(records)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)
    return state
