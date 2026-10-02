"""session-wrap: writes the note, seeds it from git, sends nothing without --post."""
from __future__ import annotations

import datetime as dt
import json
import re

from session_hooks_testlib import git, load_script, run_cli, section

HEADINGS = ["## What changed", "## Decisions", "## Next session starts here", "## Open questions"]
NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}_session(_\d+)?\.md$")


def notes(repo):
    folder = repo / "notes" / "sessions"
    return sorted(folder.iterdir()) if folder.is_dir() else []


def test_writes_note_with_four_headings_and_next_line(repo):
    result = run_cli("session-wrap", "--repo", repo, "--next", "Wire the login form",
                     "--decision", "Keep the file ledger", "--question", "Who owns the API?")
    assert result.returncode == 0, result.stderr
    written = notes(repo)
    assert len(written) == 1
    assert NAME_RE.match(written[0].name)
    text = written[0].read_text()
    for heading in HEADINGS:
        assert heading in text
    assert section(text, "Next session starts here") == ["Wire the login form"]
    assert section(text, "Decisions") == ["- Keep the file ledger"]
    assert section(text, "Open questions") == ["- Who owns the API?"]
    assert re.search(r"^- [0-9a-f]{7,} first commit$", text, re.M)
    assert "Uncommitted at wrap time: 0 paths." in text
    assert re.match(r"^# Session note: website, \d{4}-\d{2}-\d{2} \d{2}:\d{2}$", text.splitlines()[0])


def test_next_line_from_stdin(repo):
    result = run_cli("session-wrap", "--repo", repo, stdin="Ship the footer\n")
    assert result.returncode == 0
    text = notes(repo)[0].read_text()
    assert section(text, "Next session starts here") == ["Ship the footer"]


def test_missing_next_line_is_said_not_hidden(repo):
    result = run_cli("session-wrap", "--repo", repo, "--post")
    assert result.returncode == 0
    assert "no next line given" in result.stdout
    assert "nothing to send" in result.stdout
    assert not (repo / "notes" / "ledger.md").exists()
    assert "Not given." in notes(repo)[0].read_text()


def test_hook_payload_sets_cwd_and_session_start(tmp_path, repo):
    three_days_ago = (dt.datetime.now().astimezone() - dt.timedelta(days=3)).isoformat()
    (repo / "old.txt").write_text("old\n")
    git(repo, "add", "old.txt")
    git(repo, "commit", "-q", "-m", "old work",
        env={"GIT_AUTHOR_DATE": three_days_ago, "GIT_COMMITTER_DATE": three_days_ago})
    (repo / "new.txt").write_text("new\n")
    git(repo, "add", "new.txt")
    git(repo, "commit", "-q", "-m", "new work")

    started = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps({"type": "summary"}) + "\n"
                          + json.dumps({"type": "user", "timestamp": started}) + "\n")
    payload = json.dumps({"cwd": str(repo), "transcript_path": str(transcript),
                          "hook_event_name": "SessionEnd"})
    result = run_cli("session-wrap", "--since-start", cwd=tmp_path, stdin=payload)
    assert result.returncode == 0, result.stderr
    text = notes(repo)[0].read_text()
    assert "from the harness transcript" in text
    assert "new work" in text
    assert "old work" not in text


def test_explicit_since_and_env_start(repo):
    later = (dt.datetime.now().astimezone() + dt.timedelta(hours=1)).isoformat()
    run_cli("session-wrap", "--repo", repo, "--next", "x", env={"SESSION_START_TIME": later})
    text = notes(repo)[0].read_text()
    assert "(session start, SESSION_START_TIME)" in text
    assert "- none" in text


def test_skip_if_noted_when_a_note_exists_after_start(repo):
    an_hour_ago = str(int((dt.datetime.now() - dt.timedelta(hours=1)).timestamp()))
    env = {"SESSION_START_TIME": an_hour_ago}
    assert run_cli("session-wrap", "--repo", repo, "--next", "agent wrote this", env=env).returncode == 0
    second = run_cli("session-wrap", "--repo", repo, "--since-start", "--skip-if-noted", env=env)
    assert second.returncode == 0
    assert "skipped" in second.stdout
    assert len(notes(repo)) == 1


def test_skip_if_noted_without_a_start_time_writes_and_says_why(repo):
    first = run_cli("session-wrap", "--repo", repo, "--next", "one")
    assert first.returncode == 0
    second = run_cli("session-wrap", "--repo", repo, "--skip-if-noted")
    assert "could not determine whether this session already has a note" in second.stdout
    assert len(notes(repo)) == 2


def test_without_post_nothing_is_sent(repo):
    result = run_cli("session-wrap", "--repo", repo, "--next", "Wire the login form")
    assert result.returncode == 0
    assert "would append an open item tagged (website) to notes/ledger.md" in result.stdout
    assert "nothing sent (dry run)" in result.stdout
    assert not (repo / "notes" / "ledger.md").exists()


def test_with_post_the_next_line_lands_in_the_ledger(repo):
    result = run_cli("session-wrap", "--repo", repo, "--next", "Wire the login form", "--post")
    assert result.returncode == 0, result.stderr
    assert "tracker: sent" in result.stdout
    ledger = (repo / "notes" / "ledger.md").read_text()
    assert "- [ ] (website) Next session: Wire the login form (session note: notes/sessions/" in ledger
    start = run_cli("session-start", "--repo", repo).stdout
    assert "open items: 1 for website" in start


def test_post_failure_exits_1_and_keeps_the_note(repo, write_config):
    write_config({"tracker": "jira"})
    result = run_cli("session-wrap", "--repo", repo, "--next", "x", "--post")
    assert result.returncode == 1
    assert "no tracker named 'jira'" in result.stdout
    assert len(notes(repo)) == 1


def test_custom_template(repo, write_config):
    (repo / "note.tmpl").write_text("# $project on $branch\n\n## Next session starts here\n$next\n")
    write_config({"note_template": "note.tmpl"})
    run_cli("session-wrap", "--repo", repo, "--next", "Ship it")
    text = notes(repo)[0].read_text()
    assert text.startswith("# website on main")
    assert section(text, "Next session starts here") == ["Ship it"]


def test_same_minute_gets_a_suffix(tmp_path):
    wrap = load_script("session-wrap")
    folder = tmp_path / "notes"
    folder.mkdir()
    when = dt.datetime(2026, 3, 4, 5, 6)
    first = wrap._note_path(folder, when)
    assert first.name == "2026-03-04_0506_session.md"
    first.write_text("x")
    assert wrap._note_path(folder, when).name == "2026-03-04_0506_session_2.md"


def test_outside_git_still_writes_and_says_so(tmp_path, home):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = run_cli("session-wrap", "--repo", plain, "--next", "x")
    assert result.returncode == 0
    text = notes(plain)[0].read_text()
    assert "could not determine: git history (not inside a git work tree)" in text


def test_help_works():
    result = run_cli("session-wrap", "--help")
    assert result.returncode == 0
    assert "--post" in result.stdout
