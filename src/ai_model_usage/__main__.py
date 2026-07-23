"""Command-line interface for the shared telemetry core."""

from __future__ import annotations

import argparse
import json
import sys

from ai_model_usage import collect_telemetry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai-model-usage")
    parser.add_argument(
        "--json",
        action="store_true",
        help="print a versioned full-window telemetry collection",
    )
    args = parser.parse_args(argv)
    if not args.json:
        parser.error("--json is required")

    collection = collect_telemetry()
    json.dump(collection.to_envelope(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 1 if collection.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
