"""Tests for the probe loader, the bundled probes and the CLI.

Every path that cannot decide must come back STALE with a reason, never OK.
Hermetic: HOME is a temp folder, the config and probe folders live under it.
"""

import datetime
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import textwrap

import pytest

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(MODULE, "bin", "job-health")
PROBES = os.path.join(MODULE, "probes")


def _load():
    if "job_health" in sys.modules and getattr(sys.modules["job_health"], "__file__", "") == BIN:
        return sys.modules["job_health"]
    loader = importlib.machinery.SourceFileLoader("job_health", BIN)
    spec = importlib.util.spec_from_loader("job_health", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["job_health"] = mod
    loader.exec_module(mod)
    return mod


jh = _load()


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("JOB_HEALTH_CONFIG", raising=False)
    monkeypatch.delenv("JOB_HEALTH_PROBES", raising=False)
    return h


def stamp(hours_ago=0.0):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_probe(folder, name, body):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (name + ".py")).write_text(textwrap.dedent(body))
    return folder


# -------------------------------------------------------------- the loader

class TestLoader:

    def test_a_probe_from_an_extra_dir_runs(self, tmp_path, monkeypatch):
        extra = write_probe(tmp_path / "extra", "always_ok", """
            from job_health import Health, OK
            def probe(job):
                return Health(job["name"], "", OK, 1.0, "fine")
        """)
        monkeypatch.setenv("JOB_HEALTH_PROBES", "/nonexistent:" + str(extra))
        row = jh.run_job({"name": "mine", "owner": "work", "probe": "always_ok"})
        assert (row.name, row.owner, row.state) == ("mine", "work", jh.OK)

    def test_extra_dirs_are_searched_before_the_bundled_folder(self, tmp_path):
        extra = write_probe(tmp_path / "extra", "log_file", """
            from job_health import Health, OK
            def probe(job):
                return Health(job["name"], "", OK, 0.0, "overridden")
        """)
        assert jh.find_probe("log_file", [str(extra), PROBES]).startswith(str(extra))
        assert jh.find_probe("log_file", [PROBES]).startswith(PROBES)

    def test_a_probe_that_raises_is_stale_with_the_exception_text(self, tmp_path):
        extra = write_probe(tmp_path / "extra", "boom", """
            def probe(job):
                raise RuntimeError("disk exploded")
        """)
        row = jh.run_job({"name": "b", "probe": "boom"}, [str(extra)])
        assert row.state == jh.STALE
        assert "RuntimeError" in row.outcome and "disk exploded" in row.outcome

    def test_an_unknown_probe_is_stale_and_reported(self, tmp_path):
        row = jh.run_job({"name": "u", "probe": "no_such_probe"}, [str(tmp_path)])
        assert row.state == jh.STALE
        assert "unknown probe" in row.outcome and "no_such_probe" in row.outcome
        assert "u STALE" in jh.headline([row])

    def test_a_path_as_probe_name_is_refused(self, tmp_path):
        row = jh.run_job({"name": "p", "probe": "../../etc/x"}, [str(tmp_path)])
        assert row.state == jh.STALE
        assert "not a valid probe name" in row.outcome

    def test_a_probe_file_that_does_not_load_is_stale(self, tmp_path):
        extra = write_probe(tmp_path / "extra", "broken", "def probe(job:\n")
        row = jh.run_job({"name": "s", "probe": "broken"}, [str(extra)])
        assert row.state == jh.STALE
        assert "failed to load" in row.outcome

    def test_a_file_without_a_probe_function_is_stale(self, tmp_path):
        extra = write_probe(tmp_path / "extra", "empty", "X = 1\n")
        row = jh.run_job({"name": "e", "probe": "empty"}, [str(extra)])
        assert row.state == jh.STALE
        assert "no probe(job) function" in row.outcome

    def test_a_probe_returning_nonsense_is_stale(self, tmp_path):
        extra = write_probe(tmp_path / "extra", "nonsense", """
            def probe(job):
                return "all good"
        """)
        row = jh.run_job({"name": "n", "probe": "nonsense"}, [str(extra)])
        assert row.state == jh.STALE
        assert "not a Health row" in row.outcome

    def test_a_probe_returning_an_unknown_state_is_stale(self, tmp_path):
        extra = write_probe(tmp_path / "extra", "weird", """
            from job_health import Health
            def probe(job):
                return Health(job["name"], "", "GREEN", 1.0, "looks fine")
        """)
        row = jh.run_job({"name": "w", "probe": "weird"}, [str(extra)])
        assert row.state == jh.STALE
        assert "unknown state" in row.outcome

    def test_a_probe_that_hangs_is_stale(self, tmp_path):
        extra = write_probe(tmp_path / "extra", "slow", """
            import time
            def probe(job):
                time.sleep(5)
        """)
        row = jh.run_job({"name": "h", "probe": "slow", "timeout_seconds": 0.2}, [str(extra)])
        assert row.state == jh.STALE
        assert "no answer within" in row.outcome

    def test_missing_name_or_probe_is_stale(self):
        assert jh.run_job({"probe": "log_file"}).state == jh.STALE
        row = jh.run_job({"name": "x"})
        assert row.state == jh.STALE and "no probe named" in row.outcome
        assert jh.run_job("just a string").state == jh.STALE

    def test_duplicate_job_names_are_flagged(self, tmp_path):
        rows = jh.run_all([{"name": "d", "probe": "nope"}, {"name": "d", "probe": "nope"}],
                          [str(tmp_path)])
        assert "duplicate" in rows[1].outcome


