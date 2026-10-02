"""Hermetic tests for the context-hygiene scripts.

Every test runs the real scripts through subprocess. HOME and
CONTEXT_HYGIENE_HOME point into tmp_path, so nothing touches the real home
directory. No network.
"""
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
INVENTORY = BIN / "project-inventory.sh"
SNAPSHOT = BIN / "inventory-snapshot.sh"
MARKER = BIN / "compaction-marker.sh"
SNIPPET = BIN / "context-hygiene-snippet.sh"

DAY = 86400


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def state(tmp_path):
    return tmp_path / "state"


@pytest.fixture
def env(home, state):
    e = {k: v for k, v in os.environ.items()
         if k not in ("CLAUDE_PROJECT_DIR", "PREFIX", "CONTEXT_HYGIENE_STALE_DAYS",
                      "CONTEXT_HYGIENE_SKIP")}
    e["HOME"] = str(home)
    e["CONTEXT_HYGIENE_HOME"] = str(state)
    return e


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    return p


def run(script, *args, env, cwd=None):
    return subprocess.run(
        [str(script), *args], env=env, cwd=cwd,
        capture_output=True, text=True, timeout=60,
    )


def make(path, age_days, text="x"):
    """Create a file whose mtime is age_days (plus one hour) in the past."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    t = time.time() - age_days * DAY - 3600
    os.utime(path, (t, t))


# ---------- project-inventory.sh ----------

def test_inventory_lists_newest_first_with_ages(proj, env):
    make(proj / "fresh.txt", 1)
    make(proj / "middle.txt", 10)
    make(proj / "old.txt", 60)
    r = run(INVENTORY, str(proj), env=env)
    assert r.returncode == 0
    out = r.stdout
    assert out.startswith("PROJECT INVENTORY")
    assert "3 files. Newest first." in out
    assert out.index("./fresh.txt") < out.index("./middle.txt") < out.index("./old.txt")
    assert re.search(r"\d{4}-\d{2}-\d{2}\t1B\t1d\t\./fresh\.txt", out)
    assert re.search(r"\t60d\t\./old\.txt", out)


def test_inventory_stale_block_lists_only_stale_files(proj, env):
    make(proj / "fresh.txt", 2)
    make(proj / "stale_a.txt", 45)
    make(proj / "stale_b.txt", 90)
    r = run(INVENTORY, str(proj), env=env)
    assert "STALE: 2 file(s) untouched for more than 30 days." in r.stdout
    block = r.stdout.split("STALE:")[1]
    assert "stale_a.txt" in block and "stale_b.txt" in block
    assert "fresh.txt" not in block


def test_inventory_no_stale_block_when_nothing_is_old(proj, env):
    make(proj / "a.txt", 1)
    make(proj / "b.txt", 5)
    r = run(INVENTORY, str(proj), env=env)
    assert "STALE" not in r.stdout


def test_inventory_stale_days_is_configurable(proj, env):
    make(proj / "a.txt", 1)
    make(proj / "b.txt", 5)
    env["CONTEXT_HYGIENE_STALE_DAYS"] = "3"
    r = run(INVENTORY, str(proj), env=env)
    assert "STALE: 1 file(s) untouched for more than 3 days." in r.stdout


def test_inventory_skips_noise_directories(proj, env):
    make(proj / "keep.txt", 1)
    make(proj / ".git" / "config", 1)
    make(proj / "node_modules" / "pkg" / "index.js", 1)
    make(proj / "__pycache__" / "m.pyc", 1)
    make(proj / ".DS_Store", 1)
    r = run(INVENTORY, str(proj), env=env)
    assert "1 files." in r.stdout
    assert "./keep.txt" in r.stdout
    for noise in (".git", "node_modules", "__pycache__", ".DS_Store"):
        assert noise not in r.stdout


def test_inventory_skips_pytest_cache_by_default(proj, env):
    make(proj / "keep.txt", 1)
    make(proj / ".pytest_cache" / "v" / "cache" / "lastfailed", 1)
    make(proj / "pkg" / ".pytest_cache" / "README.md", 1)
    r = run(INVENTORY, str(proj), env=env)
    assert "1 files." in r.stdout
    assert ".pytest_cache" not in r.stdout


def test_inventory_skip_list_is_configurable(proj, env):
    make(proj / "keep.txt", 1)
    make(proj / "build" / "out.bin", 1)
    make(proj / "vendor" / "lib.c", 1)
    make(proj / "pkg.egg-info" / "PKG-INFO", 1)
    env["CONTEXT_HYGIENE_SKIP"] = "vendor *.egg-info"
    r = run(INVENTORY, str(proj), env=env)
    assert "./keep.txt" in r.stdout and "./build/out.bin" in r.stdout
    assert "vendor" not in r.stdout and "egg-info" not in r.stdout


def test_inventory_empty_skip_list_skips_nothing_but_ds_store(proj, env):
    make(proj / "keep.txt", 1)
    make(proj / ".git" / "config", 1)
    make(proj / ".pytest_cache" / "x", 1)
    make(proj / ".DS_Store", 1)
    env["CONTEXT_HYGIENE_SKIP"] = ""
    r = run(INVENTORY, str(proj), env=env)
    assert "3 files." in r.stdout
    assert "./.git/config" in r.stdout and "./.pytest_cache/x" in r.stdout


def test_inventory_help_documents_the_skip_variable(env):
    r = run(INVENTORY, "--help", env=env)
    assert "CONTEXT_HYGIENE_SKIP" in r.stdout and ".pytest_cache" in r.stdout


def test_inventory_truncates_at_max_files_and_says_so(proj, env):
    for i in range(5):
        make(proj / f"f{i}.txt", i + 1)
    r = run(INVENTORY, str(proj), "2", env=env)
    assert "5 files." in r.stdout
    assert "./f0.txt" in r.stdout and "./f1.txt" in r.stdout
    assert "./f2.txt" not in r.stdout
    assert "... 3 more not shown." in r.stdout


def test_inventory_handles_spaces_and_quotes_in_file_names(proj, env):
    make(proj / "sub dir" / "it's a file.txt", 1)
    make(proj / "CLAUDE.md", 2)
    r = run(INVENTORY, str(proj), env=env)
    assert "./sub dir/it's a file.txt" in r.stdout
    assert "2 files." in r.stdout
    make(proj / "added file.txt", 0)
    run(SNAPSHOT, str(proj), env=env)
    make(proj / "second one.txt", 0)
    assert "NEW (1):\n  ./second one.txt" in run(SNAPSHOT, str(proj), env=env).stdout


def test_inventory_and_snapshot_stay_fast_on_a_thousand_files(proj, env):
    """Guards against one process per file, which took about 7 ms per file."""
    for i in range(1000):
        (proj / f"f{i}.txt").write_text("x")
    start = time.time()
    r = run(INVENTORY, str(proj), env=env)
    run(SNAPSHOT, str(proj), env=env)
    assert "1000 files." in r.stdout
    assert time.time() - start < 15


def test_inventory_reports_last_modified_of_instruction_files(proj, env):
    make(proj / "CLAUDE.md", 3)
    make(proj / "other.txt", 1)
    r = run(INVENTORY, str(proj), env=env)
    assert re.search(r"--- CLAUDE\.md last modified \d{4}-\d{2}-\d{2}", r.stdout)
    assert "AGENTS.md last modified" not in r.stdout


def test_inventory_uses_claude_project_dir_when_no_argument(proj, env):
    make(proj / "here.txt", 1)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    r = run(INVENTORY, env=env, cwd=str(proj.parent))
    assert "./here.txt" in r.stdout


def test_inventory_reports_instead_of_failing(proj, env, tmp_path):
    r = run(INVENTORY, str(tmp_path / "nope"), env=env)
    assert r.returncode == 0
    assert "[inventory] cannot enter" in r.stdout
    r = run(INVENTORY, str(proj), env=env)
    assert "[inventory] no files under" in r.stdout


def test_inventory_rejects_bad_numbers(proj, env):
    r = run(INVENTORY, str(proj), "lots", env=env)
    assert r.returncode == 2
    env["CONTEXT_HYGIENE_STALE_DAYS"] = "soon"
    r = run(INVENTORY, str(proj), env=env)
    assert r.returncode == 2


@pytest.mark.parametrize("script", [INVENTORY, SNAPSHOT, MARKER, SNIPPET])
def test_every_script_has_help(script, env):
    r = run(script, "--help", env=env)
    assert r.returncode == 0
    assert "Usage:" in r.stdout
    assert r.stderr == ""


# ---------- inventory-snapshot.sh ----------

def test_snapshot_first_run_stores_a_baseline(proj, env, state):
    make(proj / "a.txt", 2)
    r = run(SNAPSHOT, str(proj), env=env)
    assert r.returncode == 0
    assert "No previous snapshot. Baseline stored: 1 files." in r.stdout
    stored = list((state / "snapshots").glob("*.tsv"))
    assert len(stored) == 1
    assert "./a.txt" in stored[0].read_text()


def test_snapshot_second_run_shows_new_modified_and_gone(proj, env):
    make(proj / "keep.txt", 5)
    make(proj / "edit.txt", 5)
    make(proj / "drop.txt", 5)
    run(SNAPSHOT, str(proj), env=env)

    make(proj / "added.txt", 0)                       # new
    (proj / "drop.txt").unlink()                      # gone
    t = time.time() - 60                              # modified: a different mtime
    os.utime(proj / "edit.txt", (t, t))

    r = run(SNAPSHOT, str(proj), env=env)
    assert r.returncode == 0
    assert "NEW (1):\n  ./added.txt" in r.stdout
    assert "MODIFIED (1):\n  ./edit.txt" in r.stdout
    assert "GONE (1):\n  ./drop.txt" in r.stdout
    assert "keep.txt" not in r.stdout
    assert "GONE means absent now" in r.stdout


def test_snapshot_reports_no_changes_when_nothing_moved(proj, env):
    make(proj / "a.txt", 2)
    run(SNAPSHOT, str(proj), env=env)
    r = run(SNAPSHOT, str(proj), env=env)
    assert "No file changes since the last snapshot." in r.stdout


def test_snapshot_no_store_leaves_the_old_snapshot(proj, env):
    make(proj / "a.txt", 2)
    run(SNAPSHOT, str(proj), env=env)
    make(proj / "b.txt", 0)
    first = run(SNAPSHOT, str(proj), "--no-store", env=env)
    again = run(SNAPSHOT, str(proj), env=env)
    assert "NEW (1):\n  ./b.txt" in first.stdout
    assert "NEW (1):\n  ./b.txt" in again.stdout      # still new: it was not stored
    last = run(SNAPSHOT, str(proj), env=env)
    assert "No file changes" in last.stdout           # now it was stored


def test_snapshot_state_stays_out_of_the_project_and_home(proj, env, home, state):
    make(proj / "a.txt", 2)
    before = sorted(p.name for p in proj.iterdir())
    run(SNAPSHOT, str(proj), env=env)
    assert sorted(p.name for p in proj.iterdir()) == before
    assert list(home.iterdir()) == []
    assert (state / "snapshots").is_dir()


def test_snapshot_default_state_dir_is_under_home(proj, env, home):
    del env["CONTEXT_HYGIENE_HOME"]
    make(proj / "a.txt", 2)
    run(SNAPSHOT, str(proj), env=env)
    assert list((home / ".config" / "context-hygiene" / "snapshots").glob("*.tsv"))


def test_snapshot_reports_a_missing_directory(env, tmp_path):
    r = run(SNAPSHOT, str(tmp_path / "nope"), env=env)
    assert "[snapshot] cannot enter" in r.stdout


def test_snapshot_skips_pytest_cache_like_the_inventory_does(proj, env, state):
    make(proj / "keep.txt", 1)
    make(proj / ".pytest_cache" / "v" / "cache" / "lastfailed", 1)
    make(proj / "pkg" / ".pytest_cache" / "README.md", 1)
    make(proj / "node_modules" / "pkg" / "index.js", 1)
    inv = run(INVENTORY, str(proj), env=env)
    snap = run(SNAPSHOT, str(proj), env=env)
    assert "1 files." in inv.stdout                     # the inventory and the snapshot
    assert "Baseline stored: 1 files." in snap.stdout   # agree on what the project has
    stored = next((state / "snapshots").glob("*.tsv")).read_text()
    assert ".pytest_cache" not in stored and "node_modules" not in stored
    make(proj / ".pytest_cache" / "v" / "new", 0)
    assert "No file changes" in run(SNAPSHOT, str(proj), env=env).stdout


def test_snapshot_skip_list_is_configurable(proj, env, state):
    make(proj / "keep.txt", 1)
    make(proj / "build" / "out.bin", 1)
    make(proj / "vendor" / "lib.c", 1)
    make(proj / "pkg.egg-info" / "PKG-INFO", 1)
    env["CONTEXT_HYGIENE_SKIP"] = "vendor *.egg-info"
    run(SNAPSHOT, str(proj), env=env)
    stored = next((state / "snapshots").glob("*.tsv")).read_text()
    assert "./keep.txt" in stored and "./build/out.bin" in stored
    assert "vendor" not in stored and "egg-info" not in stored


def test_snapshot_empty_skip_list_skips_nothing_but_ds_store(proj, env, state):
    make(proj / "keep.txt", 1)
    make(proj / ".git" / "config", 1)
    make(proj / ".DS_Store", 1)
    env["CONTEXT_HYGIENE_SKIP"] = ""
    r = run(SNAPSHOT, str(proj), env=env)
    assert "Baseline stored: 2 files." in r.stdout
    assert "./.git/config" in next((state / "snapshots").glob("*.tsv")).read_text()


def test_snapshot_help_documents_the_skip_variable(env):
    r = run(SNAPSHOT, "--help", env=env)
    assert "CONTEXT_HYGIENE_SKIP" in r.stdout and ".pytest_cache" in r.stdout


# ---------- compaction-marker.sh ----------

def test_marker_log_writes_a_tab_separated_line_and_prints_nothing(proj, env, state):
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    r = run(MARKER, "--log", env=env)
    assert r.returncode == 0
    assert r.stdout == ""
    lines = (state / "compaction.log").read_text().splitlines()
    assert len(lines) == 1
    stamp, path = lines[0].split("\t")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", stamp)
    assert path == str(proj)


def test_marker_log_is_the_default_mode(proj, env, state):
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    run(MARKER, env=env)
    assert (state / "compaction.log").exists()


def test_marker_announce_prints_the_count_and_the_instruction(proj, env, state):
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    run(MARKER, "--log", env=env)
    one = run(MARKER, "--announce", env=env)
    assert "CONTEXT WAS COMPACTED. The log holds 1 compaction(s) for proj, counted across all sessions." in one.stdout
    assert "re-read it" in one.stdout
    assert str(state / "compaction.log") in one.stdout
    run(MARKER, "--log", env=env)
    two = run(MARKER, "--announce", env=env)
    assert "holds 2 compaction(s)" in two.stdout


def test_marker_counts_each_project_separately(tmp_path, env):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    for p, times in ((a, 3), (b, 1)):
        env["CLAUDE_PROJECT_DIR"] = str(p)
        for _ in range(times):
            run(MARKER, "--log", env=env)
    env["CLAUDE_PROJECT_DIR"] = str(b)
    assert "holds 1 compaction(s) for b" in run(MARKER, "--announce", env=env).stdout


def test_marker_announce_without_a_log_entry_still_speaks(proj, env):
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    r = run(MARKER, "--announce", env=env)
    assert r.returncode == 0
    assert "CONTEXT WAS COMPACTED." in r.stdout
    assert "--log hook is missing" in r.stdout
    assert "re-read it from the file" in r.stdout


def test_marker_unknown_mode_fails_loudly(env):
    r = run(MARKER, "--bogus", env=env)
    assert r.returncode == 2
    assert "unknown mode" in r.stderr


def test_marker_never_writes_outside_its_state_dir(proj, env, home, state):
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    run(MARKER, "--log", env=env)
    assert list(home.iterdir()) == []
    assert [p.name for p in state.iterdir()] == ["compaction.log"]


def test_marker_reports_when_it_cannot_write_the_log(proj, env, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory should be")
    env["CONTEXT_HYGIENE_HOME"] = str(blocker)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    r = run(MARKER, "--log", env=env)
    assert "could not write" in r.stderr


# ---------- context-hygiene-snippet.sh ----------

def snippet(env, *args, prefix="/opt/tools/bin"):
    env = dict(env, PREFIX=prefix)
    return run(SNIPPET, *args, env=env)


def test_snippet_claude_part_is_valid_json_with_the_three_hooks(env):
    r = snippet(env, "--claude")
    assert r.returncode == 0
    cfg = json.loads(r.stdout)
    start = cfg["hooks"]["SessionStart"]
    assert "matcher" not in start[0]
    assert start[0]["hooks"][0]["command"] == '"/opt/tools/bin/project-inventory.sh"'
    assert start[1]["matcher"] == "compact"
    assert start[1]["hooks"][0]["command"] == '"/opt/tools/bin/compaction-marker.sh" --announce'
    pre = cfg["hooks"]["PreCompact"][0]["hooks"][0]
    assert pre["type"] == "command"
    assert pre["command"] == '"/opt/tools/bin/compaction-marker.sh" --log'


def test_snippet_codex_part_is_valid_json_and_says_what_it_leaves_out(env):
    r = snippet(env, "--codex")
    cfg = json.loads(r.stdout)
    assert cfg["hooks"]["SessionStart"][0]["matcher"] == "startup|resume"
    assert "--announce" not in r.stdout
    assert "no announce entry" in r.stderr


def test_snippet_default_prints_both_fragments(env):
    r = snippet(env)
    assert r.returncode == 0
    assert "# Claude Code:" in r.stdout and "# Codex:" in r.stdout
    assert r.stdout.count('"hooks"') >= 4
    assert "# Note: no announce entry" in r.stdout


def test_snippet_default_prefix_is_home_local_bin(env, home):
    r = run(SNIPPET, "--claude", env=env)
    cfg = json.loads(r.stdout)
    command = cfg["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert command == f'"{home}/.local/bin/project-inventory.sh"'
    assert "not an executable file" in r.stderr     # nothing installed there: it warns


def test_snippet_handles_a_prefix_with_spaces_and_a_trailing_slash(env):
    r = snippet(env, "--claude", prefix="/opt/my tools/bin/")
    cfg = json.loads(r.stdout)
    command = cfg["hooks"]["PreCompact"][0]["hooks"][0]["command"]
    assert command == '"/opt/my tools/bin/compaction-marker.sh" --log'


@pytest.mark.parametrize("prefix", ["relative/bin", '/opt/a"b', "/opt/$HOME", "/opt/a`b"])
def test_snippet_refuses_unusable_prefixes(env, prefix):
    r = snippet(env, "--claude", prefix=prefix)
    assert r.returncode == 2
    assert r.stdout == ""
    assert "PREFIX must" in r.stderr


def test_snippet_rejects_unknown_options(env):
    r = snippet(env, "--apply")
    assert r.returncode == 2
    assert r.stdout == ""


def test_snippet_does_not_warn_when_scripts_are_installed(env, tmp_path):
    prefix = tmp_path / "bin"
    prefix.mkdir()
    for name in ("project-inventory.sh", "compaction-marker.sh"):
        f = prefix / name
        f.write_text("#!/bin/sh\n")
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    r = snippet(env, "--claude", prefix=str(prefix))
    assert r.stderr == ""


def test_snippet_never_writes_any_file(env, home, tmp_path):
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    snippet(env)
    snippet(env, "--claude")
    snippet(env, "--codex")
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


def test_printed_commands_run_for_real(env, proj, tmp_path):
    """The command strings the snippet prints work when a shell runs them."""
    make(proj / "doc.md", 1)
    cfg = json.loads(snippet(env, "--claude", prefix=str(BIN)).stdout)
    env = dict(env, CLAUDE_PROJECT_DIR=str(proj))

    inventory = cfg["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    r = subprocess.run(inventory, shell=True, env=env, cwd=str(tmp_path),
                       capture_output=True, text=True, timeout=60)
    assert "PROJECT INVENTORY" in r.stdout and "./doc.md" in r.stdout

    log = cfg["hooks"]["PreCompact"][0]["hooks"][0]["command"]
    announce = cfg["hooks"]["SessionStart"][1]["hooks"][0]["command"]
    subprocess.run(log, shell=True, env=env, cwd=str(tmp_path), timeout=60)
    r = subprocess.run(announce, shell=True, env=env, cwd=str(tmp_path),
                       capture_output=True, text=True, timeout=60)
    assert "holds 1 compaction(s)" in r.stdout


# ---------- GNU branch, emulated on macOS ----------

@pytest.mark.skipif(sys.platform != "darwin",
                    reason="on Linux the other tests already run the GNU branch")
def test_gnu_branch_gives_the_same_answers(proj, env, tmp_path):
    """Shim stat and date so they behave like GNU coreutils, then compare."""
    shims = tmp_path / "shims"
    shims.mkdir()
    (shims / "stat").write_text(
        '#!/bin/sh\n'
        '[ "$1" = "-c" ] || exit 1\n'                     # GNU: -f is filesystem mode
        "fmt=$(printf '%s' \"$2\" | sed 's/%Y/%m/g; s/%s/%z/g; s/%n/%N/g')\n"
        'shift 2\n'
        'exec /usr/bin/stat -f "$fmt" "$@"\n')
    (shims / "date").write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "-r" ]; then exit 1; fi\n'          # GNU: -r wants a file name
        'if [ "$1" = "-d" ]; then n="${2#@}"; shift 2; exec /bin/date -r "$n" "$@"; fi\n'
        'exec /bin/date "$@"\n')
    for s in shims.iterdir():
        s.chmod(0o755)

    make(proj / "new.txt", 1)
    make(proj / "old.txt", 50)
    gnu_env = dict(env, PATH=f"{shims}:{env['PATH']}")

    real = run(INVENTORY, str(proj), env=env)
    gnu = run(INVENTORY, str(proj), env=gnu_env)
    assert gnu.returncode == 0
    assert gnu.stdout == real.stdout
    assert gnu.stdout.index("./new.txt") < gnu.stdout.index("./old.txt")

    run(SNAPSHOT, str(proj), env=gnu_env)
    make(proj / "added.txt", 0)
    r = run(SNAPSHOT, str(proj), env=gnu_env)
    assert "NEW (1):\n  ./added.txt" in r.stdout
