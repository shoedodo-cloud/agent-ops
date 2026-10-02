"""Unit tests for lib/sessionhooks.py: time parsing, stdin, notes, config, since."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
from pathlib import Path

import pytest

import session_hooks_testlib  # noqa: F401  (puts the module on sys.path)
import sessionhooks as sh


@pytest.mark.parametrize("raw", [
    "2026-01-02T03:04:05Z",
    "2026-01-02T03:04:05.123456789Z",
    "2026-01-02T03:04:05+00:00",
])
def test_parse_time_utc_forms(raw):
    got = sh.parse_time(raw).astimezone(dt.timezone.utc).replace(microsecond=0)
    assert got == dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)


def test_parse_time_epoch_naive_and_garbage():
    assert sh.parse_time("1767323045").tzinfo is not None
    assert sh.parse_time("2026-01-02 03:04").tzinfo is not None
    assert sh.parse_time("yesterday-ish") is None
    assert sh.parse_time("") is None


def test_split_stdin():
    assert sh.split_stdin('{"cwd": "/x"}') == ({"cwd": "/x"}, "")
    assert sh.split_stdin("  Ship it \n") == ({}, "Ship it")
    assert sh.split_stdin("{not json") == ({}, "{not json")


def test_read_stdin_from_a_stream_and_none():
    assert sh.read_stdin(io.StringIO("hello")) == "hello"
    assert sh.read_stdin(None) == ""


def test_read_stdin_from_a_pipe_does_not_hang():
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"from a pipe")
    os.close(write_fd)
    with os.fdopen(read_fd) as stream:
        assert sh.read_stdin(stream) == "from a pipe"
    read_fd, write_fd = os.pipe()          # writer never closes: give up after the wait
    with os.fdopen(read_fd) as stream:
        assert sh.read_stdin(stream, wait_seconds=0.2) == ""
    os.close(write_fd)


def test_newest_note_prefers_dated_names_over_mtime(tmp_path):
    (tmp_path / "2026-01-02_0900_session.md").write_text("b")
    (tmp_path / "2026-01-01_0900_session.md").write_text("a")   # newer mtime, older name
    (tmp_path / "README.md").write_text("undated")
    (tmp_path / ".hidden.md").write_text("hidden")
    assert sh.newest_note(tmp_path).name == "2026-01-02_0900_session.md"
    assert sh.newest_note(tmp_path / "missing") is None


def test_section():
    text = "# t\n## Decisions\n- a\n\n## Next session starts here\nDo x\n  and y\n## Open questions\n- q\n"
    assert sh.section(text, "next session starts here") == ["Do x", "  and y"]
    assert sh.section(text, "Nope") == []


def test_config_missing_bad_and_good(tmp_path):
    env = {"SESSION_HOOKS_CONFIG": str(tmp_path / "c.json")}
    assert sh.load_config(env).found is False
    (tmp_path / "c.json").write_text("[1]")
    with pytest.raises(sh.ConfigError, match="JSON object"):
        sh.load_config(env)
    (tmp_path / "c.json").write_text(json.dumps({"tracker": "linear"}))
    assert sh.load_config(env).data == {"tracker": "linear"}
    assert sh.config_path({"HOME": str(tmp_path)}) == tmp_path / ".config" / "session-hooks" / "config.json"


def test_resolve_since_order(tmp_path):
    now = dt.datetime(2026, 5, 1, 12, 0).astimezone()
    notes = tmp_path / "notes"
    explicit = sh.resolve_since("3 hours ago", {}, {}, notes, now)
    assert explicit.git_since == "3 hours ago" and explicit.start is None
    env_start = sh.resolve_since(None, {"SESSION_START_TIME": "2026-05-01T09:00:00+00:00"}, {}, notes, now)
    assert env_start.start is not None and "SESSION_START_TIME" in env_start.label
    nothing = sh.resolve_since(None, {}, {"transcript_path": str(tmp_path / "missing.jsonl")}, notes, now)
    assert nothing.start is None and "last 24 hours" in nothing.label
    notes.mkdir()
    (notes / "2026-04-30_1000_session.md").write_text("x")
    from_note = sh.resolve_since(None, {}, {}, notes, now)
    assert from_note.start is None and "time of the last session note" in from_note.label


def test_resolve_project_and_notes_dir(tmp_path):
    assert sh.resolve_project("api", {"SESSION_PROJECT": "x"}, tmp_path) == ("api", "--project")
    assert sh.resolve_project(None, {"SESSION_PROJECT": "x"}, tmp_path) == ("x", "SESSION_PROJECT")
    assert sh.resolve_project(None, {}, tmp_path / "website")[0] == "website"
    assert sh.notes_dir({}, tmp_path) == tmp_path / "notes" / "sessions"
    assert sh.notes_dir({"SESSION_NOTES_DIR": str(tmp_path / "abs")}, Path("/elsewhere")) == tmp_path / "abs"


def test_example_config_loads_both_adapters(tmp_path):
    from session_hooks_testlib import MODULE
    data = json.loads((MODULE / "config.example.json").read_text())
    plans = {}
    for name in ("file", "linear"):
        tracker = sh.load_tracker(sh.Config(Path("x.json"), True, {**data, "tracker": name}), {}, tmp_path)
        assert tracker.name == name
        plans[name] = tracker.plan_post("website")
    assert plans == {"file": "append an open item tagged (website) to notes/ledger.md",
                     "linear": "create a Linear issue in team ENG, project Website"}