# ----------------------------------------------------------- log_file probe

def log_job(path, **kw):
    job = {"name": "nightly", "probe": "log_file", "path": str(path),
           "ok_regex": r"\bDONE\b", "fail_regex": r"\bFAIL\b", "max_age_hours": 24}
    job.update(kw)
    return job


class TestLogFileProbe:

    def test_a_recent_success_line_is_ok(self, tmp_path):
        log = tmp_path / "job.log"
        log.write_text("%s START\n%s DONE copied=4\n" % (stamp(2), stamp(1)))
        assert jh.run_job(log_job(log)).state == jh.OK

    def test_a_recent_failure_line_is_failing(self, tmp_path):
        log = tmp_path / "job.log"
        log.write_text("%s FAIL exit=23\n" % stamp(1))
        assert jh.run_job(log_job(log)).state == jh.FAILING

    def test_fail_regex_wins_when_both_match(self, tmp_path):
        log = tmp_path / "job.log"
        log.write_text("%s DONE with FAIL on 2 files\n" % stamp(1))
        assert jh.run_job(log_job(log)).state == jh.FAILING

    def test_an_old_success_is_stale(self, tmp_path):
        log = tmp_path / "job.log"
        log.write_text("%s DONE\n" % stamp(30))
        assert jh.run_job(log_job(log)).state == jh.STALE

    def test_a_line_matching_neither_pattern_is_stale(self, tmp_path):
        log = tmp_path / "job.log"
        log.write_text("%s something new\n" % stamp(1))
        row = jh.run_job(log_job(log))
        assert row.state == jh.STALE
        assert "no outcome could be read" in row.outcome

    def test_no_stamped_line_is_stale_and_says_so(self, tmp_path):
        log = tmp_path / "job.log"
        log.write_text("DONE but no timestamp\nanother line\n")
        row = jh.run_job(log_job(log))
        assert row.state == jh.STALE
        assert "none with a readable" in row.outcome

    def test_trailing_unstamped_detail_is_skipped(self, tmp_path):
        log = tmp_path / "job.log"
        log.write_text("[%s] backup: DONE\n- file a\n- file b\n" % stamp(1))
        assert jh.run_job(log_job(log, timestamp="bracketed_iso")).state == jh.OK

    def test_missing_file_is_stale(self, tmp_path):
        row = jh.run_job(log_job(tmp_path / "absent.log"))
        assert row.state == jh.STALE
        assert "log not found" in row.outcome

    def test_tilde_is_expanded_against_home(self, home):
        (home / "job.log").write_text("%s DONE\n" % stamp(1))
        assert jh.run_job(log_job("~/job.log")).state == jh.OK

    def test_config_problems_are_stale(self, tmp_path):
        log = tmp_path / "job.log"
        log.write_text("%s DONE\n" % stamp(1))
        assert "no `ok_regex`" in jh.run_job(log_job(log, ok_regex="")).outcome
        assert "bad regex" in jh.run_job(log_job(log, fail_regex="(")).outcome
        assert "unknown timestamp" in jh.run_job(log_job(log, timestamp="epoch")).outcome
        job = log_job(log)
        del job["path"]
        assert jh.run_job(job).state == jh.STALE


