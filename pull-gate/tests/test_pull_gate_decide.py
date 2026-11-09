"""Tests for the decision logic in bin/pull-gate: classification, verdicts, config, CLI.

The decision code can auto-merge pulled files onto a machine that then runs them, so these
tests are written from the attacker's side. Everything that is not plainly covered by the
safe list must be refused or left without a verdict.
"""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent.parent
GATE = MODULE_DIR / "bin" / "pull-gate"
EXAMPLE = MODULE_DIR / "config.example.json"


def load_gate():
    """Import bin/pull-gate (no .py suffix, and a dash in the name) as a module."""
    sys.dont_write_bytecode = True
    loader = importlib.machinery.SourceFileLoader("pull_gate", str(GATE))
    spec = importlib.util.spec_from_loader("pull_gate", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["pull_gate"] = module
    loader.exec_module(module)
    return module


pg = load_gate()
EXEC = json.loads(EXAMPLE.read_text())["exec_patterns"]


def policy():
    """A configured policy: two safe prefixes, one safe repo, the shipped exec patterns."""
    return pg.Policy(safe_prefixes=("status/", "reports/"), safe_repos=("alpha",),
                     exec_patterns=EXEC)


def verdict(paths, repo="alpha", pol=None):
    return pg.decide(paths, repo, pol or policy())


# --- what may fast-forward ---------------------------------------------------

def test_status_only():
    assert verdict(["status/host-a.md", "status/host-b.md"]).verdict == pg.AUTO_FF


def test_reports_only():
    assert verdict(["reports/host-a/2026-01.md"]).verdict == pg.AUTO_FF


def test_mixed_safe_prefixes():
    paths = ["status/host-a.md", "reports/host-b/a.md", "status/sweep.log"]
    assert verdict(paths).verdict == pg.AUTO_FF


# --- each of these is a way the safe list could be widened by accident -------

def test_any_code_file_blocks_the_whole_batch():
    """One unsafe path poisons the batch. It is a single fast-forward, all or nothing."""
    result = verdict(["status/host-a.md", "tools/build.py"])
    assert result.verdict == pg.MANUAL
    assert "build.py" in result.reason


def test_notes_outside_the_safe_list_get_no_verdict():
    """Accumulated notes are not on the safe list, and nothing says they are inert."""
    result = verdict(["inbox/todo.md"])
    assert result.verdict == pg.NO_VERDICT


def test_hooks_and_settings_refused():
    for path in (".claude/settings.json", "bin/pull-gate-session-start.sh",
                 "launchd/example.plist", "job_health.py", ".github/workflows/ci.yml",
                 "hooks/pre-commit", "Makefile"):
        assert verdict([path]).verdict != pg.AUTO_FF, path


def test_prefix_lookalikes_do_not_match():
    """The trailing slash on a safe prefix is what keeps 'status/' from matching 'status_x'."""
    for path in ("status_notes/x.md", "statusreport.txt", "reports-dump.txt",
                 "notstatus/host-a.md", "reports_old/x.md"):
        assert verdict([path]).verdict != pg.AUTO_FF, path


def test_path_traversal_refused():
    for path in ("status/../tools/run.py", "../alpha/bin/x.sh", "/etc/passwd"):
        result = verdict([path])
        assert result.verdict == pg.MANUAL, path
        assert result.cause == "suspicious", path


def test_control_characters_in_a_path_are_refused():
    result = verdict(["status/a\nb.md"])
    assert result.verdict == pg.MANUAL
    assert result.cause == "suspicious"
    assert "\n" not in result.reason, "a hostile path must not be able to forge a line"


def test_other_repos_refused_even_for_safe_paths():
    """A project with its own status/ directory is a different thing."""
    result = verdict(["status/host-a.md"], repo="beta")
    assert result.verdict == pg.MANUAL
    assert "safe_repos" in result.reason


def test_empty_incoming_is_not_a_merge():
    assert verdict([]).verdict == pg.MANUAL
    assert verdict(["", ""]).verdict == pg.MANUAL
    assert verdict(["  "]).verdict == pg.MANUAL


def test_reason_names_the_offender():
    """A refusal that only says 'unsafe' trains people to stop reading refusals."""
    result = verdict(["status/a.md", "bin/evil.sh"])
    assert "evil.sh" in result.reason


# --- guards ------------------------------------------------------------------

def test_mid_operation_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "alpha"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert pg.repo_is_mid_operation(str(repo)) is False
    (repo / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
    assert pg.repo_is_mid_operation(str(repo)) is True
    # git cannot answer for a folder that is not a repo: None, which is never "no operation"
    assert pg.repo_is_mid_operation(str(tmp_path / "does-not-exist")) is None


def test_mid_operation_is_unknown_when_git_cannot_run(tmp_path, monkeypatch):
    def no_git(*args, **kwargs):
        raise pg.CannotDecide("cannot run git")

    monkeypatch.setattr(pg, "_git", no_git)
    assert pg.repo_is_mid_operation(str(tmp_path)) is None


def test_safe_prefix_must_end_in_a_slash():
    """Dropping the slash would widen the list to every path that merely starts with the word."""
    try:
        pg.Policy(safe_prefixes=("status",))
    except pg.ConfigError as exc:
        assert "must end with '/'" in str(exc)
    else:
        raise AssertionError("a prefix without a trailing slash was accepted")


def test_nothing_is_safe_by_default():
    empty = pg.Policy()
    assert empty.safe_prefixes == () and empty.safe_repos == () and empty.exec_patterns == ()


# --- empty config, no verdict, precedence ------------------------------------

def test_empty_policy_means_nothing_auto_merges():
    for paths in (["status/host-a.md"], ["reports/a.md"], ["README.md"], ["a", "b", "c"]):
        for repo in ("alpha", "beta", "status", "reports"):
            result = pg.decide(paths, repo, pg.Policy())
            assert result.verdict != pg.AUTO_FF, (paths, repo)
            assert result.verdict == pg.NO_VERDICT, (paths, repo)


def test_safe_prefix_without_a_safe_repo_does_not_merge():
    pol = pg.Policy(safe_prefixes=("status/",), exec_patterns=EXEC)
    assert pg.decide(["status/host-a.md"], "alpha", pol).verdict == pg.MANUAL


def test_safe_repo_without_a_safe_prefix_does_not_merge():
    pol = pg.Policy(safe_repos=("alpha",), exec_patterns=EXEC)
    assert pg.decide(["status/host-a.md"], "alpha", pol).verdict == pg.NO_VERDICT


def test_a_path_covered_by_no_pattern_has_no_verdict():
    result = verdict(["docs/guide.txt"])
    assert result.verdict == pg.NO_VERDICT
    assert result.cause == "unclassified"
    assert "guide.txt" in result.reason
    assert result.classes["unclassified"] == ["docs/guide.txt"]


def test_an_unclassified_path_is_not_hidden_by_safe_neighbours():
    result = verdict(["status/a.md", "status/b.md", "docs/guide.txt"])
    assert result.verdict == pg.NO_VERDICT


def test_exec_pattern_beats_safe_prefix():
    for path in ("status/run.sh", "reports/tool.py", "status/RUN.SH", "status/hooks/x"):
        result = verdict([path])
        assert result.verdict == pg.MANUAL, path
        assert result.cause == "exec", path


def test_exec_outranks_unclassified_in_a_mixed_batch():
    result = verdict(["docs/guide.txt", "bin/tool"])
    assert result.verdict == pg.MANUAL and result.cause == "exec"


# --- config loading ----------------------------------------------------------

def write(path, data):
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return str(path)


def test_example_config_loads_and_enables_no_auto_merge(tmp_path):
    loaded = pg.load_policy(str(EXAMPLE))
    assert loaded.state == "ok", loaded.problem
    assert loaded.policy.safe_prefixes == () and loaded.policy.safe_repos == ()
    assert len(loaded.policy.exec_patterns) >= 7
    for path in ("status/host-a.md", "reports/a.md"):
        assert pg.decide([path], "alpha", loaded.policy).verdict != pg.AUTO_FF


def test_example_exec_patterns_catch_the_usual_suspects():
    pol = pg.Policy(exec_patterns=EXEC)
    for path in ("run.sh", "a/b/tool.py", "bin/x", "tools/bin/x", ".github/workflows/ci.yml",
                 "settings.json", ".tool/settings.local.json", "x.plist", "hooks/pre-push",
                 ".githooks/post-merge", "Makefile", "sub/Makefile", "lib/a.js"):
        assert pol.is_exec(path), path
    for path in ("README.md", "status/host-a.md", "docs/notes.txt"):
        assert not pol.is_exec(path), path


def test_underscore_keys_are_comments(tmp_path):
    cfg = write(tmp_path / "c.json", {"_comment": "hello", "_x": 1, "safe_prefixes": ["status/"]})
    loaded = pg.load_policy(cfg)
    assert loaded.state == "ok"
    assert loaded.policy.safe_prefixes == ("status/",)


def test_missing_config_is_an_empty_policy_and_says_so(tmp_path):
    loaded = pg.load_policy(str(tmp_path / "nope.json"))
    assert loaded.state == "missing"
    assert loaded.policy.safe_prefixes == ()


def test_bad_configs_are_reported_never_half_applied(tmp_path):
    bad = {
        "not json": "{oops",
        "not an object": "[1, 2]",
        "unknown key": {"safe_prefix": ["status/"]},
        "string instead of list": {"safe_repos": "alpha"},
        "non-string item": {"exec_patterns": [1]},
        "bad regex": {"exec_patterns": ["("]},
        "empty prefix": {"safe_prefixes": [""]},
        "prefix without slash": {"safe_prefixes": ["status"]},
        "absolute prefix": {"safe_prefixes": ["/status/"]},
        "dotdot prefix": {"safe_prefixes": ["a/../b/"]},
        "repo given as a path": {"safe_repos": ["work/alpha"]},
        "empty pattern": {"exec_patterns": [""]},
    }
    for label, content in bad.items():
        loaded = pg.load_policy(write(tmp_path / "bad.json", content))
        assert loaded.state == "invalid", label
        assert loaded.problem, label
        assert loaded.policy.safe_prefixes == (), label
        assert pg.decide(["status/host-a.md"], "alpha", loaded.policy).verdict != pg.AUTO_FF, label


def test_config_path_uses_the_env_var_then_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PULL_GATE_CONFIG", raising=False)
    assert pg.config_path() == str(tmp_path / ".config" / "pull-gate" / "config.json")
    monkeypatch.setenv("PULL_GATE_CONFIG", str(tmp_path / "other.json"))
    assert pg.config_path() == str(tmp_path / "other.json")


# --- command line ------------------------------------------------------------

def run_cli(tmp_path, cfg, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PULL_GATE_")}
    env["HOME"] = str(tmp_path)
    if cfg is not None:
        env["PULL_GATE_CONFIG"] = cfg
    return subprocess.run([sys.executable, str(GATE)] + list(args), capture_output=True,
                          text=True, env=env, cwd=str(tmp_path))


def configured(tmp_path):
    return write(tmp_path / "cfg.json", {"safe_prefixes": ["status/"], "safe_repos": ["alpha"],
                                         "exec_patterns": EXEC})


def test_cli_exit_codes(tmp_path):
    cfg = configured(tmp_path)
    ok = run_cli(tmp_path, cfg, "decide", "alpha", "status/host-a.md")
    assert (ok.returncode, ok.stdout.split()[0]) == (0, "AUTO-FF")
    manual = run_cli(tmp_path, cfg, "decide", "alpha", "status/host-a.md", "bin/x.sh")
    assert (manual.returncode, manual.stdout.split()[0]) == (1, "MANUAL")
    none = run_cli(tmp_path, cfg, "decide", "alpha", "docs/guide.txt")
    assert (none.returncode, none.stdout.split()[0]) == (2, "NO-VERDICT")


def test_cli_repo_can_be_a_path(tmp_path):
    cfg = configured(tmp_path)
    result = run_cli(tmp_path, cfg, "decide", str(tmp_path / "work" / "alpha"), "status/a.md")
    assert result.returncode == 0, result.stdout + result.stderr


def test_cli_with_no_config_never_auto_merges(tmp_path):
    result = run_cli(tmp_path, None, "decide", "alpha", "status/host-a.md")
    assert result.returncode == 2
    assert "no config" in result.stderr


def test_cli_unusable_config_exits_3(tmp_path):
    cfg = write(tmp_path / "bad.json", "{oops")
    result = run_cli(tmp_path, cfg, "decide", "alpha", "status/host-a.md")
    assert result.returncode == 3
    assert "unusable" in result.stderr
    assert result.stdout == ""


def test_cli_usage_errors_exit_64(tmp_path):
    assert run_cli(tmp_path, None, "decide").returncode == 64
    assert run_cli(tmp_path, None, "bogus").returncode == 64
    assert run_cli(tmp_path, None).returncode == 64


def test_help_documents_the_config_and_the_defaults(tmp_path):
    result = run_cli(tmp_path, None, "--help")
    assert result.returncode == 0
    for word in ("safe_prefixes", "safe_repos", "exec_patterns", "PULL_GATE_CONFIG",
                 "PULL_GATE_THROTTLE", "empty"):
        assert word in result.stdout, word
    assert run_cli(tmp_path, None, "check", "--help").returncode == 0
    assert run_cli(tmp_path, None, "decide", "--help").returncode == 0


def test_a_non_plain_mode_under_a_safe_prefix_needs_a_person():
    v = pg.decide(["status/host-a.md"], "alpha", policy(), {"status/host-a.md": "120000"})
    assert v.verdict == pg.MANUAL and v.cause == "exec"


def test_example_patterns_cover_agent_config_files():
    pol = policy()
    for path in (".envrc", ".mcp.json", "CLAUDE.md", "sub/AGENTS.md", ".gitattributes",
                 ".gitmodules", "package.json", ".claude/settings.local.json", ".claude/commands/x.md"):
        assert pol.is_exec(path), path
