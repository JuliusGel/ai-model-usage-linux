"""Codex adapter: locating the CLI without a trustworthy PATH.

The systemd user service starts in ``default.target``, seconds before gnome-session imports
the login shell's PATH, so a service started at boot never sees nvm/bun/volta bin
directories. Resolution must not depend on the inherited PATH.
"""

from __future__ import annotations

import os
import stat

import pytest

from ai_usage_indicator.providers.codex import (
    CodexProvider,
    _child_path,
    resolve_codex_command,
)


def _make_executable(directory, name="codex"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/usr/bin/env node\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """An empty HOME and an empty PATH — the boot-wave environment, exaggerated."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("ai_usage_indicator.providers.codex.Path.home", lambda: home)
    monkeypatch.setenv("PATH", "")
    return home


def test_absolute_command_is_used_as_given(tmp_path, fake_home):
    path = _make_executable(tmp_path / "opt", "codex")
    assert resolve_codex_command(str(path)) == str(path)


def test_absolute_command_that_is_not_executable_is_rejected(tmp_path, fake_home):
    path = tmp_path / "opt" / "codex"
    path.parent.mkdir(parents=True)
    path.write_text("not executable\n")
    assert resolve_codex_command(str(path)) is None


def test_path_lookup_wins_when_available(tmp_path, fake_home, monkeypatch):
    on_path = _make_executable(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert resolve_codex_command("codex") == str(on_path)


def test_falls_back_to_local_bin_when_path_is_empty(fake_home):
    expected = _make_executable(fake_home / ".local" / "bin")
    assert resolve_codex_command("codex") == str(expected)


def test_finds_codex_under_nvm_when_path_is_empty(fake_home):
    """The exact production failure: nvm's bin dir is missing from the service's PATH."""
    expected = _make_executable(fake_home / ".nvm/versions/node/v24.15.0/bin")
    assert resolve_codex_command("codex") == str(expected)


def test_newest_nvm_version_wins(fake_home):
    """Version dirs sort badly as strings (v24.9.0 > v24.15.0), so prefer newest install."""
    older = _make_executable(fake_home / ".nvm/versions/node/v24.9.0/bin")
    newer = _make_executable(fake_home / ".nvm/versions/node/v24.15.0/bin")
    os.utime(older.parent, (1_000_000, 1_000_000))
    os.utime(newer.parent, (2_000_000, 2_000_000))
    assert resolve_codex_command("codex") == str(newer)


def test_missing_cli_reports_an_actionable_error(fake_home):
    assert resolve_codex_command("codex") is None
    record = CodexProvider().safe_fetch()
    assert "not found" in record.error
    assert "command" in record.error


def test_child_path_puts_the_cli_directory_first():
    """codex is a `#!/usr/bin/env node` script, so node must be findable beside it."""
    result = _child_path("/home/u/.nvm/versions/node/v24.15.0/bin/codex", "/usr/bin:/bin")
    assert result.split(os.pathsep)[0] == "/home/u/.nvm/versions/node/v24.15.0/bin"
    assert result.endswith("/usr/bin:/bin")


def test_child_path_does_not_duplicate_an_already_present_directory():
    inherited = "/usr/bin:/opt/tools/bin:/bin"
    result = _child_path("/opt/tools/bin/codex", inherited)
    assert result.split(os.pathsep).count("/opt/tools/bin") == 1


def test_child_path_tolerates_an_empty_inherited_path():
    assert _child_path("/opt/tools/bin/codex", "") == "/opt/tools/bin"
