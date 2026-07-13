"""Backend service: fetch provider usage and write the state snapshot the GNOME
extension renders.

Usage:
    ai-usage-indicator            # run forever, refreshing on the configured interval
    ai-usage-indicator --once     # fetch once, write state.json, exit (used by "Refresh now")
"""

from __future__ import annotations

import argparse
import sys
import time

from ai_usage_indicator.config import load_config
from ai_usage_indicator.providers import build_provider
from ai_usage_indicator.state import STATE_PATH, write_state

MIN_INTERVAL = 30  # guard against a misconfigured tiny refresh hammering the endpoints


def build_providers(config) -> list:
    providers = []
    for entry in config.providers:
        try:
            # Note: we don't authenticate() here — safe_fetch() handles auth per cycle so a
            # missing/expired token surfaces as an error record instead of dropping the provider.
            providers.append(build_provider({**entry, "auto_refresh": config.auto_refresh}))
        except Exception as exc:  # noqa: BLE001 - a bad entry shouldn't kill the service
            print(f"[ai-usage-indicator] skipping provider {entry!r}: {exc}", file=sys.stderr)
    return providers


def fetch_all(providers) -> list:
    return [p.safe_fetch() for p in providers]


def main() -> None:
    parser = argparse.ArgumentParser(prog="ai-usage-indicator")
    parser.add_argument(
        "--once", action="store_true", help="fetch once, write state, and exit"
    )
    args = parser.parse_args()

    config = load_config()
    providers = build_providers(config)

    if args.once:
        write_state(fetch_all(providers))
        print(f"wrote {STATE_PATH}")
        return

    interval = max(MIN_INTERVAL, config.refresh_seconds)
    print(f"[ai-usage-indicator] service started; refresh every {interval}s -> {STATE_PATH}")
    while True:
        write_state(fetch_all(providers))
        time.sleep(interval)


if __name__ == "__main__":
    main()
