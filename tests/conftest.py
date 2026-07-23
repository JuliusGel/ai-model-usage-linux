"""Shared test fixtures: locate and load the recorded provider JSON payloads."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(*parts: str) -> dict:
    """Load a JSON fixture by path segments relative to ``tests/fixtures``."""
    return json.loads(FIXTURES.joinpath(*parts).read_text())


@pytest.fixture
def fixture():
    return load_fixture
