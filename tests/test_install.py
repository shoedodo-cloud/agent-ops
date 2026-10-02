"""Tests for install.sh. Hermetic: every path lives under tmp_path, HOME is faked."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MODULES = [
    "chrome-lanes",
    "context-hygiene",
    "job-health",
    "pull-gate",
    "secret-sweep",
    "session-hooks",
    "session-usage",
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    prefix = tmp_path / "prefix"
    aoh = tmp_path / "aoh"
    e = dict(os.environ)
    e["PREFIX"] = str(prefix)
    e["AGENT_OPS_HOME"] = str(aoh)
    return e


def run(env, *args, script=None):
    return subprocess.run(
        ["bash", str(script or REPO / "install.sh"), *args],
        env=env, capture_output=True, text=True, timeout=60,
    )


def test_install_one_module(env):
    r = run(env, "secret-sweep")
    assert r.returncode == 0, r.stderr
    dest = Path(env["AGENT_OPS_HOME"]) / "secret-sweep"
    link = Path(env["PREFIX"]) / "secret-sweep"
    assert dest.is_dir() and (dest / "README.md").is_file()
    assert link.is_symlink()
    assert link.resolve() == (dest / "bin" / "secret-sweep").resolve()
    assert not (dest / "tests").exists()
    assert f"Installed secret-sweep -> {dest}" in r.stdout
    assert "Next:" in r.stdout
    h = subprocess.run([str(link), "--help"], capture_output=True, text=True, env=env)
    assert h.returncode == 0 and h.stdout.strip()
    assert not (Path(env["HOME"]) / ".local").exists()


def test_install_keeps_helper_folders_beside_the_tool(env):
    r = run(env, "job-health", "session-hooks")
    assert r.returncode == 0, r.stderr
    aoh = Path(env["AGENT_OPS_HOME"])
    assert (aoh / "job-health" / "probes").is_dir()
    assert (aoh / "session-hooks" / "lib").is_dir()
    assert (aoh / "session-hooks" / "trackers").is_dir()
    for tool in ("job-health", "session-start"):
        h = subprocess.run([str(Path(env["PREFIX"]) / tool), "--help"],
                           capture_output=True, text=True, env=env)
        assert h.returncode == 0, h.stderr


def test_install_two_modules(env):
    r = run(env, "secret-sweep", "pull-gate")
    assert r.returncode == 0, r.stderr
    aoh, prefix = Path(env["AGENT_OPS_HOME"]), Path(env["PREFIX"])
    assert (aoh / "secret-sweep").is_dir() and (aoh / "pull-gate").is_dir()
    assert (prefix / "secret-sweep").is_symlink()
    assert (prefix / "pull-gate").is_symlink()
    assert (prefix / "pull-gate-session-start.sh").is_symlink()
    assert "Installed secret-sweep" in r.stdout and "Installed pull-gate" in r.stdout


def test_unknown_module_exits_1_and_creates_nothing(env):
    r = run(env, "secret-sweep", "no-such-module")
    assert r.returncode == 1
    assert "unknown module: no-such-module" in r.stderr
    assert not Path(env["AGENT_OPS_HOME"]).exists()
    assert not Path(env["PREFIX"]).exists()


@pytest.mark.parametrize("bad", ["../etc", ".git", "tests/x", ""])
def test_odd_names_are_unknown_modules(env, bad):
    r = run(env, bad)
    assert r.returncode == 1
    assert not Path(env["AGENT_OPS_HOME"]).exists()


def test_uninstall_removes_links_and_copies(env):
    run(env, "secret-sweep", "pull-gate")
    foreign = Path(env["PREFIX"]) / "other-tool"
    foreign.symlink_to("/bin/ls")
    r = run(env, "--uninstall", "secret-sweep", "pull-gate")
    assert r.returncode == 0, r.stderr
    aoh, prefix = Path(env["AGENT_OPS_HOME"]), Path(env["PREFIX"])
    assert not (aoh / "secret-sweep").exists() and not (aoh / "pull-gate").exists()
    assert not (prefix / "secret-sweep").is_symlink()
    assert not (prefix / "pull-gate").is_symlink()
    assert not (prefix / "pull-gate-session-start.sh").is_symlink()
    assert foreign.is_symlink()


def test_uninstall_of_a_module_that_is_not_installed_fails(env):
    r = run(env, "--uninstall", "secret-sweep")
    assert r.returncode == 1
    assert "not installed" in r.stderr


def test_list_names_all_seven(env):
    r = run(env, "--list")
    assert r.returncode == 0
    assert r.stdout.split() == MODULES


def test_help_and_no_arguments(env):
    assert run(env, "--help").returncode == 0
    r = run(env)
    assert r.returncode == 1 and "Usage:" in r.stdout


def test_reinstall_replaces_the_copy_and_drops_stale_links(env, tmp_path):
    repo = tmp_path / "repo"
    (repo / "demo" / "bin").mkdir(parents=True)
    shutil.copy(REPO / "install.sh", repo / "install.sh")
    tool = repo / "demo" / "bin" / "demo"
    tool.write_text("#!/bin/sh\necho v1\n")
    old = repo / "demo" / "bin" / "demo-old"
    old.write_text("#!/bin/sh\necho old\n")
    assert run(env, "demo", script=repo / "install.sh").returncode == 0
    assert (Path(env["PREFIX"]) / "demo-old").is_symlink()
    old.unlink()
    tool.write_text("#!/bin/sh\necho v2\n")
    assert run(env, "demo", script=repo / "install.sh").returncode == 0
    prefix = Path(env["PREFIX"])
    assert not (prefix / "demo-old").is_symlink()
    out = subprocess.run([str(prefix / "demo")], capture_output=True, text=True)
    assert out.stdout.strip() == "v2"


def test_excludes_and_setup_section_in_a_fake_repo(env, tmp_path):
    repo = tmp_path / "repo"
    mod = repo / "demo"
    for d in ("bin", "tests", "lib", "__pycache__", "lib/.pytest_cache"):
        (mod / d).mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "install.sh", repo / "install.sh")
    (mod / "bin" / "demo").write_text(
        "#!/usr/bin/env python3\nimport os\n"
        "here = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))\n"
        "print(open(os.path.join(here, 'lib', 'data.txt')).read().strip())\n")
    (mod / "lib" / "data.txt").write_text("helper data\n")
    (mod / "tests" / "test_demo.py").write_text("")
    (mod / "__pycache__" / "x.pyc").write_text("")
    (mod / "lib" / ".pytest_cache" / "v").write_text("")
    (mod / "README.md").write_text(
        "# demo\n\n## Setup in 60 seconds\n\n1. First step: `./install.sh demo`\n   with a continuation line\n\n"
        "2. Second step\n   and its own continuation\n\n## Usage\n\nnot shown\n")
    r = run(env, "demo", script=repo / "install.sh")
    assert r.returncode == 0, r.stderr
    dest = Path(env["AGENT_OPS_HOME"]) / "demo"
    assert (dest / "lib" / "data.txt").is_file()
    assert not (dest / "tests").exists()
    assert not (dest / "__pycache__").exists()
    assert not (dest / "lib" / ".pytest_cache").exists()
    # step 1 is the install itself: shown as done, its continuation lines left out
    assert ("Next:\n1. (done: installed by install.sh)\n\n"
            "2. Second step\n   and its own continuation\n") in r.stdout
    assert "First step" not in r.stdout and "with a continuation line" not in r.stdout
    assert "not shown" not in r.stdout
    link = Path(env["PREFIX"]) / "demo"
    # the tool reads lib/ through the symlink, which only works because lib/ was copied too
    out = subprocess.run([str(link)], capture_output=True, text=True)
    assert out.stdout.strip() == "helper data", out.stderr


def test_refuses_to_replace_a_real_file_in_prefix(env):
    prefix = Path(env["PREFIX"])
    prefix.mkdir()
    (prefix / "secret-sweep").write_text("mine")
    r = run(env, "secret-sweep")
    assert r.returncode == 1
    assert "not a symlink" in r.stderr
    assert (prefix / "secret-sweep").read_text() == "mine"
    assert not Path(env["AGENT_OPS_HOME"]).exists()


def test_setup_shows_step_one_as_done_for_a_real_module(env):
    r = run(env, "secret-sweep")
    assert r.returncode == 0, r.stderr
    assert "Next:\n" in r.stdout and "\n1. (done: installed by install.sh)\n" in r.stdout
    assert "From a clone of this repo" not in r.stdout
    assert "\n2. " in r.stdout                       # the other steps are still printed


def test_refuses_to_replace_a_symlink_that_points_outside_agent_ops_home(env, tmp_path):
    prefix = Path(env["PREFIX"])
    prefix.mkdir()
    foreign_target = tmp_path / "somebody-elses-tool"
    foreign_target.write_text("#!/bin/sh\n")
    (prefix / "secret-sweep").symlink_to(foreign_target)
    r = run(env, "secret-sweep")
    assert r.returncode == 1
    assert "symlink points outside" in r.stderr and "--force" in r.stderr
    assert str(prefix / "secret-sweep") in r.stderr and str(foreign_target) in r.stderr
    assert os.readlink(prefix / "secret-sweep") == str(foreign_target)
    assert not Path(env["AGENT_OPS_HOME"]).exists()      # nothing was installed


def test_force_replaces_a_foreign_symlink_but_never_a_real_file(env, tmp_path):
    prefix = Path(env["PREFIX"])
    prefix.mkdir()
    foreign_target = tmp_path / "somebody-elses-tool"
    foreign_target.write_text("#!/bin/sh\n")
    (prefix / "secret-sweep").symlink_to(foreign_target)
    r = run(env, "--force", "secret-sweep")
    assert r.returncode == 0, r.stderr
    dest = Path(env["AGENT_OPS_HOME"]) / "secret-sweep"
    assert (prefix / "secret-sweep").resolve() == (dest / "bin" / "secret-sweep").resolve()
    assert foreign_target.read_text() == "#!/bin/sh\n"   # the old target itself is untouched

    (prefix / "pull-gate").write_text("mine")
    r = run(env, "secret-sweep", "pull-gate", "--force")  # --force may come after the names
    assert r.returncode == 1 and "not a symlink" in r.stderr
    assert (prefix / "pull-gate").read_text() == "mine"


def test_a_link_from_an_earlier_install_is_replaced_without_force(env):
    assert run(env, "secret-sweep").returncode == 0
    assert run(env, "secret-sweep").returncode == 0


def test_force_alone_is_a_usage_error(env):
    r = run(env, "--force")
    assert r.returncode == 1 and "Usage:" in r.stdout


def test_warns_when_prefix_is_not_on_path(env):
    env["PATH"] = os.environ["PATH"]
    assert "not on your PATH" in run(env, "secret-sweep").stdout
    env["PATH"] = env["PREFIX"] + os.pathsep + os.environ["PATH"]
    assert "not on your PATH" not in run(env, "secret-sweep").stdout
