"""Fixtures for the session-hooks tests: a fake HOME and a temp git repo per test."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from session_hooks_testlib import TOOL_ENV, git


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch) -> Path:
    """A fake HOME for every test. Git and the tools never see the real one."""
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    for name in TOOL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake / ".gitconfig"))
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "alice")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "alice.example.com")
    monkeypatch.setenv("SESSION_HOOKS_CONFIG", str(fake / ".config" / "session-hooks" / "config.json"))
    return fake


@pytest.fixture
def repo(tmp_path, home) -> Path:
    """A git repo called 'website' with one commit on main."""
    root = tmp_path / "website"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("hello\n")
    git(root, "add", "README.md")
    git(root, "commit", "-q", "-m", "first commit")
    return root


@pytest.fixture
def write_config(home):
    """Write the config file the tools read, and return its path."""
    def _write(data: dict) -> Path:
        path = home / ".config" / "session-hooks" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path
    return _write
