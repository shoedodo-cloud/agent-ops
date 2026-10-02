"""The adapter loader, and the GitHub skeleton in CONTRIBUTING.md run with a fake gh."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from session_hooks_testlib import MODULE
import sessionhooks as sh
from trackers.base import TrackerError


def skeleton() -> str:
    text = (MODULE / "CONTRIBUTING.md").read_text()
    block = re.search(r"<!-- skeleton:start -->\n```python\n(.*?)```\n<!-- skeleton:end -->", text, re.S)
    assert block, "CONTRIBUTING.md lost its skeleton markers"
    return block.group(1)


def config(data):
    return sh.Config(Path("unused.json"), True, data)


class FakeGh:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.calls = []
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


def test_skeleton_is_short():
    assert len(skeleton().strip().splitlines()) <= 32


def test_skeleton_loads_from_an_extra_folder_and_works(tmp_path):
    extra = tmp_path / "my-trackers"
    extra.mkdir()
    (extra / "github.py").write_text(skeleton())
    env = {"SESSION_HOOKS_TRACKERS": str(extra)}
    tracker = sh.load_tracker(config({"tracker": "github", "trackers": {"github": {"repo": "example/website"}}}),
                              env, tmp_path)
    rows = [{"number": 7, "title": "Fix the flaky test", "url": "https://example.com/issues/7", "state": "OPEN"}]
    tracker.run = FakeGh(stdout=json.dumps(rows))
    assert tracker.open_items("website") == [
        {"id": "#7", "title": "Fix the flaky test", "url": "https://example.com/issues/7", "state": "open"}]
    assert tracker.run.calls[0] == ["gh", "issue", "list", "--label", "website", "--json",
                                    "number,title,url,state", "--repo", "example/website"]

    tracker.run = FakeGh(stdout="https://example.com/issues/8\n")
    assert tracker.post_note("website", "Wire the login form") == "https://example.com/issues/8"
    assert tracker.run.calls[0][:4] == ["gh", "issue", "create", "--label"]

    # The default post_note titles the item with the first line only and keeps the whole text as the body.
    text = "Wire the login form\nSession note: notes/sessions/a.md, main at abc1234"
    tracker.post_note("website", text)
    call = tracker.run.calls[-1]
    assert call[call.index("--title") + 1] == "Wire the login form"
    assert call[call.index("--body") + 1] == text

    tracker.run = FakeGh(returncode=1, stderr="not logged in")
    with pytest.raises(TrackerError, match="not logged in"):
        tracker.open_items("website")


def test_extra_folder_is_searched_before_the_shipped_one(tmp_path):
    extra = tmp_path / "x"
    extra.mkdir()
    (extra / "file.py").write_text(
        "from trackers.base import Tracker\n"
        "class Mine(Tracker):\n    name = 'mine'\n    def open_items(self, project):\n        return []\n"
        "ADAPTER = Mine\n")
    tracker = sh.load_tracker(config({"tracker": "file"}), {"SESSION_HOOKS_TRACKERS": str(extra)}, tmp_path)
    assert tracker.name == "mine"


def test_default_is_the_shipped_file_tracker(tmp_path):
    tracker = sh.load_tracker(sh.Config(Path("none.json"), False, {}), {}, tmp_path)
    assert tracker.name == "file"


@pytest.mark.parametrize("name, message", [
    ("../evil", "invalid tracker name"),
    ("base", "invalid tracker name"),
    ("jira", "no tracker named 'jira'"),
])
def test_bad_names_raise(tmp_path, name, message):
    with pytest.raises(TrackerError, match=re.escape(message)):
        sh.load_tracker(config({"tracker": name}), {}, tmp_path)


def test_broken_adapters_raise_tracker_error(tmp_path):
    extra = tmp_path / "x"
    extra.mkdir()
    (extra / "noadapter.py").write_text("X = 1\n")
    (extra / "boom.py").write_text("raise RuntimeError('broken on import')\n")
    env = {"SESSION_HOOKS_TRACKERS": str(extra)}
    with pytest.raises(TrackerError, match="does not set ADAPTER"):
        sh.load_tracker(config({"tracker": "noadapter"}), env, tmp_path)
    with pytest.raises(TrackerError, match="broken on import"):
        sh.load_tracker(config({"tracker": "boom"}), env, tmp_path)


def test_default_item_line_shows_the_link_only_when_it_differs_from_the_id():
    from trackers.base import Context, Tracker
    tracker = Tracker({}, Context(Path(".")))
    item = {"id": "#7", "title": "Fix it", "url": "https://example.com/issues/7", "state": "open"}
    assert tracker.format_item(item) == "#7 [open] Fix it  https://example.com/issues/7"
    assert tracker.format_item({**item, "id": "notes/ledger.md:3", "url": "notes/ledger.md:3"}) == \
        "notes/ledger.md:3 [open] Fix it"
    assert tracker.scope_label("website") == "website"
    assert tracker.more_items_note() == ""
