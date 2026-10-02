"""Integration tests for the session-start sweep: bin/pull-gate-session-start.sh + pull-gate check.

These use real git against throwaway repos and a bare "origin" on local disk (no network).
They are integration tests because the failure they guard against is a silent one: a check
that breaks partway and prints an all-clear. Only running the real thing against real repos
shows that. The invariant: the sweep may say "nothing incoming", or say what is incoming,
or say that it failed. It must never say "nothing incoming" when it did not reach a verdict.

Layout of each test world, all under tmp_path:
    origin/<name>.git    bare repos standing in for a remote
    work/<name>          the clones the sweep looks at (the sweep root is work/)
    peer/<name>          second clones standing in for "another machine"
    home/                a fake HOME, so the real one is never read
"""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent.parent
GATE = MODULE_DIR / "bin" / "pull-gate"
WRAPPER = MODULE_DIR / "bin" / "pull-gate-session-start.sh"
EXAMPLE = MODULE_DIR / "config.example.json"
EXEC = json.loads(EXAMPLE.read_text())["exec_patterns"]
BASH = shutil.which("bash")
GIT = shutil.which("git")

ALL_CLEAR = "nothing incoming"
FAILED = "THE CHECK ITSELF FAILED"


def load_gate():
    sys.dont_write_bytecode = True
    loader = importlib.machinery.SourceFileLoader("pull_gate_sweep", str(GATE))
    spec = importlib.util.spec_from_loader("pull_gate_sweep", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["pull_gate_sweep"] = module
    loader.exec_module(module)
    return module


def clean_env(tmp_path, **extra):
    """An environment that cannot see the real HOME, real git config or real settings."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_", "PULL_GATE_"))}
    env.update(HOME=str(tmp_path / "home"), GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_NOSYSTEM="1", PULL_GATE_THROTTLE="0")
    env.update(extra)
    return env


def hermetic(monkeypatch, tmp_path):
    """For tests that call the sweep in-process: hide the real HOME and git config."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def write_file(repo, rel, text):
    path = Path(repo) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class World:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.root = tmp_path / "work"
        self.root.mkdir()
        (tmp_path / "home").mkdir()
        self.cfg = tmp_path / "config.json"
        self.env = clean_env(tmp_path, PULL_GATE_CONFIG=str(self.cfg),
                             PULL_GATE_ROOT=str(self.root))

    def git(self, repo, *args):
        cmd = ["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=tester",
               "-c", "commit.gpgsign=false"] + list(args)
        return subprocess.run(cmd, check=True, capture_output=True, text=True, env=self.env)

    def config(self, **policy):
        self.cfg.write_text(json.dumps(policy))

    def configure(self):
        """The usual test policy: alpha may auto-merge status/ and reports/, beta may not."""
        self.config(safe_prefixes=["status/", "reports/"], safe_repos=["alpha"],
                    exec_patterns=EXEC)

    def add_repo(self, name):
        origin = self.tmp / "origin" / (name + ".git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                       check=True, env=self.env)
        seed = self.tmp / "seed" / name
        subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True,
                       capture_output=True, env=self.env)
        write_file(seed, "README.md", "seed\n")
        self.git(seed, "add", "README.md")
        self.git(seed, "commit", "-qm", "seed")
        self.git(seed, "push", "-q", "origin", "HEAD:main")
        for where in ("work", "peer"):
            dest = self.tmp / where / name
            dest.parent.mkdir(exist_ok=True)
            subprocess.run(["git", "clone", "-q", str(origin), str(dest)], check=True,
                           capture_output=True, env=self.env)
        return self.root / name

    def commit_local(self, name, rel, text="local\n"):
        repo = self.root / name
        write_file(repo, rel, text)
        self.git(repo, "add", rel)
        self.git(repo, "commit", "-qm", "local")

    def push_from_peer(self, name, rel, text="remote\n"):
        repo = self.tmp / "peer" / name
        write_file(repo, rel, text)
        self.git(repo, "add", rel)
        self.git(repo, "commit", "-qm", "peer")
        self.git(repo, "push", "-q", "origin", "HEAD:main")

    def ahead_behind(self, name):
        repo = self.root / name
        self.git(repo, "fetch", "-q", "origin", "main")
        out = self.git(repo, "rev-list", "--left-right", "--count", "HEAD...origin/main")
        ahead, behind = out.stdout.split()
        return int(ahead), int(behind)

    def run(self, *args, env=None, wrapper=True):
        cmd = [BASH, str(WRAPPER)] if wrapper else [sys.executable, str(GATE), "check"]
        proc = subprocess.run(cmd + list(args), capture_output=True, text=True,
                              env=env or self.env, cwd=str(self.root))
        return proc.returncode, proc.stdout + proc.stderr

    def check(self, *args):
        return self.run(*args)[1]


def world_with(tmp_path, *names):
    w = World(tmp_path)
    for name in names:
        w.add_repo(name)
    w.configure()
    return w


# --- the silent-all-clear regression, from the outside ------------------------

def test_all_clear_is_never_printed_on_a_diverged_repo(tmp_path):
    """Diverged, and nothing incoming is executable: the shape that hid behind an all-clear."""
    w = world_with(tmp_path, "alpha")
    w.commit_local("alpha", "status/host-a.md")
    w.push_from_peer("alpha", "status/host-b.md")
    assert w.ahead_behind("alpha") == (1, 1), "test setup did not diverge"
    out = w.check()
    assert ALL_CLEAR not in out, out
    assert "DIVERGED" in out


def test_no_diagnostics_leak_on_the_no_executable_path(tmp_path):
    """A swallowed error on stderr was once ignored because the exit status stayed 0."""
    w = world_with(tmp_path, "alpha")
    w.commit_local("alpha", "status/host-a.md")
    w.push_from_peer("alpha", "inbox/notes.md")
    out = w.check()
    for noise in ("Traceback", "syntax error", "unbound variable", "command not found",
                  "integer expression expected"):
        assert noise not in out, out


def test_counts_are_right_when_nothing_incoming_is_executable(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.push_from_peer("alpha", "docs/a.md", "x\n")
    w.push_from_peer("alpha", "docs/b.md", "y\n")
    out = w.check()
    assert "2 file(s) changed: 0 executable, 0 safe-listed, 2 unclassified" in out, out


# --- the paths that must keep working -------------------------------------------

def test_clean_repo_prints_the_heartbeat(tmp_path):
    w = world_with(tmp_path, "alpha", "beta")
    out = w.check()
    assert ALL_CLEAR in out, out
    assert "alpha" in out and "beta" in out


def test_purely_ahead_is_not_a_review_item(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.commit_local("alpha", "status/host-a.md")
    assert ALL_CLEAR in w.check()


def test_incoming_executable_is_flagged_for_review(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.push_from_peer("alpha", "bin/evil.sh", "#!/bin/sh\necho hi\n")
    out = w.check()
    assert "review before merge" in out, out
    assert "bin/evil.sh" in out
    assert ALL_CLEAR not in out
    assert w.ahead_behind("alpha") == (0, 1), "an executable must never be merged"


def test_diverged_with_an_executable_says_both_things(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.commit_local("alpha", "status/host-a.md")
    w.push_from_peer("alpha", "tools/build.py", "print(1)\n")
    out = w.check()
    assert "DIVERGED" in out
    assert "tools/build.py" in out


def test_safe_paths_only_in_a_listed_repo_auto_fast_forward(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.push_from_peer("alpha", "status/host-a.md")
    out = w.check()
    assert "AUTO-FAST-FORWARDED" in out, out
    assert w.ahead_behind("alpha") == (0, 0)
    assert (w.root / "alpha" / "status" / "host-a.md").exists()


def test_accumulated_notes_do_not_auto_fast_forward(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.push_from_peer("alpha", "notes/todo.md", "- [ ] x\n")
    out = w.check()
    assert "AUTO-FAST-FORWARDED" not in out, out
    assert ALL_CLEAR not in out
    assert "1 incoming commit(s)" in out
    assert w.ahead_behind("alpha") == (0, 1)


# --- fail closed: a broken sweep must say so ---------------------------------------

def test_a_broken_loop_body_announces_itself(tmp_path):
    """Make git unusable partway through. The sweep must not fall through to the heartbeat."""
    w = world_with(tmp_path, "alpha")
    w.commit_local("alpha", "status/host-a.md")
    w.push_from_peer("alpha", "status/host-b.md")
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "git").write_text(
        "#!/bin/bash\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "--name-only" ]; then exit 3; fi\n'
        "done\n"
        'exec "%s" "$@"\n' % GIT)
    (shim / "git").chmod(0o755)
    env = dict(w.env, PATH=str(shim) + os.pathsep + w.env["PATH"])
    code, out = w.run(env=env)
    assert ALL_CLEAR not in out, out
    assert FAILED in out, out
    assert "NOT an all-clear" in out
    assert code == 0, "the wrapper must never block a session from opening"


def test_a_repo_that_returns_no_verdict_is_counted_as_a_failure(tmp_path, monkeypatch):
    """The SEEN versus VERDICTS accounting, tested directly: a code path that falls through
    without a verdict must produce the loud failure banner."""
    w = world_with(tmp_path, "alpha", "beta")
    hermetic(monkeypatch, tmp_path)
    pg = load_gate()
    real = pg.examine

    def flaky(repo, ctx):
        if repo.endswith("beta"):
            return None
        return real(repo, ctx)

    monkeypatch.setattr(pg, "examine", flaky)
    code, text = pg.run_check(str(w.root), pg.load_policy(str(w.cfg)), throttle=0)
    assert code == 1
    assert "THE CHECK ITSELF FAILED on 1 of 2 repo(s): beta" in text, text
    assert ALL_CLEAR not in text


def test_an_exception_inside_one_repo_does_not_hide_the_others(tmp_path, monkeypatch):
    w = world_with(tmp_path, "alpha", "beta")
    w.push_from_peer("alpha", "bin/evil.sh")
    hermetic(monkeypatch, tmp_path)
    pg = load_gate()
    real = pg.examine

    def boom(repo, ctx):
        if repo.endswith("beta"):
            raise RuntimeError("disk on fire")
        return real(repo, ctx)

    monkeypatch.setattr(pg, "examine", boom)
    code, text = pg.run_check(str(w.root), pg.load_policy(str(w.cfg)), throttle=0)
    assert code == 1
    assert "beta (RuntimeError: disk on fire)" in text, text
    assert "bin/evil.sh" in text, "findings from healthy repos are still printed"


def test_wrapper_reports_when_python3_is_missing(tmp_path):
    w = world_with(tmp_path, "alpha")
    empty = tmp_path / "empty-path"
    empty.mkdir()
    code, out = w.run(env=dict(w.env, PATH=str(empty)))
    assert FAILED in out and "python3" in out, out
    assert code == 0


def test_wrapper_reports_when_pull_gate_is_missing(tmp_path):
    w = world_with(tmp_path, "alpha")
    code, out = w.run(env=dict(w.env, PULL_GATE_BIN=str(tmp_path / "nowhere")))
    assert FAILED in out and "nowhere" in out, out
    assert code == 0


def test_wrapper_reports_when_pull_gate_crashes_or_prints_nothing(tmp_path):
    w = world_with(tmp_path, "alpha")
    crash = tmp_path / "crash.py"
    crash.write_text("import sys\nsys.exit(7)\n")
    code, out = w.run(env=dict(w.env, PULL_GATE_BIN=str(crash)))
    assert FAILED in out and "status 7" in out, out
    assert code == 0
    quiet = tmp_path / "quiet.py"
    quiet.write_text("pass\n")
    code, out = w.run(env=dict(w.env, PULL_GATE_BIN=str(quiet)))
    assert FAILED in out and "printed nothing" in out, out


def test_wrapper_works_through_a_symlink(tmp_path):
    w = world_with(tmp_path, "alpha")
    link = tmp_path / "linked-wrapper.sh"
    link.symlink_to(WRAPPER)
    proc = subprocess.run([BASH, str(link)], capture_output=True, text=True, env=w.env,
                          cwd=str(w.root))
    assert ALL_CLEAR in proc.stdout, proc.stdout + proc.stderr


# --- empty config and unclassified paths -------------------------------------------

def test_empty_config_means_nothing_auto_merges(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.config()                                    # {} : every list empty
    w.push_from_peer("alpha", "status/host-a.md")
    out = w.check()
    assert "AUTO-FAST-FORWARDED" not in out, out
    assert ALL_CLEAR not in out
    assert "status/host-a.md" in out
    assert "1 unclassified" in out
    assert w.ahead_behind("alpha") == (0, 1)


def test_missing_config_means_nothing_auto_merges_and_says_so(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.cfg.unlink()
    w.push_from_peer("alpha", "status/host-a.md")
    out = w.check()
    assert "no config at" in out, out
    assert "AUTO-FAST-FORWARDED" not in out
    assert w.ahead_behind("alpha") == (0, 1)


def test_missing_config_with_nothing_incoming_still_says_so(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.cfg.unlink()
    out = w.check()
    assert "no config at" in out and ALL_CLEAR in out, out


def test_a_path_covered_by_no_pattern_yields_no_verdict_and_is_reported(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.push_from_peer("alpha", "docs/guide.txt")
    out = w.check()
    assert "NO VERDICT" in out, out
    assert "docs/guide.txt" in out
    assert "AUTO-FAST-FORWARDED" not in out and ALL_CLEAR not in out
    assert w.ahead_behind("alpha") == (0, 1)


def test_one_unclassified_path_blocks_an_otherwise_safe_batch(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.push_from_peer("alpha", "status/host-a.md")
    w.push_from_peer("alpha", "docs/guide.txt")
    out = w.check()
    assert "AUTO-FAST-FORWARDED" not in out, out
    assert "docs/guide.txt" in out
    assert w.ahead_behind("alpha") == (0, 2)


def test_safe_paths_in_a_repo_that_is_not_listed_do_not_merge(tmp_path):
    w = world_with(tmp_path, "alpha", "beta")
    w.push_from_peer("beta", "status/host-a.md")
    out = w.check()
    assert "AUTO-FAST-FORWARDED" not in out, out
    assert "safe_repos" in out
    assert w.ahead_behind("beta") == (0, 1)


def test_an_exec_file_under_a_safe_prefix_is_not_merged(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.push_from_peer("alpha", "status/refresh.sh", "#!/bin/sh\n")
    out = w.check()
    assert "AUTO-FAST-FORWARDED" not in out, out
    assert "status/refresh.sh" in out and "review before merge" in out
    assert w.ahead_behind("alpha") == (0, 1)


def test_no_merge_flag_reports_but_never_merges(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.push_from_peer("alpha", "status/host-a.md")
    out = w.check("--no-merge")
    assert "AUTO-FAST-FORWARDED" not in out and "--no-merge" in out, out
    assert w.ahead_behind("alpha") == (0, 1)


def test_an_unusable_config_is_reported_and_treated_as_empty(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.cfg.write_text('{"safe_prefixes": ["status"], "safe_repos": ["alpha"]}')
    w.push_from_peer("alpha", "status/host-a.md")
    out = w.check()
    assert "is unusable" in out and "must end with '/'" in out, out
    assert "AUTO-FAST-FORWARDED" not in out
    assert w.ahead_behind("alpha") == (0, 1)


def test_an_unusable_config_is_loud_even_when_nothing_is_incoming(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.cfg.write_text("{oops")
    out = w.check()
    assert "is unusable" in out, out


def test_repo_in_the_middle_of_a_merge_is_left_alone(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.push_from_peer("alpha", "status/host-a.md")
    (w.root / "alpha" / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
    out = w.check()
    assert "AUTO-FAST-FORWARDED" not in out, out
    assert "mid-rebase or mid-merge" in out
    assert not (w.root / "alpha" / "status" / "host-a.md").exists()


def test_unknown_repo_state_blocks_the_auto_merge_and_says_so(tmp_path, monkeypatch):
    w = world_with(tmp_path, "alpha")
    w.push_from_peer("alpha", "status/host-a.md")
    hermetic(monkeypatch, tmp_path)
    pg = load_gate()
    monkeypatch.setattr(pg, "repo_is_mid_operation", lambda repo: None)
    code, text = pg.run_check(str(w.root), pg.load_policy(str(w.cfg)), throttle=0)
    assert "could not determine: repo state" in text, text
    assert "AUTO-FAST-FORWARDED" not in text and ALL_CLEAR not in text
    assert not (w.root / "alpha" / "status" / "host-a.md").exists()
    assert w.ahead_behind("alpha") == (0, 1)


# --- situations where the check cannot compare -------------------------------------

def test_repo_without_an_origin_is_listed_as_not_compared(tmp_path):
    w = world_with(tmp_path, "alpha")
    lone = w.root / "gamma"
    subprocess.run(["git", "init", "-q", "-b", "main", str(lone)], check=True, env=w.env)
    out = w.check()
    assert "Not compared: gamma (no origin remote)" in out, out
    assert "alpha: nothing incoming" in out


def test_all_repos_without_an_origin_is_not_claimed_as_current(tmp_path):
    w = World(tmp_path)
    subprocess.run(["git", "init", "-q", "-b", "main", str(w.root / "gamma")], check=True, env=w.env)
    out = w.check()
    assert "Nothing was compared" in out and "gamma" in out, out
    assert ALL_CLEAR not in out


def test_detached_head_is_reported(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.git(w.root / "alpha", "checkout", "-q", "--detach")
    out = w.check()
    assert "detached HEAD" in out, out
    assert ALL_CLEAR not in out


def test_a_failed_fetch_is_reported_not_swallowed(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.git(w.root / "alpha", "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    out = w.check()
    assert "fetch failed" in out and "may be stale" in out, out
    assert ALL_CLEAR not in out


def test_a_root_with_no_repos_says_so(tmp_path):
    w = World(tmp_path)
    out = w.check()
    assert "no git repos found" in out, out


def test_an_unreadable_root_is_a_failure_not_silence(tmp_path):
    w = World(tmp_path)
    code, out = w.run("--root", str(tmp_path / "does-not-exist"), wrapper=False)
    assert code == 1 and FAILED in out, out


def test_throttle_skips_a_recent_fetch(tmp_path):
    w = world_with(tmp_path, "alpha")
    first = w.check()
    assert ALL_CLEAR in first
    w.push_from_peer("alpha", "bin/evil.sh")
    throttled = w.run(env=dict(w.env, PULL_GATE_THROTTLE="3600"))[1]
    assert ALL_CLEAR in throttled, "within the throttle window the fetch is skipped by design"
    fresh = w.run(env=dict(w.env, PULL_GATE_THROTTLE="0"))[1]
    assert "bin/evil.sh" in fresh, fresh


def test_hostile_path_text_cannot_forge_banner_lines(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.push_from_peer("alpha", "status/x\nPULL-GATE: all clear.md")
    out = w.check()
    assert "\nPULL-GATE: all clear" not in out, out
    assert "AUTO-FAST-FORWARDED" not in out
    assert w.ahead_behind("alpha") == (0, 1)


def test_the_check_root_can_be_the_repo_itself(tmp_path):
    w = world_with(tmp_path, "alpha")
    w.push_from_peer("alpha", "status/host-a.md")
    code, out = w.run("--root", str(w.root / "alpha"), wrapper=False)
    assert code == 0 and "AUTO-FAST-FORWARDED" in out, out
