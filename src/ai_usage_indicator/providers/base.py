"""Provider plugin interface. New providers are additive: implement this and register it."""

from __future__ import annotations

import abc
import json
import os
from pathlib import Path

from ai_usage_indicator.usage import UsageRecord


def atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON to `path` atomically with 0600 perms.

    Used to update a CLI's OAuth credential file after a token refresh. We write a temp
    file in the same directory, fsync, then rename over the original, so a crash mid-write
    can never leave the CLI's credentials truncated/corrupt.
    """
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


class Provider(abc.ABC):
    """A source of AI-subscription usage.

    Implementations keep their own credential/state handling inside ``authenticate``
    and ``fetch_usage``; the rest of the app only ever sees a normalized ``UsageRecord``.
    Provider-specific breakage must stay contained here — surface it as
    ``UsageRecord(error=...)`` rather than raising into the tray loop.
    """

    #: Stable machine id used in config (e.g. "claude").
    id: str
    #: Human-facing name shown in the menu (e.g. "Claude").
    display_name: str

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}

    @abc.abstractmethod
    def authenticate(self) -> None:
        """Prepare credentials/session. May be a no-op. Raise on unrecoverable auth failure."""

    @abc.abstractmethod
    def fetch_usage(self) -> UsageRecord:
        """Return current usage. Should not raise for transient errors — return a record
        with ``error`` set so the tray can degrade gracefully."""

    def safe_fetch(self) -> UsageRecord:
        """Wrapper the app calls: never raises, always returns a record."""
        try:
            return self.fetch_usage()
        except Exception as exc:  # noqa: BLE001 - deliberately contain provider breakage
            return UsageRecord(
                provider_id=self.id,
                display_name=self.display_name,
                used=0.0,
                limit=None,
                error=str(exc),
            )
