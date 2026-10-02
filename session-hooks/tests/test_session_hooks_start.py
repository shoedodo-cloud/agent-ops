"""session-start: prints a baseline, marks what it could not establish, writes nothing."""
from __future__ import annotations

import os
import shutil
import sys

from session_hooks_testlib import git, run_cli


def snapshot(root):
    """Every file under root (including .git) with its size and mtime."""
    seen = {}
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            st = os.stat(path)
            seen[path] = (st.st_size, st.st_mtime_ns)
    return seen


def test_prints_branch_and_could_not_determine_without_upstream(repo):
    result = run_cli("session-start", "--repo", repo)
    assert result.returncode == 0
    out = result.stdout
    assert "branch: main" in out
    assert "could not determine: ahead/behind upstream (branch 'main' has no upstream branch)" in out
    assert "uncommitted: 0 paths" in out
    assert "last session note: none, notes/sessions does not exist" in out
    assert "could not determine: open tracker items (no ledger at notes/ledger.md)" in out
    assert "read-only: nothing merged, committed or written, no git fetch" in out


def test_ahead_behind_against_a_local_upstream(tmp_path, repo):
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "fetch", "-q", "origin")
    git(repo, "branch", "-q", "-u", "origin/main")
    (repo / "b.txt").write_text("b\n")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "second commit")

    out = run_cli("session-start", "--repo", repo).stdout
    assert "upstream: origin/main, ahead 1, behind 0 (as of the last fetch" in out

    fetched = run_cli("session-start", "--repo", repo, "--fetch").stdout
    assert "upstream: origin/main, ahead 1, behind 0 (fetched origin just now)" in fetched
    assert "one git fetch ran" in fetched


def test_outside_a_git_repo_says_so_and_exits_0(tmp_path, home):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = run_cli("session-start", "--repo", plain)
    assert result.returncode == 0
    assert "could not determine: branch, upstream, uncommitted" in result.stdout
    assert "not inside a git work tree" in result.stdout


def test_shows_newest_note_first_line_and_next_section(repo):
    notes = repo / "notes" / "sessions"
    notes.mkdir(parents=True)
    (notes / "2026-01-01_0900_session.md").write_text("# old note\n\n## Next session starts here\nOld thing\n")
    (notes / "2026-01-02_0900_session.md").write_text(
        "# Session note: website\n\n## Next session starts here\nWire the login form\n\n## Open questions\n- none\n")
    out = run_cli("session-start", "--repo", repo).stdout
    assert "last session note: notes/sessions/2026-01-02_0900_session.md" in out
    assert "first line: # Session note: website" in out
    assert "Wire the login form" in out
    assert "Old thing" not in out


def test_notes_dir_from_env(repo):
    custom = repo / "docs" / "handoff"
    custom.mkdir(parents=True)
    (custom / "2026-02-01_1000_session.md").write_text("# custom\n")
    out = run_cli("session-start", "--repo", repo, env={"SESSION_NOTES_DIR": "docs/handoff"}).stdout
    assert "last session note: docs/handoff/2026-02-01_1000_session.md" in out
    assert 'no "Next session starts here" section in that note' in out


def test_lists_open_ledger_items_for_this_project_only(repo):
    ledger = repo / "notes" / "ledger.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        "# Ledger\n"
        "- [ ] (website) Fix the flaky test\n"
        "- [~] (website, api) Move auth\n"
        "- [x] (website) Ship the footer\n"
        "- [ ] (api) Not ours\n")
    out = run_cli("session-start", "--repo", repo).stdout
    assert "open items: 2 for website" in out
    assert "notes/ledger.md:2 [open] Fix the flaky test" in out
    assert "notes/ledger.md:3 [in progress] Move auth" in out
    assert "Ship the footer" not in out
    assert "Not ours" not in out


def test_project_flag_changes_the_filter(repo):
    ledger = repo / "notes" / "ledger.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("- [ ] (api) Rotate the test keys\n")
    out = run_cli("session-start", "--repo", repo, "--project", "api").stdout
    assert "open items: 1 for api" in out


def test_writes_nothing(repo, write_config):
    write_config({"tracker": "file"})
    (repo / "notes" / "sessions").mkdir(parents=True)
    (repo / "notes" / "sessions" / "2026-01-01_0900_session.md").write_text("# note\n")
    (repo / "notes" / "ledger.md").write_text("- [ ] (website) one\n")
    (repo / "dirty.txt").write_text("uncommitted\n")
    before = snapshot(repo)
    assert run_cli("session-start", "--repo", repo).returncode == 0
    assert snapshot(repo) == before


def test_bad_config_is_reported_not_hidden(repo, write_config):
    path = write_config({})
    path.write_text("{not json")
    out = run_cli("session-start", "--repo", repo).stdout
    assert "could not determine: config (cannot read" in out
    assert "branch: main" in out


def test_unknown_tracker_is_reported(repo, write_config):
    write_config({"tracker": "jira"})
    out = run_cli("session-start", "--repo", repo).stdout
    assert "could not determine: open tracker items (no tracker named 'jira'" in out


def test_no_tracker_flag(repo):
    out = run_cli("session-start", "--repo", repo, "--no-tracker").stdout
    assert "tracker: skipped (--no-tracker)" in out


def test_copied_script_without_its_folder_says_so(tmp_path, home):
    lonely = tmp_path / "bin"
    lonely.mkdir()
    src = os.path.join(os.path.dirname(__file__), "..", "bin", "session-start")
    shutil.copy(src, lonely / "session-start")
    import subprocess
    result = subprocess.run([sys.executable, str(lonely / "session-start")], cwd=tmp_path,
                            capture_output=True, text=True, input="")
    assert result.returncode == 0
    assert "could not determine: anything" in result.stdout
    assert "SESSION_HOOKS_HOME" in result.stdout


def test_help_works():
    result = run_cli("session-start", "--help")
    assert result.returncode == 0
    assert "could not determine" in result.stdout
