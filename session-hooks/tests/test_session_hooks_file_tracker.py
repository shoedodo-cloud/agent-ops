"""The file ledger adapter: round trip, filters, and failures."""
from __future__ import annotations

import datetime as dt
import os

import pytest

import session_hooks_testlib  # noqa: F401  (puts the module on sys.path)
from trackers.base import Context, TrackerError
from trackers.file import FileTracker


def tracker(root, settings=None, env=None):
    return FileTracker(settings or {}, Context(root, env or {}))


def test_round_trip(tmp_path):
    t = tracker(tmp_path)
    with pytest.raises(TrackerError, match="no ledger at notes/ledger.md"):
        t.open_items("website")

    first = t.create_item("website", "Fix the flaky test", "seen twice\n- [ ] (website) not an item")
    before = dt.date.today().isoformat()
    second = t.post_note("website", "Wire the\nlogin form")
    after = dt.date.today().isoformat()          # two reads, so a midnight rollover cannot flake
    t.create_item("api", "Rotate the test keys", "")

    items = t.open_items("website")
    titles = [i["title"] for i in items]
    assert len(titles) == 2 and titles[0] == "Fix the flaky test"
    assert titles[1] in {f"Next session: Wire the login form ({d})" for d in (before, after)}
    assert all(i["state"] == "open" for i in items)
    assert [i["id"] for i in items] == [first, second]

    lines = (tmp_path / "notes" / "ledger.md").read_text().splitlines()
    number = int(first.rsplit(":", 1)[1])
    assert lines[number - 1] == "- [ ] (website) Fix the flaky test"
    assert lines[number] == "  seen twice"
    assert [i["title"] for i in t.open_items("api")] == ["Rotate the test keys"]


def test_states_tags_and_case(tmp_path):
    ledger = tmp_path / "notes" / "ledger.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        "- [ ] (Website) open one\n"
        "* [~] (api, website) in progress\n"
        "- [x] (website) done\n"
        "- [X] (website) done too\n"
        "  - [ ] (website) indented, a body line\n"
        "- [ ] (websites) other project\n")
    items = tracker(tmp_path).open_items("website")
    assert [(i["title"], i["state"]) for i in items] == [
        ("open one", "open"), ("in progress", "in progress")]


def test_ledger_path_env_beats_config(tmp_path):
    t = tracker(tmp_path, {"path": "from-config.md"}, {"SESSION_LEDGER": "from-env.md"})
    assert t.path == tmp_path / "from-env.md"
    t = tracker(tmp_path, {"path": "from-config.md"})
    assert t.path == tmp_path / "from-config.md"


def test_keeps_file_mode(tmp_path):
    ledger = tmp_path / "notes" / "ledger.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("# Ledger\n")
    os.chmod(ledger, 0o640)
    tracker(tmp_path).create_item("website", "x", "")
    assert ledger.stat().st_mode & 0o777 == 0o640


def test_project_with_parens_is_cleaned(tmp_path):
    t = tracker(tmp_path)
    t.create_item("web(site)", "x", "")
    assert "- [ ] (web site) x" in (tmp_path / "notes" / "ledger.md").read_text()
    with pytest.raises(TrackerError):
        t.create_item("(,)", "x", "")
