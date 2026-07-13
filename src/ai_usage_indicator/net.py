"""Tiny stdlib HTTP helper — no third-party deps so the indicator stays lightweight."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class HttpError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


def get_json(url: str, headers: dict[str, str], timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise HttpError(exc.code, exc.read().decode("utf-8", "replace")[:300]) from exc


def post_json(url: str, headers: dict[str, str], body: dict, timeout: float = 20.0) -> dict:
    data = json.dumps(body).encode("utf-8")
    hdrs = {"Content-Type": "application/json", **headers}
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise HttpError(exc.code, exc.read().decode("utf-8", "replace")[:300]) from exc