# ----------------------------------------------------------- git_repo probe

class TestGitRepoProbe:

    def test_missing_path_is_stale(self, tmp_path):
        row = jh.run_job({"name": "g", "probe": "git_repo", "path": str(tmp_path / "nope")})
        assert row.state == jh.STALE and "not found" in row.outcome

    def test_a_plain_folder_is_stale(self, tmp_path):
        row = jh.run_job({"name": "g", "probe": "git_repo", "path": str(tmp_path)})
        assert row.state == jh.STALE and "not a git work tree" in row.outcome

    def test_a_clean_repo_with_upstream_is_ok(self, tmp_path):
        origin, work = str(tmp_path / "o.git"), str(tmp_path / "w")
        run = lambda *a: subprocess.run(list(a), check=True, capture_output=True)
        run("git", "init", "-q", "--bare", "-b", "main", origin)
        run("git", "clone", "-q", origin, work)
        for k, v in (("user.email", "bob"), ("user.name", "Bob"), ("commit.gpgsign", "false")):
            run("git", "-C", work, "config", k, v)
        (tmp_path / "w" / "f").write_text("x")
        run("git", "-C", work, "add", "f")
        run("git", "-C", work, "commit", "-qm", "f")
        run("git", "-C", work, "push", "-q", "origin", "main")
        row = jh.run_job({"name": "g", "probe": "git_repo", "path": work})
        assert row.state == jh.OK, row.outcome


# ------------------------------------------------------- time_machine probe

