"""Codex (ChatGPT plan) usage provider.

Uses the installed Codex CLI's machine-readable app-server protocol. The CLI owns all
authentication; this adapter never reads or writes its token file.
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from ai_usage_indicator.providers.base import Provider, ProviderError
from ai_usage_indicator.telemetry import Telemetry
from ai_usage_indicator.telemetry_parsers import snapshot_from_codex_rate_limits

DEFAULT_CODEX_HOME = Path.home() / ".codex"
DEFAULT_TIMEOUT = 15.0


def _send(proc: subprocess.Popen, message: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _read_response(
    proc: subprocess.Popen,
    request_id: int,
    *,
    deadline: float,
) -> dict:
    assert proc.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise ProviderError("Codex app-server timed out")
            line = proc.stdout.readline()
            if not line:
                raise ProviderError(
                    f"Codex app-server exited before response ({proc.poll()})"
                )
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise ProviderError(f"Codex app-server error: {message['error']}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise ProviderError("Codex app-server returned a malformed result")
            return result
    finally:
        selector.close()


def _read_app_server_rate_limits(
    *,
    command: str = "codex",
    codex_home: Path = DEFAULT_CODEX_HOME,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Perform one read-only ``account/rateLimits/read`` JSON-RPC exchange."""
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    try:
        proc = subprocess.Popen(
            [command, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )
    except OSError as exc:
        raise ProviderError(f"could not start Codex app-server: {exc}") from exc

    deadline = time.monotonic() + timeout
    try:
        _send(
            proc,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "ai-model-usage", "version": "0.1.0"}
                },
            },
        )
        _read_response(proc, 1, deadline=deadline)
        _send(proc, {"method": "initialized"})
        _send(proc, {"id": 2, "method": "account/rateLimits/read", "params": None})
        return _read_response(proc, 2, deadline=deadline)
    finally:
        if proc.stdin:
            proc.stdin.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


class CodexProvider(Provider):
    id = "codex"
    display_name = "Codex"

    def __init__(self, provider_id: str = "codex", config: dict | None = None) -> None:
        super().__init__(config)
        self.id = provider_id
        self.display_name = self.config.get("display_name", "Codex")
        self._command = str(self.config.get("command", "codex"))
        # ``auth_path`` is accepted as a migration hint from old configs; its parent is
        # the CLI home. The file itself is never opened.
        configured_home = self.config.get("codex_home")
        if configured_home is None and self.config.get("auth_path"):
            configured_home = Path(self.config["auth_path"]).parent
        self._codex_home = Path(configured_home or DEFAULT_CODEX_HOME)
        self._timeout = float(self.config.get("timeout_seconds", DEFAULT_TIMEOUT))

    def authenticate(self) -> None:
        return None

    def fetch_telemetry(self) -> Telemetry:
        data = _read_app_server_rate_limits(
            command=self._command,
            codex_home=self._codex_home,
            timeout=self._timeout,
        )
        return snapshot_from_codex_rate_limits(
            data,
            observed_at=datetime.now(timezone.utc),
            provider=self.id,
        )