def load_tm():
    spec = importlib.util.spec_from_file_location("tm_probe_under_test",
                                                  os.path.join(PROBES, "time_machine.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestTimeMachineProbe:

    def _fake(self, monkeypatch, latest, destinfo=("", "")):
        tm = load_tm()
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(tm, "TMUTIL", sys.executable)   # a path that exists

        def fake_run(cmd, timeout=20):
            if cmd[-1] == "latestbackup":
                return (0 if latest else 1), latest, ""
            return 0, destinfo[0], destinfo[1]
        monkeypatch.setattr(tm, "_run", fake_run)
        return tm

    def test_not_macos_is_stale(self, monkeypatch):
        tm = load_tm()
        monkeypatch.setattr(sys, "platform", "linux")
        row = tm.probe({"name": "tm"})
        assert row.state == jh.STALE and "macOS-only" in row.outcome

    def test_a_recent_backup_is_ok(self, monkeypatch):
        t = datetime.datetime.now() - datetime.timedelta(hours=2)
        tm = self._fake(monkeypatch, "/Volumes/B/%s.backup" % t.strftime("%Y-%m-%d-%H%M%S"))
        assert tm.probe({"name": "tm"}).state == jh.OK

    def test_no_destination_is_stale(self, monkeypatch):
        tm = self._fake(monkeypatch, "", ("", "tmutil: No destinations configured."))
        row = tm.probe({"name": "tm"})
        assert row.state == jh.STALE and "no Time Machine destination" in row.outcome

    def test_detached_destination_is_stale_not_never(self, monkeypatch):
        tm = self._fake(monkeypatch, "", ("Name : Backups\nKind : Local", ""))
        row = tm.probe({"name": "tm"})
        assert row.state == jh.STALE and "not attached" in row.outcome

    def test_a_silent_tmutil_is_stale_cannot_tell(self, monkeypatch):
        tm = self._fake(monkeypatch, "", ("", ""))
        row = tm.probe({"name": "tm"})
        assert row.state == jh.STALE and "cannot tell" in row.outcome


# ------------------------------------------------- the README's own example

def test_the_readme_probe_example_works(tmp_path):
    readme = open(os.path.join(MODULE, "README.md")).read()
    section = readme.split("## Write your own probe", 1)[1]
    code = re.search(r"```python\n(.*?)```", section, re.S).group(1)
    extra = write_probe(tmp_path / "probes", "newest_file", code)
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    job = {"name": "db-dump", "probe": "newest_file",
           "glob": str(dumps / "*.sql.gz"), "max_age_hours": 26}
    assert jh.run_job(job, [str(extra)]).state == jh.STALE          # no file yet
    (dumps / "a.sql.gz").write_text("")
    assert jh.run_job(job, [str(extra)]).state == jh.FAILING        # empty dump
    (dumps / "b.sql.gz").write_text("data")
    os.utime(dumps / "a.sql.gz", (1, 1))
    assert jh.run_job(job, [str(extra)]).state == jh.OK
    assert jh.run_job({"name": "x", "probe": "newest_file"}, [str(extra)]).state == jh.STALE


# --------------------------------------------------------------------- CLI

def cli(home, *args, config=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("JOB_HEALTH_")}
    env["HOME"] = str(home)
    if config is not None:
        env["JOB_HEALTH_CONFIG"] = str(config)
    return subprocess.run([sys.executable, BIN] + list(args),
                          capture_output=True, text=True, env=env, timeout=60)


class TestCli:

    def _config(self, tmp_path, jobs):
        p = tmp_path / "jobs.json"
        p.write_text(json.dumps(jobs))
        return p

    def test_help_works(self, home):
        r = cli(home, "--help")
        assert r.returncode == 0 and "STALE" in r.stdout

    def test_report_all_ok_exits_zero(self, home, tmp_path):
        log = tmp_path / "ok.log"
        log.write_text("%s DONE\n" % stamp(1))
        r = cli(home, "report", config=self._config(tmp_path, [log_job(log)]))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "## Scheduled jobs" in r.stdout and "not OK" not in r.stdout

    def test_report_puts_the_headline_first_and_exits_one(self, home, tmp_path):
        log = tmp_path / "bad.log"
        log.write_text("%s FAIL\n" % stamp(1))
        r = cli(home, "report", config=self._config(tmp_path, [log_job(log)]))
        assert r.returncode == 1
        assert r.stdout.splitlines()[0].startswith("**1 job(s) not OK: nightly FAILING")

    def test_headline_when_all_ok_still_says_something(self, home, tmp_path):
        log = tmp_path / "ok.log"
        log.write_text("%s DONE\n" % stamp(1))
        r = cli(home, "headline", config=self._config(tmp_path, [log_job(log)]))
        assert r.stdout.strip() == "1 job(s) OK"

    def test_default_config_path_is_under_home(self, home, tmp_path):
        cfg = home / ".config" / "job-health"
        cfg.mkdir(parents=True)
        (cfg / "jobs.json").write_text(json.dumps([{"name": "x", "probe": "nope"}]))
        r = cli(home, "headline")
        assert r.returncode == 1 and "x STALE" in r.stdout

    def test_missing_empty_or_bad_config_exits_two_and_says_so_on_stdout(self, home, tmp_path):
        r = cli(home, "report")
        assert r.returncode == 2 and "could not run" in r.stdout and "no config" in r.stdout
        r = cli(home, "report", config=self._config(tmp_path, []))
        assert r.returncode == 2 and "nothing is being watched" in r.stdout
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert cli(home, "report", config=bad).returncode == 2
        r = cli(home, "report", config=self._config(tmp_path, {"name": "x"}))
        assert r.returncode == 2 and "JSON list" in r.stdout

    def test_probe_is_verbose_but_never_prints_config_values(self, home, tmp_path):
        log = tmp_path / "ok.log"
        log.write_text("%s DONE\n" % stamp(1))
        job = log_job(log, token="s3cret-value-123")
        r = cli(home, "probe", "nightly", config=self._config(tmp_path, [job]))
        assert r.returncode == 0, r.stdout
        assert "log_file.py" in r.stdout and "verdict:   OK" in r.stdout
        assert "token" in r.stdout and "s3cret-value-123" not in r.stdout

    def test_probe_of_an_unknown_job_exits_two(self, home, tmp_path):
        r = cli(home, "probe", "ghost", config=self._config(tmp_path, [{"name": "a", "probe": "x"}]))
        assert r.returncode == 2 and "no job named" in r.stdout
